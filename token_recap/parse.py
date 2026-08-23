from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_iso(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def in_window(ts: datetime, start: datetime, end: datetime) -> bool:
    return start <= ts < end


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def as_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
