from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from token_recap.buckets import TokenBuckets
from token_recap.native import grok_native_usd
from token_recap.parse import as_int, as_str, in_window, parse_iso


def collect_grok(log: Path, start: datetime, end: datetime) -> TokenBuckets:
    buckets = TokenBuckets()
    if not log.is_file():
        return buckets
    try:
        fh = log.open()
    except OSError:
        return buckets
    # inference_done carries no model, so track the running value per session
    # from the "model changed" events interleaved in the unified log.
    models: dict[str, str] = {}
    with fh:
        for line in fh:
            if "shell.turn.inference_done" not in line and "model changed" not in line:
                continue
            try:
                obj: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = as_str(obj.get("sid"))
            if obj.get("msg") == "model changed":
                ctx = obj.get("ctx")
                if isinstance(ctx, dict):
                    model = as_str(ctx.get("model"))
                    if model:
                        models[sid] = model
                continue
            if obj.get("msg") != "shell.turn.inference_done":
                continue
            ts = parse_iso(as_str(obj.get("ts")))
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
                model=models.get(sid) or "grok-4.6",
            )
    return buckets
