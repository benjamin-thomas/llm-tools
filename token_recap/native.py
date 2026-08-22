from __future__ import annotations

from token_recap.buckets import MTOK, Rates

# Anthropic first-party list, platform.claude.com, Aug 2026.
# (input, 5m write, 1h write, cache read, output) $/MTok
_CLAUDE: dict[str, tuple[float, float, float, float, float]] = {
    "fable": (10.0, 12.50, 20.0, 1.00, 50.0),
    "opus": (5.0, 6.25, 10.0, 0.50, 25.0),
    "sonnet": (2.0, 2.50, 4.0, 0.20, 10.0),
    "haiku": (1.0, 1.25, 2.0, 0.10, 5.0),
}

# xAI grok-4.6. Prompt >= 200k doubles every bucket on that request.
_GROK_LO = Rates("grok-4.6 <200k", input=2.0, output=6.0, cache_read=0.50, cache_write=0.0)
_GROK_HI = Rates("grok-4.6 ≥200k", input=4.0, output=12.0, cache_read=1.00, cache_write=0.0)

# OpenAI gpt-5.3-codex (Codex CLI default on the public price table).
_CODEX = Rates("gpt-5.3-codex", input=1.75, output=14.0, cache_read=0.175, cache_write=0.0)

# First-party api.deepseek.com, V4 Flash, off-peak. Peak hours are 2×.
# Cache writes are billed as cache-miss input.
DEEPSEEK_COM_FLASH_OFFPEAK = Rates(
    name="DeepSeek.com Flash",
    input=0.22,
    output=0.66,
    cache_read=0.007,
    cache_write=0.22,
)

# RunInfra hosted DeepSeek V4 Pro. No write line: new tokens billed as input.
RUNINFRA_PRO = Rates(
    name="RunInfra Pro",
    input=0.60,
    output=1.90,
    cache_read=0.03,
    cache_write=0.60,
)


def claude_family(model: str) -> str | None:
    name = model.lower()
    if "synthetic" in name:
        return None
    if "fable" in name:
        return "fable"
    if "haiku" in name:
        return "haiku"
    if "sonnet" in name:
        return "sonnet"
    if "opus" in name:
        return "opus"
    return "opus"


def claude_native_usd(
    model: str,
    uncached: int,
    write_5m: int,
    write_1h: int,
    cache_read: int,
    output: int,
) -> float:
    family = claude_family(model)
    if family is None:
        return 0.0
    inp, w5, w1h, cread, out = _CLAUDE[family]
    return (
        uncached / MTOK * inp
        + write_5m / MTOK * w5
        + write_1h / MTOK * w1h
        + cache_read / MTOK * cread
        + output / MTOK * out
    )


def grok_native_usd(prompt: int, cached: int, output: int) -> float:
    rates = _GROK_HI if prompt >= 200_000 else _GROK_LO
    uncached = prompt - cached if prompt >= cached else 0
    return rates.cost(uncached, 0, cached, output)


def codex_native_usd(
    uncached: int, cache_write: int, cache_read: int, output: int
) -> float:
    return _CODEX.cost(uncached, cache_write, cache_read, output)
