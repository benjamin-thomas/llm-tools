---
name: knowledge-condense
description: Condense the current session's lasting learnings into the project's _knowledge base after a short targeted interview and explicit user approval.
---

# Knowledge Condense

Use this at the end of a session when the work produced durable understanding worth preserving, but not through the `_tickets/` workflow.

## Purpose of the Knowledge Base

The knowledge base serves two readers:
- a human who needs fast onboarding
- the next LLM agent who needs high-signal context

Treat it as double-bookkeeping: a condensed second specification that stays aligned with the code. Document what the system does and why, especially when that is subtle, cross-cutting, or easy to misunderstand. Do not document generic coding style, framework conventions, or obvious file structure.

## Workflow

1. Analyze the session
- Review what was built, explored, or decided in the current conversation.
- Identify domain concepts, invariants, constraints, or non-obvious subsystem behavior worth preserving.

2. Interview the user
- Ask 2-4 targeted questions.
- Focus on:
  - invariants
  - business or system "why"
  - concept mapping
  - subtle edge cases
- Accept "that's obvious" or "nothing to capture" as valid outcomes.

3. Read the current knowledge base
- If `_knowledge/` exists, read `_knowledge/INDEX.md` and the relevant category files.
- Avoid duplication.
- If you notice small drift, flag it and propose fixes inline.
- If you notice broad drift, recommend `knowledge-verify` and stay focused on current-session learnings.

4. Propose updates
- Show exactly what files to create or update and the content you would write.
- Wait for explicit approval before writing.

5. Write only after approval
- Create or update knowledge files.
- Keep `INDEX.md` files in sync.

## Structure

```text
_knowledge/
  INDEX.md
  <category>/
    INDEX.md
    <topic>.md
```

Topic files should use:

```markdown
---
summary: <one-line description>
updated: <YYYY-MM-DD>
relates: [<other-category>, ...]
---

<Present-tense knowledge about how the system works and why>
```

## Rules

- Never write without explicit user approval.
- Never skip the interview.
- Never document generic coding patterns unless inseparable from the domain.
- Keep the knowledge base small and trustworthy.
- If the session disproved existing knowledge, raise and correct it with approval.
