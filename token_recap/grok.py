from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from token_recap.buckets import TokenBuckets
from token_recap.native import grok_native_usd
from token_recap.parse import as_int, in_window, parse_iso


def collect_grok(log: Path, start: datetime, end: datetime) -> TokenBuckets:
    buckets = TokenBuckets()
    if not log.is_file():
        return buckets
    try:
        fh = log.open()
    except OSError:
        return buckets
    with fh:
        for line in fh:
            if "shell.turn.inference_done" not in line:
                continue
            try:
                obj: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("msg") != "shell.turn.inference_done":
                continue
            ts = parse_iso(str(obj.get("ts") or ""))
            if ts is None or not in_window(ts, start, end):
                continue
            ctx = obj.get("ctx")
            if not isinstance(ctx, dict):
                continue
            prompt = as_int(ctx.get("prompt_tokens"))
            cached = as_int(ctx.get("cached_prompt_tokens"))
            uncached = prompt - cached if prompt >= cached else 0
            output = as_int(ctx.get("completion_tokens"))
            buckets.add(
                uncached=uncached,
                cache_read=cached,
                output=output,
                reasoning=as_int(ctx.get("reasoning_tokens")),
                native_usd=grok_native_usd(prompt, cached, output),
                model="grok-4.6",
            )
    return buckets
