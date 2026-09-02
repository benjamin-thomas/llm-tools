from __future__ import annotations

from dataclasses import dataclass, field


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
class ModelUsage:
    """One model's share of a window: the same buckets, plus its call count."""

    calls: int = 0
    uncached: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    reasoning: int = 0
    native_usd: float = 0.0
    # Kept so a rate can be looked up later: the same model id costs different
    # money depending on who served it.
    model_id: str = ""
    provider: str = ""

    @property
    def tokens(self) -> int:
        return self.uncached + self.cache_write + self.cache_read + self.output

    @property
    def cached(self) -> int:
        """Reads and writes together: both are cache traffic, priced apart."""
        return self.cache_read + self.cache_write

    def merge(self, other: ModelUsage) -> None:
        self.model_id = self.model_id or other.model_id
        self.provider = self.provider or other.provider
        self.calls += other.calls
        self.uncached += other.uncached
        self.cache_write += other.cache_write
        self.cache_read += other.cache_read
        self.output += other.output
        self.native_usd += other.native_usd


@dataclass
class TokenBuckets:
    calls: int = 0
    uncached: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    reasoning: int = 0
    native_usd: float = 0.0
    models: dict[str, ModelUsage] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.uncached + self.cache_write + self.cache_read + self.output

    def by_cost(self) -> list[tuple[str, ModelUsage]]:
        """Dearest first — the ordering that answers "what is costing me"."""
        return sorted(
            self.models.items(),
            key=lambda kv: (-kv[1].native_usd, -kv[1].output, kv[0]),
        )

    def by_output(self) -> list[tuple[str, ModelUsage]]:
        """Busiest first, for a section where nothing has a price to sort by.

        Output is the closest stand-in for work done: input is mostly the same
        context resent each turn, and calls only measure how finely a harness
        chops a task up.
        """
        return sorted(
            self.models.items(),
            key=lambda kv: (-kv[1].output, -kv[1].calls, kv[0]),
        )

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
        model_id: str | None = None,
        provider: str = "",
    ) -> None:
        self.calls += 1
        self.uncached += uncached
        self.cache_write += cache_write
        self.cache_read += cache_read
        self.output += output
        self.reasoning += reasoning
        self.native_usd += native_usd
        if model:
            self.models.setdefault(model, ModelUsage()).merge(
                ModelUsage(
                    model_id=model_id or model,
                    provider=provider,
                    calls=1,
                    uncached=uncached,
                    cache_write=cache_write,
                    cache_read=cache_read,
                    output=output,
                    reasoning=reasoning,
                    native_usd=native_usd,
                )
            )

    def merge(self, other: TokenBuckets) -> None:
        self.calls += other.calls
        self.uncached += other.uncached
        self.cache_write += other.cache_write
        self.cache_read += other.cache_read
        self.output += other.output
        self.reasoning += other.reasoning
        self.native_usd += other.native_usd
        for name, usage in other.models.items():
            self.models.setdefault(name, ModelUsage()).merge(usage)
