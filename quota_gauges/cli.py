from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from quota_gauges.gauge import (
    Gauge,
    bar,
    elapsed,
    margin,
    projected,
    runs_out_at,
    tone,
)
from quota_gauges.install import DEST, install
from quota_gauges.sources import claude_gauges, hyper_gauge, mistral_gauge, read_mistral
from token_recap.anthropic_usage import best_claude_limits
from token_recap.format import fmt_span
from token_recap.hyper_usage import fetch_hyper_balance, hyper_key
from token_recap.windows import aware_local, local_tz

CLAUDE_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
BAR_WIDTH = 20
_COLOURS = {"ok": "32", "warn": "33", "over": "31", "stale": "2", "unknown": "2"}


def collect(now: datetime) -> list[Gauge]:
    """Every gauge, each source failing on its own without sinking the rest."""
    gauges = claude_gauges(best_claude_limits(CLAUDE_CREDENTIALS, now), now, problem="no reading: no session open and usage endpoint unreachable")
    key = hyper_key(os.environ)
    if key is None:
        gauges.append(hyper_gauge(None, now, problem="no Hyper API key"))
    else:
        balance = fetch_hyper_balance(key)
        gauges.append(hyper_gauge(balance, now, problem="credits endpoint unreachable"))
    gauges.append(mistral_gauge(read_mistral(), now))
    return gauges


def render_line(gauge: Gauge, now: datetime) -> str:
    head = f"{gauge.label:<14}"
    if gauge.used is None:
        return f"{head}{bar(0.0, None, BAR_WIDTH)}   --  {gauge.problem}"
    pace = elapsed(gauge, now) if gauge.paced else None
    parts = [f"{head}{bar(gauge.used, pace, BAR_WIDTH)} {gauge.used * 100:>4.0f}%"]
    room = margin(gauge, now) if gauge.paced else None
    if pace is not None and room is not None:
        parts.append(f"pace {pace * 100:.0f}%")
        parts.append(f"{room * 100:+.0f} room" if room >= 0 else f"{room * 100:+.0f} over")
        empty = runs_out_at(gauge, now)
        if empty is not None:
            parts.append(f"out {empty.astimezone(now.tzinfo):%a %H:%M}")
        else:
            landing = projected(gauge, now)
            if landing is not None:
                parts.append(f"→ {landing * 100:.0f}% at reset")
    if gauge.detail:
        parts.append(gauge.detail)
    if gauge.end is not None and gauge.end > now:
        parts.append(f"resets in {fmt_span(gauge.end - now)}")
    if tone(gauge, now) == "stale" and gauge.as_of is not None:
        parts.append(f"as of {gauge.as_of.astimezone(now.tzinfo):%d %b %H:%M}")
    return "  ".join(parts)


def render(gauges: list[Gauge], now: datetime, color: bool) -> str:
    lines: list[str] = []
    for gauge in gauges:
        line = render_line(gauge, now)
        if color:
            line = f"\033[{_COLOURS[tone(gauge, now)]}m{line}\033[0m"
        lines.append(line)
    return "\n".join(lines) + "\n"


def as_json(gauges: list[Gauge], now: datetime) -> list[dict[str, Any]]:
    def stamp(moment: datetime | None) -> str | None:
        return moment.isoformat() if moment else None

    return [
        {
            "key": g.key,
            "label": g.label,
            "used": g.used,
            "elapsed": elapsed(g, now) if g.paced else None,
            "margin": margin(g, now) if g.paced else None,
            "projected": projected(g, now) if g.paced else None,
            "runs_out_at": stamp(runs_out_at(g, now)),
            "resets_at": stamp(g.end),
            "as_of": stamp(g.as_of),
            "tone": tone(g, now),
            "detail": g.detail,
            "problem": g.problem,
        }
        for g in gauges
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Subscription meters against an even pace: Claude, Hyper, Mistral."
    )
    p.add_argument(
        "command",
        nargs="?",
        choices=["install"],
        help=(
            f"install: copy the Mistral extension and its native host to {DEST} "
            "and point Chrome at them. Rerun after reading any change to them."
        ),
    )
    p.add_argument("--json", action="store_true", help="Machine-readable output.")
    p.add_argument("--no-color", action="store_true")
    args = p.parse_args(argv)
    if args.command == "install":
        for line in install(Path(__file__).resolve().parent.parent):
            print(line)
        print(f"Chrome: load the unpacked extension from {DEST / 'chrome'}, or reload it.")
        return 0
    now = aware_local(None, local_tz())
    gauges = collect(now)
    if args.json:
        print(json.dumps(as_json(gauges, now), indent=1))
    else:
        print(render(gauges, now, color=not args.no_color), end="")
    return 0
