"""Grok's weekly pool, read live from the billing endpoint its CLI calls.

The same figure is written to the CLI's log on every successful fetch (see
grok.grok_limits), but that copy is only as fresh as the last time Grok ran.
Asking directly matches what /usage shows, which matters most exactly when it
matters at all: near the cap.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from token_recap.parse import parse_iso
from token_recap.windows import Window

BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
_ISSUER = "https://auth.x.ai"


@dataclass(frozen=True)
class XaiUsage:
    window: Window
    used: float  # 0..1 of the weekly pool


def xai_usage(auth: Path, now: datetime, timeout: float = 5.0) -> XaiUsage | None:
    payload = _fetch(auth, timeout)
    config = payload.get("config") if isinstance(payload, dict) else None
    if not isinstance(config, dict):
        return None
    used = config.get("creditUsagePercent")
    period = config.get("currentPeriod")
    if not isinstance(used, (int, float)) or not isinstance(period, dict):
        return None
    start = parse_iso(str(period.get("start") or ""))
    end = parse_iso(str(period.get("end") or ""))
    if start is None or end is None:
        return None
    end = end.astimezone(now.tzinfo)
    if end <= now:
        return None
    window = Window(f"{end:%a %H:%M} live", start.astimezone(now.tzinfo), end)
    return XaiUsage(window, float(used) / 100.0)


def _fetch(auth: Path, timeout: float) -> dict[str, Any] | None:
    token = _token(auth)
    if token is None:
        return None
    request = urllib.request.Request(
        BILLING_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "token-recap",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body: Any = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _token(auth: Path) -> str | None:
    """The OIDC session token, whose entry is keyed by issuer and client id.

    An API key is not accepted by this path, so only an `oidc` entry is worth
    sending. An expired one is left to fail on its own: the CLI owns the
    refresh, and a reporting tool has no business rotating its credentials.
    """
    try:
        blob: Any = json.loads(auth.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(blob, dict):
        return None
    for name, entry in blob.items():
        if not str(name).startswith(_ISSUER) or not isinstance(entry, dict):
            continue
        if entry.get("auth_mode") != "oidc":
            continue
        token = entry.get("key")
        if isinstance(token, str) and token:
            return token
    return None
