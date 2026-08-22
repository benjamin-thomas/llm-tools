from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from token_recap.buckets import TokenBuckets
from token_recap.native import codex_native_usd
from token_recap.parse import as_int, in_window, parse_iso


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
    try:
        fh = path.open()
    except OSError:
        return
    with fh:
        for line in fh:
            if "token_count" not in line:
                continue
            try:
                obj: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = obj.get("payload")
            if obj.get("type") != "event_msg" or not isinstance(payload, dict):
                continue
            if payload.get("type") != "token_count":
                continue
            ts = parse_iso(str(obj.get("timestamp") or ""))
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
            cache_write = as_int(last.get("cache_write_input_tokens"))
            output = as_int(last.get("output_tokens"))
            buckets.add(
                uncached=uncached,
                cache_write=cache_write,
                cache_read=cached,
                output=output,
                reasoning=as_int(last.get("reasoning_output_tokens")),
                native_usd=codex_native_usd(uncached, cache_write, cached, output),
                model="codex",
            )
