from __future__ import annotations

from collections.abc import Callable

from token_recap.buckets import MTOK, Rates
from token_recap.catalogue import catalogue_rates

# (provider, model) -> a published rate, or None when nobody lists one.
Lookup = Callable[[str, str], Rates | None]

# Anthropic first-party list, platform.claude.com, Aug 2026. (input, output)
# $/MTok per rate tier. Every Claude cache rate is a fixed multiple of the
# input rate, so only the base pair is tabulated. The 1M window is standard
# priced from 4.6 on, so a "[1m]" model id needs no tier of its own.
#
# One rate per family, set to the current model in it. Sonnet is Sonnet 5's
# 2.00/10.00 — announced as an intro rate to 2026-08-31, since made standard —
# which under-states a Sonnet 4.6 or older session (those bill at 3.00/15.00).
_CLAUDE: dict[str, tuple[float, float]] = {
    "fable": (10.0, 50.0),  # also Mythos 5, same card
    "opus": (5.0, 25.0),
    "sonnet": (2.0, 10.0),
    "haiku": (1.0, 5.0),
}
# A model priced off its family's card, matched by a part of its id. Claude
# Opus 5.5 succeeded Opus 5 at a lower price, while Opus 5 is still served at
# the family's.
_CLAUDE_MODEL: dict[str, tuple[float, float]] = {
    "opus-5-5": (4.0, 20.0),
}
_WRITE_5M = 1.25
_WRITE_1H = 2.00
_CACHE_READ = 0.10
# Cache reads off the ordinary 0.1x: Claude Fable 5.1 reads at 0.025x base
# input ($0.25/MTok), Opus 5.5 at 0.05x ($0.20/MTok). Mythos 5.1 was
# unconfirmed at launch, so it keeps the ordinary multiple.
_CACHE_READ_MODEL: dict[str, float] = {
    "fable-5-1": 0.025,
    "opus-5-5": 0.05,
}

# Re-exported for callers that need to derive a rate card rather than a cost.
CLAUDE_WRITE_5M = _WRITE_5M
CLAUDE_WRITE_1H = _WRITE_1H
CLAUDE_CACHE_READ = _CACHE_READ

# xAI list, docs.x.ai, Aug 2026. (input, cache read, output) $/MTok, at the
# <200k and the ≥200k rate. A prompt of 200k or more — cached tokens counted —
# bills every bucket of that request, output included, at the ≥200k row.
# 4.5 and 4.6 share input and output but not the cached-input rate. Writes are
# free on every card: xAI publishes no cache-write line.
_GROK: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    "grok-4.6": ((2.0, 0.50, 6.0), (4.0, 1.00, 12.0)),
    "grok-4.5": ((2.0, 0.30, 6.0), (4.0, 0.60, 12.0)),
    "grok-build-0.1": ((1.0, 0.20, 2.0), (2.0, 0.40, 4.0)),
}
GROK_FALLBACK = "grok-4.6"
_GROK_LONG_CONTEXT = 200_000

# OpenAI first-party list, developers.openai.com/api/docs/pricing, Aug 2026.
# (input, cache read, cache write, output) $/MTok. GPT-5.6 charges a premium
# for a cache write; every earlier model bills a write as ordinary input, so
# its write rate is just its input rate.
_CODEX: dict[str, tuple[float, float, float, float]] = {
    "gpt-5.6-sol": (4.00, 0.40, 5.00, 20.00),
    "gpt-5.6": (4.00, 0.40, 5.00, 20.00),
    "gpt-5.6-terra": (2.00, 0.20, 2.50, 12.00),
    "gpt-5.6-luna": (0.20, 0.02, 0.25, 1.20),
    "gpt-5.5": (5.00, 0.50, 5.00, 30.00),
    "gpt-5.4": (2.50, 0.25, 2.50, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 0.75, 4.50),
    "gpt-5.3-codex": (1.75, 0.175, 1.75, 14.00),
    "gpt-5.2": (1.75, 0.175, 1.75, 14.00),
    "gpt-5.2-codex": (1.75, 0.175, 1.75, 14.00),
    "gpt-5.1-codex-max": (1.25, 0.125, 1.25, 10.00),
    "gpt-5.1-codex-mini": (0.25, 0.025, 0.25, 2.00),
    "gpt-5.1-codex": (1.25, 0.125, 1.25, 10.00),
    "gpt-5-codex": (1.25, 0.125, 1.25, 10.00),
    "gpt-5": (1.25, 0.125, 1.25, 10.00),
}

