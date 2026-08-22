from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


MTOK = 1_000_000.0


@dataclass
class Rates:
    name: str
    input: float
    output: float
    cache_read: float
    cache_write: float

    def cost(
        self,
        uncached: int,
        cache_write: int,
        cache_read: int,
        output: int,
    ) -> float:
        return (
            uncached / MTOK * self.input
            + cache_write / MTOK * self.cache_write
            + cache_read / MTOK * self.cache_read
            + output / MTOK * self.output
        )


@dataclass
class TokenBuckets:
    calls: int = 0
    uncached: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    reasoning: int = 0
    native_usd: float = 0.0
    models: Counter[str] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return self.uncached + self.cache_write + self.cache_read + self.output

    def add(
        self,
        *,
        uncached: int = 0,
        cache_write: int = 0,
        cache_read: int = 0,
        output: int = 0,
        reasoning: int = 0,
        native_usd: float = 0.0,
        model: str | None = None,
    ) -> None:
        self.calls += 1
        self.uncached += uncached
        self.cache_write += cache_write
        self.cache_read += cache_read
        self.output += output
        self.reasoning += reasoning
        self.native_usd += native_usd
        if model:
            self.models[model] += 1

    def merge(self, other: TokenBuckets) -> None:
        self.calls += other.calls
        self.uncached += other.uncached
        self.cache_write += other.cache_write
        self.cache_read += other.cache_read
        self.output += other.output
        self.reasoning += other.reasoning
        self.native_usd += other.native_usd
        self.models.update(other.models)


def load_deepseek_rates(path: Path) -> Rates:
    data: dict[str, Any] = json.loads(path.read_text())
    providers = data.get("providers")
    if not isinstance(providers, dict):
        raise ValueError(f"no providers in {path}")
    for provider in providers.values():
        if not isinstance(provider, dict):
            continue
        models = provider.get("models")
        if not isinstance(models, list):
            continue
        for model in models:
            if not isinstance(model, dict):
                continue
            if model.get("id") != "deepseek-v4-flash":
                continue
            cost = model.get("cost")
            if not isinstance(cost, dict):
                raise ValueError("deepseek-v4-flash has no cost table")
            return Rates(
                name=str(model.get("name") or "DeepSeek V4 Flash"),
                input=float(cost["input"]),
                output=float(cost["output"]),
                cache_read=float(cost.get("cacheRead") or 0),
                cache_write=float(cost.get("cacheWrite") or 0),
            )
    raise ValueError(f"deepseek-v4-flash not found in {path}")
