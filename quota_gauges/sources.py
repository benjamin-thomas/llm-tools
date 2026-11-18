"""Each provider's meter, turned into Gauges.

The readings themselves live in token_recap (anthropic_usage, openai_usage,
mistral_usage), which both tools share. The mapping here is pure and takes
whatever the fetch produced (None, or why there is none), so every source is testable offline.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from quota_gauges.gauge import Gauge
from token_recap.anthropic_usage import ClaudeLimits
from token_recap.codex import CodexLimits
from token_recap.mistral_usage import MistralBudget

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


def codex_gauges(
    limits: CodexLimits | None, now: datetime, problem: str = ""
) -> list[Gauge]:
    """The week, preceded by a 5h window only on plans that have one: Pro
    Lite has none."""
    if limits is None:
        return [Gauge("codex-week", "Codex week", None, problem=problem)]
    gauges: list[Gauge] = []
    if limits.short is not None:
        used, resets = limits.short
        gauges.append(Gauge(
            "codex-5h", "Codex 5h", used, resets - _FIVE_HOURS, resets,
            paced=False, as_of=limits.as_of,
        ))
    gauges.append(Gauge(
        "codex-week", "Codex week", limits.used,
        limits.window.start, limits.window.end, as_of=limits.as_of,
    ))
    return gauges


def mistral_gauge(reading: MistralBudget | str, now: datetime) -> Gauge:
    """`reading` is the budget, or why there is none."""
    if isinstance(reading, str):
        return Gauge("mistral", "Mistral month", None, problem=reading)
    return Gauge(
        "mistral",
        "Mistral month",
        reading.used,
        _month_before(reading.resets),
        reading.resets,
        detail=reading.detail,
    )


def _month_before(moment: datetime) -> datetime:
    """The same instant a calendar month earlier; the reset is on the 1st."""
    if moment.month == 1:
        return moment.replace(year=moment.year - 1, month=12)
    return moment.replace(month=moment.month - 1)

