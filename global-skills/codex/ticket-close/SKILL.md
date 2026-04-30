---
name: ticket-close
description: Present AI-reviewed ticket work for human approval, capture any lasting domain knowledge through a short interview, and then delete or flunk the ticket.
---

# Ticket Close

Handle the human approval gate for tickets in `_tickets/ai-reviewed/`.

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

Read from `_tickets/ai-reviewed/`. On approval, delete the ticket directory. On rejection, move it to `_tickets/flunked/<subject>/`.

When moving or deleting tickets:
- preserve the subject directory on moves
- derive the subject from the source path, never from `ticket.md`
- remove the source subject directory if it becomes empty

## Workflow

1. Let the user choose a ticket
- If `_tickets/` does not exist, tell the user to run `ticket-create` first and stop.
- List tickets in `_tickets/ai-reviewed/`, grouped by subject.
- If none exist, report that there is nothing to close and stop.
- Never auto-pick.

2. Present the work for final review
- Read `ticket.md`, including all prior `AI Review` and `Human Review` sections.
- Summarize:
  - title and subject
  - goal
  - latest AI review summary
  - what changed
- Ask: `Approve or reject?`

3. If rejected
- Ask what needs to change.
- Append:

```markdown
## Human Review (YYYY-MM-DD)

**Status**: REJECTED

### Feedback
<human feedback>
```

- Move the ticket to `_tickets/flunked/<subject>/`.
- Report that it has been sent back for rework.
- Stop. Do not do knowledge capture.

4. If approved, analyze before interviewing
- Read the diff and surrounding code.
- Read the current `_knowledge/` contents if present.
- Form hypotheses about domain significance, not just code mechanics.

5. Interview the user
- Ask 2-4 targeted questions.
- Focus on:
  - domain invariants
  - business why
  - concept mapping
  - domain subtleties
- Accept that many tickets yield no new knowledge.

6. Propose knowledge updates only if warranted
- Knowledge should capture business-domain truth, not coding conventions.
- If `_knowledge/` does not exist, bootstrap it only when there is something real to capture.
- Use this structure:

```text
_knowledge/
  INDEX.md
  <category>/
    INDEX.md
    <topic>.md
```

- Topic files should use:

```markdown
---
summary: <one line>
updated: <YYYY-MM-DD>
relates: [<category>, ...]
---

<Present-tense domain knowledge>
```

- Propose the specific additions or modifications before writing them.
- If nothing is worth capturing, say so explicitly.

7. Delete the ticket
- Remove the ticket directory from `_tickets/ai-reviewed/<subject>/`.
- Remove the now-empty subject directory if applicable.

8. Report
- Confirm the ticket was closed and deleted.
- Note whether knowledge was updated or intentionally left unchanged.

## Rules

- Never delete a ticket without explicit human approval.
- Never auto-pick a ticket.
- Never skip the interview after approval.
- Never propose knowledge updates without interview-backed evidence.
- Never document generic coding patterns or implementation trivia unless inseparable from the domain.
- Always append new human review sections; never rewrite earlier ones.
