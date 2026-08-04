---
name: worker-orchestrator
description: Use when the user wants to coordinate several CLI agents running in tmux windows — dispatch a prompt to one agent or broadcast to all, re-send after a change (e.g. re-review a new commit), read or compare their output, or run a multi-agent review. You are the coordinator; the human launches the agents.
---

# Worker Orchestrator

You coordinate other CLI agents that the human has launched — one per tmux
window — using the local `tmux-orchestrator` command. The human picks each
agent's model and reasoning effort by launching it themselves. Your job is to
**dispatch** work to those windows and **read their panes** to judge the
results. There is no protocol and no self-reporting: you see exactly what a
human watching the windows would see, and you interpret it.

Tmux is just the transport; the human-facing capability is coordinating a team
of agents. Prefer the words **mission** or **work item** in prose — `task` is
overloaded across AI CLIs.

## The loop

1. `tmux-orchestrator list` — see the session and which windows exist (address
   them by index; the human does not name windows).
2. Dispatch: `send` to one window, or `broadcast` to all.
3. `wait` for a window to settle, or `read` it when you expect it is done.
4. Read the pane, interpret the result yourself, and summarize back to the human.
5. Re-`broadcast` when something changes (e.g. a new commit to re-review).

## Commands

```bash
tmux-orchestrator list                              # session + windows + detected type
tmux-orchestrator send 2 "run the tests and report failures"
git log -1 -p | tmux-orchestrator send 3 -          # pipe a diff straight in (- = stdin)
tmux-orchestrator broadcast "a new commit landed — re-review HEAD"
tmux-orchestrator read 2                             # recent output (-n N, or --all)
tmux-orchestrator wait 2 --settle 6                  # block until the pane goes quiet
tmux-orchestrator rename 2 backend                   # optional: label a window by role
```

**Prefer stdin for anything long.** A multi-KB prompt typed as a shell argument
is awkward to quote and easy to mangle; write it to a file and pipe it in with
`send <target> -`.

**For a really long or important mission, send a pointer instead of the text.**
Write the prompt to a file and dispatch one line:

```bash
tmux-orchestrator send 2 "Read /tmp/mission-backend.md and follow it exactly"
```

Pasting depends on the target TUI's paste handling; a one-line pointer has none
of those failure modes, and the agent can re-read the file if it loses context.

**Targets** are a window index (`2`) — the human never names windows, so the
index is the default way to address them — or a name / unique substring once a
window has been renamed. Run `list` first to see the indices.

## Operating rules

- You are one of the windows. `broadcast` skips your own window by default —
  never dispatch a prompt into your own pane.
- **Read before you conclude.** A window going quiet is not the same as success.
  Capture the pane and check what it actually says before reporting to the human.
- **A dispatch is not a delivery.** `send` and `broadcast` submit the prompt and
  then confirm the pane reacted. Read the exit code:

  | exit | meaning | what to do |
  |------|---------|------------|
  | `0`  | the pane was still, then visibly reacted to Enter | proceed |
  | `1`  | `WARNING: ... did not react to Enter` — the pane was still and ignored it | the prompt is sitting in that composer, unsent; submit by hand with `tmux send-keys -t <session>:<idx> Enter`, then `read` |
  | `3`  | `NOTE: ... was still redrawing` — the submit could **not** be confirmed | usually the agent was already busy; `read` the pane and see whether the prompt actually arrived before relying on it |

  Never ignore `1` or `3` — an unsent prompt looks exactly like an agent that is
  thinking hard. Exit `3` is not a failure and not a success: it means the tool
  could not tell, because a pane that repaints on its own gives no evidence that
  Enter did anything.
- **Success means the pane reacted, not that the agent understood.** Even on
  exit `0`, `read` the pane before concluding a mission landed.
- Codex windows are handled automatically (Codex needs a special typing
  sequence); you do not do anything different for them.
- Keep the human in the loop and summarize each agent's result clearly. When
  comparing agents, say where they agree and where they diverge.
- Ask the human before broad, risky, destructive, or costly dispatches — for
  example broadcasting a "make changes" instruction to every agent at once.
- Agents do not talk to each other; route everything through yourself.

## Coordinating longer work

For a multi-agent effort — say frontend on one window, backend on another, a
reviewer on a third — it helps to `rename` each window by its role first
(`rename 2 backend`), so your later `send`/`read` calls and the human's status
bar read as roles instead of `node`/`codex`. The human never has to name a
window — you do it for them, or you just use indices. Then give each window its
own focused mission with `send`, let them work, and periodically `read`/`wait`
to track progress. Hand the reviewer the others' output once it is ready.
Nothing is stateful: re-`list` any time to re-discover what is running.
