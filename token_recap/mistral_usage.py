"""Mistral's monthly Vibe budget, read live from the public billing API.

Vibe's own footer reads it from `GET /v1/billing/budget` with the API key it
keeps in ~/.vibe/.env, so this does the same. The key is only ever read at
run time, from the environment or that file.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from token_recap.parse import parse_iso

BUDGET_URL = "https://api.mistral.ai/v1/billing/budget"
VIBE_ENV = Path.home() / ".vibe" / ".env"
_KEY_VAR = "MISTRAL_API_KEY"
_CURRENCY = {"EUR": "€", "USD": "$"}


@dataclass(frozen=True)
class MistralBudget:
    used: float  # 0..1 of the month's budget
    spent: float
    size: float
    unit: str  # a currency symbol
    resets: datetime

    @property
    def detail(self) -> str:
        return f"{self.spent:.2f} {self.unit} of {self.size:g} {self.unit}"


def mistral_key(env: Mapping[str, str], dotenv: Path = VIBE_ENV) -> str | None:
    """The environment's key first, else the one Vibe keeps in its .env."""
    key = env.get(_KEY_VAR)
    if key:
        return key
    try:
        lines = dotenv.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        name, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and name.strip() == _KEY_VAR:
            value = value.strip().strip("\"'")
            return value or None
    return None


def parse_budget(body: Any) -> MistralBudget | None:
    """The Vibe budget in the billing answer; it also carries an API budget,
    which is not the subscription's."""
    if not isinstance(body, dict):
        return None
    raw: Any = cast(dict[str, Any], body).get("vibe_budget")
    if not isinstance(raw, dict):
        return None
    budget = cast(dict[str, Any], raw)
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
    )


def fetch_budget(key: str, timeout: float = 5.0) -> MistralBudget | str:
    """The budget, or why there is none."""
    request = urllib.request.Request(
        BUDGET_URL,
        headers={"Authorization": f"Bearer {key}", "User-Agent": "token-recap"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body: Any = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # 403 is a key whose member lacks the usage permission.
        return f"billing endpoint answered HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError):
        return "billing endpoint unreachable"
    return parse_budget(body) or "no Vibe budget in the billing answer"


def read_mistral(env: Mapping[str, str], dotenv: Path = VIBE_ENV) -> MistralBudget | str:
    key = mistral_key(env, dotenv)
    if key is None:
        return f"no Mistral API key (${_KEY_VAR} or {dotenv})"
    return fetch_budget(key)
