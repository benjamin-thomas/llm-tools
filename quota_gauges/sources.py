"""Each provider's meter, turned into Gauges.

The readings themselves live in token_recap (anthropic_usage, hyper_usage,
mistral_usage), which both tools share. The mapping here is pure and takes
whatever the fetch produced (or None), so every source is testable offline.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from quota_gauges.gauge import Gauge
from token_recap.anthropic_usage import ClaudeLimits
from token_recap.hyper_usage import HYPER_DAILY, hyper_used
from token_recap.mistral_usage import READING, extension_error, mistral_budget, read_reading
from token_recap.windows import hyper_window

_FIVE_HOURS = timedelta(hours=5)


def claude_gauges(
    limits: ClaudeLimits | None, now: datetime, problem: str = ""
) -> list[Gauge]:
    """The 5h window and the week, both from the one usage answer."""
    if limits is None:
        return [
            Gauge("claude-5h", "Claude 5h", None, paced=False, problem=problem),
            Gauge("claude-week", "Claude week", None, problem=problem),
        ]
    five = Gauge(
        "claude-5h", "Claude 5h", None, paced=False, as_of=limits.as_of,
        problem="no 5h reading",
    )
    if limits.session is not None:
        used, resets = limits.session
        five = Gauge(
            "claude-5h",
            "Claude 5h",
            used,
            resets - _FIVE_HOURS if resets else None,
            resets,
            paced=False,
            as_of=limits.as_of,
        )
    weekly = Gauge(
        "claude-week",
        "Claude week",
        limits.used,
        limits.window.start,
        limits.window.end,
        as_of=limits.as_of,
    )
    return [five, weekly]


def hyper_gauge(balance: float | None, now: datetime, problem: str = "") -> Gauge:
    start, end = hyper_window(now)
    if balance is None:
        return Gauge("hyper", "Hyper day", None, start, end, problem=problem)
    return Gauge(
        "hyper", "Hyper day", hyper_used(balance), start, end,
        detail=f"{balance:g} HC left of {HYPER_DAILY}",
    )


# Hyper's API gives no refresh time, so every change of the balance is kept
# here with its time: the day turns where the balance jumps back up, which is
# what HYPER_RESET (token_recap/windows.py) is checked against.
HYPER_LOG = Path.home() / ".cache" / "quota-gauges" / "hyper-balance.log"


def record_balance(log: Path, now: datetime, balance: float | None) -> None:
    if balance is None:
        return
    try:
        lines = log.read_text().splitlines()
    except OSError:
        lines = []
    if lines and lines[-1].split()[-1] == f"{balance:g}":
        return
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as out:
        out.write(f"{now.isoformat()} {balance:g}\n")


def mistral_gauge(reading: dict[str, Any] | None, now: datetime) -> Gauge:
    if reading is None:
        return Gauge(
            "mistral", "Mistral month", None,
            problem="no reading yet: is the Chrome extension running?",
        )
    budget = mistral_budget(reading)
    error = extension_error(reading)
    if budget is None:
        return Gauge("mistral", "Mistral month", None, problem=error or "unreadable reading")
    return Gauge(
        "mistral",
        "Mistral month",
        budget.used,
        _month_before(budget.resets),
        budget.resets,
        detail=f"{budget.detail} · {error}" if error else budget.detail,
        as_of=budget.as_of,
    )


def _month_before(moment: datetime) -> datetime:
    """The same instant a calendar month earlier; the reset is on the 1st."""
    if moment.month == 1:
        return moment.replace(year=moment.year - 1, month=12)
    return moment.replace(month=moment.month - 1)


def read_mistral(path: Path = READING) -> dict[str, Any] | None:
    return read_reading(path)
