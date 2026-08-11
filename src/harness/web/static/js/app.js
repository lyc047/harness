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
  };

  const state = {
    phase: 'connecting',
    ws: null,
    reconnectAttempt: 0,
    activeSession: null,
    currentAssistant: null,   // { el, body, text, reasoning, reasoningEl }
    lastAssistantEl: null,
    currentApproval: null,    // { toolCallId }
  };

  let toolCards = {};         // tool_call.id -> { el, output, status }
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
    state.lastAssistantEl = el;
    state.currentAssistant = {
      el: el,
      body: null,
      text: '',
      reasoning: '',
      reasoningEl: null,
    };
    return state.currentAssistant;
  }

  function ensureReasoning(msg) {
    if (!msg.reasoningEl) {
      msg.reasoningEl = document.createElement('details');
      msg.reasoningEl.className = 'reasoning';
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

  function finishAssistantMessage() {
    const msg = state.currentAssistant;
    if (!msg) return;
    if (msg.body) {
      msg.body.innerHTML = MD.renderMarkdown(msg.text);
      msg.dirty = false;
    } else if (msg.reasoningEl) {
      // reasoning-only turn: leave the panel, nothing else to render
    }
    state.currentAssistant = null;
    scrollBottom();
  }

  // ---------------------------------------------------------------- tool cards

  function createToolCard(tc) {
    const parent = state.lastAssistantEl;
    if (!parent) return;

    let args;
    try { args = JSON.parse(tc.arguments || '{}'); } catch (e) { args = {}; }
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

  function markAwaitingApproval(id) {
    const card = toolCards[id];
    if (card && card.status === 'running') {
      card.status = 'awaiting_approval';
      card.el.dataset.state = 'awaiting_approval';
    }
  }

  function markRunning(id) {
    const card = toolCards[id];
    if (card && card.status === 'awaiting_approval') {
      card.status = 'running';
      card.el.dataset.state = 'running';
    }
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
    }
    els.transcript.appendChild(el);
    scrollBottom();
  }

  // ---------------------------------------------------------------- sessions

  function clearTranscript() {
    els.transcript.innerHTML = '';
    toolCards = {};
    state.currentAssistant = null;
    state.lastAssistantEl = null;
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
          label.textContent = s.id;
          label.addEventListener('click', function () {
            if (s.id !== state.activeSession) {
              clearTranscript();
              send({ type: 'set_session', session_id: s.id });
            }
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
        els.modelLabel.textContent =
          'model ' + msg.model + ' · sandbox ' + msg.sandbox_mode +
          ' · permissions ' + msg.permissions_default + ' · max_turns ' + msg.max_turns;
        refreshSessions();
        refreshTranscript();
        setPhase('idle');
        break;
      case 'run_started':
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
      case 'approval_required':
        openApprovalDialog(msg.tool_call);
        break;
      case 'run_done':
        finishAssistantMessage();
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
      case 'session_switched':
        state.activeSession = msg.session_id;
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

  // ---------------------------------------------------------------- boot

  setPhase('connecting');
  connectWS();
})();
