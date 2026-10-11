from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import os
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

from token_recap.buckets import TokenBuckets
from token_recap.anthropic_usage import ClaudeLimits, best_claude_limits
from token_recap.claude import collect_claude
from token_recap.codex import CodexLimits, codex_limits, collect_codex
from token_recap.format import Meter, ProviderView, render_report
from token_recap.grok import GrokLimits, collect_grok, grok_limits
from token_recap.hyper_usage import (
    HYPER_DAILY,
    OPENCODE_AUTH,
    fetch_hyper_balance,
    hyper_key,
    hyper_used,
)
from token_recap.mistral_usage import (
    READING,
    MistralBudget,
    extension_error,
    mistral_budget,
    read_reading,
)
from token_recap.harness import (
    API_SECTION,
    FREE_SECTION,
    HYPER_SECTION,
    MISTRAL_SECTION,
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
    hyper_billing_window,
    hyper_window,
    last_weekly_reset,
    local_tz,
    next_weekly_reset,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Recap token usage from local logs, each subscription over its "
            "current window, priced at each provider's own API list rate."
        )
    )
    p.add_argument(
        "--tz",
        help="IANA timezone for the reset clocks (default: local).",
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
        help="Ask no meter over the network (Claude's endpoint, Grok, Hyper).",
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


def window_for(name: str, now: datetime, logged: Window | None = None) -> Window:
    """The provider's current week: the window it published, else the
    observed reset clock for the providers that publish nothing."""
    spec = RESETS[name]
    return logged or Window(
        f"{spec.label} local",
        last_weekly_reset(now, spec),
        next_weekly_reset(now, spec),
    )


def api_window(now: datetime) -> Window:
    """Pay-per-token work answers to no reset, so it gets a plain rolling week."""
    return Window("no reset · last 7 days", now - timedelta(days=7), now)


def hyper_day(now: datetime) -> Window:
    start, end = hyper_window(now)
    return Window("16:33 UTC", start.astimezone(now.tzinfo), end.astimezone(now.tzinfo))


# What a box cannot see, said once in the box rather than left to be guessed.
#
# Vibe CLI keeps no usage on disk. Checked against mistral-vibe 2.26.1: its
# session totals (session_cost, session_prompt_tokens) live in memory only and
# reach ACP clients, never a file; the per-call usage it does write sits in a
# recovery journal under ~/.vibe/logs/session/unified/*/journal/, which is cut
# back at every checkpoint, so only the last few calls survive; and no hook is
# handed usage (post_agent gets a session id and a transcript path). Sessions
# from before 2.x (~/.vibe/logs/session/session_*/meta.json) did carry
# stats.session_cost. Worth a fresh look after a Vibe upgrade.
#
# Hyper is the one allowance shared with another device: its meter counts that
# device, its token rows only ever count this one.
_SOURCES = {
    MISTRAL_SECTION: "Vibe CLI: no usage data available · pi and opencode only",
    HYPER_SECTION: "shared account: the meter counts every device, the tokens this one",
}
# Whether a full meter can be priced from the tokens beside it. Hyper's meter
# is daily and shared while its tokens run a month on this machine alone;
# Mistral's counts Vibe CLI, which the logs never see, so it gives a floor.
_CAPACITY = {HYPER_SECTION: "none", MISTRAL_SECTION: "floor"}

# A reading older than this no longer describes the meter.
_MAX_AGE = timedelta(minutes=30)


def mistral_window(now: datetime) -> Window:
    """The calendar month in UTC, which is when the Vibe budget refills."""
    utc = now.astimezone(timezone.utc)
    start = utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    # Shown in local time like every other box; only the anchor is UTC.
    return Window("1st 00:00 UTC", start.astimezone(now.tzinfo), end.astimezone(now.tzinfo))


def _span(window: Window) -> tuple[datetime, datetime]:
    return window.start, window.end


def _claude_meter(limits: ClaudeLimits | None, offline: bool) -> Meter:
    if limits is None:
        why = (
            "not available: no session open lately, endpoint not asked (--offline)"
            if offline
            else "not available: no session open lately and usage endpoint unreachable"
        )
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


def _hyper_meter(balance: float | None, asked: bool, keyed: bool) -> Meter:
    if balance is not None:
        return Meter(hyper_used(balance), f"{balance:g} HC left of {HYPER_DAILY}")
    if not keyed:
        return Meter(None, "not available: no Hyper API key")
    if not asked:
        return Meter(None, "not available: credits endpoint not asked (--offline)")
    return Meter(None, "not available: credits endpoint unreachable")


def _mistral_meter(budget: MistralBudget | None, now: datetime, error: str = "") -> Meter:
    if budget is None:
        return Meter(None, f"not available: {error or 'no reading from the Chrome extension'}")
    note = f"{budget.detail} · {error}" if error else budget.detail
    if budget.as_of is not None and now - budget.as_of > _MAX_AGE:
        note += f" · as of {budget.as_of.astimezone(now.tzinfo):%d %b %H:%M}"
    return Meter(budget.used, note)


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
    claude_root: Path,
    claude_credentials: Path,
    grok_log: Path,
    grok_auth: Path,
    codex_root: Path,
    pi_root: Path,
    opencode_db: Path,
    color: bool | None = None,
    offline: bool = False,
    hyper_auth: Path = OPENCODE_AUTH,
    mistral_reading: Path = READING,
    hyper_balance: Callable[[str], float | None] = fetch_hyper_balance,
) -> str:
    key = hyper_key(os.environ, hyper_auth)
    reading = read_reading(mistral_reading)
    balance = None if offline or key is None else hyper_balance(key)
    codex_seen: CodexLimits | None = codex_limits(codex_root, now)
    claude_seen: ClaudeLimits | None = best_claude_limits(claude_credentials, now, offline)
    grok_seen: GrokLimits | None = grok_limits(grok_log, now)
    # The logged copy is only as fresh as the last time Grok ran, so ask.
    grok_live: XaiUsage | None = None if offline else xai_usage(grok_auth, now)
    # Each provider that states its own window beats the guessed reset clock.
    logged: dict[str, Window | None] = {
        "codex": codex_seen.window if codex_seen else None,
        "claude": claude_seen.window if claude_seen else None,
        "grok": _first_window(grok_live, grok_seen),
    }
    meters: dict[str, Meter | None] = {
        "claude": _claude_meter(claude_seen, offline),
        "grok": _grok_meter(grok_live, grok_seen),
        "codex": _codex_meter(codex_seen),
        HYPER_SECTION: _hyper_meter(balance, asked=not offline, keyed=key is not None),
        MISTRAL_SECTION: _mistral_meter(mistral_budget(reading), now, extension_error(reading)),
    }
    plans = (
        ("claude", "Claude Code", lambda s, e: collect_claude(claude_root, s, e)),
        ("grok", "Grok", lambda s, e: collect_grok(grok_log, s, e)),
        ("codex", "Codex", lambda s, e: collect_codex(codex_root, s, e)),
    )
    windows = {name: window_for(name, now, logged.get(name)) for name, _, _ in plans}
    # Free work answers to no reset either, and Ollama publishes none, so both
    # share the paid section's rolling week.
    windows[API_SECTION] = api_window(now)
    windows[FREE_SECTION] = windows[API_SECTION]
    windows[OLLAMA_SECTION] = windows[API_SECTION]
    windows[HYPER_SECTION] = hyper_day(now)
    windows[MISTRAL_SECTION] = mistral_window(now)
    buckets = {name: collect(*_span(windows[name])) for name, _, collect in plans}

    # A harness can spend any provider's quota, so its calls are filed under
    # whichever section paid, not under a section of its own.
    spans = {name: _span(window) for name, window in windows.items()}
    # Hyper's tokens are summed over its billing month rather than the day its
    # meter runs on: a day of tokens cannot be set against a monthly price.
    hyper_tokens = Window("billing month from the 12th", *hyper_billing_window(now))
    spans[HYPER_SECTION] = _span(hyper_tokens)
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
    titles.append((MISTRAL_SECTION, "Mistral", "Mistral"))
    for name, title, short in titles:
        section_buckets = buckets.get(name) or TokenBuckets()
        if not section_buckets.total:
            # A box that spent nothing says nothing, and it is how a dropped
            # subscription leaves the report: by its window going quiet. Calls
            # are no test, since an aborted one is logged with no tokens.
            continue
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
                source=_SOURCES.get(name, source if name in PROVIDER_NAMED else ""),
                # Free work is free, and Ollama publishes no rate at all: in
                # both a column of $0 and dashes says nothing.
                prices=name not in (FREE_SECTION, OLLAMA_SECTION),
                tokens_window=hyper_tokens if name == HYPER_SECTION else None,
                capacity=_CAPACITY.get(name, "exact"),
            )
        )
    return render_report(now, views, color=color)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tz = ZoneInfo(args.tz) if args.tz else local_tz()
    now = aware_local(None, tz)
    print(
        recap_text(
            now,
            args.claude_root,
            args.claude_credentials,
            args.grok_log,
            args.grok_auth,
            args.codex_root,
            args.pi_root,
            args.opencode_db,
            color=False if args.no_color else True,
            offline=args.offline,
        ),
        end="",
    )
    return 0
