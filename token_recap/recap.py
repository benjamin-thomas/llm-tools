from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from token_recap.buckets import TokenBuckets
from token_recap.anthropic_usage import ClaudeLimits, claude_limits
from token_recap.claude import collect_claude
from token_recap.codex import CodexLimits, codex_limits, collect_codex
from token_recap.format import Meter, ProviderView, render_report
from token_recap.grok import GrokLimits, collect_grok, grok_limits
from token_recap.harness import (
    API_SECTION,
    FREE_SECTION,
    HYPER_SECTION,
    OLLAMA_SECTION,
    PROVIDER_NAMED,
    split_free,
)
from token_recap.opencode import collect_opencode
from token_recap.pi import collect_pi
from token_recap.xai_usage import XaiUsage, xai_usage
from token_recap.windows import (
    RESETS,
    Window,
    aware_local,
    last_weekly_reset,
    local_tz,
    next_weekly_reset,
    widen_weeks,
)


def parse_when(text: str, now: datetime) -> datetime:
    raw = datetime.fromisoformat(text)
    if raw.tzinfo is None:
        return raw.replace(tzinfo=now.tzinfo)
    return raw.astimezone(now.tzinfo)


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Recap Claude Code / Grok / Codex token usage from local logs, "
            "priced at each provider's own API list rate."
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
        "--weeks",
        type=positive_int,
        default=1,
        metavar="N",
        help=(
            "How many weekly windows to cover, ending at the current one "
            "(default: 1, the unfinished week). --weeks 4 is roughly a month. "
            "Ignored alongside --since/--until."
        ),
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
        "--claude-credentials",
        type=Path,
        default=Path.home() / ".claude" / ".credentials.json",
        help="OAuth token used to read Claude's own usage meter.",
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help="Skip the one network call that reads Claude's usage meter.",
    )
    p.add_argument(
        "--grok-log",
        type=Path,
        default=Path.home() / ".grok" / "logs" / "unified.jsonl",
    )
    p.add_argument(
        "--grok-auth",
        type=Path,
        default=Path.home() / ".grok" / "auth.json",
        help="OIDC token used to read Grok's own usage meter.",
    )
    p.add_argument(
        "--codex-root",
        type=Path,
        default=Path.home() / ".codex" / "sessions",
    )
    p.add_argument(
        "--pi-root",
        type=Path,
        default=Path.home() / ".pi" / "agent" / "sessions",
    )
    p.add_argument(
        "--opencode-db",
        type=Path,
        default=Path.home() / ".local" / "share" / "opencode" / "opencode.db",
    )
    p.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colours (colour is on by default, including pipes).",
    )
    return p


def window_for(
    name: str,
    now: datetime,
    since: datetime | None,
    until: datetime | None,
    logged: Window | None = None,
    weeks: int = 1,
) -> Window:
    """The span to report on for one provider.

    An explicit --since/--until wins. Otherwise use the window the provider
    itself published in its log, and fall back to the observed reset clock for
    the providers that publish nothing — then reach back over `weeks` of them.
    """
    spec = RESETS[name]
    if since is not None or until is not None:
        start = since if since is not None else last_weekly_reset(now, spec)
        end = until if until is not None else now
        return Window("custom window", start, end)
    current = logged or Window(
        f"{spec.label} local",
        last_weekly_reset(now, spec),
        next_weekly_reset(now, spec),
    )
    return widen_weeks(current, weeks)


# Sections fed only by the harnesses, which appear only when they hold work.
_HARNESS_ONLY = (API_SECTION, FREE_SECTION, OLLAMA_SECTION, HYPER_SECTION)


def api_window(
    now: datetime, since: datetime | None, until: datetime | None, weeks: int
) -> Window:
    """The span for pay-per-token work, which answers to no provider reset.

    It gets a plain rolling span of the same width as everyone else's window
    rather than a borrowed provider's clock — but an explicit range still
    governs it, exactly as it governs every other section.
    """
    rolling = now - timedelta(weeks=weeks)
    if since is None and until is None:
        return Window("no reset · rolling", rolling, now)
    return Window(
        "custom window",
        since if since is not None else rolling,
        until if until is not None else now,
    )


def _span(window: Window) -> tuple[datetime, datetime]:
    return window.start, window.end


def _claude_meter(limits: ClaudeLimits | None, offline: bool) -> Meter:
    if limits is None:
        why = "skipped: --offline" if offline else "not available: usage endpoint unreachable"
        return Meter(None, why)
    return Meter(limits.used, limits.note)


def _first_window(live: XaiUsage | None, logged: GrokLimits | None) -> Window | None:
    if live is not None:
        return live.window
    return logged.window if logged else None


def _grok_meter(live: XaiUsage | None, logged: GrokLimits | None) -> Meter:
    tier = logged.tier if logged else "Grok"
    if live is not None:
        return Meter(live.used, f"{tier} · live")
    if logged is not None:
        return Meter(logged.used, f"{tier} · as of {logged.as_of:%d %b %H:%M}")
    return Meter(None, "not available: no billing reading")


def _codex_meter(limits: CodexLimits | None) -> Meter:
    if limits is None:
        return Meter(None, "not available: no limits logged")
    note = f"as of {limits.as_of:%d %b %H:%M}"
    if limits.short is not None:
        used, _ = limits.short
        note = f"5h {used * 100:.0f}% · {note}"
    return Meter(limits.used, note)


