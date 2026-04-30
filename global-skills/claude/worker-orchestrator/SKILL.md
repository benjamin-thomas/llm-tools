---
name: worker-orchestrator
description: Use when the user wants to coordinate worker agents, delegate work to visible AI CLI panes, ask other models for opinions, compare worker outputs, run a multi-worker review, or inspect worker/job state.
---

# Worker Orchestrator

You can coordinate visible AI CLI workers through the local `tmux-orchestrator` command. Tmux is the implementation detail; the human-facing capability is worker orchestration.

Use this capability when the human wants help from other models, comparison between agents, background review, implementation support outside your own session, or a status check on the worker pool. Keep the human in the loop and summarize worker results clearly.

## Operating Rules

- Workers are named `worker.*`.
- Workers do not communicate directly with each other.
- Use the CLI commands instead of writing protocol blocks by hand.
- Prefer the word `mission` or `work item` in human-facing prose; avoid using `task` as the central term because it is overloaded in AI CLIs.
- The router creates `job_id` and `route_token` values for `send` and `broadcast`.
- A worker is responsible for marking its own work item complete with `tmux-orchestrator complete`.
- Use `mark-done` only as a manual fallback when a worker visibly finished but did not call `complete`.
- Ask the human before broad, risky, destructive, or high-cost dispatches.

## Commands

```bash
tmux-orchestrator status
tmux-orchestrator send worker.claude "Review this diff and report risks."
tmux-orchestrator broadcast --to idle "Give an independent design critique."
tmux-orchestrator wait <job_id-or-parent_job_id> --watch
tmux-orchestrator fail <worker.name> <job_id>
tmux-orchestrator retry <job_id>
```

Check `.tmux-orchestrator/logs/events.log` when the human asks what happened or when state looks inconsistent.
