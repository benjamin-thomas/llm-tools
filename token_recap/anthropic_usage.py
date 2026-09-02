"""Claude's own usage meter, read live from the OAuth usage endpoint.

Claude Code writes no rate-limit state to its session logs, so unlike Codex
there is nothing local to read: the meter has to be asked for. The endpoint is
undocumented and answers with the same numbers the /usage screen shows, so
every failure here is non-fatal — the report simply falls back to the reset
clock and says the meter is unavailable.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from token_recap.parse import parse_iso
from token_recap.windows import Window

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_BETA = "oauth-2025-04-20"
_WEEK = timedelta(days=7)

# The endpoint rate-limits hard — a few calls in a minute earn a 429 with a
# retry-after of several minutes — and unlike Grok there is no logged copy to
# fall back on. So keep the last answer: a recent one is reused outright, and
# an old one still beats showing nothing while the limit clears.
CACHE = Path.home() / ".cache" / "token-recap" / "claude-usage.json"
_FRESH = 120.0


@dataclass(frozen=True)
class ClaudeLimits:
    window: Window
    used: float  # 0..1 of the weekly allowance
    note: str
    fresh: bool = True


def claude_limits(
    credentials: Path,
    now: datetime,
    timeout: float = 5.0,
    cache: Path = CACHE,
) -> ClaudeLimits | None:
    payload, taken_at = _payload(credentials, now, timeout, cache)
    if payload is None:
        return None
    fresh = taken_at is None or now.timestamp() - taken_at < _FRESH
    weekly = _weekly(payload)
    if weekly is None:
        return None
    used, resets = weekly
    end = resets.astimezone(now.tzinfo)
    window = Window(f"{end:%a %H:%M} live", end - _WEEK, end)
    note = _note(payload)
    if not fresh and taken_at is not None:
        seen = datetime.fromtimestamp(taken_at, tz=timezone.utc).astimezone(now.tzinfo)
        note = f"{note} · as of {seen:%d %b %H:%M}" if note else f"as of {seen:%d %b %H:%M}"
    return ClaudeLimits(window, used / 100.0, note, fresh)


def _weekly(payload: dict[str, Any]) -> tuple[float, datetime] | None:
    """The weekly utilisation and its reset, when the answer carries both."""
    weekly = payload.get("seven_day")
    if not isinstance(weekly, dict):
        return None
    used = _percent(weekly.get("utilization"))
    resets = parse_iso(str(weekly.get("resets_at") or ""))
    return None if used is None or resets is None else (used, resets)


def _payload(
    credentials: Path, now: datetime, timeout: float, cache: Path
) -> tuple[dict[str, Any] | None, float | None]:
    """The freshest answer available, without asking more often than needed."""
    kept = _read_cache(cache)
    if kept is not None and now.timestamp() - kept[0] < _FRESH:
        return kept[1], kept[0]
    live = _fetch(credentials, timeout)
    if live is not None:
        # Only keep an answer worth reusing: caching one we cannot read turns a
        # single bad response into the fallback for every later run.
        if _weekly(live) is not None:
            _write_cache(cache, now, live)
        return live, now.timestamp()
    return (kept[1], kept[0]) if kept is not None else (None, None)


def _read_cache(cache: Path) -> tuple[float, dict[str, Any]] | None:
    try:
        blob: Any = json.loads(cache.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(blob, dict):
        return None
    at = blob.get("fetched_at")
    payload = blob.get("payload")
    if not isinstance(at, (int, float)) or not isinstance(payload, dict):
        return None
    return float(at), payload


def _write_cache(cache: Path, now: datetime, payload: dict[str, Any]) -> None:
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps({"fetched_at": now.timestamp(), "payload": payload})
        )
    except OSError:
        pass  # a cache we cannot write is not a reason to fail the report


def _fetch(credentials: Path, timeout: float) -> dict[str, Any] | None:
    token = _token(credentials)
    if token is None:
        return None
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": _BETA,
            "User-Agent": "token-recap",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body: Any = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None  # offline, rate-limited, expired token: all just "no meter"
    return body if isinstance(body, dict) else None


def _token(credentials: Path) -> str | None:
    try:
        blob: Any = json.loads(credentials.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    oauth = blob.get("claudeAiOauth") if isinstance(blob, dict) else None
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    return token if isinstance(token, str) and token else None


def _percent(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _note(payload: dict[str, Any]) -> str:
    """The other caps worth knowing: the 5h window and any per-model one."""
    parts: list[str] = []
    session = payload.get("five_hour")
    if isinstance(session, dict):
        used = _percent(session.get("utilization"))
        if used is not None:
            parts.append(f"5h {used:.0f}%")
    for limit in _scoped(payload):
        parts.append(limit)
    return " · ".join(parts)


def _scoped(payload: dict[str, Any]) -> list[str]:
    """A weekly cap that applies to one model only, when one is in force."""
    limits = payload.get("limits")
    if not isinstance(limits, list):
        return []
    out: list[str] = []
    for entry in limits:
        if not isinstance(entry, dict) or entry.get("kind") != "weekly_scoped":
            continue
        used = _percent(entry.get("percent"))
        scope = entry.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        name = model.get("display_name") if isinstance(model, dict) else None
        if used is None or not isinstance(name, str):
            continue
        out.append(f"{name} {used:.0f}%")
    return out
