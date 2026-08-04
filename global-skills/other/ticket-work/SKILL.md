---
name: ticket-work
description: Pick a ticket from _tickets/todo or _tickets/flunked, confirm the plan with the user, execute it, and move it through doing to done.
---

# Ticket Work

Execute one ticket and move it to `_tickets/done/<subject>/` when complete.

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

Tickets live at `_tickets/<state>/<subject>/<NNN_name>/`.

When moving tickets:
- preserve the subject directory under the target state
- derive the subject from the source path, never from `ticket.md`
- remove the source subject directory if it becomes empty

## Workflow

0. Check `doing/` for stalled work
- If any tickets are already in `_tickets/doing/`, warn the user and list them.
- Do not touch them without explicit instruction.
- Continue only after the user acknowledges.

1. Let the user choose a ticket
- If `_tickets/` does not exist, tell the user to run `ticket-create` first and stop.
- List tickets in `_tickets/todo/` and `_tickets/flunked/`, grouped by subject.
- Within each subject, order by numeric prefix.
- Never auto-pick.

2. Read the ticket thoroughly
- Read `ticket.md`.
- For flunked tickets, read all prior `AI Review` and `Human Review` sections.

3. Read knowledge and surrounding code
- Read `_knowledge/INDEX.md` when present, then only relevant files.
- Read the files named in the ticket context and related code paths.

4. Present a recap before starting
- Show:
  - ticket title and subject
  - goal
  - acceptance criteria
  - a concrete plan
  - estimated scope, file count, and risks
- Ask: `Ready to start? Any additional instructions?`
- Do not move the ticket or edit code before user confirmation.

5. Start work
- Move the ticket into `_tickets/doing/<subject>/`.
- Implement the plan.
- Follow the acceptance criteria as the definition of done.

6. Verify your own work
- Check every acceptance criterion.
- Run relevant tests or validation.
- Fix anything missing before declaring completion.

7. Signal completion
- Move the ticket into `_tickets/done/<subject>/`.
- Tell the user:
  - what changed
  - which criteria you verified
  - that the ticket is ready for `ticket-review`

## Context Window Rule

If the ticket is too large to finish safely within one context window:
- stop
- move it back to `_tickets/todo/<subject>/`
- explain why it needs to be split
- direct the user back to `ticket-create`

## Rules

- Never start work before user confirmation.
- Never auto-pick a ticket.
- Never skip acceptance criteria.
- Never leave a ticket in `doing/` unless the user explicitly wants that state preserved.
- Never touch stalled `doing/` tickets from a previous session without permission.
