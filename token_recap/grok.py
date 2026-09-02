from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from token_recap.buckets import TokenBuckets
from token_recap.native import GROK_FALLBACK, grok_native_usd
from token_recap.parse import as_int, as_str, in_window, parse_iso
from token_recap.windows import Window

_BILLING = "billing: fetched credits config"


@dataclass(frozen=True)
class GrokLimits:
    """What the Grok CLI's own /usage panel reads, taken from the same log."""

    window: Window
    used: float  # 0..1 of the weekly pool
    as_of: datetime
    tier: str


def grok_limits(log: Path, now: datetime) -> GrokLimits | None:
    """The weekly pool reading the CLI records whenever it refreshes billing.

    xAI publishes no usage endpoint, but the CLI logs the answer it gets, so
    the meter is on disk after all — with the period bounds, which beats the
    observed reset clock the same way Codex's logged window does.
    """
    newest: tuple[str, dict[str, Any]] | None = None
    try:
        fh = log.open()
    except OSError:
        return None
    with fh:
        for line in fh:
            if _BILLING not in line:
                continue
            try:
                obj: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("msg") != _BILLING:
                continue
            ctx = obj.get("ctx")
            stamp = as_str(obj.get("ts"))
            if not isinstance(ctx, dict) or not stamp:
                continue
            if newest is None or stamp > newest[0]:
                newest = (stamp, ctx)
    return None if newest is None else _limits_from(newest[0], newest[1], now)


def _limits_from(stamp: str, ctx: dict[str, Any], now: datetime) -> GrokLimits | None:
    as_of = parse_iso(stamp)
    config = ctx.get("config")
    if as_of is None or not isinstance(config, dict):
        return None
    used = config.get("creditUsagePercent")
    period = config.get("currentPeriod")
    if not isinstance(used, (int, float)) or not isinstance(period, dict):
        return None
    start = parse_iso(as_str(period.get("start")))
    end = parse_iso(as_str(period.get("end")))
    if start is None or end is None:
        return None
    end = end.astimezone(now.tzinfo)
    if end <= now:
        return None  # a stale reading: the pool has refilled since
    window = Window(f"{end:%a %H:%M} logged", start.astimezone(now.tzinfo), end)
    tier = as_str(ctx.get("subscriptionTier")) or "Grok"
    return GrokLimits(window, float(used) / 100.0, as_of.astimezone(now.tzinfo), tier)




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
            model = models.get(sid) or GROK_FALLBACK
            buckets.add(
                uncached=uncached,
                cache_read=cached,
                output=output,
                # reasoning_tokens are already inside completion_tokens, so
                # they are tracked but never added to the billed output.
                reasoning=as_int(ctx.get("reasoning_tokens")),
                native_usd=grok_native_usd(model, prompt, cached, output),
                model=model,
                provider="xai",
            )
    return buckets
