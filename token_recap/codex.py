from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from token_recap.buckets import TokenBuckets
from token_recap.native import CODEX_FALLBACK, codex_native_usd
from token_recap.parse import as_int, as_str, in_window, parse_iso


def collect_codex(root: Path, start: datetime, end: datetime) -> TokenBuckets:
    buckets = TokenBuckets()
    if not root.is_dir():
        return buckets
    start_ts = start.timestamp()
    for path in root.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < start_ts - 120:
                continue
        except OSError:
            continue
        _read_codex_jsonl(path, start, end, buckets)
    return buckets


def _read_codex_jsonl(
    path: Path, start: datetime, end: datetime, buckets: TokenBuckets
) -> None:
    """Attribute each turn's usage to the model that ran it.

    A rollout names its model in the `turn_context` preceding each turn, so we
    track the running value. Turns logged before the first `turn_context` (old
    rollouts predating the field) are backfilled with the session's first known
    model, which is right unless the model was switched mid-session.
    """
    try:
        fh = path.open()
    except OSError:
        return
    current: str | None = None
    first: str | None = None
    rows: list[tuple[str | None, int, int, int, int, int]] = []
    with fh:
        for line in fh:
            if '"turn_context"' not in line and "token_count" not in line:
                continue
            try:
                obj: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            if obj.get("type") == "turn_context":
                model = as_str(payload.get("model"))
                if model:
                    current = model
                    first = first or model
                continue
            if obj.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            ts = parse_iso(as_str(obj.get("timestamp")))
            if ts is None or not in_window(ts, start, end):
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            last = info.get("last_token_usage")
            if not isinstance(last, dict):
                continue
            inp = as_int(last.get("input_tokens"))
            cached = as_int(last.get("cached_input_tokens"))
            uncached = inp - cached if inp >= cached else 0
            rows.append(
                (
                    current,
                    uncached,
                    as_int(last.get("cache_write_input_tokens")),
                    cached,
                    as_int(last.get("output_tokens")),
                    as_int(last.get("reasoning_output_tokens")),
                )
            )
    for model, uncached, cache_write, cached, output, reasoning in rows:
        name = model or first or CODEX_FALLBACK
        buckets.add(
            uncached=uncached,
            cache_write=cache_write,
            cache_read=cached,
            output=output,
            reasoning=reasoning,
            native_usd=codex_native_usd(name, uncached, cache_write, cached, output),
            model=name,
        )
