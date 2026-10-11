"""Mistral's monthly Vibe budget, as the Chrome extension last read it.

Mistral serves the budget only to a browser session behind Cloudflare: an API
key gets a 401 and a script gets a challenge page. So an extension fetches it
from inside Chrome and its native host writes the answer here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from token_recap.parse import parse_iso

READING = Path.home() / ".cache" / "quota-gauges" / "mistral.json"
_CURRENCY = {"EUR": "€", "USD": "$"}


@dataclass(frozen=True)
class MistralBudget:
    used: float  # 0..1 of the month's budget
    spent: float
    size: float
    unit: str  # a currency symbol
    resets: datetime
    as_of: datetime | None

    @property
    def detail(self) -> str:
        return f"{self.spent:.2f} {self.unit} of {self.size:g} {self.unit}"


def read_reading(path: Path = READING) -> dict[str, Any] | None:
    try:
        blob: Any = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return blob if isinstance(blob, dict) else None


def extension_error(reading: dict[str, Any] | None) -> str:
    """What the extension last failed on, if its last try failed. The native
    host keeps the previous budget beside it, so both can be shown."""
    error = reading.get("error") if reading else None
    if not isinstance(error, str) or not error:
        return ""
    # 401/403 is the usual one: the chat.mistral.ai session has expired.
    hint = " · log in to chat.mistral.ai" if error in ("HTTP 401", "HTTP 403") else ""
    return f"extension: {error}{hint}"


def mistral_budget(reading: dict[str, Any] | None) -> MistralBudget | None:
    if reading is None:
        return None
    budget = reading.get("budget")
    budget = budget if isinstance(budget, dict) else {}
    percent = budget.get("usage_percentage")
    size = budget.get("initial_budget")
    resets = parse_iso(str(budget.get("reset_at") or ""))
    if not isinstance(percent, (int, float)) or not isinstance(size, (int, float)) or resets is None:
        return None
    currency = str(budget.get("currency") or "")
    return MistralBudget(
        used=percent / 100.0,
        spent=size * percent / 100.0,
        size=float(size),
        unit=_CURRENCY.get(currency, currency),
        resets=resets,
        as_of=parse_iso(str(reading.get("fetched_at") or "")),
    )
