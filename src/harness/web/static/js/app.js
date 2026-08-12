/**
 * Harness web client — Codex-style agent UI.
 *
 * State machine: connecting → idle ⇄ running ⇄ approval_pending,
 * running → paused → running. The phase lives on document.body[data-phase]
 * and drives CSS (composer disable, status pill, stop button).
 */
(function () {
  'use strict';

  const MD = window.HarnessMarkdown;
  const $ = function (sel) { return document.querySelector(sel); };

  const els = {
    body: document.body,
    statusPill: $('#status-pill'),
    modeSelect: $('#mode-select'),
    modelLabel: $('#model-label'),
    sessionList: $('#session-list'),
    transcript: $('#transcript'),
    planPanel: $('#plan-panel'),
    input: $('#input'),
    sendBtn: $('#send-btn'),
    stopBtn: $('#stop-btn'),
    approvalOverlay: $('#approval-overlay'),
    approvalTitle: $('#approval-title'),
    approvalArgs: $('#approval-args'),
    approvalEdit: $('#approval-edit'),
    editToggle: $('#edit-toggle'),
    editSubmit: $('#edit-submit'),
    pauseOverlay: $('#pause-overlay'),
    pauseInfo: $('#pause-info'),
    jumpBtn: $('#jump-btn'),
    jumpPanel: $('#jump-panel'),
  };

  const state = {
    phase: 'connecting',
    ws: null,
    reconnectAttempt: 0,
    activeSession: null,
    currentAssistant: null,   // { el, body, text, reasoning, reasoningEl, step }
    lastAssistantEl: null,
    currentApproval: null,    // { toolCallId }
    steps: [],                // history-jump timeline: { n, el, preview }
    stepCount: 0,
    lastActionStep: 0,        // the step the user last rolled back / branched at
  };

  let toolCards = {};         // tool_call.id -> { el, output, status }
  let subagentStack = [];     // open nested subagent runs, innermost last
  let rafPending = false;

  // ---------------------------------------------------------------- phase

  function setPhase(phase) {
    state.phase = phase;
    els.body.dataset.phase = phase;
    const labels = {
      connecting: 'connecting…',
      idle: 'idle',
      running: 'running',
      approval_pending: 'approval pending',
      paused: 'paused',
    };
    els.statusPill.textContent = labels[phase] || phase;
    els.sendBtn.disabled = phase !== 'idle';
    els.input.disabled = phase !== 'idle';
    els.stopBtn.hidden = !(phase === 'running' || phase === 'approval_pending');
  }

  // ---------------------------------------------------------------- ws

  function connectWS() {
    const q = state.activeSession
      ? '?session_id=' + encodeURIComponent(state.activeSession)
      : '';
    const ws = new WebSocket('/ws' + q);
    state.ws = ws;

    ws.onopen = function () {
      state.reconnectAttempt = 0;
    };
    ws.onmessage = function (ev) {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      handleMessage(msg);
    };
    ws.onclose = function () {
      if (state.currentApproval) { els.approvalOverlay.hidden = true; state.currentApproval = null; }
      if (state.phase === 'paused') { els.pauseOverlay.hidden = true; }
      setPhase('connecting');
      if (state.reconnectAttempt < 8) {
        const delay = Math.min(1000 * Math.pow(2, state.reconnectAttempt), 30000);
        state.reconnectAttempt += 1;
        setTimeout(connectWS, delay);
      }
    };
  }

  function send(obj) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(obj));
    }
  }

  // ---------------------------------------------------------------- transcript

  function scrollBottom() {
    els.transcript.scrollTop = els.transcript.scrollHeight;
  }

  function appendUserMessage(text) {
    const el = document.createElement('div');
    el.className = 'message user';
    const body = document.createElement('div');
    body.className = 'body';
    body.textContent = text;
    el.appendChild(body);
    els.transcript.appendChild(el);
    registerStep(el, text.slice(0, 60));
    scrollBottom();
  }

  function appendSystemBubble(text) {
    const el = document.createElement('div');
    el.className = 'message system';
    el.textContent = text;
    els.transcript.appendChild(el);
    scrollBottom();
  }

  function startAssistantMessage() {
    const el = document.createElement('div');
    el.className = 'message assistant';
    els.transcript.appendChild(el);
    const step = registerStep(el, '…');
    state.lastAssistantEl = el;
    state.currentAssistant = {
      el: el,
      body: null,
      text: '',
      reasoning: '',
      reasoningEl: null,
      step: step,
    };
    return state.currentAssistant;
  }

  function ensureReasoning(msg) {
    if (!msg.reasoningEl) {
      msg.reasoningEl = document.createElement('details');
      msg.reasoningEl.className = 'reasoning';
      msg.reasoningEl.open = true; // auto-open: show the model's thinking while it works
      const summary = document.createElement('summary');
      summary.textContent = 'Thoughts';
      const pre = document.createElement('pre');
      msg.reasoningEl.appendChild(summary);
      msg.reasoningEl.appendChild(pre);
      msg.el.appendChild(msg.reasoningEl);
    }
    msg.reasoningEl.querySelector('pre').textContent = msg.reasoning;
  }

  function ensureBody(msg) {
    if (!msg.body) {
      msg.body = document.createElement('div');
      msg.body.className = 'body markdown';
      msg.el.appendChild(msg.body);
    }
  }

  function appendAssistantText(text) {
    const msg = state.currentAssistant;
    if (!msg) return;
    ensureBody(msg);
    msg.text += text;
    msg.dirty = true;
    if (!rafPending) {
      rafPending = true;
      requestAnimationFrame(function () {
        rafPending = false;
        if (msg.dirty && msg.body) {
          msg.body.innerHTML = MD.renderMarkdown(msg.text);
          msg.dirty = false;
          scrollBottom();
        }
      });
    }
  }

  function appendReasoning(text) {
    const msg = state.currentAssistant;
    if (!msg) return;
    msg.reasoning += text;
    ensureReasoning(msg);
    scrollBottom();
  }

  function finishAssistantMessage(finalText) {
    const msg = state.currentAssistant;
    if (!msg) return;
    if (!msg.body && finalText) {
      // The final turn streamed no visible text (e.g. the model ended with
      // empty content after doing the work) — fall back to the run's final
      // output so a finished task always shows a reply.
      msg.text = finalText;
      ensureBody(msg);
    }
    if (msg.body) {
      msg.body.innerHTML = MD.renderMarkdown(msg.text);
      msg.dirty = false;
    } else if (msg.reasoningEl) {
      // reasoning-only turn: leave the panel, nothing else to render
    }
    if (msg.step) {
      msg.step.preview = (msg.text || 'agent reply').slice(0, 60);
    }
    state.currentAssistant = null;
    renderJumpPanel();
    scrollBottom();
  }

  // ---------------------------------------------------------------- tool cards

  function argsOf(tc) {
    try { return JSON.parse(tc.arguments || '{}'); } catch (e) { return {}; }
  }

  function createToolCard(tc) {
    const parent = state.lastAssistantEl;
    if (!parent) return;

    const args = argsOf(tc);
    let title;
    if (tc.name === 'bash') {
      title = '$ ' + (args.command || '');
    } else {
      title = tc.name + ' ' + JSON.stringify(args);
    }

    const card = document.createElement('div');
    card.className = 'tool-card';
    card.dataset.state = 'running';

    const header = document.createElement('div');
    header.className = 'tool-card-header';
    const spinner = document.createElement('span');
    spinner.className = 'spinner';
    const titleEl = document.createElement('span');
    titleEl.className = 'tool-card-title';
    titleEl.textContent = title;
    header.appendChild(spinner);
    header.appendChild(titleEl);

    const output = document.createElement('details');
    output.className = 'tool-card-output';
    const summary = document.createElement('summary');
    summary.textContent = 'output';
    const pre = document.createElement('pre');
    output.appendChild(summary);
    output.appendChild(pre);

    card.appendChild(header);
    card.appendChild(output);
    parent.appendChild(card);

    toolCards[tc.id] = { el: card, output: pre, status: 'running' };
    scrollBottom();
  }

  function updateToolCard(id, result) {
    const card = toolCards[id];
    if (!card) return;
    const status = result.is_error ? 'error' : 'done';
    card.status = status;
    card.el.dataset.state = status;
    card.output.textContent =
      result.content + (result.truncated ? '\n…(output truncated)' : '');
    if (result.is_error) {
      card.el.querySelector('.tool-card-output').open = true;
    }
  }

  function findToolCard(id) {
    // approval/decision frames only carry the tool_call id; look in the parent
    // cards first, then any open subagent run's scoped card map
    if (toolCards[id]) return toolCards[id];
    for (let i = subagentStack.length - 1; i >= 0; i--) {
      if (subagentStack[i].tools[id]) return subagentStack[i].tools[id];
    }
    return null;
  }

  function markAwaitingApproval(id) {
    const card = findToolCard(id);
    if (card && card.status === 'running') {
      card.status = 'awaiting_approval';
      card.el.dataset.state = 'awaiting_approval';
    }
  }

  function markRunning(id) {
    const card = findToolCard(id);
    if (card && card.status === 'awaiting_approval') {
      card.status = 'running';
      card.el.dataset.state = 'running';
    }
  }

  // ---------------------------------------------------------------- subagent run view

  function startSubagentRun(name, runId) {
    const parent = state.lastAssistantEl;
    if (!parent) return;
    const card = document.createElement('div');
    card.className = 'subagent-run';
    card.dataset.state = 'running';

    const header = document.createElement('div');
    header.className = 'subagent-header';
    const spinner = document.createElement('span');
    spinner.className = 'spinner';
    const title = document.createElement('span');
    title.className = 'subagent-title';
    title.textContent = 'subagent: ' + name;
    header.appendChild(spinner);
    header.appendChild(title);

    const body = document.createElement('div');
    body.className = 'subagent-body';

    card.appendChild(header);
    card.appendChild(body);
    parent.appendChild(card);

    subagentStack.push({
      name: name,
      runId: runId,
      el: card,
      body: body,
      text: '',
      textEl: null,
      reasoning: '',
      reasoningEl: null,
      tools: {},
    });
    scrollBottom();
  }

  function routeSubagentEvent(runId, agent, ev) {
    // events arrive depth-first; find the innermost open run with this run_id
    let run = null;
    for (let i = subagentStack.length - 1; i >= 0; i--) {
      if (subagentStack[i].runId === runId) { run = subagentStack[i]; break; }
    }
    if (!run) return;

    if (ev.type === 'text') {
      if (!run.textEl) {
        run.textEl = document.createElement('div');
        run.textEl.className = 'subagent-text';
        run.body.appendChild(run.textEl);
      }
      run.text += ev.text;
      run.textEl.textContent = run.text; // plain text — the delivery is one-shot
    } else if (ev.type === 'reasoning') {
      if (!run.reasoningEl) {
        run.reasoningEl = document.createElement('details');
        run.reasoningEl.className = 'reasoning';
        run.reasoningEl.open = true;
        const summary = document.createElement('summary');
        summary.textContent = 'Thoughts';
        const pre = document.createElement('pre');
        run.reasoningEl.appendChild(summary);
        run.reasoningEl.appendChild(pre);
        run.body.appendChild(run.reasoningEl);
      }
      run.reasoning += ev.text;
      run.reasoningEl.querySelector('pre').textContent = run.reasoning;
    } else if (ev.type === 'tool_call') {
      createSubagentToolCard(run, ev.tool_call);
    } else if (ev.type === 'tool_result') {
      const card = run.tools[ev.tool_call_id];
      if (card) {
        card.status = ev.is_error ? 'error' : 'done';
        card.el.dataset.state = card.status;
        card.output.textContent =
          ev.content + (ev.truncated ? '\n…(output truncated)' : '');
        if (ev.is_error) {
          card.el.querySelector('.tool-card-output').open = true;
        }
      }
    }
    scrollBottom();
  }

  function createSubagentToolCard(run, tc) {
    const el = document.createElement('div');
    el.className = 'tool-card subagent-tool-card';
    el.dataset.state = 'running';

    const header = document.createElement('div');
    header.className = 'tool-card-header';
    const spinner = document.createElement('span');
    spinner.className = 'spinner';
    const title = document.createElement('span');
    title.className = 'tool-card-title';
    title.textContent = tc.name + ' ' + JSON.stringify(argsOf(tc));
    header.appendChild(spinner);
    header.appendChild(title);

    const output = document.createElement('details');
    output.className = 'tool-card-output';
    const summary = document.createElement('summary');
    summary.textContent = 'output';
    const pre = document.createElement('pre');
    output.appendChild(summary);
    output.appendChild(pre);

    el.appendChild(header);
    el.appendChild(output);
    run.body.appendChild(el);

    // scoped per-run map, not the global toolCards — subagent tool_call ids
    // would collide with the parent's (both start at "t1" in tests/real runs)
    run.tools[tc.id] = { el: el, output: pre, status: 'running' };
  }

  function closeStaleSubagents() {
    if (!subagentStack.length) return;
    subagentStack.forEach(function (run) {
      const spinner = run.el.querySelector('.subagent-header .spinner');
      if (spinner) spinner.remove();
      run.el.dataset.state = 'error';
    });
    subagentStack = [];
  }

  function endSubagentRun(msg) {
    // close the innermost open run with this run_id
    let idx = -1;
    for (let i = subagentStack.length - 1; i >= 0; i--) {
      if (subagentStack[i].runId === msg.run_id) { idx = i; break; }
    }
    if (idx === -1) return;
    const run = subagentStack[idx];
    subagentStack.splice(idx, 1);

    const spinner = run.el.querySelector('.subagent-header .spinner');
    if (spinner) spinner.remove();
    const status = document.createElement('span');
    status.className = 'subagent-status';
    status.textContent = (msg.is_error ? '✗ ' : '✓ ') + msg.turns + ' turn' + (msg.turns === 1 ? '' : 's');
    run.el.querySelector('.subagent-header').appendChild(status);
    run.el.dataset.state = msg.is_error ? 'error' : 'done';

    // the final output is usually already streamed as text; only show it when
    // the run produced none (e.g. it ended on an error before any text)
    if (msg.output && !run.text) {
      if (!run.textEl) {
        run.textEl = document.createElement('div');
        run.textEl.className = 'subagent-text';
        run.body.appendChild(run.textEl);
      }
      run.textEl.textContent = msg.output;
    }
    scrollBottom();
  }

  // ---------------------------------------------------------------- approval dialog

  function openApprovalDialog(tc) {
    state.currentApproval = { toolCallId: tc.id };
    markAwaitingApproval(tc.id);
    els.approvalTitle.textContent = 'Approval required: ' + tc.name;
    els.approvalArgs.textContent = prettyJson(tc.arguments);
    els.approvalArgs.hidden = false;
    els.approvalEdit.hidden = true;
    els.editToggle.hidden = false;
    els.editSubmit.hidden = true;
    els.approvalOverlay.hidden = false;
    setPhase('approval_pending');
  }

  function prettyJson(raw) {
    try { return JSON.stringify(JSON.parse(raw || '{}'), null, 2); }
    catch (e) { return String(raw || '{}'); }
  }

  function closeApprovalDialog(decision) {
    const cardId = state.currentApproval ? state.currentApproval.toolCallId : null;
    els.approvalOverlay.hidden = true;
    state.currentApproval = null;
    if (cardId) {
      // after a decision the tool is either running again or was denied (card
      // state flips via tool_result); before a terminal event just mark running.
      markRunning(cardId);
    }
    if (decision !== undefined) {
      setPhase('running');
    }
  }

  function submitDecision(decision) {
    const cardId = state.currentApproval ? state.currentApproval.toolCallId : null;
    send({ type: 'approval', decision: decision });
    closeApprovalDialog();
    if (cardId) markRunning(cardId);
    setPhase('running');
  }

  // ---------------------------------------------------------------- pause overlay

  function showPauseOverlay(frame) {
    els.pauseInfo.textContent =
      'Checkpoint ' + frame.checkpoint_id + ' · session ' + frame.session_id + ' · turn ' + frame.turns;
    els.pauseOverlay.hidden = false;
    setPhase('paused');
  }

  function hidePauseOverlay() {
    els.pauseOverlay.hidden = true;
  }

  // ---------------------------------------------------------------- plan panel

  const MARKERS = { pending: '·', in_progress: '→', done: '✓', failed: '✗' };

  function renderPlan(plan) {
    els.planPanel.hidden = false;
    els.planPanel.innerHTML = '';
    const goal = document.createElement('div');
    goal.className = 'plan-goal';
    goal.textContent = '🎯 ' + plan.goal;
    els.planPanel.appendChild(goal);

    const list = document.createElement('div');
    list.className = 'plan-steps';
    (plan.steps || []).forEach(function (step) {
      const row = document.createElement('div');
      row.className = 'plan-step';
      row.dataset.id = step.id;
      row.dataset.status = step.status;
      const marker = document.createElement('span');
      marker.className = 'plan-marker';
      marker.textContent = MARKERS[step.status] || '·';
      const text = document.createElement('span');
      text.textContent = step.title + ' — ' + step.description;
      row.appendChild(marker);
      row.appendChild(text);
      list.appendChild(row);
    });
    els.planPanel.appendChild(list);
  }

  function markStep(step) {
    const row = els.planPanel.querySelector('.plan-step[data-id="' + step.id + '"]');
    if (row) {
      row.dataset.status = step.status;
      const marker = row.querySelector('.plan-marker');
      if (marker) marker.textContent = MARKERS[step.status] || '·';
    }
  }

  // ---------------------------------------------------------------- commands

  const REST_COMMANDS = ['help', 'tools', 'skills', 'permissions', 'checkpoints'];

  function handleSlashCommand(line) {
    const parts = line.slice(1).split(/\s+/);
    const name = parts[0].toLowerCase();
    const arg = parts.slice(1).join(' ').trim();

    if (REST_COMMANDS.indexOf(name) !== -1) {
      appendUserMessage(line);
      fetch('/api/' + name)
        .then(function (r) { return r.json(); })
        .then(function (data) { renderCommandResult(name, data); })
        .catch(function () { appendSystemBubble('/' + name + ' failed (server unreachable)'); });
    } else if (name === 'new') {
      appendUserMessage(line);
      fetch('/api/sessions', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          clearTranscript();
          send({ type: 'set_session', session_id: data.session.id });
        });
    } else if (name === 'clear') {
      appendUserMessage(line);
      send({ type: 'command', name: 'clear' });
      clearTranscript();
    } else if (name === 'mcp') {
      appendUserMessage(line);
      send({ type: 'command', name: 'mcp', arg: arg });
    } else if (name === 'plan') {
      if (!arg) { appendSystemBubble('Usage: /plan <goal>'); return; }
      appendUserMessage(line);
      setPhase('running');
      send({ type: 'plan', goal: arg });
    } else {
      appendUserMessage(line);
      appendSystemBubble('Unknown command: /' + name + '  (try /help)');
    }
  }

  function renderCommandResult(name, data) {
    const el = document.createElement('div');
    el.className = 'message system';

    if (name === 'help') {
      el.textContent = data.help;
    } else if (name === 'tools') {
      el.textContent = 'Tools:\n' + (data.tools || []).map(function (t) {
        return '  ' + t.name + ' — ' + t.description;
      }).join('\n');
    } else if (name === 'skills') {
      const skills = data.skills || [];
      el.textContent = skills.length
        ? 'Skills:\n' + skills.map(function (s) {
            return '  ' + s.name + ' — ' + s.description;
          }).join('\n')
        : 'No skills found. Run the skill to create it.';
    } else if (name === 'permissions') {
      el.textContent = 'default = ' + data.default + '\n\n' + data.toml;
    } else if (name === 'checkpoints') {
      const cps = data.checkpoints || [];
      if (!cps.length) {
        el.textContent = 'No saved checkpoints.';
      } else {
        const title = document.createElement('div');
        title.textContent = 'Checkpoints:';
        el.appendChild(title);
        cps.forEach(function (c) {
          const row = document.createElement('div');
          row.className = 'checkpoint-row';
          const span = document.createElement('span');
          span.textContent = '  ' + c.id + '  (saved ' + c.created_at + ')';
          const btn = document.createElement('button');
          btn.textContent = 'Resume';
          btn.addEventListener('click', function () {
            clearTranscript();
            setPhase('running');
            send({ type: 'resume_checkpoint', checkpoint_id: c.id });
          });
          row.appendChild(span);
          row.appendChild(btn);
          el.appendChild(row);
        });
      }
    } else if (name === 'mcp') {
      if (!data.ok) {
        el.textContent = '⚠️ ' + (data.message || 'mcp command failed');
      } else if (data.action === 'added') {
        el.textContent = '✅ Connected ' + data.name + ' — ' +
          (data.tools || []).length + ' tools: ' + (data.tools || []).join(', ');
      } else if (data.action === 'removed') {
        el.textContent = '✅ Removed ' + data.name;
      } else if (data.action === 'list') {
        const servers = data.servers || [];
        el.textContent = servers.length
          ? 'MCP servers:\n' + servers.map(function (s) {
              return '  ' + s.name + ' — ' + (s.tools || []).length +
                ' tools: ' + (s.tools || []).join(', ');
            }).join('\n')
          : 'No MCP servers connected.  Use /mcp add stdio <name> <command> ...';
      } else {
        el.textContent = data.message || 'mcp command result';
      }
    }
    els.transcript.appendChild(el);
    scrollBottom();
  }

  // ---------------------------------------------------------------- sessions

  function clearTranscript() {
    els.transcript.innerHTML = '';
    toolCards = {};
    subagentStack = [];
    state.currentAssistant = null;
    state.lastAssistantEl = null;
    state.steps = [];
    state.stepCount = 0;
    hideJumpPanel();
    renderJumpPanel();
  }

  function refreshSessions() {
    fetch('/api/sessions?limit=50')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        els.sessionList.innerHTML = '';
        (data.sessions || []).forEach(function (s) {
          const item = document.createElement('div');
          item.className = 'session-item' + (s.id === state.activeSession ? ' active' : '');

          const label = document.createElement('span');
          label.className = 'session-label' + (s.name ? ' named' : '');
          label.textContent = s.name || s.id;
          label.title = s.id + (s.parent_session_id ? ' (分支自 ' + s.parent_session_id + ')' : '');
          label.addEventListener('click', function () {
            if (s.id !== state.activeSession) {
              clearTranscript();
              send({ type: 'set_session', session_id: s.id });
            }
          });
          // 双击标题 → 内联重命名(回车/失焦提交,Esc 取消)
          label.addEventListener('dblclick', function (e) {
            e.stopPropagation();
            startRename(s, label);
          });

          const del = document.createElement('button');
          del.className = 'session-delete';
          del.textContent = '×';
          del.title = 'Delete session';
          del.addEventListener('click', function (e) {
            e.stopPropagation();
            if (s.id === state.activeSession) return;
            fetch('/api/sessions/' + encodeURIComponent(s.id), { method: 'DELETE' })
              .then(refreshSessions)
              .catch(function () {});
          });

          item.appendChild(label);
          item.appendChild(del);
          els.sessionList.appendChild(item);
        });
      })
      .catch(function () {});
  }

  function startRename(session, label) {
    const input = document.createElement('input');
    input.className = 'rename-input';
    input.value = session.name || '';
    input.placeholder = session.id;
    label.replaceWith(input);
    input.focus();
    input.select();
    const commit = function (ok) {
      const name = input.value.trim();
      input.onblur = null;          // 避免 Enter 后 blur 重复提交
      if (ok && name && name !== (session.name || '')) {
        fetch('/api/sessions/' + encodeURIComponent(session.id), {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name })
        })
          .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); })
          .then(function () { refreshSessions(); })
          .catch(function () {
            appendSystemBubble('重命名失败');
            refreshSessions();
          });
      } else {
        refreshSessions();
      }
    };
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); commit(true); }
      else if (e.key === 'Escape') { commit(false); }
    });
    input.addEventListener('blur', function () { commit(true); });
  }

  function refreshTranscript() {
    clearTranscript();
    if (!state.activeSession) return;
    fetch('/api/sessions/' + encodeURIComponent(state.activeSession) + '/messages')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        renderHistory(data.messages || []);
      })
      .catch(function () {});
  }

  function renderHistory(messages) {
    messages.forEach(function (m) {
      if (m.role === 'user') {
        appendUserMessage(m.content || '');
      } else if (m.role === 'assistant') {
        const msg = startAssistantMessage();
        if (m.reasoning_content) {
          msg.reasoning = m.reasoning_content;
          ensureReasoning(msg);
        }
        if (m.content) {
          ensureBody(msg);
          msg.text = m.content;
          msg.body.innerHTML = MD.renderMarkdown(m.content);
        }
        finishAssistantMessage();
        (m.tool_calls || []).forEach(function (tc) {
          createToolCard(tc);
        });
      } else if (m.role === 'tool') {
        // tool results arrive as separate role="tool" messages in history
        const card = toolCards[m.tool_call_id];
        if (card) {
          card.status = 'done';
          card.el.dataset.state = 'done';
          card.output.textContent =
            m.content + (m.truncated ? '\n…(output truncated)' : '');
        }
      }
    });
    scrollBottom();
  }

  // ---------------------------------------------------------------- message dispatch

  function handleMessage(msg) {
    switch (msg.type) {
      case 'ready':
        state.activeSession = msg.session_id;
        els.modeSelect.value = msg.mode || 'ask';
        els.modeSelect.disabled = false;
        els.modelLabel.textContent =
          'model ' + msg.model + ' · sandbox ' + msg.sandbox_mode +
          ' · permissions ' + msg.permissions_default + ' · max_turns ' + msg.max_turns;
        refreshSessions();
        refreshTranscript();
        setPhase('idle');
        break;
      case 'run_started':
        // a fresh run: any subagent cards still open (from a cancelled or
        // errored prior run) are stale — detach them so events can't leak in
        closeStaleSubagents();
        break; // bubble was already created optimistically on send
      case 'text':
        appendAssistantText(msg.text);
        break;
      case 'reasoning':
        appendReasoning(msg.text);
        break;
      case 'tool_call':
        createToolCard(msg.tool_call);
        break;
      case 'tool_result':
        updateToolCard(msg.tool_call_id, msg);
        break;
      case 'subagent_start':
        startSubagentRun(msg.agent, msg.run_id);
        break;
      case 'subagent_event':
        routeSubagentEvent(msg.run_id, msg.agent, msg.event);
        break;
      case 'subagent_end':
        endSubagentRun(msg);
        break;
      case 'approval_required':
        openApprovalDialog(msg.tool_call);
        break;
      case 'run_done':
        finishAssistantMessage(msg.result && msg.result.final_output);
        closeApprovalDialog();
        refreshSessions();
        setPhase('idle');
        break;
      case 'run_error':
        appendSystemBubble('⚠️ ' + (msg.message || 'run failed'));
        finishAssistantMessage();
        closeApprovalDialog();
        setPhase('idle');
        break;
      case 'run_cancelled':
        finishAssistantMessage();
        closeApprovalDialog();
        setPhase('idle');
        break;
      case 'paused':
        showPauseOverlay(msg);
        break;
      case 'resumed':
        hidePauseOverlay();
        setPhase('running');
        break;
      case 'session_created':
        refreshSessions();
        break;
      case 'session_renamed':
        refreshSessions();
        break;
      case 'mode_changed':
        els.modeSelect.value = msg.mode;
        break;
      case 'session_switched':
        state.activeSession = msg.session_id;
        refreshSessions();
        refreshTranscript();
        break;
      case 'rolled_back':
        setPhase('idle');
        appendSystemBubble(
          '↩ 已回退到第 ' + (state.lastActionStep || msg.to_idx) + ' 步' +
          (msg.restored && msg.restored.length
            ? '\n已还原文件:' + msg.restored.map(function (p) { return '\n  · ' + p; }).join('')
            : '')
        );
        refreshSessions();
        refreshTranscript();
        break;
      case 'session_error':
        appendSystemBubble('No such session: ' + msg.session_id);
        break;
      case 'command_result':
        if (msg.payload) renderCommandResult(msg.name, msg.payload);
        break;
      case 'plan_start':
        renderPlan(msg.plan);
        break;
      case 'step_start':
      case 'step_end':
        markStep(msg.step);
        break;
      case 'plan_revised':
        renderPlan(msg.plan);
        break;
      case 'plan_done':
        renderPlan(msg.plan);
        finishAssistantMessage();
        closeApprovalDialog();
        setPhase('idle');
        break;
      case 'fatal':
        appendSystemBubble('⚠️ ' + (msg.message || 'fatal server error'));
        setPhase('idle');
        break;
      case 'pong':
        break;
      default:
        break;
    }
  }

  // ---------------------------------------------------------------- composer

  function submitInput() {
    if (state.phase !== 'idle') return;
    const line = els.input.value.trim();
    if (!line) return;
    els.input.value = '';
    resizeInput();
    if (line.charAt(0) === '/') {
      handleSlashCommand(line);
    } else {
      appendUserMessage(line);
      startAssistantMessage();
      setPhase('running');
      send({ type: 'message', content: line });
    }
  }

  function resizeInput() {
    els.input.style.height = 'auto';
    els.input.style.height = Math.min(els.input.scrollHeight, 220) + 'px';
  }

  els.input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submitInput();
    }
  });
  els.input.addEventListener('input', resizeInput);
  els.sendBtn.addEventListener('click', submitInput);
  els.stopBtn.addEventListener('click', function () { send({ type: 'cancel' }); });

  $('#new-session').addEventListener('click', function () {
    fetch('/api/sessions', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        clearTranscript();
        send({ type: 'set_session', session_id: data.session.id });
      });
  });

  document.querySelectorAll('#approval-overlay [data-decision]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      submitDecision(btn.dataset.decision);
    });
  });

  els.modeSelect.addEventListener('change', function () {
    send({ type: 'set_mode', mode: els.modeSelect.value });
  });

  els.editToggle.addEventListener('click', function () {
    els.approvalArgs.hidden = true;
    els.approvalEdit.value = els.approvalArgs.textContent;
    els.approvalEdit.hidden = false;
    els.editToggle.hidden = true;
    els.editSubmit.hidden = false;
  });

  els.editSubmit.addEventListener('click', function () {
    let parsed;
    try {
      parsed = JSON.parse(els.approvalEdit.value);
    } catch (e) {
      window.alert('Edited arguments are not valid JSON');
      return;
    }
    submitDecision('e:' + JSON.stringify(parsed));
  });

  $('#resume-btn').addEventListener('click', function () {
    hidePauseOverlay();
    setPhase('running');
    send({ type: 'resume' });
  });
  $('#dismiss-pause-btn').addEventListener('click', function () {
    hidePauseOverlay();
    setPhase('idle');
  });

  // ---------------------------------------------------------------- history jump

  function registerStep(el, preview) {
    state.stepCount += 1;
    const step = { n: state.stepCount, el: el, preview: preview };
    state.steps.push(step);
    addStepBadge(el, step.n);
    addMsgActions(el, step.n);
    renderJumpPanel();
    return step;
  }

  function addStepBadge(el, n) {
    const badge = document.createElement('span');
    badge.className = 'step-badge';
    badge.textContent = String(n);
    badge.title = '第 ' + n + ' 步';
    badge.addEventListener('click', function (e) {
      e.stopPropagation();
      jumpToStep(n);
    });
    el.appendChild(badge);
  }

  function addMsgActions(el, n) {
    const actions = document.createElement('div');
    actions.className = 'msg-actions';

    const rb = document.createElement('button');
    rb.className = 'msg-action';
    rb.textContent = '回退';
    rb.title = '回退到这一步,丢弃之后的对话与文件改动';
    rb.addEventListener('click', function (e) {
      e.stopPropagation();
      if (!window.confirm('回退到第 ' + n + ' 步?之后的所有对话和代码改动都会被丢弃。')) return;
      state.lastActionStep = n;
      send({ type: 'rollback', step: n });
    });

    const br = document.createElement('button');
    br.className = 'msg-action';
    br.textContent = '分叉';
    br.title = '从此处开始一个继承历史的新会话(共用同一工作区)';
    br.addEventListener('click', function (e) {
      e.stopPropagation();
      if (!window.confirm('从第 ' + n + ' 步分支出一个新会话?')) return;
      state.lastActionStep = n;
      send({ type: 'branch', step: n });
    });

    actions.appendChild(rb);
    actions.appendChild(br);
    el.appendChild(actions);
  }

  function renderJumpPanel() {
    const has = state.steps.length > 0;
    els.jumpBtn.hidden = !has;
    if (!has) {
      hideJumpPanel();
      els.jumpPanel.innerHTML = '';
      return;
    }
    els.jumpPanel.innerHTML = '';
    state.steps.forEach(function (s) {
      const row = document.createElement('div');
      row.className = 'jump-row';
      const num = document.createElement('span');
      num.className = 'jump-num';
      num.textContent = String(s.n);
      const prev = document.createElement('span');
      prev.className = 'jump-preview';
      prev.textContent = s.preview || '…';
      row.appendChild(num);
      row.appendChild(prev);
      row.addEventListener('click', function () { jumpToStep(s.n); });
      els.jumpPanel.appendChild(row);
    });
  }

  function jumpToStep(n) {
    const step = state.steps.find(function (s) { return s.n === n; });
    if (!step) return;
    hideJumpPanel();
    step.el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    step.el.classList.add('flash');
    setTimeout(function () { step.el.classList.remove('flash'); }, 1500);
  }

  function hideJumpPanel() {
    els.jumpPanel.hidden = true;
  }

  // ---------------------------------------------------------------- boot

  els.jumpBtn.addEventListener('click', function () {
    if (els.jumpPanel.hidden) {
      renderJumpPanel();
      els.jumpPanel.hidden = false;
    } else {
      hideJumpPanel();
    }
  });
  document.addEventListener('click', function (e) {
    if (
      !els.jumpPanel.hidden &&
      !els.jumpPanel.contains(e.target) &&
      e.target !== els.jumpBtn
    ) {
      hideJumpPanel();
    }
  });

  setPhase('connecting');
  connectWS();
})();
