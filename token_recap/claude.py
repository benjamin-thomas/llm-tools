from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from token_recap.buckets import TokenBuckets
from token_recap.native import claude_native_usd
from token_recap.parse import as_int, in_window, parse_iso


def collect_claude(root: Path, start: datetime, end: datetime) -> TokenBuckets:
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
        _read_claude_jsonl(path, start, end, buckets)
    return buckets


def _read_claude_jsonl(
    path: Path, start: datetime, end: datetime, buckets: TokenBuckets
) -> None:
    seen: set[str] = set()
    try:
        fh = path.open()
    except OSError:
        return
    with fh:
        for line in fh:
            if '"assistant"' not in line or '"usage"' not in line:
                continue
            try:
                obj: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "assistant":
                continue
            ts = parse_iso(str(obj.get("timestamp") or ""))
            if ts is None or not in_window(ts, start, end):
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            key = str(msg.get("id") or obj.get("requestId") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            creation = usage.get("cache_creation")
            write_5m = 0
            write_1h = 0
            if isinstance(creation, dict):
                write_5m = as_int(creation.get("ephemeral_5m_input_tokens"))
                write_1h = as_int(creation.get("ephemeral_1h_input_tokens"))
            write = write_5m + write_1h
            if write == 0:
                write = as_int(usage.get("cache_creation_input_tokens"))
                write_5m = write
            details = usage.get("output_tokens_details")
            thinking = 0
            if isinstance(details, dict):
                thinking = as_int(details.get("thinking_tokens"))
            uncached = as_int(usage.get("input_tokens"))
            cache_read = as_int(usage.get("cache_read_input_tokens"))
            output = as_int(usage.get("output_tokens"))
            model = str(msg.get("model") or "unknown")
            buckets.add(
                uncached=uncached,
                cache_write=write,
                cache_read=cache_read,
                output=output,
                reasoning=thinking,
                native_usd=claude_native_usd(
                    model, uncached, write_5m, write_1h, cache_read, output
                ),
                model=model,
            )
