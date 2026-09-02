"""Shared rules for harnesses that are not tied to one provider.

Claude Code, the Grok CLI and Codex each speak to one provider, so a section
per tool and a section per provider are the same thing. opencode and pi do not:
one pi session can call xAI, OpenAI and Anthropic in turn. What matters for a
quota is not which tool made the call but which plan paid for it, so a call is
filed under the subscription that carried it, and everything else lands in a
pay-per-token section of its own.
"""

from __future__ import annotations

from token_recap.buckets import Rates, TokenBuckets
from token_recap.catalogue import catalogue_rates
from token_recap.native import (
    CLAUDE_WRITE_5M,
    claude_cache_read,
    claude_card,
    claude_family,
    claude_native_usd,
    codex_native_usd,
    codex_rates,
    grok_native_usd,
    grok_rates,
    knows_codex,
    knows_grok,
)

# The section a pay-per-token call goes to: no plan quota, real money instead.
API_SECTION = "api"

# ... unless it priced to nothing. Free tiers and promotional models would
# otherwise pad the paid section with rows that cost nothing, which is exactly
# the comparison the paid section exists to make.
FREE_SECTION = "free"

# Ollama's hosted tier is a subscription of its own, so it neither competes
# for a pay-per-token budget nor belongs with the free tiers. It publishes no
# meter and no rate card, so the card carries counts only.
OLLAMA_SECTION = "ollama"

# Charm Hyper is a subscription too, and gets a card of its own for the same
# reason. Unlike Ollama it does publish a rate card, so its rows keep their
# price columns: no money changes hands per call, but the list rate is what
# says whether the flat fee is earning its keep.
HYPER_SECTION = "hyper"

# The sections whose rows are named for the provider that served the call
# rather than the harness that placed it — see harness_label.
PROVIDER_NAMED = (API_SECTION, FREE_SECTION, OLLAMA_SECTION)


def harness_label(provider: str, model: str, harness: str, section: str) -> str:
    """How a harness call is named in the breakdown.

    Which half of the name earns its place depends on the section. Under a
    plan, the harness matters: it says which tool is eating that quota. Under
    pay-per-token the harness is just trivia — the bill does not care which
    tool placed the call — while the provider decides the price, and two of
    them serve the same model id at different rates.

    Ollama sits with the paid sections despite being a plan, because its local
    and hosted ids share one card and only the provider tells them apart.
    Hyper is one provider id, so its rows name the harness like any other plan.
    """
    if section in PROVIDER_NAMED:
        return f"{provider}/{model}" if provider else model
    return f"{model} ({harness})"


def split_free(buckets: TokenBuckets) -> tuple[TokenBuckets, TokenBuckets]:
    """Divide pay-per-token work into what charged and what did not.

    The verdict is per model rather than per call. Deciding call by call puts
    the same model in both boxes: an aborted call spends nothing whatever the
    model charges, and a free model still logs those. A model's own total says
    plainly enough whether it billed anything over the window.
    """
    paid, free = TokenBuckets(), TokenBuckets()
    for name, use in buckets.models.items():
        into = free if use.native_usd <= 0 and use.tokens > 0 else paid
        into.calls += use.calls
        into.uncached += use.uncached
        into.cache_write += use.cache_write
        into.cache_read += use.cache_read
        into.output += use.output
        into.reasoning += use.reasoning
        into.native_usd += use.native_usd
        into.models[name] = use
    return paid, free

# Which provider id rides which subscription, per harness. Neither harness
# persists an auth mode with a request, so this is inferred from the current
# credential store and cannot be proven for an old record — treat a section as
# where the quota most likely went, not as an audited fact.
#
# The two maps differ on "openai" and must not be merged: pi names the Codex
# plan's OAuth separately, so a bare "openai" there is an API key, while
# opencode has one id per vendor and decides auth at request time, so its
# "openai" is the ChatGPT OAuth this account signs in with.
PI_SECTIONS: dict[str, str] = {
    "openai-codex": "codex",
    "xai": "grok",
    "ollama": OLLAMA_SECTION,
    "ollama-cloud": OLLAMA_SECTION,
    "hyper": HYPER_SECTION,
}
OPENCODE_SECTIONS: dict[str, str] = {
    "openai": "codex",
    "xai": "grok",
    "ollama": OLLAMA_SECTION,
    "ollama-cloud": OLLAMA_SECTION,
    "hyper": HYPER_SECTION,
}


def section_for(provider: str, sections: dict[str, str]) -> str:
    return sections.get(provider.lower().strip(), API_SECTION)


def harness_usd(
    model: str,
    uncached: int,
    cache_write: int,
    cache_read: int,
    output: int,
    logged_usd: float,
    write_1h: int = 0,
) -> float:
    """List-price one harness call, preferring our own cards.

    A harness reaches models we hold no card for, but it records what it
    believes the call cost. Use our card where we have one so every provider is
    priced the same way, and fall back to the harness's own figure otherwise.
    """
    name = model.lower().strip()
    if claude_family(name) is not None:
        # Our Claude card prices a 1h write at 2× input and a 5m one at 1.25×.
        # A harness that records the 1h subset gets that split; one that does
        # not bills every write at the cheaper 5m rate rather than guess.
        long_write = min(write_1h, cache_write)
        return claude_native_usd(
            model, uncached, cache_write - long_write, long_write, cache_read, output
        )
    if knows_grok(name):
        # The ≥200k cliff keys on the whole prompt, cached tokens included.
        return grok_native_usd(model, uncached + cache_read, cache_read, output)
    if knows_codex(name):
        return codex_native_usd(model, uncached, cache_write, cache_read, output)
    return logged_usd


def display_rates(model: str, provider: str) -> Rates | None:
    """The provider's list price per MTok, for showing beside the counts.

    Our own card wins, but only for the provider that actually sets that price:
    the same model id routed through a reseller is a different rate. Grok is
    shown at its <200k tier, the one almost every request bills at.
    """
    name = model.lower().strip()
    who = provider.lower().strip()
    if who in ("", "anthropic") and claude_family(name) is not None:
        card = claude_card(name)
        if card is not None:
            inp, out = card
            return Rates(
                model,
                input=inp,
                output=out,
                cache_read=inp * claude_cache_read(name),
                cache_write=inp * CLAUDE_WRITE_5M,
            )
    if who in ("", "xai") and knows_grok(name):
        return grok_rates(name, 0)
    if who in ("", "openai", "openai-codex") and knows_codex(name):
        return codex_rates(name)
    return catalogue_rates(who, name)
