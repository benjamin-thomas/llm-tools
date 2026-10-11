"""Claude's own usage meter, read live from the OAuth usage endpoint.

Claude Code writes no rate-limit state to its session logs, so unlike Codex
there is nothing local to read: the meter has to be asked for. The endpoint is
undocumented and answers with the same numbers the /usage screen shows, so
every failure here is non-fatal — the report simply falls back to the reset
clock and says the meter is unavailable.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
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

# Claude Code hands its status line the same two meters on every repaint, and
# statusline.rb keeps them here, stamped with the API response they came with.
# Reading that costs nothing, so the endpoint is asked only once it is old —
# no session has talked to the API lately.
STATUSLINE_DROP = (
    Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    / "claude-statusline"
    / "rate-limits.json"
)
_STATUSLINE_MAX_AGE = timedelta(minutes=30)


@dataclass(frozen=True)
class ClaudeLimits:
    window: Window
    used: float  # 0..1 of the weekly allowance
    note: str
    fresh: bool = True
    # The 5h bucket: used 0..1, and its reset — None while no session runs.
    session: tuple[float, datetime | None] | None = None
    as_of: datetime | None = None  # when the endpoint gave this answer


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
    if end <= now:
        return None  # a kept answer from a week that has since closed
    window = Window(f"{end:%a %H:%M} live", end - _WEEK, end)
    note = _note(payload)
    seen = (
        now
        if taken_at is None
        else datetime.fromtimestamp(taken_at, tz=timezone.utc).astimezone(now.tzinfo)
    )
    if not fresh:
        note = f"{note} · as of {seen:%d %b %H:%M}" if note else f"as of {seen:%d %b %H:%M}"
    return ClaudeLimits(
        window, used / 100.0, note, fresh, _session(payload, now), seen
    )


def _session(
    payload: dict[str, Any], now: datetime
) -> tuple[float, datetime | None] | None:
    block = payload.get("five_hour")
    if not isinstance(block, dict):
        return None
    used = _percent(block.get("utilization"))
    if used is None:
        return None
    resets = parse_iso(str(block.get("resets_at") or ""))
    return used / 100.0, resets.astimezone(now.tzinfo) if resets else None


def best_claude_limits(
    credentials: Path,
    now: datetime,
    offline: bool = False,
    drop: Path = STATUSLINE_DROP,
    cache: Path = CACHE,
) -> ClaudeLimits | None:
    """The status line's copy while it is fresh, else the endpoint, else the
    copy however old — an old reading still beats none, and says its age."""
    blob = _read_json(drop)
    copied = limits_from_statusline(blob, now) if blob else None
    if copied is not None and copied.as_of and now - copied.as_of <= _STATUSLINE_MAX_AGE:
        return copied
    live = None if offline else claude_limits(credentials, now, cache=cache)
    if live is not None or copied is None:
        return live
    seen = f"as of {copied.as_of:%d %b %H:%M}" if copied.as_of else "as of ?"
    note = f"{copied.note} · {seen}" if copied.note else seen
    return replace(copied, note=note, fresh=False)


def limits_from_statusline(blob: dict[str, Any], now: datetime) -> ClaudeLimits | None:
    """Claude's meters as Claude Code last handed them to its status line.

    Free to read, unlike the usage endpoint, which rate-limits hard. Its only
    weakness is age: it is as fresh as the last API response of any session.
    """
    taken_at = blob.get("fetched_at")
    if not isinstance(taken_at, (int, float)):
        return None
    weekly = _statusline_window(blob.get("seven_day"))
    if weekly is None:
        return None
    used, end = weekly
    end = end.astimezone(now.tzinfo)  # shown in local time, like the endpoint's
    if end <= now:
        return None  # that week has closed: its reading says nothing now
    session = _statusline_window(blob.get("five_hour"))
    if session is not None and session[1] <= now:
        session = (0.0, None)  # the 5h window lapsed: nothing spent in a new one
    return ClaudeLimits(
        Window(f"{end:%a %H:%M} live", end - _WEEK, end),
        used,
        f"5h {session[0] * 100:.0f}%" if session else "",
        session=(session[0], session[1]) if session else None,
        as_of=datetime.fromtimestamp(taken_at, tz=timezone.utc).astimezone(now.tzinfo),
    )


def _statusline_window(block: Any) -> tuple[float, datetime] | None:
    if not isinstance(block, dict):
        return None
    used = block.get("used_percentage")
    resets = block.get("resets_at")
    if not isinstance(used, (int, float)) or not isinstance(resets, (int, float)):
        return None
    return used / 100.0, datetime.fromtimestamp(resets, tz=timezone.utc)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        blob: Any = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return blob if isinstance(blob, dict) else None


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
