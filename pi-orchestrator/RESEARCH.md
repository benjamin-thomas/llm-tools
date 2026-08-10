# Research Notes

Research date: 2026-08-04.

## Required Distinction

Most multi-agent products optimize for autonomous parallel execution. This
project instead needs an operator cockpit:

- the human remains the authority;
- sessions are collaborators, not disposable background jobs;
- entering a worker means using its full native conversation and controls;
- the protocol schedules work sequentially unless parallel reads are useful;
- the shared checkout is intentional;
- process quality matters more than task throughput.

This distinction eliminates many otherwise capable dashboards and swarm tools.

## OpenCode Findings

OpenCode has useful primitives:

- per-agent and per-prompt model selection;
- separate sessions and fresh child contexts;
- programmatic session creation, prompting, aborting, and event streaming;
- normal interactive switching through the session list;
- `tui.selectSession` in the current SDK;
- configurable built-in keybindings.

OpenCode Ensemble adds model-pinned teammates, messages, tasks, and
`team_view`, which calls `tui.selectSession`. In the experiment, workers could
share the checkout by passing `worktree: false`.

The mismatch was architectural rather than a missing API:

- teammates behaved as one-shot delegated jobs;
- completed teammates did not wake for ordinary follow-up team messages;
- fresh phase sessions had to be represented as new teammate identities;
- its primary UX is a mission-control dashboard and task board;
- its defaults assume parallel workers and worktrees;
- stable OpenCode does not document a plugin API for registering an arbitrary
  one-key coordinator/worker toggle.

OpenCode remains capable of hosting such a system, but Ensemble is not the
right codebase to extend for this workflow.

## Pi Foundation

Pi exposes the required low-level extension primitives:

- `registerShortcut` and `registerCommand`;
- custom overlays, widgets, status lines, and editors;
- model and thinking-level control;
- session lifecycle events;
- context transformation and custom compaction;
- embedded `AgentSession` APIs;
- RPC prompts, steering, follow-ups, aborts, and new sessions.

Official references:

- [Pi extensions](https://pi.dev/docs/latest/extensions)
- [Pi RPC mode](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)
- [Pi keybindings](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/keybindings.md)

## Ranked Pi Candidates

### 1. pi-parallel-sessions

Repository: <https://github.com/liushihao456/pi-sessions>

Strongest match for the cockpit itself:

- one Pi process hosts multiple live native sessions;
- exactly one session owns the terminal;
- inactive agent runtimes may continue in the background;
- switching restores the full native Pi UI and slash commands;
- `/sessions` and `Ctrl-R` open the switcher;
- sessions share an in-process path lock manager;
- saved sessions can be resumed as live children.

The path lock manager is not a global writer lease. It guards inferred paths
around individual tool calls, can allow simultaneous writes to different files,
and cannot reliably classify arbitrary shell mutation. The POC must add its own
global writer lease before it can satisfy the protocol.

The implementation also reaches into Pi internals, including `InteractiveMode`
and runtime/service classes, rather than using only documented extension APIs.
Any fork must pin a compatible Pi version range and run upgrade tests against
each supported Pi release.

Missing:

- fixed model-seat identities;
- coordinator/worker authority;
- addressed messages;
- phase and role state;
- context reset on role rotation;
- human approval gates.

This is the recommended fork base because its core abstraction is already
"several live sessions, one cockpit owner."

### 2. pi-interactive-subagents

Repository: <https://github.com/HazAT/pi-interactive-subagents>

Strongest match for interactive worker behavior:

- named subagents run in real multiplexer panes;
- a human may type into a worker and take it over;
- model, working directory, context mode, skills, and system prompt are
  configurable per spawn;
- interactive workers can remain quiet while the human thinks and types;
- child results flow back to the parent.

The README states that human input permanently disables auto-exit. Research of
the current implementation found code and tests that appear to ignore takeover
when deciding whether to auto-exit after a normal response. Treat this as an
open verification item, not a guaranteed feature.

Missing or mismatched:

- the cockpit is the external multiplexer rather than one Pi TUI;
- no rotating TDD protocol;
- no fixed seat abstraction above spawned roles;
- no hard coordinator-only room policy.

This is the best package to trial if multiplexer panes are acceptable after
all, and the best source of worker-lifecycle ideas for a custom extension.

### 3. pi-subagentura

Repository: <https://github.com/lmn451/pi-subagentura>

Notable capabilities:

- observable, attachable Pi child processes in tmux or Zellij;
- explicit model, cwd, persona, and context inheritance;
- true follow-up turns preserving child model context;
- durable status and immutable turn artifacts;
- child registry rehydration after reload or same-session restart;
- reusable parallel and phased workflows.

It is closer to a durable worker runtime than a minimal cockpit. It is a useful
fallback if persistence and recovery prove harder than session switching.

### 4. pi-interactive-shell

Repository: <https://github.com/nicobailon/pi-interactive-shell>

This extension offers full PTY interaction, live observation, immediate human
takeover, and hand-back to an agent. It works for arbitrary interactive CLIs,
not only Pi workers.

It is an excellent reference for takeover semantics but not a team/session
orchestrator by itself.

### 5. PI-agentteam

Repository: <https://github.com/LinYS77/PI-agentteam>

This package launches real Pi workers in tmux panes and uses durable, typed
mailboxes with leader-controlled task completion. Humans can focus a pane and
talk directly to a worker.

Its limitations for this project are fixed roles, tmux as the cockpit, no
managed context-reset policy, no built-in role rotation, and no one-key native
Pi session toggle.

### 6. pi-agent-teams

Repository: <https://github.com/tmustier/pi-agent-teams>

This is a capable shared-board and messaging system, but normal teammates are
headless RPC workers. Its transcript panel is not a native worker session.
This is closer to Ensemble than to the desired cockpit.

## Supporting Components

### tdd-enforcer

Repository: <https://github.com/Cyclone1070/tdd-enforcer>

Useful enforcement ideas:

- RED locks implementation files and permits tests;
- GREEN locks tests and permits implementation;
- REFACTOR permits both;
- transitions execute configured test commands;
- invalid transitions are rejected;
- snapshots permit phase rollback.

The POC should borrow the state-machine and tool-gating ideas without adopting
its private nested Git repository until rollback semantics are actually needed.

### pi-intercom

Repository: <https://github.com/nicobailon/pi-intercom>

Useful messaging ideas:

- addressed local-session messaging;
- ask/reply correlation;
- presence and activity state;
- delivery policy controlling whether incoming messages trigger turns;
- cancellation and duplicate suppression.

The POC needs a smaller broker: worker-to-coordinator and
coordinator-to-worker only.

## External Cockpits

Several terminal-first tools provide excellent session entry and switching,
including:

- [Agent Deck](https://github.com/asheshgoplani/agent-deck)
- [Agent of Empires](https://github.com/agent-of-empires/agent-of-empires)
- [workmux](https://github.com/raine/workmux)
- [NTM](https://github.com/Dicklesworthstone/ntm)

They are valuable comparisons, but they manage independent terminal agents
rather than implementing the rotating seat and TDD protocol. They also return
to an external tmux-like cockpit, which the existing `tmux-orchestrator`
already covers.

## Conclusion

No existing package was verified to provide the full requested workflow.
Before writing code, trial `pi-parallel-sessions` and
`pi-interactive-subagents` independently. If neither is sufficient, extend
`pi-parallel-sessions`; do not start a new CLI runtime.
