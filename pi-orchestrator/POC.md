# POC Recipe

## Goal

Determine whether Pi can provide a better operator experience than the current
tmux orchestrator without sacrificing full worker interaction, shared-directory
TDD discipline, or model diversity.

Do not begin with a custom implementation. Run two focused package trials,
record friction, and fork only after identifying the smallest missing layer.

## Preparation

Use a small throwaway Git repository with:

- a fast deterministic unit test suite;
- no network or service dependencies;
- one known, bounded behavior to add;
- a clean Git state;
- no secrets in the working tree.

Choose three provider/model IDs that Pi can resolve before spawning anything.
Record them as seats A, B, and C. Do not silently fall back to another model.

Baseline measurements:

- time to create/switch/close a session;
- keystrokes required to enter and leave a worker;
- whether a worker remains fully interactive;
- whether model and context are visible;
- what happens to an in-flight turn while switched away;
- how recovery behaves after Pi restarts;
- whether two sessions can accidentally write concurrently.

## Trial A: Native Single-Terminal Cockpit

Candidate: [`pi-parallel-sessions`](https://github.com/liushihao456/pi-sessions)

Install according to the package README:

```bash
pi install npm:pi-parallel-sessions
```

Restart Pi after installation.

Exercise:

1. Start the parent session as the coordinator.
2. Open `/sessions` or its configured shortcut.
3. Create three child sessions in the same repository directory.
4. Set a different favorite model in each child.
5. Switch repeatedly between parent and children.
6. Enter several ordinary prompts directly in each child.
7. Start a read-only operation, switch away, and verify it continues.
8. Attempt conflicting edits and inspect the path-lock behavior.
9. Stop one child and resume a saved session as a replacement.
10. Restart Pi and document which live-session state is recoverable.

Pass criteria:

- selected child is a full native Pi UI, not a transcript viewer;
- switching feels like changing cockpit rather than attaching another terminal;
- ordinary interaction needs no coordinator relay;
- all sessions can share exactly the same cwd;
- one custom shortcut can toggle parent/current child;
- no transcript or model identity is silently lost.

The conflict test must include simultaneous writes to different files and
mutations hidden behind package scripts, interpreters, and shell commands. A
path-lock success is not evidence of the required global writer lease.

Likely missing after this trial:

- coordinator dispatch;
- fixed model seats;
- tool/phase policies;
- role rotation and context recreation;
- durable run evidence.

## Trial B: Interactive Worker Lifecycle

Candidate:
[`pi-interactive-subagents`](https://github.com/HazAT/pi-interactive-subagents)

Run this trial separately from Trial A to avoid extension and keybinding
interactions.

Install according to its README. Configure three named agents with:

- the same cwd;
- explicit provider/model IDs;
- no auto-exit for sessions intended for human interaction;
- role-appropriate tool access;
- fresh context by default.

Exercise:

1. Spawn a tester worker.
2. Enter its pane and type an ordinary prompt.
3. Determine whether human input actually disables autonomous auto-exit in the
   installed version. Current source research conflicts with the README.
4. Return to the parent and send guidance back to the worker.
5. Complete and route the result to the parent.
6. Repeat with implementer and reviewer definitions.
7. Replace a worker with a fresh session using the same fixed model.

Pass criteria:

- workers are real Pi sessions;
- entering a worker is immediate and reliable;
- human takeover is unambiguous;
- parent/child result routing survives direct human interaction;
- fresh replacement does not change the shared working tree.

Likely mismatch:

- the external multiplexer remains the cockpit;
- no deterministic TDD relay;
- no fixed seat abstraction independent of role.

## Decision Gate

After both trials, choose exactly one path:

### Extend pi-parallel-sessions

Recommended if cockpit switching is right but workflow control is missing.
Add a narrow `relay` layer rather than importing a full team/task-board system.
Pin the supported Pi version range because the current package uses internal Pi
classes and runtime modules.

### Adopt pi-interactive-subagents

Choose this if multiplexer panes are acceptable and its human-takeover behavior
is substantially better than the existing tmux orchestration.

### Extend pi-subagentura

Choose this only if durable child recovery and artifact history prove more
important than single-process simplicity.

### Stop

Stop if none of the trials materially improves operator control over the
existing `tmux-orchestrator`. A new tool must justify its maintenance cost.

## Custom Extension Plan

If `pi-parallel-sessions` is selected as the base, implement in these slices.

### Slice 1: Seat Registry

Persist:

```ts
type Seat = {
  id: string
  provider: string
  model: string
  generation: number
  sessionId?: string
  role?: "red" | "green" | "review" | "refactor"
}
```

Acceptance tests:

- duplicate seat IDs are rejected;
- unavailable models fail before a run starts;
- model identity cannot change during a run;
- replacing context increments generation and creates a fresh session.

### Slice 2: Cockpit Toggle

Add a Pi shortcut using `registerShortcut`:

- coordinator -> current phase worker;
- worker -> coordinator;
- fallback to a picker if no current worker exists;
- never advance protocol state as a side effect.

Acceptance tests:

- toggle is symmetric;
- selected session receives ordinary user input;
- switching during streaming does not duplicate or abort the turn;
- closed worker returns safely to coordinator;
- binding conflicts are detected at startup.

### Slice 3: Writer Lease

Intercept write, edit, and mutating shell operations.

Acceptance tests:

- only the active RED or GREEN lease holder can mutate files;
- the active REFACTOR lease holder can mutate only after review approval;
- reviewer and coordinator writes are denied;
- direct human interaction does not implicitly grant a lease;
- stale generation cannot mutate after replacement;
- lease release occurs on abort, close, and phase transition.
- lease release blocks until the generation is quiescent and the workspace is
  stable.

Start with conservative mutating-shell detection. Unknown shell commands should
require confirmation rather than being assumed read-only.

Authoritative state must live outside the checkout and outside worker-visible
mounts. Test that worker tools cannot write phase state, evidence receipts, or
human approvals.

### Slice 4: Phase Machine

Implement phases and evidence validation from `PROTOCOL.md`.

Acceptance tests:

- GREEN cannot start without a verified failing test;
- compilation/setup failure does not satisfy RED;
- REVIEW receives the exact verified diff fingerprint;
- file changes invalidate prior verification receipts;
- fingerprints include base commit, all relevant file content, verification
  specification, non-secret environment inputs, and tool versions;
- phase skips are rejected;
- all automatic transitions are recorded in `events.jsonl`.

### Slice 5: Role Rotation and Context Reset

Acceptance tests:

- cycle rotation follows the configured table;
- seat model remains fixed while role changes;
- each role change creates a fresh generation;
- new context contains approved artifacts but not old conversation history;
- reset leaves cwd and working tree untouched.

### Slice 6: Isolated Broker

Implement only:

- coordinator -> worker assignment;
- worker -> coordinator report/question;
- coordinator -> worker reply;
- human -> current cockpit owner through the native UI.

Acceptance tests:

- worker-to-worker addresses are rejected;
- messages are deduplicated;
- replies correlate to questions;
- a message cannot silently grant permissions or approve a gate;
- disconnected generations cannot consume current assignments.

### Slice 7: Human Gates

Commands:

```text
/relay-status
/relay-approve
/relay-reject <reason>
/relay-pause
/relay-resume
/relay-reset-seat <seat>
```

Acceptance tests:

- gate policy is deterministic;
- rejection returns to the correct phase;
- manual intervention invalidates stale evidence when files changed;
- coordinator cannot fabricate human approval;
- run can resume after process restart at a gate.

FINAL_VERIFY is executed by trusted harness code after all model actors are
idle; it is not delegated to a model session.

## First End-to-End Scenario

Use one bounded behavior with a fast test suite:

1. Seat A receives RED in a fresh context.
2. Human toggles into A, discusses the test, then returns to coordinator.
3. A writes one failing test and supplies the failure receipt.
4. Human approves RED.
5. Seat B receives GREEN in a fresh context.
6. Human may enter B and redirect implementation.
7. B makes the test and suite pass.
8. Seat C receives REVIEW in a fresh, read-only context.
9. C reports findings tied to the verified diff.
10. Human approves or rejects.
11. Roles rotate and all old worker generations close.

## Evaluation

Compare the POC with the existing tmux workflow using:

- escaped defects;
- mutation score where practical;
- human corrections per cycle;
- time spent navigating versus engineering;
- model and token cost;
- context-reset reliability;
- number of transport failures;
- ability to understand exactly who changed what and why.

The POC succeeds only if cockpit interaction becomes simpler without weakening
the human gates or one-writer discipline.
