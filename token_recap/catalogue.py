"""models.dev rates, read from the copy opencode already keeps on disk.

Our own cards cover the three first-party providers and are authoritative for
them — spot-checked against this catalogue, they agree to the cent. What they
do not cover is the tail a harness reaches through a router or a coding plan,
which is what this is for.

Rates must be looked up by (provider, model), never by model alone: 200-odd
providers resell the same model ids at their own margins, so an unqualified
lookup for `claude-opus-5` returns a reseller's $5.50, not Anthropic's $5.00.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from token_recap.buckets import Rates

CACHE = Path.home() / ".cache" / "opencode" / "models.json"
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
    path: Path = CACHE,
    overrides: Path = PI_MODELS,
) -> Rates | None:
    """A published rate for one provider's copy of a model.

    The user's own override wins: where they have set a price for a provider,
    that is what they are actually charged, whatever a public catalogue says.
    """
    key = (provider.lower().strip(), model.lower().strip())
    return _overrides(str(overrides)).get(key) or _catalogue(str(path)).get(key)
