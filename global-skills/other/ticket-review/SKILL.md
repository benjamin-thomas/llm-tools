---
name: ticket-review
description: Review a completed ticket against its acceptance criteria, append an AI review section, and move it to ai-reviewed or flunked.
---

# Ticket Review

Review implementation only. Do not fix it.

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

Read from `_tickets/done/`. Move to `_tickets/ai-reviewed/<subject>/` or `_tickets/flunked/<subject>/`.

When moving tickets:
- preserve the subject directory under the target state
- derive the subject from the source path, never from `ticket.md`
- remove the source subject directory if it becomes empty

## Workflow

1. Let the user choose a ticket
- If `_tickets/` does not exist, tell the user to run `ticket-create` first and stop.
- List tickets in `_tickets/done/`, grouped by subject.
- If none exist, report that there is nothing to review and stop.
- Never auto-pick.

2. Read the full ticket history
- Read `ticket.md`.
- Treat acceptance criteria as the checklist.
- Read all prior dated `AI Review` and `Human Review` sections for context.

3. Verify every criterion
- Gather evidence from files, tests, commands, or observed behavior.
- Cite `file:line` references and command results.
- Mark each criterion as pass, fail, or partial.

4. Append a new review section

```markdown
## AI Review (YYYY-MM-DD)

**Status**: PASS | FAIL | PARTIAL

### Criteria Assessment
- ✓ Criterion 1 — <evidence>
- ✗ Criterion 2 — **FAIL**: <what is wrong and what is expected>
- ~ Criterion 3 — **PARTIAL**: <what passes and what does not>

### Summary
<Overall assessment and anything the human reviewer should watch>

### Issues (if FAIL)
1. <Specific issue and what fixed looks like>
```

- Always append. Never edit or delete earlier review sections.

5. Move the ticket
- PASS: move to `_tickets/ai-reviewed/<subject>/`
- FAIL: move to `_tickets/flunked/<subject>/`
- PARTIAL: ask the user whether to pass it through or send it back
- After the user decides on a PARTIAL result, rewrite the new status line to:
  - `PASS (PARTIAL, user-approved)` or
  - `FAIL (PARTIAL, user-rejected)`

6. Report
- Summarize the ticket, subject, review result, key findings, and destination.
- If it moved to `ai-reviewed`, remind the user to finish with `ticket-close`.

## Rules

- Never modify implementation code.
- Never auto-pick a ticket.
- Never skip any acceptance criterion.
- Never pass a ticket with failing criteria.
- Always append review history; never overwrite earlier sections.
- Make failures actionable: what is wrong, where it is wrong, and what fixed looks like.
