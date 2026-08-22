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
    grok_native_usd,
)


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


if __name__ == "__main__":
    unittest.main()
