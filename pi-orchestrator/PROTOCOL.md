# Cockpit Protocol

## Terms

### Coordinator

The session that owns workflow state, dispatches work, evaluates evidence, and
asks the human for approval. It is not allowed to write production code during
the relay.

### Seat

A durable identity with a model fixed for the run:

```text
seat A -> provider/model-a
seat B -> provider/model-b
seat C -> provider/model-c
```

A seat is not a role and is not a conversation. Its active conversation is a
replaceable generation.

### Role

The temporary responsibility assigned to a seat for one cycle:

- RED: write one failing test;
- GREEN: make the approved test pass minimally;
- REVIEW: inspect test honesty, correctness, and missing cases;
- REFACTOR: improve structure without changing behavior, when approved.

### Generation

A fresh Pi session created when a seat receives a new role. Replacing a
generation uses Pi's real fresh-session operation, such as `/new`,
`ctx.newSession()`, or RPC `new_session`; old transcripts remain auditable but
are not supplied to the new model context.

### Cockpit Owner

The one session whose native Pi UI currently owns the terminal. The owner may
be the coordinator or one worker generation.

## Rotation

Three-seat rotation:

| Cycle | RED | GREEN | REVIEW |
|---|---|---|---|
| 1 | A | B | C |
| 2 | B | C | A |
| 3 | C | A | B |

Four-seat rotation:

| Cycle | RED | GREEN | REVIEW |
|---|---|---|---|
| 1 | A | B | C, D |
| 2 | B | C | D, A |
| 3 | C | D | A, B |
| 4 | D | A | B, C |

The model remains attached to the seat. The role prompt, permissions, and
conversation are recreated each turn.

## Shared Workspace

All sessions use the same working directory.

Hard invariants:

1. At most one session holds the writer lease.
2. Only the writer lease holder may call write/edit or mutating shell tools.
3. Only the active phase actor may run the test suite.
4. REVIEW sessions are read-only.
5. The coordinator is read-only during the relay.
6. A cockpit switch does not transfer the writer lease.
7. Human edits are allowed, but mark the current phase evidence stale until
   verification is rerun.
8. A lease cannot be released until the actor is idle, has no pending tool or
   message work, its owned subprocesses are stopped, and workspace stability is
   verified.

The extension must enforce these rules in tool hooks. Prompt instructions are
not sufficient.

## State Machine

```text
IDLE
  -> EXPLORE
  -> DESIGN_GATE
  -> RED_ACTIVE
  -> RED_GATE
  -> GREEN_ACTIVE
  -> GREEN_VERIFIED
  -> REVIEW_ACTIVE
  -> REVIEW_GATE
  -> REFACTOR_ACTIVE? 
  -> FINAL_VERIFY
  -> CYCLE_GATE
  -> ROTATE
  -> RED_ACTIVE | COMPLETE
```

Required transition evidence:

| Transition | Evidence |
|---|---|
| RED_ACTIVE -> RED_GATE | focused test diff, command, nonzero exit, expected failure fingerprint |
| RED_GATE -> GREEN_ACTIVE | human or configured automatic approval |
| GREEN_ACTIVE -> GREEN_VERIFIED | focused test passes, full verification passes |
| GREEN_VERIFIED -> REVIEW_ACTIVE | production diff fingerprint and verification receipt |
| REVIEW_ACTIVE -> REVIEW_GATE | structured review verdict tied to the same fingerprint |
| REVIEW_GATE -> REFACTOR_ACTIVE | explicit accepted finding or refactor request; current GREEN seat receives a fresh REFACTOR generation and lease |
| REVIEW_GATE -> FINAL_VERIFY | approval with no required changes |
| REFACTOR_ACTIVE -> FINAL_VERIFY | refactor diff plus focused verification passes; lease is released after quiescence |
| FINAL_VERIFY -> CYCLE_GATE | clean required verification receipt |
| CYCLE_GATE -> ROTATE | human or configured automatic approval |

No model may directly mutate the state-machine phase. Models submit evidence;
deterministic coordinator code validates and advances state.

The REFACTOR actor is the seat that held GREEN for the cycle, but in a fresh
REFACTOR generation. It may edit tests and production code only within the
accepted review scope. FINAL_VERIFY is run by the trusted coordinator harness,
not by the coordinator model, and holds no model-facing writer lease.

## Human Gates

Each gate supports one policy:

- `always`: stop and ask;
- `on_failure`: stop only if verification fails;
- `on_disagreement`: stop if reviewers differ or evidence conflicts;
- `manual`: never advance until the user invokes the transition;
- `never`: advance after mechanical validation.

