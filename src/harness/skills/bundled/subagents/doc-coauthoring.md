---
name: doc-coauthoring
description: Guide users through a structured workflow for co-authoring documentation. Use when user wants to write documentation, proposals, technical specs, decision docs, or similar structured content. This workflow helps users efficiently transfer context, refine content through iteration, and verify the doc works for readers. Trigger when user mentions writing docs, creating proposals, drafting specs, or similar documentation tasks.
---

# Doc Co-Authoring Workflow

This skill provides a structured workflow for guiding users through collaborative document creation. Act as an active guide, walking users through three stages: Context Gathering, Refinement & Structure, and Reader Testing.

## When to Offer This Workflow

**Trigger conditions:**
- User mentions writing documentation: "write a doc", "draft a proposal", "create a spec", "write up"
- User mentions specific doc types: "PRD", "design doc", "decision doc", "RFC"
- User seems to be starting a substantial writing task

**Initial offer:**
Offer the user a structured workflow for co-authoring the document. Explain the three stages:

1. **Context Gathering**: User provides all relevant context while you ask clarifying questions
2. **Refinement & Structure**: Iteratively build each section through brainstorming and editing
3. **Reader Testing**: Test the doc with a fresh agent (no context) to catch blind spots before others read it

Explain that this approach helps ensure the doc works well when others read it (including when they paste it into an agent). Ask if they want to try this workflow or prefer to work freeform. If user declines, work freeform. If user accepts, proceed to Stage 1.

## Stage 1: Context Gathering

**Goal:** Close the gap between what the user knows and what you know, enabling smart guidance later.

### Initial Questions

Start by asking the user for meta-context about the document:

1. What type of document is this? (e.g., technical spec, decision doc, proposal)
2. Who's the primary audience?
3. What's the desired impact when someone reads this?
4. Is there a template or specific format to follow?
5. Any other constraints or context to know?

Inform them they can answer in shorthand or dump information however works best for them.

**If user provides a template or mentions a doc type:**
- Ask if they have a template document to share
- If they provide a file, read it

**If user mentions editing an existing document:**
- Read the current file
- Check for images without alt-text; if any exist, explain that when others use an agent to understand the doc, the agent won't see them. Ask if they want alt-text generated.

### Info Dumping

Once initial questions are answered, encourage the user to dump all the context they have. Request information such as:
- Background on the project/problem
- Related team discussions or shared documents
- Why alternative solutions aren't being used
- Organizational context (team dynamics, past incidents, politics)
- Timeline pressures or constraints
- Technical architecture or dependencies
- Stakeholder concerns

Advise them not to worry about organizing it — just get it all out. They can point you at files in the workspace to read, or paste content directly.

**During context gathering:** if the user mentions entities/projects you don't know, ask if workspace files should be searched to learn more, and wait for confirmation before searching.

**Asking clarifying questions:**
When the user signals they've done their initial dump, ask 5-10 numbered questions based on gaps in the context. Tell them they can answer in shorthand (e.g., "1: yes, 2: see design.md, 3: no because backwards compat").

**Exit condition:**
Sufficient context has been gathered when your questions show understanding — when you can ask about edge cases and trade-offs without needing basics explained.

**Transition:**
Ask if there's more context they want to provide, or if it's time to move on to drafting. When ready, proceed to Stage 2.

## Stage 2: Refinement & Structure

**Goal:** Build the document section by section through brainstorming, curation, and iterative refinement.

**Instructions to user:**
Explain that the document will be built section by section. For each section:
1. Clarifying questions will be asked about what to include
2. 5-20 options will be brainstormed
3. User will indicate what to keep/remove/combine
4. The section will be drafted
5. It will be refined through surgical edits

Start with whichever section has the most unknowns (usually the core decision/proposal), then work through the rest.

**Section ordering:**
If the document structure is clear, ask which section they'd like to start with and suggest starting with the one with the most unknowns. Summary sections are best left for last. If the user doesn't know what sections they need, suggest 3-5 sections appropriate for the doc type and ask if the structure works.

**Once structure is agreed:**
Create the initial document with all section headers and placeholder text like "[To be written]" or "[Content here]" using `write_file` (e.g., `decision-doc.md`, `technical-spec.md`). Confirm the filename and indicate it's time to fill in each section.

**For each section:**

