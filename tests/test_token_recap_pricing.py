from __future__ import annotations

import unittest

from token_recap.native import (
    claude_native_usd,
    codex_native_usd,
    grok_native_usd,
)


def one_mtok_out(model: str) -> float:
    """Cost of 1M output tokens on `model`, the cleanest per-model signal."""
    return claude_native_usd(model, 0, 0, 0, 0, 1_000_000)


class NativeRatesTest(unittest.TestCase):
    def test_claude_opus_cache_read_is_fifty_cents_per_million(self) -> None:
        usd = claude_native_usd("claude-opus-5", 0, 0, 0, 1_000_000, 0)
        self.assertAlmostEqual(usd, 0.50)

    def test_grok_200k_cliff_doubles_the_request(self) -> None:
        lo = grok_native_usd("grok-4.6", 199_999, 0, 1_000)
        hi = grok_native_usd("grok-4.6", 200_000, 0, 1_000)
        self.assertGreater(hi, lo * 1.9)

    def test_grok_cliff_is_keyed_on_prompt_size_not_output(self) -> None:
        """A 200k prompt bills its output at the high rate too."""
        out = grok_native_usd("grok-4.6", 200_000, 200_000, 1_000_000)
        self.assertAlmostEqual(out - grok_native_usd("grok-4.6", 200_000, 200_000, 0), 12.0)

    def test_grok_4_5_reads_cache_cheaper_than_4_6(self) -> None:
        """The two share input and output, but not the cached-input rate."""
        # A 1M-token prompt read wholly from cache, so only the read rate bills.
        self.assertAlmostEqual(grok_native_usd("grok-4.5", 1_000_000, 1_000_000, 0), 0.60)
        self.assertAlmostEqual(grok_native_usd("grok-4.6", 1_000_000, 1_000_000, 0), 1.00)
        # Same, under the 200k cliff: 100k cached reads at the low rate.
        self.assertAlmostEqual(grok_native_usd("grok-4.5", 100_000, 100_000, 0), 0.03)
        self.assertAlmostEqual(grok_native_usd("grok-4.6", 100_000, 100_000, 0), 0.05)

    def test_grok_unknown_model_falls_back_to_4_6(self) -> None:
        args = (1_000, 500, 200)
        self.assertAlmostEqual(
            grok_native_usd("grok-9-turbo", *args), grok_native_usd("grok-4.6", *args)
        )


class ClaudePerModelTest(unittest.TestCase):
    def test_each_tier_is_priced_from_its_own_card(self) -> None:
        self.assertAlmostEqual(one_mtok_out("claude-fable-5"), 50.0)
        self.assertAlmostEqual(one_mtok_out("claude-mythos-5"), 50.0)
        self.assertAlmostEqual(one_mtok_out("claude-opus-5"), 25.0)
        self.assertAlmostEqual(one_mtok_out("claude-opus-4-8"), 25.0)
        self.assertAlmostEqual(one_mtok_out("claude-sonnet-5"), 10.0)
        self.assertAlmostEqual(one_mtok_out("claude-haiku-4-5-20251001"), 5.0)

    def test_fable_costs_twice_opus(self) -> None:
        self.assertAlmostEqual(
            one_mtok_out("claude-fable-5"), 2 * one_mtok_out("claude-opus-5")
        )

    def test_cache_rates_are_derived_multiples_of_input(self) -> None:
        write_5m = claude_native_usd("claude-opus-5", 0, 1_000_000, 0, 0, 0)
        write_1h = claude_native_usd("claude-opus-5", 0, 0, 1_000_000, 0, 0)
        read = claude_native_usd("claude-opus-5", 0, 0, 0, 1_000_000, 0)
        self.assertAlmostEqual(write_5m, 6.25)
        self.assertAlmostEqual(write_1h, 10.0)
        self.assertAlmostEqual(read, 0.50)

    def test_sonnet_family_bills_at_the_sonnet_5_card(self) -> None:
        """One rate per family: older Sonnets are not modelled separately."""
        for model in ("claude-sonnet-5", "claude-sonnet-4-5", "claude-3-7-sonnet"):
            self.assertAlmostEqual(one_mtok_out(model), 10.0, msg=model)

    def test_sonnet_5_input_is_two_dollars_per_million(self) -> None:
        self.assertAlmostEqual(claude_native_usd("claude-sonnet-5", 1_000_000, 0, 0, 0, 0), 2.0)

    def test_one_million_window_id_bills_at_the_standard_card(self) -> None:
        """1M context is standard-priced from 4.6 on, so "[1m]" is not a tier."""
        self.assertAlmostEqual(one_mtok_out("claude-opus-5[1m]"), 25.0)
        self.assertAlmostEqual(one_mtok_out("claude-fable-5[1m]"), 50.0)

    def test_non_anthropic_model_under_claude_root_is_not_billed(self) -> None:
        self.assertEqual(one_mtok_out("deepseek-v4-flash"), 0.0)
        self.assertEqual(one_mtok_out("<synthetic>"), 0.0)

    def test_unknown_claude_model_falls_back_to_opus(self) -> None:
        self.assertAlmostEqual(one_mtok_out("claude-something-new"), 25.0)


class CodexPerModelTest(unittest.TestCase):
    def test_each_model_is_priced_from_its_own_card(self) -> None:
        out = 1_000_000
        self.assertAlmostEqual(codex_native_usd("gpt-5.5", 0, 0, 0, out), 30.0)
        self.assertAlmostEqual(codex_native_usd("gpt-5.6-sol", 0, 0, 0, out), 20.0)
        self.assertAlmostEqual(codex_native_usd("gpt-5.4", 0, 0, 0, out), 15.0)
        self.assertAlmostEqual(codex_native_usd("gpt-5.3-codex", 0, 0, 0, out), 14.0)
        self.assertAlmostEqual(
            codex_native_usd("gpt-5.1-codex-max", 0, 0, 0, out), 10.0
        )
        self.assertAlmostEqual(
            codex_native_usd("gpt-5.1-codex-mini", 0, 0, 0, out), 2.0
        )

    def test_gpt_5_5_is_dearer_than_the_codex_flagship(self) -> None:
        args = (1_000_000, 0, 500_000, 200_000)
        self.assertGreater(
            codex_native_usd("gpt-5.5", *args),
            codex_native_usd("gpt-5.3-codex", *args),
        )

    def test_model_with_no_published_rate_falls_back(self) -> None:
        args = (1_000, 0, 500, 200)
        self.assertAlmostEqual(
            codex_native_usd("gpt-5.6-sol-spark", *args),
            codex_native_usd("gpt-5.6-sol", *args),
        )


if __name__ == "__main__":
    unittest.main()
