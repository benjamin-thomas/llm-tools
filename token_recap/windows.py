from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class WeeklyReset:
    weekday: int  # Monday=0 … Sunday=6
    hour: int
    minute: int
    label: str


@dataclass(frozen=True)
class Window:
    """A span to report on, and how it was arrived at."""

    label: str
    start: datetime
    end: datetime


# Local wall clock. Claude "12:00" and Codex "12:28" are noon; Grok is 12:47 AM.
#
# None of these three is a published product constant: every provider anchors
# the weekly refill per account, and no provider documents a global weekday and
# time. These are this account's observed resets, so they are a fallback, not a
# truth. Codex publishes its live window in its own logs and that is preferred
# (see codex.codex_limit_window); it drifts — it was a Thursday when this was
# written and a Friday two weeks later — which is why guessing goes stale.
# Grok logs its billing period too (see grok.grok_limits) and Claude answers
# with one over the wire (anthropic_usage), so all three prefer a stated window
# and fall back here only when none is on hand. Grok's anchor is fixed in UTC
# (…T22:47:24.847880Z, +7d exactly), so "Sun 00:47" is this account's local
# rendering of it and holds only while the machine sits at UTC+2.
RESETS: dict[str, WeeklyReset] = {
    "claude": WeeklyReset(weekday=1, hour=12, minute=0, label="Tue 12:00"),
    "grok": WeeklyReset(weekday=6, hour=0, minute=47, label="Sun 00:47"),
    "codex": WeeklyReset(weekday=3, hour=12, minute=28, label="Thu 12:28"),
}


def last_weekly_reset(now: datetime, spec: WeeklyReset) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    candidate = now.replace(
        hour=spec.hour, minute=spec.minute, second=0, microsecond=0
    )
    days_back = (now.weekday() - spec.weekday) % 7
    candidate = candidate - timedelta(days=days_back)
    if candidate > now:
        candidate -= timedelta(days=7)
    return candidate


def next_weekly_reset(now: datetime, spec: WeeklyReset) -> datetime:
    return last_weekly_reset(now, spec) + timedelta(days=7)


def widen_weeks(window: Window, weeks: int) -> Window:
    """Reach back over the `weeks - 1` windows before this one.

    The end stays put, so week 1 is always the current unfinished week and a
    wider span simply adds whole finished weeks behind it.
    """
    if weeks <= 1:
        return window
    return Window(
        f"{window.label} ×{weeks}wk",
        window.start - timedelta(weeks=weeks - 1),
        window.end,
    )


def local_tz() -> tzinfo:
    tz = datetime.now().astimezone().tzinfo
    if tz is not None:
        return tz
    return ZoneInfo("Europe/Paris")


def aware_local(now: datetime | None, tz: tzinfo) -> datetime:
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)
