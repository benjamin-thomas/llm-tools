"""Charm Hyper's daily allowance, read live from its credits endpoint.

Hyper answers with the balance alone: neither the allowance nor its reset is
published, so both are as observed on this account (the reset lives with the
other clocks, in windows.py). The allowance is shared with another device, so
this reading counts usage the local logs never see.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

HYPER_DAILY = 250
HYPER_URL = "https://hyper.charm.land/v1/credits"
# opencode keeps a long-lived Hyper API key. pi only holds an OAuth token that
# expires within the hour and is pi's to refresh, not ours.
OPENCODE_AUTH = Path.home() / ".local" / "share" / "opencode" / "auth.json"


def hyper_key(env: Mapping[str, str], auth: Path = OPENCODE_AUTH) -> str | None:
    key = env.get("HYPER_API_KEY")
    if key:
        return key
    try:
        blob: Any = json.loads(auth.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    entry = blob.get("hyper") if isinstance(blob, dict) else None
    if not isinstance(entry, dict) or entry.get("type") != "api":
        return None
    key = entry.get("key")
    return key if isinstance(key, str) and key else None


def fetch_hyper_balance(key: str, timeout: float = 5.0) -> float | None:
    request = urllib.request.Request(
        HYPER_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "token-recap",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body: Any = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    balance = body.get("balance") if isinstance(body, dict) else None
    return float(balance) if isinstance(balance, (int, float)) else None


def hyper_used(balance: float) -> float:
    """0..1 of the day's allowance. A top-up can lift the balance past the
    allowance, which reads as nothing used rather than as a negative."""
    return max(0.0, (HYPER_DAILY - balance) / HYPER_DAILY)
