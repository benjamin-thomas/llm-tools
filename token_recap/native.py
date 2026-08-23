from __future__ import annotations

from token_recap.buckets import MTOK, Rates

# Anthropic first-party list, platform.claude.com, Aug 2026. (input, output)
# $/MTok per rate tier. Every Claude cache rate is a fixed multiple of the
# input rate, so only the base pair is tabulated.
#
# Every Sonnet bills at the 3.00/15.00 list. Sonnet 5 has a 2.00/10.00 intro
# rate that lapses 2026-08-31; pricing it at list keeps one dated constant out
# of the table, at the cost of over-stating Sonnet 5 until then.
_CLAUDE: dict[str, tuple[float, float]] = {
    "fable": (10.0, 50.0),  # also Mythos 5, same card
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}
_WRITE_5M = 1.25
_WRITE_1H = 2.00
_CACHE_READ = 0.10

# xAI list, docs.x.ai, Aug 2026. grok-4.5 and grok-4.6 share one rate card.
# Prompt >= 200k doubles every bucket on that request.
_GROK_LO = Rates("grok <200k", input=2.0, output=6.0, cache_read=0.50, cache_write=0.0)
_GROK_HI = Rates("grok ≥200k", input=4.0, output=12.0, cache_read=1.00, cache_write=0.0)

# OpenAI first-party list, developers.openai.com/api/docs/pricing, Aug 2026.
# (input, cache read, output) $/MTok. Cache writes bill as ordinary input, so
# there is no separate write line.
_CODEX: dict[str, tuple[float, float, float]] = {
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.3-codex": (1.75, 0.175, 14.00),
    "gpt-5.2": (1.75, 0.175, 14.00),
    "gpt-5.2-codex": (1.75, 0.175, 14.00),
    "gpt-5.1-codex-max": (1.25, 0.125, 10.00),
    "gpt-5.1-codex-mini": (0.25, 0.025, 2.00),
    "gpt-5-codex": (1.25, 0.125, 10.00),
}

# Research previews and internal aliases have no published rate. Bill them at
# the current Codex default, on the grounds that an unnamed model is more
# likely current than old, and let the model breakdown show the real name.
CODEX_FALLBACK = "gpt-5.6-sol"

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
    if "fable" in name or "mythos" in name:
        return "fable"
    if "haiku" in name:
        return "haiku"
    if "sonnet" in name:
        return "sonnet"
    if "opus" in name:
        return "opus"
    if "claude" not in name:
        return None  # a non-Anthropic model logged under ~/.claude
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
    inp, out = _CLAUDE[family]
    return (
        uncached / MTOK * inp
        + write_5m / MTOK * inp * _WRITE_5M
        + write_1h / MTOK * inp * _WRITE_1H
        + cache_read / MTOK * inp * _CACHE_READ
        + output / MTOK * out
    )


def grok_native_usd(prompt: int, cached: int, output: int) -> float:
    rates = _GROK_HI if prompt >= 200_000 else _GROK_LO
    uncached = prompt - cached if prompt >= cached else 0
    return rates.cost(uncached, 0, cached, output)


def codex_rates(model: str) -> Rates:
    key = model.lower().strip()
    inp, cread, out = _CODEX.get(key, _CODEX[CODEX_FALLBACK])
    return Rates(model, input=inp, output=out, cache_read=cread, cache_write=0.0)


def codex_native_usd(
    model: str, uncached: int, cache_write: int, cache_read: int, output: int
) -> float:
    return codex_rates(model).cost(uncached, cache_write, cache_read, output)