1. **Clarifying Questions**: Announce work will begin on the [SECTION NAME] section. Ask 5-10 specific questions about what should be included. They can answer in shorthand or just indicate what's important to cover.
2. **Brainstorming**: Brainstorm 5-20 things that might be included, looking for forgotten context and angles not yet mentioned. Offer to brainstorm more.
3. **Curation**: Ask which points should be kept, removed, or combined, with brief justifications to learn their priorities ("Keep 1,4,7,9", "Remove 3 (duplicates 1)", "Combine 11 and 12"). If they give freeform feedback ("looks good but..."), extract their preferences and apply them.
4. **Gap Check**: Ask if anything important is missing for the section.
5. **Drafting**: Use `write_file` to replace the placeholder with the actual drafted content. Confirm the section has been drafted and ask them to read it and indicate what to change.
6. **Iterative Refinement**: As feedback arrives, make surgical edits with `write_file` (never reprint the whole doc into chat). Continue until the user is satisfied.

**Key instruction for user (first section):** Ask them to indicate what to change rather than editing the doc directly — this helps you learn their style ("Remove the X bullet — already covered by Y", "Make the third paragraph more concise").

**Quality Checking:** After 3 consecutive iterations with no substantial changes, ask if anything can be removed without losing important information. When done, confirm the section is complete and ask if ready to move on.

**Near Completion:** As you approach 80%+ of sections done, announce you'll re-read the entire document checking for flow, consistency, redundancy, contradictions, and filler. Read it and provide feedback. When all sections are drafted, review the complete document for coherence and completeness, give final suggestions, and ask if ready for Reader Testing.

## Stage 3: Reader Testing

**Goal:** Test the document with a fresh reader (no context bleed) to verify it works for readers. This catches blind spots — things that make sense to the author but confuse others.

**Note on mechanics in this harness:** this subagent cannot spawn further subagents, so reader testing is done either by the *parent agent* (it can delegate to a fresh subagent that sees only the document) or manually by the user.

### Step 1: Predict Reader Questions

Announce your intention to predict what questions readers might ask. Generate 5-10 questions a reader would realistically ask when trying to use/discover this document.

### Step 2: Test with a Fresh Reader

- **If the parent agent is available:** hand the document path and the question list back to the parent, asking it to run a fresh subagent on just the document content + questions (no conversation history), and report what the fresh reader got right/wrong.
- **Otherwise (manual):** give the user testing instructions: open a new conversation, paste the document content, ask the generated questions, and check whether the answers are correct and nothing is ambiguous.

For each question, the fresh reader should provide: the answer, anything ambiguous or unclear, and what knowledge/context the doc assumes is already known.

### Step 3: Additional Checks

Also ask the fresh reader (or have the user ask):
- "What in this doc might be ambiguous or unclear to readers?"
- "What knowledge or context does this doc assume readers already have?"
- "Are there any internal contradictions or inconsistencies?"

### Step 4: Iterate Based on Results

If the fresh reader struggled, report the specific issues, loop back to refinement for the problematic sections, and re-test.

**Exit Condition:** When the fresh reader consistently answers questions correctly and doesn't surface new gaps or ambiguities, the doc is ready.

## Final Review

When Reader Testing passes, announce the doc passed. Before completion:
1. Recommend the user do a final read-through themselves — they own this document
2. Suggest double-checking any facts, links, or technical details
3. Ask them to verify it achieves the impact they wanted

**If they want a final review, provide it. Otherwise:** announce completion with final tips:
- Consider linking this conversation in an appendix so readers can see how the doc was developed
- Use appendices to provide depth without bloating the main doc
- Update the doc as feedback arrives from real readers

## Tips for Effective Guidance

**Tone:** Be direct and procedural. Explain rationale briefly when it affects user behavior. Don't "sell" the approach — just execute it.

**Handling Deviations:** If the user wants to skip a stage, ask if they want to skip it and write freeform. If they seem frustrated, acknowledge it's taking longer than expected and suggest ways to move faster. Always give the user agency to adjust the process.

**Context Management:** If context is missing on something mentioned, proactively ask — don't let gaps accumulate.

**File Management:** Use `write_file` to draft and edit sections (never reprint the whole doc into chat). Keep the working file path consistent and confirm edits are complete.

**Quality over Speed:** Don't rush through stages. Each iteration should make meaningful improvements. The goal is a document that actually works for readers.
