---
name: ticket-create
description: Interview the user, inspect the codebase and knowledge base, and create context-window-sized ticket specs under _tickets/todo grouped by subject.
---

# Ticket Create

Create ticket specs only. Do not implement the work.

## State Machine

```
_tickets/
  todo/
  doing/
  done/
  flunked/
  ai-reviewed/
  (deleted)
```

`(deleted)` is a terminal pseudo-state handled by `ticket-close`. Never create `_tickets/deleted/`.

Tickets live at `_tickets/<state>/<subject>/<NNN_name>/ticket.md`.

## Workflow

1. Locate `_tickets/`
- If `_tickets/` does not exist, ask where to create it. Default to `./_tickets/`.
- Ensure `_tickets/todo/` exists.

2. Read knowledge first
- If `_knowledge/` exists, read `_knowledge/INDEX.md` and then only the relevant files.

3. Interview the user
- Clarify:
  - subject name; reuse an existing subject if it fits
  - what should be built
  - constraints and requirements
  - what done looks like
- Keep asking until the spec is unambiguous. Usually 2-4 rounds.

4. Research the codebase
- Read relevant files and conventions.
- Verify assumptions if needed.
- Clean up any temporary artifacts you create.

5. Split by context-window size
- Each ticket must fit in one context window without compaction.
- Split into sibling tickets when the work spans multiple independent concerns.
- Do not split simple, conceptually single work just because it touches many files.

6. Write ticket specs
- Subject determines the directory path and is the source of truth.
- Number per subject, across all ticket states, starting at `010` and incrementing by `10`.
- Default naming: `NNN_lowercase-hyphenated-description`.
- Keep hierarchy flat. Never nest tickets.

Use this format:

```markdown
---
summary: One-line description of the ticket
created: YYYY-MM-DD
---

# <Title>

## Goal
<What and why.>

## Context
<Current state, relevant files, patterns, dependencies, and knowledge references.>

## Acceptance Criteria
1. <Verifiable criterion>
2. <Verifiable criterion>
3. <Verifiable criterion>

## Notes
<Constraints, edge cases, or decisions from the interview.>
```

Do not record the subject in frontmatter.

7. Present before writing
- Show the proposed tree rooted at `_tickets/todo/<subject>/`.
- Summarize each ticket with its goal and acceptance criteria count.
- Incorporate feedback.
- Only write files after explicit user approval.

## Rules

- Never implement the tickets.
- Never skip the interview.
- Never create a ticket too large for one context window.
- Never nest tickets.
- Never put the subject in `ticket.md` frontmatter.
- Respect existing numbering within the subject across all states.