def recap_text(
    now: datetime,
    since: datetime | None,
    until: datetime | None,
    claude_root: Path,
    claude_credentials: Path,
    grok_log: Path,
    grok_auth: Path,
    codex_root: Path,
    pi_root: Path,
    opencode_db: Path,
    color: bool | None = None,
    weeks: int = 1,
    offline: bool = False,
) -> str:
    combined = TokenBuckets()
    custom = since is not None or until is not None
    # An explicit range overrides every window, so don't go looking for one.
    codex_seen: CodexLimits | None = None if custom else codex_limits(codex_root, now)
    claude_seen: ClaudeLimits | None = (
        None if custom or offline else claude_limits(claude_credentials, now)
    )
    grok_seen: GrokLimits | None = None if custom else grok_limits(grok_log, now)
    # The logged copy is only as fresh as the last time Grok ran, so ask.
    grok_live: XaiUsage | None = (
        None if custom or offline else xai_usage(grok_auth, now)
    )
    # Each provider that states its own window beats the guessed reset clock.
    logged: dict[str, Window | None] = {
        "codex": codex_seen.window if codex_seen else None,
        "claude": claude_seen.window if claude_seen else None,
        "grok": _first_window(grok_live, grok_seen),
    }
    # A meter reads one weekly bucket, so it describes a wider span not at all:
    # buckets overlap by days rather than tiling into weeks, so they can be
    # neither summed nor averaged into one.
    meters: dict[str, Meter | None] = {}
    if weeks == 1:
        meters = {
            "claude": _claude_meter(claude_seen, offline),
            "grok": _grok_meter(grok_live, grok_seen),
            "codex": _codex_meter(codex_seen),
        }
    plans = (
        ("claude", "Claude Code", lambda s, e: collect_claude(claude_root, s, e)),
        ("grok", "Grok", lambda s, e: collect_grok(grok_log, s, e)),
        ("codex", "Codex", lambda s, e: collect_codex(codex_root, s, e)),
    )
    windows = {
        name: window_for(name, now, since, until, logged.get(name), weeks)
        for name, _, _ in plans
    }
    # Free work answers to no reset either, and neither Ollama nor Hyper
    # publishes a reset to answer to, so all four share the paid span and every
    # box describes the same stretch of time.
    windows[API_SECTION] = api_window(now, since, until, weeks)
    windows[FREE_SECTION] = windows[API_SECTION]
    windows[OLLAMA_SECTION] = windows[API_SECTION]
    windows[HYPER_SECTION] = windows[API_SECTION]
    buckets = {name: collect(*_span(windows[name])) for name, _, collect in plans}

    # A harness can spend any provider's quota, so its calls are filed under
    # whichever section paid, not under a section of its own.
    spans = {name: _span(window) for name, window in windows.items()}
    # Provider-named rows say nothing about the harness that placed the call,
    # so their box says once what the rows no longer repeat.
    contributors: set[str] = set()
    for harness, collected in (
        ("pi", collect_pi(pi_root, spans)),
        ("opencode", collect_opencode(opencode_db, spans)),
    ):
        for section, extra in collected.items():
            buckets.setdefault(section, TokenBuckets()).merge(extra)
            if section in PROVIDER_NAMED:
                contributors.add(harness)
    source = f"usage from {' and '.join(sorted(contributors))}" if contributors else ""

    # Pay-per-token work divides once, on each model's own total, so a model
    # lands in exactly one of the two boxes.
    paid, free = split_free(buckets.get(API_SECTION) or TokenBuckets())
    buckets[API_SECTION], buckets[FREE_SECTION] = paid, free

    views: list[ProviderView] = []
    titles = [(n, t, n.capitalize()) for n, t, _ in plans]
    titles.append((API_SECTION, "Pay per token", "API"))
    titles.append((FREE_SECTION, "Pay per token (free)", "Free"))
    titles.append((OLLAMA_SECTION, "Ollama", "Ollama"))
    titles.append((HYPER_SECTION, "Charm Hyper", "Hyper"))
    for name, title, short in titles:
        section_buckets = buckets.get(name) or TokenBuckets()
        if name in _HARNESS_ONLY and not section_buckets.calls:
            continue  # nothing landed here, so the box would say nothing
        combined.merge(section_buckets)
        views.append(
            ProviderView(
                key=name,
                title=title,
                window=windows[name],
                buckets=section_buckets,
                meter=meters.get(name),
                short=short,
                # Rows that name their harness already say where they came
                # from, so only the provider-named boxes carry the line.
                source=source if name in PROVIDER_NAMED else "",
                # Free work is free, and Ollama publishes no rate at all: in
                # both a column of $0 and dashes says nothing.
                prices=name not in (FREE_SECTION, OLLAMA_SECTION),
            )
        )
    return render_report(now, custom, weeks, views, combined, color=color)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tz = ZoneInfo(args.tz) if args.tz else local_tz()
    now = aware_local(None, tz)
    since = parse_when(args.since, now) if args.since else None
    until = parse_when(args.until, now) if args.until else None
    if until is None and since is not None:
        until = now
    print(
        recap_text(
            now,
            since,
            until,
            args.claude_root,
            args.claude_credentials,
            args.grok_log,
            args.grok_auth,
            args.codex_root,
            args.pi_root,
            args.opencode_db,
            color=False if args.no_color else True,
            weeks=args.weeks,
            offline=args.offline,
        ),
        end="",
    )
    return 0
