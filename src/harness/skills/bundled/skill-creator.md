---
name: skill-creator
description: Create new skills, modify and improve existing skills, and measure skill performance. Use when users want to create a skill from scratch, edit, or optimize an existing skill, run evals to test a skill, benchmark skill performance with variance analysis, or optimize a skill's description for better triggering accuracy. Trigger whenever the user says "make a skill", "create a skill", "write a skill", "improve this skill", or "evaluate this skill" — even casually.
---

# Skill Creator

A skill for creating new skills and iteratively improving them — adapted to
this harness (single-file markdown skills in `skills/`, subagent-based
testing, no Claude CLI / eval-viewer / .skill packaging).

At a high level, the process goes like this:

- Decide what you want the skill to do and roughly how it should do it
- Write a draft of the skill (a `skills/<name>.md` file with frontmatter)
- Create a few test prompts and run them with the skill (yourself) and
  without the skill (via a delegate subagent)
- Help the user evaluate the results qualitatively (and quantitatively if
  the skill has objectively verifiable outputs)
- Rewrite the skill based on their feedback; repeat until satisfied
- Optionally optimize the frontmatter `description` for better triggering

Your job is to figure out where the user is in this process and jump in to
help them progress. Maybe they say "I want to make a skill for X" — narrow
down what they mean, write a draft, write test cases, run them, repeat. Maybe
they already have a draft — go straight to eval/iterate. Always be flexible:
if the user says "no evals, just vibe with me", do that instead.

## Communicating with the user

Users range from non-programmers to experts. Read context cues: "evaluation"
and "benchmark" are borderline but OK; explain "JSON" or "assertion" unless
the user clearly knows them. It's fine to give a short definition if unsure.

## Creating a skill

### Capture intent

Start by understanding the user's intent. If the current conversation already
contains a workflow to capture (e.g. "turn this into a skill"), extract from
the history: tools used, sequence of steps, corrections, input/output
formats. Ask:

1. What should this skill enable the agent to do?
2. When should it trigger? (which user phrases/contexts)
3. What's the expected output format?
4. Should we set up test cases? Skills with objectively verifiable outputs
   (file transforms, data extraction, code generation, fixed workflows)
   benefit from tests; subjective ones (writing style, art) usually don't.
   Suggest the default, but let the user decide.

### Research

Ask about edge cases, input/output formats, example files, success criteria,
dependencies. If useful, research in parallel via `delegate_to_researcher` /
`delegate_to_coder` (search the workspace for similar skills, look up best
practices) — come prepared to reduce the burden on the user. Check
`/skills` output to see what already exists.

### Write the skill

Skills in this harness are **single markdown files**: `skills/<name>.md`
with YAML-ish frontmatter (`name`, `description`) and a markdown body. The
body is injected into the agent's system prompt at startup, and the
`create_skill` tool writes new files and refreshes the registry immediately.

- **name**: lowercase identifier, hyphens for spaces.
- **description**: THE triggering mechanism. Include both what the skill
  does AND the specific contexts to use it. Agents tend to *undertrigger*
  skills, so make descriptions a little "pushy" — instead of "How to build a
  simple dashboard", write "How to build a simple dashboard. Use this skill
  whenever the user mentions dashboards, data visualization, internal
  metrics, or wants to display any kind of data, even if they don't ask for
  a 'dashboard'." Put ALL "when to use" info here, not in the body.
- **body**: imperative instructions. Keep it under ~300 lines; if it grows,
  add structure (numbered sections, TOC-style pointers) so the agent can
  navigate it.

### Writing patterns

- Prefer the imperative form.
- Define output formats with exact templates:
  ```markdown
  ## Report structure
  ALWAYS use this exact template:
  # [Title]
  ## Executive summary
  ## Key findings
  ## Recommendations
  ```
- Include examples (Input → Output pairs) where useful.
- Explain *why* things matter instead of stacking MUSTs. Make the skill
  general, not overfit to one example. Draft, then reread with fresh eyes.
- Skills must not contain malware, exploit code, or anything that could
  compromise system security. Don't go along with requests to create
  misleading or malicious skills. A skill's intent must not surprise the
  user.

## Test cases