# Research previews and internal aliases (gpt-5.3-codex-spark, codex-auto-review,
# gpt-reserve) have no published rate. Bill them at the current Codex default —
# an unnamed model is likelier current than old — and let the model breakdown
# show the real name. This row is an estimate, not a quoted rate.
CODEX_FALLBACK = "gpt-5.6-sol"


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


def claude_cache_read(model: str) -> float:
    """What one cached input token costs this model, as a multiple of input."""
    name = model.lower()
    return next((rate for part, rate in _CACHE_READ_MODEL.items() if part in name), _CACHE_READ)


def claude_card(model: str) -> tuple[float, float] | None:
    """The (input, output) $/MTok pair for a model: its own card, else its family's."""
    family = claude_family(model)
    if family is None:
        return None
    name = model.lower()
    return next((card for part, card in _CLAUDE_MODEL.items() if part in name), _CLAUDE[family])


def claude_native_usd(
    model: str,
    uncached: int,
    write_5m: int,
    write_1h: int,
    cache_read: int,
    output: int,
) -> float:
    card = claude_card(model)
    if card is None:
        return 0.0
    inp, out = card
    return (
        uncached / MTOK * inp
        + write_5m / MTOK * inp * _WRITE_5M
        + write_1h / MTOK * inp * _WRITE_1H
        + cache_read / MTOK * inp * claude_cache_read(model)
        + output / MTOK * out
    )


def knows_grok(model: str) -> bool:
    """Whether we hold an xAI card for this model, rather than a fallback."""
    return model.lower().strip() in _GROK


def knows_codex(model: str) -> bool:
    """Whether we hold an OpenAI card for this model, rather than a fallback."""
    return model.lower().strip() in _CODEX


def grok_rates(model: str, prompt: int) -> Rates:
    key = model.lower().strip()
    lo, hi = _GROK.get(key, _GROK[GROK_FALLBACK])
    long_context = prompt >= _GROK_LONG_CONTEXT
    inp, cache_read, out = hi if long_context else lo
    tier = "≥200k" if long_context else "<200k"
    return Rates(
        f"{key} {tier}", input=inp, output=out, cache_read=cache_read, cache_write=0.0
    )


def grok_native_usd(model: str, prompt: int, cached: int, output: int) -> float:
    uncached = prompt - cached if prompt >= cached else 0
    return grok_rates(model, prompt).cost(uncached, 0, cached, output)


def codex_rates(model: str, lookup: Lookup = catalogue_rates) -> Rates:
    """Our card, else OpenAI's catalogue entry, else the current default.

    A model newer than the card would otherwise bill at the fallback while
    its row shows the catalogue's rates, so the total would contradict them.
    """
    key = model.lower().strip()
    card = _CODEX.get(key)
    if card is None:
        listed = lookup("openai", key)
        if listed is not None:
            return listed
        card = _CODEX[CODEX_FALLBACK]
    inp, cread, cwrite, out = card
    return Rates(model, input=inp, output=out, cache_read=cread, cache_write=cwrite)


def codex_native_usd(
    model: str,
    uncached: int,
    cache_write: int,
    cache_read: int,
    output: int,
    lookup: Lookup = catalogue_rates,
) -> float:
    return codex_rates(model, lookup).cost(uncached, cache_write, cache_read, output)
