"""One subscription meter, and what it says against an even pace.

Everything here is a pure function of a Gauge and the clock, so the tray, the
terminal and the tests all read the same verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Before this share of a window has passed, one busy session extrapolates to
# nonsense, so no projection is made.
MIN_ELAPSED = 0.05
# A reading older than this no longer describes the meter.
MAX_AGE = timedelta(minutes=30)
# How far over an even pace still only earns a warning.
WARN_BAND = 0.10
# For a gauge with no pace to compare against, how full is worrying.
UNPACED_WARN = 0.70
UNPACED_OVER = 0.90


@dataclass(frozen=True)
class Gauge:
    key: str
    label: str
    used: float | None  # 0..1, or None when the provider gave no reading
    start: datetime | None = None
    end: datetime | None = None  # the reset
    # Whether an even pace means anything. The 5h window opens on the first
    # message, so being "behind" it is meaningless.
    paced: bool = True
    detail: str = ""
    as_of: datetime | None = None  # when the reading was taken, if not live
    problem: str = ""  # why there is no reading


def _seconds(later: datetime, earlier: datetime) -> float:
    """Real seconds between two instants. Plain subtraction goes by wall
    clock when both share a zone, which is an hour off across a clock change."""
    return later.timestamp() - earlier.timestamp()


def elapsed(gauge: Gauge, now: datetime) -> float | None:
    if gauge.start is None or gauge.end is None:
        return None
    total = _seconds(gauge.end, gauge.start)
    if total <= 0:
        return None
    return min(1.0, max(0.0, _seconds(now, gauge.start) / total))


def margin(gauge: Gauge, now: datetime) -> float | None:
    """Room left against an even pace: positive is unspent, negative is over."""
    gone = elapsed(gauge, now)
    if gauge.used is None or gone is None:
        return None
    return gone - gauge.used


def projected(gauge: Gauge, now: datetime) -> float | None:
    """Where the meter lands at the reset if the rate so far holds."""
    gone = elapsed(gauge, now)
    if gauge.used is None or gone is None or gone < MIN_ELAPSED:
        return None
    return gauge.used / gone


def runs_out_at(gauge: Gauge, now: datetime) -> datetime | None:
    """When the rate so far empties the allowance, if that is before the reset."""
    if gauge.used is not None and gauge.used >= 1.0:
        return now
    gone = elapsed(gauge, now)
    if gauge.used is None or gauge.used <= 0 or gone is None or gone < MIN_ELAPSED:
        return None
    assert gauge.start is not None and gauge.end is not None
    empty = gauge.start + timedelta(seconds=_seconds(now, gauge.start) / gauge.used)
    return empty if empty < gauge.end else None


def is_stale(gauge: Gauge, now: datetime) -> bool:
    return gauge.as_of is not None and now - gauge.as_of > MAX_AGE


def tone(gauge: Gauge, now: datetime) -> str:
    """ok, warn, over, stale or unknown: the colour a gauge deserves."""
    if gauge.used is None:
        return "unknown"
    if is_stale(gauge, now):
        return "stale"
    if gauge.used >= 1.0:
        return "over"
    room = margin(gauge, now) if gauge.paced else None
    if room is None:
        if gauge.used >= UNPACED_OVER:
            return "over"
        return "warn" if gauge.used >= UNPACED_WARN else "ok"
    if room >= 0:
        return "ok"
    return "warn" if room > -WARN_BAND else "over"


def bar(used: float, pace: float | None, width: int) -> str:
    """A filled bar, with a tick where an even pace would have it."""
    used = min(1.0, max(0.0, used))
    filled = round(used * width)
    cells = ["█"] * filled + ["░"] * (width - filled)
    if pace is not None:
        at = min(width - 1, int(pace * width))
        cells[at] = "┃" if at < filled else "│"
    return "".join(cells)