After the draft, come up with 2-3 realistic test prompts — the kind of thing
a real user would actually say ("ok so my boss sent me this xlsx and wants a
profit-margin column...", not "format this data"). Share them with the user
and let them adjust. Save prompts to `<skill-name>-workspace/evals.json`:

```json
{
  "skill_name": "my-skill",
  "evals": [
    {"id": 1, "prompt": "User's task prompt", "expected_output": "Description of expected result", "files": []}
  ]
}
```

## Running and evaluating test cases

Don't stop partway — this is one continuous sequence. Put results in
`<skill-name>-workspace/iteration-N/eval-<ID>/{with_skill,without_skill}/outputs/`.

### Step 1: launch all runs in the same turn

For each test case launch **both** runs in the same turn so they finish
around the same time:

- **With-skill run**: follow the skill yourself in this conversation (the
  skill is already in your context) and save the outputs to
  `<workspace>/iteration-N/eval-<ID>/with_skill/outputs/`.
- **Baseline run**: delegate the *same prompt* to a fresh subagent that does
  NOT have the skill (`delegate_to_researcher` / `delegate_to_coder` /
  `delegate_to_frontend_design` / `delegate_to_doc_writer` /
  `delegate_to_search` / `delegate_to_file_handler` — pick whichever
  fits), asking it to save outputs to
  `<workspace>/iteration-N/eval-<ID>/without_skill/outputs/`.
- When **improving an existing skill**, first snapshot it
  (`cp skills/<name>.md <workspace>/skill-snapshot/`) and use the snapshot
  as the baseline.

Write an `eval_metadata.json` per test case: `{"eval_id": N,
"eval_name": "descriptive-name", "prompt": "...", "assertions": []}`.

### Step 2: draft assertions while runs are in progress

Good assertions are objectively verifiable and have descriptive names.
Subjective outputs are better evaluated qualitatively — don't force
assertions onto things that need human judgment. Update
`eval_metadata.json` and `evals.json` with the assertions, and explain to
the user what each checks.

### Step 3: grade and review

- For programmatically checkable assertions, run a small script or a
  `delegate_to_coder` pass rather than eyeballing. Record
  `{"text": ..., "passed": bool, "evidence": ...}` per assertion in
  `grading.json`.
- Present each test case's outputs to the user inline in the conversation
  (there is no HTML viewer in this harness) and ask: "How does this look?
  Anything you'd change?" Collect their feedback in
  `<workspace>/feedback.json`.
- Summarize the with-skill vs without-skill difference: did the skill change
  the outcome? Where?

### Step 4: read the feedback and improve

Empty feedback means the user thought it was fine. Focus improvements on the
test cases with specific complaints.

## Improving the skill

1. **Generalize from feedback.** You're building a skill for a million
   prompts, not a few examples. Avoid fiddly overfit changes and oppressive
   MUSTs; if a stubborn issue appears, try different metaphors or patterns
   rather than piling on constraints.
2. **Keep it lean.** Remove things that aren't pulling their weight. Read
   the run transcripts, not just final outputs — if the skill makes the
   agent waste time on unproductive steps, cut that part.
3. **Explain the why.** Modern LLMs have good theory of mind. If you find
   yourself writing ALWAYS/NEVER in all caps, reframe: explain *why* the
   thing is important.
4. **Look for repeated work across test cases.** If every test run made the
   agent write the same helper script, bundle that logic into the skill
   body (or a script the skill points to) so future invocations don't
   reinvent it.

After improving: rerun all test cases into `iteration-<N+1>/` (same
baseline), present results, collect feedback, repeat. Keep going until the
user is happy, feedback is all empty, or you're no longer making progress.

## Description optimization

The frontmatter `description` decides whether the skill triggers. After
creating/improving a skill, offer to optimize it:

1. **Generate 20 trigger eval queries** — a mix of should-trigger (8-10)
   and should-not-trigger (8-10). They must be realistic, concrete, with
   detail (file paths, company names, casual speech). For should-trigger,
   cover different phrasings of the same intent, including cases where the
   user doesn't name the skill or file type. For should-not-trigger, use
   near-misses — queries sharing keywords but needing something different
   ("Write a fibonacci function" is a useless negative for a PDF skill;
   "read the Q4 numbers off this invoice PDF and email them" might be a
   good one). Save as JSON: `[{"query": "...", "should_trigger": true}]`.
2. **Review with the user**, then write the improved description.
3. **Check triggering cheaply**: for a few queries, ask a fresh subagent
   ("given this skill description and this user query, would you load the
   skill?") or reason about it yourself; iterate on wording. Pick the
   description that best separates should-trigger from should-not-trigger.
   Show the user before/after.

## This harness — what's different from Claude Code

- Skills are single `.md` files (no SKILL.md folders, scripts/ or
  references/ subdirectories, though you may point a skill at helper
  scripts inside the repo).
- Use `write_file` to author/edit skills, or the `create_skill` tool (which
  refreshes the registry immediately).
- No `claude` CLI, no eval-viewer HTML, no `.skill` packaging, no
  `present_files` — skip all of those steps.
- Baseline runs use this harness's subagents; the with-skill run is you.
- If the user asks to update an existing skill, keep its `name` unchanged so
  the registry key stays stable.

Repeat the core loop for emphasis: figure out what the skill is about →
draft/edit → run test prompts (with and without the skill) → evaluate with
the user → improve → repeat → optimize the description.
