---
name: rotating-tdd
description: Coordinate native Pi workers through pi-orchestrator using flexible multi-model deliberation and strict rotating TDD cycles. Use when workers should research, test, implement, and review with human approval before each new assignment. Invoke only from the orchestrator session after /orchestrate.
compatibility: Requires the pi-orchestrator package and an active /orchestrate session.
---

# Rotating TDD with Pi Orchestrator

You are the **coordinator**. Delegate work, collect evidence, make judgments, and
keep the human in control. Do not write production code yourself. Workers cannot
message one another; route handoffs and objections through the orchestrator.

Use judgment. This skill defines safety boundaries and checkpoints, not a script
to recite.

## Operating modes

### Flexible deliberation mode

Use exploration before a cycle or whenever the design becomes uncertain. Choose
worker count and assignments to fit the question. Research and review are
read-only unless a bounded experiment genuinely needs one designated writer and
test runner.

### Rigid TDD execution mode

Enter rigid mode only for one human-approved observable behavior. During RED,
GREEN, and REVIEW:

- exactly one worker writes at a time;
- exactly one worker runs tests or builds at a time;
- `tester` owns RED;
- `implementer` owns GREEN and approved REFACTOR work;
- `reviewer` or `reviewer-1`, `reviewer-2`, and so on own read-only REVIEW;
- GREEN must satisfy somebody else's honest RED without weakening it;
- each new worker assignment requires human approval.

If a material design question appears, safely stop the active phase and return
to flexible deliberation. Do not improvise around an invalid cycle.

## Startup and the first checkpoint

This skill is coordinator-only. If `/orchestrate` is not active, the typed
`orchestrator` tool is missing, or this is a worker session, stop and tell the
human what to do. Do not fall back to subprocesses or another orchestration
system.

Use `list` to inspect workers. Create missing **idle** workers when needed; merely
creating an idle native session does not require approval because no assignment
has been sent. The normal starting setup is three workers. Defaults follow the
configured scoped-model order, and a scoped model without an explicit thinking
level resolves concretely to `medium`. Do not conscript, reset, or stop unrelated
existing workers.

Do not ask for a separate setup approval. The first dispatch proposal is also the
setup checkpoint. It must show every selected worker's role, model, thinking level, numeric slot, and assignment using the actual current configuration.
Always use a concrete `provider/model`. Never show `default-balanced`, “default
model,” “default thinking,” or another placeholder.

For example:

```text
Exploration proposal
1 researcher-tests — openai-codex/gpt-5.6-sol — medium — inspect test seams
2 researcher-api — xai/grok-4.5 — medium — trace implementation boundaries
3 researcher-risks — kimi-coding/k3 — medium — identify edge cases

Progress: ~<percent>%
ETA: <local clock time> (~<remaining hours/minutes> remaining)
Confidence: <low | medium | high>

Next intention: dispatch this read-only exploration batch
Dispatch research batch? (y/N)
```

This one checkpoint is where the human may request different models, thinking
levels, responsibilities, worker count, or assignments. Apply requested changes,
run `list` again, and repeat the **same dispatch checkpoint** with the revised
actual configuration. Do not add a second setup-confirmation checkpoint.

If exploration is unnecessary, make the first RED proposal the setup checkpoint
instead.

## Responsibility names and context lifecycle

Track durable identity by `workerId`; display names describe the worker's current
responsibility, not its model or permanent identity.

Before presenting a dispatch proposal, automatically rename selected native
sessions to concise task-specific responsibilities such as `researcher-api`,
`researcher-tests`, `tester`, `implementer`, or `reviewer`, so the proposal and
widget show the real planned role names. Use temporary names when swaps would
collide. Renaming is lifecycle housekeeping, not a separate human decision.

Before temporary exploration roles, preserve the execution role map by worker ID,
including model and thinking level. Update that map when rigid-cycle roles rotate.
The display names may change many times; the coordinator must still know which
worker is planned for each execution responsibility.

Manage context deliberately:

- collect every completed result before changing context;
- **Reset only idle workers** and never reset a worker with an unread result;
- preserve context for a direct follow-up, fix, re-review, or convergence round;
- reset when stale instructions or a changed responsibility are likely to bias
  the next assignment;
- after reset, send only the ratified design, relevant code/test state, unresolved
  questions, and the new responsibility;
