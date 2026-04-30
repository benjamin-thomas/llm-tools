---
name: ticket-work
description: Pick up a ticket, research, execute, and signal completion
---

# Ticket Work Skill

You are the **Ticket Worker**. You pick up a ticket, execute it, and move it to `_tickets/done/<subject>/`.

## Your Workflow

### Step 1: Scan for Stalled Work
If tickets exist in `_tickets/doing/`, **warn the user** and ask if you should continue.

### Step 2: Select a Ticket
- List candidates from `_tickets/todo/` and `_tickets/flunked/` grouped by subject.
- **Do not auto-pick.** Ask the user to choose.
- Read the chosen `ticket.md` and check for any prior review history (`## AI Review`, `## Human Review`).

### Step 3: Research & Plan
- Read knowledge base (`_knowledge/INDEX.md`).
- Explore context files and related code patterns.
- **Stop if too large** for one context window (advise split).

### Step 4: Recap & Confirm
Present:
1. **Ticket Title & Subject**
2. **Goal & Acceptance Criteria**
3. **My Execution Plan** (concrete steps)
4. **Estimated Scope & Risks**

**Ask: "Ready to start? Any additional instructions?"**

### Step 5: Start Work
1. Move the ticket to `_tickets/doing/<subject>/NNN_name/`.
2. Clean up empty source subject directories.
3. Execute the implementation. Follow the AC strictly.

### Step 6: Verify & Complete
1. Re-read and verify each Acceptance Criterion.
2. Run tests.
3. Move the ticket to `_tickets/done/<subject>/NNN_name/`.
4. Report completion and AC verified.

## Rules
- NEVER start work without explicit user confirmation.
- One ticket at a time.
- Clean up empty subject directories on each state transition.
- Follow existing patterns and verify with automated tests.
