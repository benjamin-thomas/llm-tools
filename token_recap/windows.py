from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone, tzinfo
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


# Charm Hyper's daily allowance refills at 18:00 Paris wall-clock time, as
# observed on this account; Hyper publishes no reset. Whether the anchor is
# really Paris, UTC or New York only shows across a clock change (25 Oct in
# Europe, 1 Nov in the US).
HYPER_RESET = time(18, 0)
HYPER_TZ = ZoneInfo("Europe/Paris")


def hyper_window(now: datetime) -> tuple[datetime, datetime]:
    """The Hyper day holding `now`, from one 18:00 Paris to the next.

    Built from wall-clock dates rather than by adding 24h, so the day around a
    clock change is 23 or 25 hours long instead of drifting off 18:00. Handed
    back in UTC: Python subtracts two datetimes sharing a zone by wall clock,
    which would make that day 24 hours long again.
    """
    local = now.astimezone(HYPER_TZ)
    day = local.date()
    if datetime.combine(day, HYPER_RESET, tzinfo=HYPER_TZ) > now:
        day -= timedelta(days=1)
    start = datetime.combine(day, HYPER_RESET, tzinfo=HYPER_TZ)
    end = datetime.combine(day + timedelta(days=1), HYPER_RESET, tzinfo=HYPER_TZ)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


# The Hyper subscription began on 12 Sep 2026, so it bills from the 12th.
HYPER_BILLING_DAY = 12


def hyper_billing_window(now: datetime) -> tuple[datetime, datetime]:
    """The Hyper billing month holding `now`, from the 12th to the 12th.

    The daily meter cannot be set against a monthly price, so the token rows
    of the Hyper box are summed over this instead. Midnight Paris is a guess:
    the payment time is not known, and a few hours either way change little.
    """
    local = now.astimezone(HYPER_TZ)
    year, month = local.year, local.month
    if local.day < HYPER_BILLING_DAY:
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    start = datetime(year, month, HYPER_BILLING_DAY, tzinfo=HYPER_TZ)
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return start, datetime(year, month, HYPER_BILLING_DAY, tzinfo=HYPER_TZ)


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
