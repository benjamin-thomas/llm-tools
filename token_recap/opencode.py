"""opencode sessions: a SQLite database, not a log directory.

Rows live in `message`, one JSON blob per message, keyed by a primary key —
so unlike the Claude and Codex streams there is no duplicate to filter. Like
pi, opencode normalises every provider into one usage shape, so `input` is
already uncached-only whichever provider served the call.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from token_recap.buckets import TokenBuckets
from token_recap.harness import (
    OPENCODE_SECTIONS,
    harness_label,
    harness_usd,
    section_for,
)

Span = tuple[datetime, datetime]


def collect_opencode(db: Path, windows: dict[str, Span]) -> dict[str, TokenBuckets]:
    out: dict[str, TokenBuckets] = {}
    if not db.is_file() or not windows:
        return out
    lo = min(start for start, _ in windows.values()).timestamp() * 1000
    hi = max(end for _, end in windows.values()).timestamp() * 1000
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        rows = con.execute(
            "SELECT time_created, data FROM message"
            " WHERE time_created >= ? AND time_created < ? AND data LIKE '%\"tokens\"%'",
            (lo, hi),
        )
        for created, data in rows:
            _add_row(created, data, windows, out)
    except sqlite3.Error:
        return out
    finally:
        con.close()
    return out


def _add_row(
    created: Any, data: Any, windows: dict[str, Span], out: dict[str, TokenBuckets]
) -> None:
    if not isinstance(data, str) or not isinstance(created, (int, float)):
        return
    try:
        obj: Any = json.loads(data)
    except json.JSONDecodeError:
        return
    if obj.get("role") != "assistant":
        return
    tokens = obj.get("tokens")
    if not isinstance(tokens, dict):
        return
    cost = obj.get("cost")
    cost = float(cost) if isinstance(cost, (int, float)) else 0.0
    provider = obj.get("providerID")
    # Not from `cost`: opencode's OpenAI plugin zeroes catalog prices under
    # OAuth but its xAI plugin does not, so a priced xAI row is still a
    # SuperGrok subscription call.
    cache = tokens.get("cache")
    cache = cache if isinstance(cache, dict) else {}
    uncached = _int(tokens.get("input"))
    cache_read = _int(cache.get("read"))
    cache_write = _int(cache.get("write"))
    output = _int(tokens.get("output"))
    # Current opencode stores output net of reasoning; versions up to 1.3.13
    # stored it inclusive. `total` says which, so reasoning is added back only
    # for the versions that left it out.
    reasoning = _int(tokens.get("reasoning"))
    total = _int(tokens.get("total"))
    if total and total == uncached + cache_read + cache_write + output + reasoning:
        output += reasoning
    model = obj.get("modelID")
    model = model if isinstance(model, str) and model else "unknown"
    usd = harness_usd(model, uncached, cache_write, cache_read, output, cost)
    who = provider if isinstance(provider, str) else ""
    section = section_for(who, OPENCODE_SECTIONS)
    span = windows.get(section)
    if span is None:
        return
    start, end = span
    stamp = created / 1000.0
    if not (start.timestamp() <= stamp < end.timestamp()):
        return
    out.setdefault(section, TokenBuckets()).add(
        uncached=uncached,
        cache_write=cache_write,
        cache_read=cache_read,
        output=output,
        reasoning=reasoning,
        native_usd=usd,
        model=harness_label(who, model, "opencode", section),
        model_id=model,
        provider=who,
    )


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0
