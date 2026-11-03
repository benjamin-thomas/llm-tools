"""Codex's own meter, read live from the endpoint its /status screen asks.

The same buckets are logged with every Codex turn (codex.codex_limits), but
that copy is only as fresh as the last turn, and from a different plan if the
subscription changed since. Asking matches /status; the log stays the fallback.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from token_recap.codex import CodexLimits, codex_limits, limits_from_buckets

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
AUTH = Path.home() / ".codex" / "auth.json"
# The panel asks every minute; the meter barely moves in two.
CACHE = Path.home() / ".cache" / "token-recap" / "codex-usage.json"
_FRESH = 120.0


def best_codex_limits(
    auth: Path,
    sessions: Path,
    now: datetime,
    offline: bool = False,
    cache: Path = CACHE,
) -> CodexLimits | None:
    """Whichever of the live answer and the logged one is newer."""
    live = None if offline else codex_usage(auth, now, cache=cache)
    logged = codex_limits(sessions, now)
    if live is None or (logged is not None and logged.as_of > live.as_of):
        return logged
    return live


def codex_usage(
    auth: Path, now: datetime, timeout: float = 5.0, cache: Path = CACHE
) -> CodexLimits | None:
    """The live meter, or the last answer kept when the endpoint cannot be had."""
    kept = _read_cache(cache)
    if kept is None or now.timestamp() - kept[0] >= _FRESH:
        live = _fetch(auth, timeout)
        if live is not None and _buckets(live):
            kept = (now.timestamp(), live)
            _write_cache(cache, {"fetched_at": kept[0], "payload": live})
    if kept is None:
        return None
    as_of = datetime.fromtimestamp(kept[0], tz=timezone.utc).astimezone(now.tzinfo)
    return limits_from_buckets(_buckets(kept[1]), as_of, now)


def _buckets(payload: dict[str, Any]) -> dict[str, Any]:
    """The endpoint's windows, renamed to the log's buckets."""
    limit: Any = payload.get("rate_limit")
    if not isinstance(limit, dict):
        return {}
    out: dict[str, Any] = {}
    for name in ("primary_window", "secondary_window"):
        window: Any = cast(dict[str, Any], limit).get(name)
        if not isinstance(window, dict):
            continue
        window = cast(dict[str, Any], window)
        seconds = window.get("limit_window_seconds")
        if not isinstance(seconds, int):
            continue
        out[name] = {
            "window_minutes": seconds // 60,
            "resets_at": window.get("reset_at"),
            "used_percent": window.get("used_percent"),
        }
    return out


def _read_cache(cache: Path) -> tuple[float, dict[str, Any]] | None:
    try:
        blob: Any = json.loads(cache.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(blob, dict):
        return None
    blob = cast(dict[str, Any], blob)
    at: Any = blob.get("fetched_at")
    payload: Any = blob.get("payload")
    if not isinstance(at, (int, float)) or not isinstance(payload, dict):
        return None
    return float(at), cast(dict[str, Any], payload)


def _write_cache(cache: Path, blob: dict[str, Any]) -> None:
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(blob))
    except OSError:
        pass


def _fetch(auth: Path, timeout: float) -> dict[str, Any] | None:
    found = _credentials(auth)
    if found is None:
        return None
    token, account = found
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "ChatGPT-Account-Id": account,
            "User-Agent": "token-recap",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body: Any = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    return cast(dict[str, Any], body) if isinstance(body, dict) else None


def _credentials(auth: Path) -> tuple[str, str] | None:
    """The ChatGPT login's access token and account. An expired token is left
    to fail: Codex owns the refresh, and rotating it here could log Codex out."""
    try:
        blob: Any = json.loads(auth.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(blob, dict):
        return None
    blob = cast(dict[str, Any], blob)
    tokens: Any = blob.get("tokens")
    if blob.get("auth_mode") != "chatgpt" or not isinstance(tokens, dict):
        return None
    tokens = cast(dict[str, Any], tokens)
    token: Any = tokens.get("access_token")
    account: Any = tokens.get("account_id")
    if not isinstance(token, str) or not token or not isinstance(account, str):
        return None
    return token, account
