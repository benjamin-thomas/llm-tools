from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from token_recap.buckets import Rates, TokenBuckets, load_deepseek_rates
from token_recap.claude import collect_claude
from token_recap.codex import collect_codex
from token_recap.format import ProviderView, render_report
from token_recap.native import RUNINFRA_PRO
from token_recap.grok import collect_grok
from token_recap.windows import (
    RESETS,
    WeeklyReset,
    aware_local,
    last_weekly_reset,
    local_tz,
    next_weekly_reset,
)


def parse_when(text: str, now: datetime) -> datetime:
    raw = datetime.fromisoformat(text)
    if raw.tzinfo is None:
        return raw.replace(tzinfo=now.tzinfo)
    return raw.astimezone(now.tzinfo)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Recap Claude Code / Grok / Codex token usage from local logs, "
            "and price the same buckets as DeepSeek V4 Flash."
        )
    )
    p.add_argument(
        "--since",
        help="Inclusive start (ISO). Default: each provider's last weekly reset.",
    )
    p.add_argument(
        "--until",
        help="Exclusive end (ISO). Default: now.",
    )
    p.add_argument(
        "--tz",
        help="IANA timezone for reset clock and naive --since/--until (default: local).",
    )
    p.add_argument(
        "--claude-root",
        type=Path,
        default=Path.home() / ".claude" / "projects",
    )
    p.add_argument(
        "--grok-log",
        type=Path,
        default=Path.home() / ".grok" / "logs" / "unified.jsonl",
    )
    p.add_argument(
        "--codex-root",
        type=Path,
        default=Path.home() / ".codex" / "sessions",
    )
    p.add_argument(
        "--models-json",
        type=Path,
        default=Path.home() / ".pi" / "agent" / "models.json",
    )
    p.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colours (colour is on by default, including pipes).",
    )
    p.add_argument(
        "--token-mult",
        type=float,
        default=2.0,
        metavar="N",
        help=(
            "Assume Flash needs N× as many tokens as this log for the same "
            "task (default: 2). Scales every bucket, including cache."
        ),
    )
    return p


def window_for(
    name: str,
    now: datetime,
    since: datetime | None,
    until: datetime | None,
) -> tuple[WeeklyReset, datetime, datetime]:
    spec = RESETS[name]
    start = since if since is not None else last_weekly_reset(now, spec)
    end = until if until is not None else (
        now if since is not None else next_weekly_reset(now, spec)
    )
    return spec, start, end


def recap_text(
    now: datetime,
    rates: Rates,
    since: datetime | None,
    until: datetime | None,
    claude_root: Path,
    grok_log: Path,
    codex_root: Path,
    color: bool | None = None,
    token_mult: float = 2.0,
) -> str:
    combined = TokenBuckets()
    custom = since is not None or until is not None
    labels = {
        "claude": "API list (Anthropic)",
        "grok": "API list (xAI)",
        "codex": "API list (OpenAI Codex)",
    }
    views: list[ProviderView] = []
    for name, title, collect in (
        ("claude", "Claude Code", lambda s, e: collect_claude(claude_root, s, e)),
        ("grok", "Grok", lambda s, e: collect_grok(grok_log, s, e)),
        ("codex", "Codex", lambda s, e: collect_codex(codex_root, s, e)),
    ):
        spec, start, end = window_for(name, now, since, until)
        display_end = end if custom else next_weekly_reset(now, spec)
        buckets = collect(start, end)
        combined.merge(buckets)
        views.append(
            ProviderView(
                key=name,
                title=title,
                spec=None if custom else spec,
                start=start,
                end=display_end,
                buckets=buckets,
                native_label=labels[name],
            )
        )
    return render_report(
        now,
        rates,
        RUNINFRA_PRO,
        token_mult,
        custom,
        views,
        combined,
        color=color,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tz = ZoneInfo(args.tz) if args.tz else local_tz()
    now = aware_local(None, tz)
    since = parse_when(args.since, now) if args.since else None
    until = parse_when(args.until, now) if args.until else None
    if until is None and since is not None:
        until = now
    rates = load_deepseek_rates(args.models_json)
    print(
        recap_text(
            now,
            rates,
            since,
            until,
            args.claude_root,
            args.grok_log,
            args.codex_root,
            color=False if args.no_color else True,
            token_mult=args.token_mult,
        ),
        end="",
    )
    return 0
