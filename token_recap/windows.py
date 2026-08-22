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


# Local wall clock. Claude "12:00" and Codex "12:28" are noon; Grok is 12:47 AM.
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
