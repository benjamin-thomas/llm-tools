# pi-orchestrator

A Pi extension that upgrades the current session into an orchestrator for a
dynamic set of independently interactive workers.

## First slice

Run `/orchestrate` and choose a communication mode, or select one directly:

```text
/orchestrate silo
/orchestrate room
```

The current Pi session becomes the orchestrator. Silo mode is the safe default
and preserves the original hub-and-spoke behavior. Then describe the worker
setup in natural language, for example:

```text
Spin up three workers using the balanced default models.
Create workers using xai/grok-4.5 and kimi-coding/k3.
Rename the Grok worker to reviewer.
```

The orchestrator receives a typed `orchestrator` tool with `list`, `spawn`,
`rename`, `send`, `wait`, `read`, `reset`, and `stop` operations. Reset gives an
idle worker a fresh native session while preserving its stable identity, slot,
name, cwd, model, and thinking level. Workers can use only
models from the scoped models active when orchestration starts.

In silo mode, communication is hub-and-spoke. The orchestrator can dispatch an
instruction, wait for that worker to settle, and read structured session
messages from a stable entry cursor. Each send returns a dispatch ID and
pre-send cursor; automatic read cursors prevent transcript replay. An explicit
`cursor` inspects history without moving that automatic cursor. Completed
replies appear as `+N` worker badges and through the `inbox` operation;
intermediate assistant turns that only invoke tools do not increase the badge.
An idle worker starts immediately; a busy worker receives a follow-up by
default, with explicit steering available.

Room mode adds a durable shared channel while retaining independent native Pi
sessions. Addressing a named participant is a visible **tell** by default and
does not wake that participant. Set `expectReply: true` for an explicit named
**call**:

- `#john` or `#tester` records a tell unless `expectReply` is true;
- `#all` always requires every live worker to respond, excluding its worker sender;
- `#human` creates persistent human attention and interrupts a coordinator wait;
- `#orchestrator` requests an immediate moderation checkpoint;
- an unaddressed message is visible room context and wakes nobody.

Worker names are unique, case-insensitive addresses; `all`, `human`, and
`orchestrator` are reserved. The typed `room` tool carries canonical recipients,
while `#name` is the human-facing notation and has editor autocomplete. Stable
worker IDs are recorded when a message is posted, so later renames do not alter
history. A called worker receives unread room context, and its completed final
answer is published back to the room automatically.

Only one `#all` broadcast epoch may be open. A second broadcast is rejected
until every snapshotted recipient has responded or explicitly failed. Delivery
alone does not satisfy the barrier. Workers cannot create response obligations
while a broadcast is open, and each worker can owe at most one response.

Every completed broadcast requires moderation before the next response call.
A configurable worker-message interval (default eight) can also request a
checkpoint. At a checkpoint, new pump deliveries and ordinary worker posts are
paused while already-delivered responses may finish; `moderate continue` resumes
pending work. A coordinator `wait` tracks the open broadcast (or the current
snapshot of calls) rather than future obligations and returns early for
moderation or `#human`. The orchestrator can then `read`, `moderate`, resolve
human requests, continue, redirect, or conclude. For example:

```text
Create three workers named implementer, tester, and reviewer.
Configure moderation every six worker responses.
Ask #all to propose an approach independently, wait for the room to settle,
moderate the responses, and report agreements and unresolved objections to me.
```

Both modes render communication as a conversational transcript with explicit
`sender → receiver` labels. Technical metadata stays hidden in the default
view; expanding a tool result reveals syntax-highlighted YAML, with embedded
JSON recursively expanded for debugging. The original value remains in
tool-result details.

### Navigation

