# pi-tps

Pi extension that records **end-to-end** token throughput (output tokens / generation wall time, TTFT included) and summarises it per `provider/model`.

The footer shows **token-weighted averages** for the last 1 minute, 10 minutes, 1 hour, and 3 hours (`1m 70 · 10m 62 · 1h 58 · 3h 55 tok/s`). Sitting idle is not sampled, and a short reply cannot dominate a window that already has a long one.

`/tps` defaults to 10m; `/tps 1m`, `/tps 1h`, and `/tps 3h` print the full table.

## Install

```sh
pi install /home/benjamin/code/github.com/benjamin-thomas/llm-tools/pi-tps
```

Or add a packages entry next to `pi-orchestrator` in `~/.pi/agent/settings.json`.

## Use

- Footer after recent replies: `1m 70 · 10m 62 · 1h 58 · 3h 55 tok/s`
- `/tps` — table (`n`, token-weighted `avg`, `p50`, `p90`) for 10m
- `/tps 1m` / `/tps 1h` / `/tps 3h` — same, other windows

Samples are stored as custom entries **in the current session** (each orchestrator worker is its own session). Nothing is shared globally.

Failed, aborted, empty, and >10-minute generations are dropped.
