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


# Charm Hyper's daily allowance refills at 16:33 UTC: on Sun 11 Oct 2026 its
# billing page said "Next Hypercredit Refresh in 31 minutes" at 18:02 Paris,
# and quota-gauges logs every change of the balance (hyper-balance.log) to
# pin the minute. UTC because a server counting days counts them in UTC: if
# after 25 Oct the refresh lands at 18:33 Paris rather than 17:33, the anchor
# was Paris wall-clock time after all.
HYPER_RESET = time(16, 33)
HYPER_RESET_TZ = timezone.utc
# The billing month is a calendar matter, kept in local time.
HYPER_BILLING_TZ = ZoneInfo("Europe/Paris")


def hyper_window(now: datetime) -> tuple[datetime, datetime]:
    """The Hyper day holding `now`, from one refresh to the next, in UTC."""
    day = now.astimezone(HYPER_RESET_TZ).date()
    if datetime.combine(day, HYPER_RESET, tzinfo=HYPER_RESET_TZ) > now:
        day -= timedelta(days=1)
    start = datetime.combine(day, HYPER_RESET, tzinfo=HYPER_RESET_TZ)
    return start, start + timedelta(days=1)


# The Hyper subscription began on 12 Sep 2026, so it bills from the 12th.
HYPER_BILLING_DAY = 12


def hyper_billing_window(now: datetime) -> tuple[datetime, datetime]:
    """The Hyper billing month holding `now`, from the 12th to the 12th.

    The daily meter cannot be set against a monthly price, so the token rows
    of the Hyper box are summed over this instead. Midnight Paris is a guess:
    the payment time is not known, and a few hours either way change little.
    """
    local = now.astimezone(HYPER_BILLING_TZ)
    year, month = local.year, local.month
    if local.day < HYPER_BILLING_DAY:
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    start = datetime(year, month, HYPER_BILLING_DAY, tzinfo=HYPER_BILLING_TZ)
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return start, datetime(year, month, HYPER_BILLING_DAY, tzinfo=HYPER_BILLING_TZ)


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
