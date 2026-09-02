"""pi sessions: ~/.pi/agent/sessions/<cwd-slug>/<stamp>_<uuid>.jsonl.

pi normalises every provider into one usage shape before storing it, so unlike
the Claude and Codex streams there is nothing to reconstruct: `input` is
already uncached-only, `output` already contains its reasoning, and the model
and provider are on the record itself rather than on an earlier event.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from token_recap.buckets import TokenBuckets
from token_recap.harness import (
    PI_SECTIONS,
    harness_label,
    harness_usd,
    section_for,
)
from token_recap.parse import as_int, as_str, in_window, parse_iso

Span = tuple[datetime, datetime]
# `/fork` copies history into a new file verbatim, so a request can appear in
# two of them. The entry id survives the copy; provider and model guard against
# an unrelated collision of pi's short random ids.
Key = tuple[str, int, str, str]


def collect_pi(root: Path, windows: dict[str, Span]) -> dict[str, TokenBuckets]:
    """Bucket pi's calls by the section whose quota paid for each one."""
    out: dict[str, TokenBuckets] = {}
    if not root.is_dir() or not windows:
        return out
    earliest = min(start for start, _ in windows.values()).timestamp()
    seen: set[Key] = set()
    # Sorted, so that when a forked copy duplicates a request the same one of
    # the pair always wins and the total does not wander between runs.
    for path in sorted(root.rglob("*.jsonl")):
        try:
            if path.stat().st_mtime < earliest - 120:
                continue
        except OSError:
            continue
        _read_pi_jsonl(path, windows, seen, out)
    return out


def _read_pi_jsonl(
    path: Path,
    windows: dict[str, Span],
    seen: set[Key],
    out: dict[str, TokenBuckets],
) -> None:
    try:
        fh = path.open()
    except OSError:
        return
    with fh:
        for line in fh:
            if '"usage"' not in line or '"assistant"' not in line:
                continue
            try:
                obj: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Top-level message entries only: a tps-sample copies usage fields
            # for throughput measurement and is not a request.
            if obj.get("type") != "message":
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            provider = as_str(msg.get("provider"))
            model = as_str(msg.get("model")) or "unknown"
            key: Key = (
                as_str(obj.get("id")),
                as_int(msg.get("timestamp")),
                provider,
                model,
            )
            if key in seen:
                continue
            seen.add(key)
            uncached = as_int(usage.get("input"))
            cache_write = as_int(usage.get("cacheWrite"))
            cache_read = as_int(usage.get("cacheRead"))
            output = as_int(usage.get("output"))
            usd = harness_usd(
                model,
                uncached,
                cache_write,
                cache_read,
                output,
                _logged_cost(usage),
                write_1h=as_int(usage.get("cacheWrite1h")),
            )
            section = section_for(provider, PI_SECTIONS)
            span = windows.get(section)
            if span is None:
                continue
            # The outer stamp is when the call landed; the inner one is when it
            # started, and only the outer survives a resume in the right order.
            ts = parse_iso(as_str(obj.get("timestamp")))
            if ts is None or not in_window(ts, *span):
                continue
            out.setdefault(section, TokenBuckets()).add(
                uncached=uncached,
                cache_write=cache_write,
                cache_read=cache_read,
                output=output,
                reasoning=as_int(usage.get("reasoning")),
                native_usd=usd,
                model=harness_label(provider, model, "pi", section),
                model_id=model,
                provider=provider,
            )


def _logged_cost(usage: dict[str, Any]) -> float:
    """pi's own list-rate total, used only for models we hold no card for."""
    cost = usage.get("cost")
    if not isinstance(cost, dict):
        return 0.0
    total = cost.get("total")
    return float(total) if isinstance(total, (int, float)) else 0.0
