from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from token_recap.buckets import TokenBuckets
from token_recap.native import CODEX_FALLBACK, codex_native_usd
from token_recap.parse import as_int, as_str, in_window, parse_iso
from token_recap.windows import Window

# A rate_limits bucket at least this wide is the weekly one, not the 5h one.
_WEEKLY_MINUTES = 1440


@dataclass(frozen=True)
class CodexLimits:
    """What Codex says about its own quota, all from one rate_limits event."""

    window: Window
    used: float  # 0..1 of the weekly allowance
    as_of: datetime
    short: tuple[float, datetime] | None  # the 5h bucket: used, its reset


def codex_limits(root: Path, now: datetime) -> CodexLimits | None:
    """Codex's own weekly meter, or None when it has not logged a live one.

    Every token_count event carries a `rate_limits` block naming each live
    bucket's width, reset instant and percentage used, so both the window and
    the meter can be read off the log instead of guessed. The server re-anchors
    that window — a reset can move before the old one arrives — so the newest
    event wins outright and nothing is carried over from an older one.
    """
    newest: tuple[str, dict[str, Any]] | None = None
    files = sorted(root.rglob("*.jsonl"), key=_mtime, reverse=True)
    for path in files[:5]:
        found = _last_limits(path)
        if found is not None and (newest is None or found[0] > newest[0]):
            newest = found
    if newest is None:
        return None
    return _limits_from(newest[0], newest[1], now)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _last_limits(path: Path) -> tuple[str, dict[str, Any]] | None:
    """The newest rate_limits block in one file, with its event timestamp."""
    found: tuple[str, dict[str, Any]] | None = None
    try:
        fh = path.open()
    except OSError:
        return None
    with fh:
        for line in fh:
            if '"rate_limits"' not in line:
                continue
            try:
                obj: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            limits = payload.get("rate_limits")
            ts = as_str(obj.get("timestamp"))
            if not isinstance(limits, dict) or not ts:
                continue
            if found is None or ts > found[0]:
                found = (ts, limits)
    return found


def _limits_from(
    stamp: str, limits: dict[str, Any], now: datetime
) -> CodexLimits | None:
    as_of = parse_iso(stamp)
    if as_of is None:
        return None
    weekly = _bucket(limits, weekly=True)
    if weekly is None:
        return None
    minutes, resets_at, used = weekly
    end = datetime.fromtimestamp(resets_at, tz=timezone.utc).astimezone(now.tzinfo)
    if end <= now:
        # Past its reset: both the window and the percentage are stale, and the
        # clock in windows.py is the better guess for the window.
        return None
    window = Window(f"{end:%a %H:%M} logged", end - timedelta(minutes=minutes), end)
    short = _bucket(limits, weekly=False)
    short_pair: tuple[float, datetime] | None = None
    if short is not None:
        _, short_resets, short_used = short
        short_pair = (
            short_used / 100.0,
            datetime.fromtimestamp(short_resets, tz=timezone.utc).astimezone(now.tzinfo),
        )
    return CodexLimits(window, used / 100.0, as_of.astimezone(now.tzinfo), short_pair)


def _bucket(
    limits: dict[str, Any], weekly: bool
) -> tuple[int, int, float] | None:
    """The widest bucket on the wanted side of the weekly/short divide."""
    best: tuple[int, int, float] | None = None
    for bucket in limits.values():
        if not isinstance(bucket, dict):
            continue
        minutes = as_int(bucket.get("window_minutes"))
        resets_at = as_int(bucket.get("resets_at"))
        if minutes <= 0 or resets_at <= 0:
            continue
        if (minutes >= _WEEKLY_MINUTES) != weekly:
            continue
        used = bucket.get("used_percent")
        used = float(used) if isinstance(used, (int, float)) else 0.0
        if best is None or minutes > best[0]:
            best = (minutes, resets_at, used)
    return best


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
    previous_total: str | None = None
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
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            last = info.get("last_token_usage")
            if not isinstance(last, dict):
                continue
            # The same token_count is often notified more than once. The
            # cumulative total tells a repeat from a real turn: only a repeat
            # leaves it unchanged, since no real turn spends nothing. Without
            # that field there is nothing to compare, so nothing is dropped.
            total = info.get("total_token_usage")
            if isinstance(total, dict):
                fingerprint = json.dumps(total, sort_keys=True)
                if fingerprint == previous_total:
                    continue
                previous_total = fingerprint
            ts = parse_iso(as_str(obj.get("timestamp")))
            if ts is None or not in_window(ts, start, end):
                continue
            inp = as_int(last.get("input_tokens"))
            cached = as_int(last.get("cached_input_tokens"))
            written = as_int(last.get("cache_write_input_tokens"))
            # input_tokens is the whole prompt: cache reads and writes included.
            uncached = max(inp - cached - written, 0)
            rows.append(
                (
                    current,
                    uncached,
                    written,
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
            provider="openai",
        )