Recommended initial configuration:

```yaml
gates:
  design: always
  red: always
  green: never
  review: always
  cycle: always
```

## Isolated Rooms (Target Architecture)

This section describes the proposed relay protocol, not the implemented
`/orchestrate room` mode. The current room broker supports moderated peer-visible
messages and explicit peer response calls; silo mode is the implemented strict
hub-and-spoke option.

The target relay topology is strict hub-and-spoke:

```text
seat A generation <-> coordinator
seat B generation <-> coordinator
seat C generation <-> coordinator
human <-> current cockpit owner
```

In the target relay, workers cannot list, address, or receive messages from
other workers. The coordinator forwards only approved artifacts needed for the
next phase.

Message envelope:

```json
{
  "id": "msg-...",
  "run": "run-...",
  "cycle": 1,
  "phase": "GREEN_ACTIVE",
  "from": "coordinator",
  "to": "seat-b/generation-2",
  "kind": "assignment",
  "artifactRefs": ["red-test.diff", "red-failure.json"],
  "text": "Make the approved test pass minimally."
}
```

A future isolated-mode broker must reject peer recipients.

## Context Reset

On a role change:

1. Mark the old generation closed.
2. Preserve its transcript and evidence references.
3. Create a fresh Pi session with the seat's fixed model.
4. Apply the new role's system prompt and tool policy.
5. Inject only the ratified design and approved evidence bundle.
6. Do not inject the old generation's conversation summary.

Before step 1, abort or settle the old turn, reject pending input, terminate
owned subprocesses, wait for idle, and confirm that the workspace remains
unchanged for a configured stability interval.

Context reset must not alter the shared working tree.

## Cockpit Controls

Minimum controls:

| Action | Suggested binding |
|---|---|
| Toggle coordinator/current worker | `Ctrl-]` |
| Open seat/session picker | `Ctrl-R` or `/cockpit` |
| Return to coordinator | `Esc Esc` or `/coordinator` |
| Stop current model turn | existing Pi abort binding |
| Hand control back to coordinator | `/handoff` |
| Show protocol state | `/relay-status` |
| Approve current gate | `/relay-approve` |
| Reject current gate | `/relay-reject <reason>` |

Bindings are provisional and must be checked against the user's Pi keymap.

Switching changes only the terminal owner. It does not automatically prompt,
resume, approve, grant tools, or transfer the writer lease.

## Operator Interaction

When the human enters a worker:

- the full native Pi session is interactive;
- ordinary prompts go directly to that worker;
- the coordinator does not mirror or rewrite the prompt;
- worker responses remain in that worker transcript;
- a durable `human_intervened` event is added to the run log;
- any changed requirement must be explicitly ratified before the state machine
  advances.

This makes cockpit entry a first-class workflow operation, not a debug view.

## Durable Run State

Suggested trusted control-plane layout:

```text
${XDG_STATE_HOME:-~/.local/state}/pi-relay/<project-id>/
  config.yaml
  runs/<run-id>/
    state.json
    events.jsonl
    seats.json
    artifacts/
      design.md
      cycle-001-red.diff
      cycle-001-red-failure.json
      cycle-001-green.diff
      cycle-001-verification.json
      cycle-001-review.json
```

Do not place authoritative run state inside the worker-writable checkout. The
coordinator extension owns this directory, writes atomically, and exposes only
typed read/report operations to workers. Human approval records may be created
only by the human-facing command handler and include event provenance.

Application-level tool hooks do not protect this directory from a malicious
same-user shell. Workers must not receive unrestricted host shell access. Use a
restricted command runner or OS sandbox that mounts the shared checkout but not
the control-plane directory. Document this threat boundary explicitly.

Do not store API keys or provider credentials in the control-plane directory.

## Canonical Workspace Fingerprint

Every gate binds to one canonical fingerprint containing:

- repository identity and base commit OID;
- content digests for tracked, staged, unstaged, and relevant untracked files;
- the exact focused and full verification command specifications;
- selected non-secret environment/configuration inputs;
- relevant tool and runtime versions.

The trusted harness recomputes the fingerprint immediately before every phase
advance. Any mismatch invalidates prior test, verification, review, and human
approval receipts.

## Future Reviewer Room

Reviewer rooms are explicitly out of scope for the first POC. If added later:

1. reviewers first submit private independent findings;
2. findings are revealed simultaneously;
3. each reviewer receives one bounded rebuttal turn;
4. each submits a final verdict tied to the same diff fingerprint;
5. the coordinator or human resolves disagreement.

Two reviewers do not elect a leader and cannot produce a majority vote. The
coordinator remains the decision authority.
