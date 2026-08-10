# Pi Plugin Trial Notes

Trial date: 2026-08-04. Pi version: `0.83.0` from
`@earendil-works/pi-coding-agent`.

## Outcome

`pi-parallel-sessions` proved the important runtime concept: one process can
host several real Pi sessions, keep inactive agents running, and transfer the
native terminal UI between them. This removes the two hackiest parts of the
existing tmux transport: `send-keys` delivery and `capture-pane` observation.

Do not adopt the package unchanged. Keep its MIT-licensed source as an
implementation reference and build a narrower extension for this cockpit.

The reference clone is currently at the gitignored path:

```text
tmp/pi-sessions/
```

It may not exist in a fresh clone and must never be treated as project source.
The upstream repository is <https://github.com/liushihao456/pi-sessions>.

## What Was Verified

- A child is a real `AgentSessionRuntime` plus native `InteractiveMode`.
- Parent and children may use the same existing cwd. The package's folder
  picker is package UX, not a Pi requirement. Its current flow requires
  `Ctrl-O` and selecting `.` for this case.
- Switching suspends only the old TUI; the inactive agent runtime remains live.
- Each live session has its own resource loader, extension runner, shortcut
  registry, model, tools, and transcript.
- Consequently, built-in `/reload` affects only the selected live session.
- Session creation, switching, and observation can use structured Pi APIs
  rather than terminal scraping.

## UX Decisions

- `Alt-S` opens the session selector.
- Do not use the upstream package's global `Ctrl-R` binding. Pi reports a
  collision with `app.session.rename`; that built-in action is actually scoped
  to the built-in resume selector, but the diagnostic is distracting.
- Do not design a tmux-style prefix or direct numbered shortcuts yet.
- The initial cockpit creates children in the coordinator's cwd without a
  folder prompt. Cross-project sessions are out of scope.

## Useful Implementation Ideas

Retain or adapt these ideas from `pi-sessions`:

- a registry mapping stable child IDs to `AgentSessionRuntime` and
  `InteractiveMode` instances;
- an adapter that stops/starts only the selected TUI;
- serialized activation so overlapping switches cannot corrupt terminal
  ownership;
- child construction through `createAgentSessionRuntime()`,
  `createAgentSessionServices()`, and `createAgentSessionFromServices()`;
- session identity routing by session ID/file, including after `/new` or
  `/resume` replaces a child's current session;
- a compact activity widget and selector;
- terminal keyboard-mode reset during handoff.

Structured orchestration can map the existing tmux operations as follows:

| Existing transport | Native Pi equivalent |
|---|---|
| list windows | explicit live-session registry |
| `send-keys` | `AgentSession.prompt()`, `steer()`, or `followUp()` |
| wait for repaint stability | `agent_settled` or `agent.waitForIdle()` |
| `capture-pane` | structured session messages/entries |
| focus tmux window | activate the child's native `InteractiveMode` |

## Do Not Copy Unchanged

- `// @ts-nocheck` and pervasive `any`;
- the folder explorer and cross-project session flow;
- the resume picker in the first implementation slice;
- regex-based mutating-shell detection and per-tool path locks;
- the internal `dist/core/model-resolver.js` import;
- process-global class instances that survive `/reload` with stale prototypes;
- the `InteractiveMode.prototype` spinner monkey patch;
- the lack of tests and a supported host/control API.

If shared process state is required, store reload-safe plain data under a
versioned `Symbol.for(...)`; do not preserve an instance of a module-local
class across reloads.

## Reload and Restart Boundary

A future cockpit-wide reload command may reload every idle child and then the
coordinator. Pi exposes `AgentSession.reload()`, while `InteractiveMode` adds UI
and keybinding refresh behavior.

A complete process restart is still required after changes to terminal handoff,
global shared-state shape, or internal `InteractiveMode` integration. Live
children do not survive restart, although their session JSONL files do.

## Cwd Boundary

Cwd is bound into the session manager, tools, settings, trust, resources, and
extensions. There is no supported in-place `setCwd()`.
`AgentSessionRuntime.switchSession(path, { cwdOverride })` can rebuild a runtime
against another cwd while retaining a transcript, but the original session
header keeps its old cwd. This is not needed for the initial same-checkout
cockpit.

## Other Candidate

`pi-interactive-subagents` has genuine tmux integration, but it creates tmux
splits and substantially duplicates the existing `tmux-orchestrator` transport.
It is not the foundation for this implementation.