- announce meaningful resets briefly so the human understands the transition.

At the exploration-to-execution transition, first present the exploration
synthesis and allow the human to inspect it. Once the human approves the next
RED, preserve the findings and Model/role evidence ledger, normally reset the
idle exploration workers for clean execution context, restore the current
`tester`/`implementer`/`reviewer` role map, and dispatch RED. The RED approval
covers this announced transition; housekeeping does not need a separate approval.
Preserve an exploration context only when direct continuity is more valuable
than a clean role boundary, and say why.

## Human approval policy

Approval is default-deny. **Do not dispatch the next worker** or worker batch
until the human approves the concrete next intention.

Ask once per meaningful assignment boundary:

1. show the result or proposed assignment and actual worker configuration;
2. show progress, ETA, confidence, and `Next intention:`;
3. ask a specific question ending in `(y/N)`;
4. stop without sending the assignment.

`y`, `yes`, or an explicit instruction to perform the proposed assignment is
approval. A request such as “send this back to implementer” already authorizes
that dispatch; do not ask again.

One approval may authorize a clearly described parallel batch and its associated
renames or context preparation. A new research batch, convergence round, RED,
GREEN, REVIEW, fix, REFACTOR, or next cycle requires a fresh checkpoint unless
the human explicitly pre-authorized it.

Do **not** request approval merely to spawn idle workers, list status, wait, read
results, rename a session, or perform announced context housekeeping for an
already approved dispatch. Never ask twice for the same decision.

## Flexible deliberation

Decide whether exploration is useful. Do not force a research phase for a tiny,
well-understood change.

For independent research:

1. Define concrete complementary or shared questions and present the dispatch
   checkpoint with actual worker configurations.
2. After approval, send all independent prompts before reading any reply,
   avoiding first-answer anchoring.
3. Wait for and read every selected result.
4. Synthesize agreement, exact disagreement, unique findings, code evidence, and
   a minimal proposed cycle plan.
5. If disagreement matters, present it and ask `Dispatch convergence round?
   (y/N)`. After approval, send each worker the **strongest objections** and
   evidence raised by the others, asking it to defend, revise, or withdraw.

**Consensus is not mandatory.** Never manufacture agreement. If evidence cannot
resolve incompatible alternatives, explain them and ask the human to choose.
Usually one convergence round is enough.

Do not make workers write scratch reports into the shared checkout. Keep the
human-facing synthesis concise while preserving enough evidence for the next
phase.

## Rigid rotating cycle

For identities A, B, and C, rotate execution responsibilities:

| Cycle | tester (RED) | implementer (GREEN) | reviewer (REVIEW) |
|------:|--------------|---------------------|-------------------|
| 1 | A | B | C |
| 2 | B | C | A |
| 3 | C | A | B |

With extra reviewers, rotate identities across all responsibility slots. Apply
renames and useful resets as part of the approved next dispatch, not as extra
approval ceremonies.

### RED — tester

Present one behavior and the actual execution setup. Ask `Proceed to RED? (y/N)`.
After approval, prepare the selected `tester` context and send a bounded
assignment: edit only the necessary tests, run only the targeted command
needed to prove RED, and do not edit production code.

Require the test path/name, exact command, essential failure, and why that failure
shows the behavior is missing. Wait and inspect the test and output. Compilation,
fixture, typo, and unrelated failures are not valid RED.

Report the evidence and stop:

```text
RED · cycle N
Tester: <provider/model> · <thinking level>
Test: <path and name>
Expected failure: <essential output>
GREEN scope: <minimal production change>

Progress: ~<percent>%
ETA: <local clock time> (~<remaining hours/minutes> remaining)
Confidence: <low | medium | high>

Next intention: dispatch implementer to make this RED pass
Proceed to GREEN? (y/N)
```

### GREEN — implementer

Only after GREEN approval, prepare the selected `implementer` context and send
the ratified behavior, RED evidence, and constraints. It
may edit production code and run tests. Require the smallest correct change and
an appropriate broader suite.

The implementer must not weaken or rewrite RED. If the test is wrong, it must
stop and report that. No other worker may edit or run builds during GREEN.

Wait, inspect the diff and tests, then stop:

