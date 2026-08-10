# Implementation Handoff

## Objective

Build the smallest typed Pi extension that reproduces the useful semantics of
`tmux-orchestrator` with native, structured Pi sessions. Do not implement the
rotating-TDD protocol until this transport slice is proven interactively.

Read, in order:

1. [TRIAL-NOTES.md](TRIAL-NOTES.md)
2. [PROTOCOL.md](PROTOCOL.md)
3. [POC.md](POC.md)
4. Pi's installed `docs/extensions.md`, `docs/sdk.md`, `docs/tui.md`, and
   `docs/keybindings.md` completely.
5. The gitignored `tmp/pi-sessions/` reference when available.

## Current Environment

- Pi: `0.83.0`, `@earendil-works/pi-coding-agent`.
- The trial package may still be installed as the local source
  `tmp/pi-sessions`. Check with `pi list`.
- Remove it before trialing the replacement and fully restart Pi, because the
  package stores process-global state and monkey-patches `InteractiveMode`.

```bash
pi remove /home/benjamin/code/github.com/benjamin-thomas/llm-tools/tmp/pi-sessions
```

## First Vertical Slice

Create a local Pi package in this directory, with ordinary TypeScript source and
unit tests. Suggested layout:

```text
pi-rotating-tdd-cockpit/
  package.json
  src/
    index.ts
    host.ts
    registry.ts
    types.ts
    ui.ts
  test/
```

Keep Pi packages in `peerDependencies` using `"*"`, as required by Pi package
documentation. Do not use `@ts-nocheck`.

### Human UI

- `Alt-S` opens a compact selector.
- It lists the coordinator and live children.
- It can create a child directly in the coordinator's cwd.
- Enter activates the selected native Pi session.
- It can stop a child.
- No folder explorer, saved-session resume flow, worktrees, or web UI.

The exact in-picker keys are not yet important. Keep them conventional and show
hints in the component.

### Coordinator Control API

Expose a coordinator-only custom tool with at least these first operations:

```text
list
rename { sessionId, name }
```

`list` returns stable IDs, display names, cwd, model identity, activity, and
whether the session currently owns the terminal.

`rename` must:

- reject the coordinator and unknown IDs;
- reject duplicate or invalid names;
- update the live registry;
- call the child's `AgentSession.setSessionName()` so the name is persisted;
- update the selector/widget immediately.

This remote rename is the first proof that the coordinator model can control a
child without entering its UI or scraping terminal content.

### Tests for the First Slice

At minimum:

- stable child IDs do not change when display names change;
- duplicate names are rejected;
- unknown IDs are rejected;
- rename updates the registry snapshot;
- coordinator-only operations reject calls from children;
- activation requests are serialized;
- creating a child uses exactly the coordinator cwd.

Keep runtime/TUI integration behind narrow interfaces so registry and control
behavior can be tested without constructing a real terminal.

## Following Transport Slices

Only after list/rename and native switching work:

1. `send`: prompt an inactive child through `AgentSession.prompt()`; define
   deterministic behavior for busy children using steer/follow-up.
2. `wait`: resolve on `agent_settled`, including retries and queued messages.
3. `read`: return structured messages/results since a recorded cursor, not a
   rendered transcript.
4. `reset`: stop an idle generation and create a fresh child with the same seat
   model and cwd.
5. Model-pinned seats and role rotation.
6. Global writer lease and phase gates.

Do not add the RED/GREEN/REVIEW state machine before `list -> rename -> send ->
wait -> read` has been demonstrated end to end.

## Important Boundaries

- Exactly one native TUI owns the terminal; inactive model turns may continue.
- Switching terminal ownership must never grant write permission or advance a
  workflow phase.
- The coordinator needs structured control APIs; a human-only selector is not
  sufficient.
- Each child has a separate extension runner. Design explicit all-session
  reload behavior later; do not assume `/reload` is process-wide.
- Runtime internals may be necessary for native TUI handoff. Isolate them in one
  adapter and pin the supported Pi version.
- No screen scraping, synthetic key delivery, tmux dependency, or worker-to-
  worker messaging.
- Authoritative workflow state will eventually live outside the checkout. It is
  out of scope for the first transport slice.

## First Interactive Acceptance Scenario

1. Start the coordinator with the new local extension.
2. Press `Alt-S` and create one same-cwd child.
3. Switch into the child, send an ordinary prompt, and return to coordinator.
4. Ask the coordinator model to list live sessions.
5. Ask it to rename the child to `seat-a`.
6. Confirm the widget/selector changes without entering the child.
7. Switch back and confirm the child's native transcript and session name remain
   intact.

Stop and reassess if native switching is less usable than the existing tmux
workflow.