- From a worker, `Alt+S`, then Enter: return to the preselected orchestrator
- From the orchestrator, `Alt+S`, then Enter: enter the preselected most-recent worker (or first live worker)
- `Alt+S`, then `0`: focus the orchestrator
- `Alt+S`, then `1` through `8`: focus a stable worker slot
- `Alt+S`, then arrow keys and Enter: select from the same switcher interactively
- `Ctrl+D` in a worker with an empty editor: close that worker and return to the orchestrator
- `/worker-name <name>`: rename the focused worker
- `/orchestrate status`: show the mode and a short status summary
- `/orchestrate stop`: stop all workers and leave orchestration mode

The persistent worker widget shows each worker's responsibility/name, model,
thinking level, activity marker, and unread count.

Each worker is a full native Pi session. Its transcript, `/model` selector,
thinking-level controls, tools, and session name remain independent. Inactive
workers may continue running while another session owns the terminal. When the
orchestrator's Pi session is resumed after exit, orchestration mode and its
workers are restored automatically from their native session files.

## Rotating TDD skill

The package ships a Pi-native `rotating-tdd` skill. After activating
`/orchestrate silo`, invoke it explicitly with:

```text
/skill:rotating-tdd <work item>
```

It uses flexible multi-worker deliberation before and between rigid TDD cycles:
workers can independently research new questions, exchange objections through
the orchestrator, and converge where the evidence permits. During a cycle,
native sessions are named by responsibility (`tester`, `implementer`, `reviewer`,
or multiple numbered reviewers), while exploration uses task-specific names such
as `researcher-tests`. Only one worker writes or runs tests at a time, and the
human approves every new worker assignment. There is no separate setup approval:
the first research or RED proposal shows the actual model and thinking setup for
approval. Role switches manage idle worker context automatically, normally
resetting exploration context before rigid execution. Compact progress updates
include approximate overall completion, local-clock ETA, remaining time,
confidence, and an explicit `Next intention:` cue. Every new worker phase or
convergence round requires human approval by default, so RED and GREEN can be
inspected and redirected before the next responsibility is dispatched. During
the session, the coordinator tracks evidence about each model's behavior in each
role; the final recap compares strengths, failures, thinking levels, and
confidence without overgeneralizing from a small sample.

## Constraints

- All workers use exactly the orchestrator cwd.
- Default workers follow the user-configured scoped-model order by
  `(stable worker position % model count)`; explicit model choices override that
  slot without changing the configured order.
- A scoped model without an explicit thinking level requests `medium`; the worker record is updated to the level the child session actually accepts.
- The first slice allows at most eight live workers.
- Silo workers cannot message peers. Room workers communicate only through the typed broker; they never invoke another session directly.
- The worker list, selected mode, room log, cursors, response obligations, and
  broadcast barrier are persisted as hidden coordinator-session metadata.
  Resuming that coordinator restores idle worker runtimes, recovers completed
  responses from marked child prompts, and redispatches only unresolved room
  obligations; interrupted ordinary generation is not restarted automatically.
- `/orchestrate stop` records that orchestration was intentionally ended, so it
  is not restored on the next resume.
- Native terminal handoff uses an isolated compatibility adapter because Pi has
  no public suspend/resume API. The adapter accepts Pi `>=0.83.0 <1.0.0`, checks
  the private capabilities it uses at runtime, and was last tested with Pi
  `0.84.1`.

## Development

```bash
npm install --ignore-scripts
npm run check
pi install /absolute/path/to/pi-orchestrator
```

The repository's `.npmrc` sets `ignore-scripts=true`, so local `npm install`
and `npm ci` cannot execute dependency lifecycle scripts. Pi packages remain
`"*"` peer dependencies, while exact development dependencies record the Pi
version used by `npm run check`. Periodically update those development versions
and smoke-test regular and fullscreen worker handoff; newer pre-1.0 Pi releases
are not blocked merely because they have not been tested yet. Restart Pi after
installing or changing terminal-handoff code.

The earlier rotating-TDD research remains in `RESEARCH.md`, `PROTOCOL.md`,
`POC.md`, `TRIAL-NOTES.md`, and `HANDOFF.md` as historical design context for
the packaged workflow.