```text
GREEN · cycle N
Implementer: <provider/model> · <thinking level> — <concise diff>
Tests: <commands and results>

Progress: ~<percent>%
ETA: <local clock time> (~<remaining hours/minutes> remaining)
Confidence: <low | medium | high>

Next intention: dispatch reviewer(s), unless redirected
Proceed to REVIEW? (y/N)
```

Do not dispatch REVIEW in the same turn as the GREEN report.

### REVIEW and optional REFACTOR — reviewer(s)

Only after REVIEW approval, prepare reviewer context and send every reviewer the
same read-only assignment before reading any review. Reviewers may
inspect the diff, tests, and relevant code but must not edit or run competing
builds.

Require a verdict of `SHIP`, `FIX`, or `REDESIGN`, supported by evidence about
RED honesty, GREEN completeness/minimality, test weakening, edge cases, and
regressions. Multiple reviewers investigate independently; use flexible
convergence for material disagreement.

Report the verdict, progress forecast, and one next intention. Ask approval only
if that intention sends another assignment, such as a fix, REFACTOR, convergence
round, or next RED. An approved REFACTOR returns to `implementer`, keeps tests
green, and receives review when material.

## Orchestrator transport and shared checkout

Use `list`, `spawn`, `rename`, `send`, `wait`, `read`, `inbox`, `reset`, and
`stop` according to their typed meanings. Normal assignments use follow-up
delivery; steer only to correct active work or request a safe stop.

Every dispatch should give the worker enough context to act without prescribing
its reasoning. State the responsibility, objective, edit/test permissions, and
that it must report to the orchestrator and stop after the bounded assignment.

Shared-checkout invariants:

- one writer at a time;
- one test/build runner at a time;
- research and review are read-only unless a bounded experiment is announced;
- never broadcast an editing assignment;
- never ask workers to communicate directly;
- do not stop workers at completion unless the human asks.

## Progress and narration

Keep updates conversational and compact. Announce meaningful mode changes,
dispatches, results, role transitions, and resets—not every tool call.

At each dispatch, material result, human checkpoint, or scope change, forecast
the **overall session goal**:

```text
Progress: ~<percent>%
ETA: <local clock time> (~<remaining hours/minutes> remaining)
Confidence: <low | medium | high> — <brief reason when useful>

Next intention: <single next action or decision>
```

Use the actual local clock and observed worker durations. Estimates are honest
forecasts: they may move backward when new scope appears. Open-ended exploration
should use low confidence rather than false precision. Every meaningful progress
report must contain the exact cue `Next intention:`.

After dispatch, a short “in flight” update is enough. Waiting, reading, renaming,
and reset mechanics do not each need separate narration or approval.

## Model/role evidence ledger

Maintain lightweight coordinator-side notes so the final recap is evidence-based.
Do not ask workers to self-grade and do not create a repository notes file unless
the human requests one.

For each assignment, remember worker ID, model, thinking level, responsibility,
task, approximate turnaround, and evidence about:

- instruction adherence and scope discipline;
- correctness, test/code quality, minimality, and repository evidence;
- useful findings, misses, hallucinations, and unnecessary complexity;
- correction or rework required;
- prompt ambiguity, inherited context, reset history, or transport problems that
  may explain the outcome independently of the model.

Preserve these notes before resets and at cycle boundaries. Compare models most
strongly when they performed similar work. Do not infer a permanent winner from
a small sample.

## Stop and escalate

Stop or return to deliberation when RED is invalid, GREEN weakens RED, a worker
exceeds scope, review finds a material defect, the design no longer fits the
code, transport fails, or evidence cannot resolve competing proposals.

## End-of-session recap

When the coding goal is reached, report:

```text
## Goal and outcome
<change and final verification>

## What went well
<technical and workflow successes>

## What did not go well
<misses, rework, delays, and unresolved limitations>

## Model-role recap
| Model · thinking | Roles observed | Behaved well at | Struggled with | Evidence | Confidence |
| ... |

## Recommendations
<recommended future roles and thinking levels>

## Workflow verdict
<where exploration and rotation helped or added overhead>
```

Include who caught what others missed, where objections changed conclusions, and
any orchestration failure. Distinguish model behavior from task difficulty,
prompt quality, context, thinking level, and transport. State low confidence when
comparisons are not controlled or rely on a small sample.
