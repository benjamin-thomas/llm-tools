from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from token_recap.buckets import TokenBuckets, load_deepseek_rates
from token_recap.native import (
    DEEPSEEK_COM_FLASH_OFFPEAK,
    RUNINFRA_PRO,
    claude_native_usd,
    codex_native_usd,
    grok_native_usd,
)


def one_mtok_out(model: str) -> float:
    """Cost of 1M output tokens on `model`, the cleanest per-model signal."""
    return claude_native_usd(model, 0, 0, 0, 0, 1_000_000)


class DeepSeekRatesTest(unittest.TestCase):
    def test_loads_runinfra_deepseek_v4_flash(self) -> None:
        payload = {
            "providers": {
                "runinfra": {
                    "models": [
                        {
                            "id": "deepseek-v4-flash",
                            "name": "DeepSeek V4 Flash (RunInfra)",
                            "cost": {
                                "input": 0.13,
                                "output": 0.27,
                                "cacheRead": 0.01,
                                "cacheWrite": 0,
                            },
                        }
                    ]
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.json"
            path.write_text(json.dumps(payload))
            rates = load_deepseek_rates(path)
        self.assertEqual(rates.input, 0.13)
        self.assertEqual(rates.output, 0.27)
        self.assertEqual(rates.cache_read, 0.01)
        self.assertEqual(rates.cache_write, 0.0)
        # 1M uncached + 2M cache read + 1M output
        self.assertAlmostEqual(rates.cost(1_000_000, 0, 2_000_000, 1_000_000), 0.42)

    def test_cache_write_zero_means_writes_are_free(self) -> None:
        payload = {
            "providers": {
                "runinfra": {
                    "models": [
                        {
                            "id": "deepseek-v4-flash",
                            "name": "DeepSeek V4 Flash (RunInfra)",
                            "cost": {
                                "input": 0.13,
                                "output": 0.27,
                                "cacheRead": 0.01,
                                "cacheWrite": 0,
                            },
                        }
                    ]
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.json"
            path.write_text(json.dumps(payload))
            rates = load_deepseek_rates(path)
        buckets = TokenBuckets()
        buckets.add(uncached=0, cache_write=3_000_000, cache_read=0, output=0)
        self.assertEqual(rates.cost(0, buckets.cache_write, 0, 0), 0.0)

    def test_claude_opus_cache_read_is_fifty_cents_per_million(self) -> None:
        usd = claude_native_usd("claude-opus-5", 0, 0, 0, 1_000_000, 0)
        self.assertAlmostEqual(usd, 0.50)

    def test_official_flash_cache_hit_is_sub_cent_per_million(self) -> None:
        usd = DEEPSEEK_COM_FLASH_OFFPEAK.cost(0, 0, 1_000_000, 0)
        self.assertAlmostEqual(usd, 0.007)

    def test_runinfra_pro_matches_user_card(self) -> None:
        hit = RUNINFRA_PRO.cost(0, 0, 1_000_000, 0)
        miss = RUNINFRA_PRO.cost(1_000_000, 0, 0, 0)
        out = RUNINFRA_PRO.cost(0, 0, 0, 1_000_000)
        self.assertAlmostEqual(hit, 0.03)
        self.assertAlmostEqual(miss, 0.60)
        self.assertAlmostEqual(out, 1.90)

    def test_token_mult_scales_cost_linearly(self) -> None:
        base = DEEPSEEK_COM_FLASH_OFFPEAK.cost(10, 20, 30, 40)
        self.assertAlmostEqual(base * 2.5, DEEPSEEK_COM_FLASH_OFFPEAK.cost(25, 50, 75, 100))

    def test_grok_200k_cliff_doubles_the_request(self) -> None:
        lo = grok_native_usd(199_999, 0, 1_000)
        hi = grok_native_usd(200_000, 0, 1_000)
        self.assertGreater(hi, lo * 1.9)


class ClaudePerModelTest(unittest.TestCase):
    def test_each_tier_is_priced_from_its_own_card(self) -> None:
        self.assertAlmostEqual(one_mtok_out("claude-fable-5"), 50.0)
        self.assertAlmostEqual(one_mtok_out("claude-mythos-5"), 50.0)
        self.assertAlmostEqual(one_mtok_out("claude-opus-5"), 25.0)
        self.assertAlmostEqual(one_mtok_out("claude-opus-4-8"), 25.0)
        self.assertAlmostEqual(one_mtok_out("claude-sonnet-5"), 15.0)
        self.assertAlmostEqual(one_mtok_out("claude-sonnet-4-6"), 15.0)
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

    def test_every_sonnet_bills_at_the_list_rate(self) -> None:
        """The Sonnet 5 intro rate is not modelled, so no Sonnet is special."""
        for model in ("claude-sonnet-5", "claude-sonnet-4-5", "claude-3-7-sonnet"):
            self.assertAlmostEqual(one_mtok_out(model), 15.0, msg=model)

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
