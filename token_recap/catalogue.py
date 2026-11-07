"""models.dev rates, read from the copy opencode already keeps on disk.

Our own cards cover the three first-party providers and are authoritative for
them — spot-checked against this catalogue, they agree to the cent. What they
do not cover is the tail a harness reaches through a router or a coding plan,
which is what this is for.

Rates must be looked up by (provider, model), never by model alone: 200-odd
providers resell the same model ids at their own margins, so an unqualified
lookup for `claude-opus-5` returns a reseller's $5.50, not Anthropic's $5.00.

opencode refreshes its copy only when it runs on this machine (a sandboxed
opencode writes elsewhere), so we keep our own as well, fetched again once a
day, and read whichever of the two is newer.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.request
from collections.abc import Callable, Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

from token_recap.buckets import Rates

CACHE = Path.home() / ".cache" / "opencode" / "models.json"
# Our own copy, so prices follow models.dev whether opencode runs or not.
OWN = Path.home() / ".cache" / "token-recap" / "models.json"
URL = "https://models.dev/api.json"
MAX_AGE = 86400.0
# pi's own catalogue: a small override file naming what the user actually pays
# on providers models.dev does not list, or lists at a different rate.
PI_MODELS = Path.home() / ".pi" / "agent" / "models.json"


@lru_cache(maxsize=1)
def _catalogue(path: str) -> dict[tuple[str, str], Rates]:
    try:
        blob: Any = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(blob, dict):
        return {}
    out: dict[tuple[str, str], Rates] = {}
    for provider, entry in blob.items():
        if not isinstance(entry, dict):
            continue
        models = entry.get("models")
        if not isinstance(models, dict):
            continue
        for model, spec in models.items():
            if not isinstance(spec, dict):
                continue
            rates = _rates(str(model), spec.get("cost"))
            if rates is not None:
                out[(str(provider).lower(), str(model).lower())] = rates
    return out


def _rates(name: str, cost: Any) -> Rates | None:
    if not isinstance(cost, dict):
        return None
    inp = _num(cost.get("input"))
    out = _num(cost.get("output"))
    if inp is None or out is None:
        return None
    read = _num(cost.get("cache_read"))
    write = _num(cost.get("cache_write"))
    return Rates(
        name,
        input=inp,
        output=out,
        cache_read=read if read is not None else 0.0,
        # No write line means a write bills as ordinary input, as it does for
        # every provider we hold a first-party card for.
        cache_write=write if write is not None else inp,
    )


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


@lru_cache(maxsize=1)
def _overrides(path: str) -> dict[tuple[str, str], Rates]:
    """pi's models.json: providers -> models list, each with its own cost."""
    try:
        blob: Any = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    providers = blob.get("providers") if isinstance(blob, dict) else None
    if not isinstance(providers, dict):
        return {}
    out: dict[tuple[str, str], Rates] = {}
    for provider, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        for spec in entry.get("models") or []:
            if not isinstance(spec, dict):
                continue
            model = spec.get("id")
            cost = spec.get("cost")
            if not isinstance(model, str) or not isinstance(cost, dict):
                continue
            rates = _rates(
                model,
                {
                    "input": cost.get("input"),
                    "output": cost.get("output"),
                    "cache_read": cost.get("cacheRead"),
                    "cache_write": cost.get("cacheWrite"),
                },
            )
            if rates is not None:
                out[(str(provider).lower(), model.lower())] = rates
    return out


def catalogue_rates(
    provider: str,
    model: str,
    path: Path | None = None,
    overrides: Path = PI_MODELS,
) -> Rates | None:
    """A published rate for one provider's copy of a model.

    The user's own override wins: where they have set a price for a provider,
    that is what they are actually charged, whatever a public catalogue says.
    """
    key = (provider.lower().strip(), model.lower().strip())
    path = path or newest_catalogue()
    return _overrides(str(overrides)).get(key) or _catalogue(str(path)).get(key)


def newest_catalogue(candidates: Iterable[Path] = (OWN, CACHE)) -> Path:
    """The copy of models.dev fetched last, ours or opencode's."""
    paths = list(candidates)
    dated: list[tuple[float, Path]] = []
    for path in paths:
        try:
            dated.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return max(dated)[1] if dated else paths[-1]


def _download() -> bytes:
    request = urllib.request.Request(URL, headers={"User-Agent": "token-recap"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read()


def refresh_catalogue(
    path: Path = OWN,
    fetch: Callable[[], bytes] = _download,
    now: Callable[[], float] = time.time,
    max_age: float = MAX_AGE,
) -> bool:
    """Fetch models.dev again when our copy is a day old; whether it was.

    Offline, or on an answer that is not a catalogue, the old copy stays.
    """
    try:
        if now() - path.stat().st_mtime < max_age:
            return False
    except OSError:
        pass  # no copy yet
    try:
        data = fetch()
        blob: Any = json.loads(data)
    except (OSError, ValueError, http.client.HTTPException):
        return False
    if not isinstance(blob, dict) or not blob:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.write_bytes(data)
    partial.replace(path)
    _catalogue.cache_clear()
    return True
