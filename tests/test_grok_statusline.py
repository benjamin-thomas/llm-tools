"""Grok's status line keeps its own rate card and paints from stdin + a cache.

A render cannot afford to start Python, so --rates is the published card and
native.py is the source of truth. The rest of the tests drive the script as
Grok will: JSON on stdin, env-injected cache/auth/log so they never hit the
network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from token_recap.native import GROK_FALLBACK, grok_rates


def api_usd(model: str, prompt: int, uncached: int, cache_read: int, output: int) -> float:
    """List price using the live window to pick the 200k row, writes free."""
    return grok_rates(model, prompt).cost(uncached, 0, cache_read, output)

STATUSLINE = Path(__file__).resolve().parent.parent / "grok-cli" / "statusline.rb"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

NOW = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)
NOW_TS = int(NOW.timestamp())
# Distinct from the live SuperGrok reading (~9%) so a stray network call fails.
USED_PERCENT = 42.0
RESETS = NOW + timedelta(days=3, hours=4)

PAYLOAD: dict[str, Any] = {
    "trigger": "state",
    "model": {"id": "grok-4.6", "display_name": "Grok 4.6"},
    "effort": {"level": "xhigh"},
    "cost": {"total_cost_usd": 1.24},
    "context_window": {
        "remaining_percentage": 73,
        "used_percentage": 27,
        "context_tokens": 135_000,
        "context_window_size": 500_000,
        "auto_compact_threshold_percent": 80,
        "session_input_tokens": 412_000,
        "session_output_tokens": 18_000,
        "session_usage": {
            "input_tokens": 20_000,
            "cache_creation_input_tokens": 5_000,
            "cache_read_input_tokens": 387_000,
            "output_tokens": 18_000,
        },
    },
}


def plain(text: str) -> str:
    return _ANSI.sub("", text)


def published_card() -> dict[str, dict[str, float]]:
    out = subprocess.run(
        ["ruby", str(STATUSLINE), "--rates"],
        capture_output=True,
        text=True,
        check=True,
    )
    data: Any = json.loads(out.stdout)
    assert isinstance(data, dict)
    return data


def render(
    payload: dict[str, Any],
    tmp: str,
    cache: dict[str, Any] | None = None,
    log_rows: list[dict[str, Any]] | None = None,
    now: int = NOW_TS,
) -> str:
    cache_path = Path(tmp) / "billing.json"
    auth_path = Path(tmp) / "auth.json"
    log_path = Path(tmp) / "unified.jsonl"
    auth_path.write_text("{}")
    if cache is not None:
        cache_path.write_text(json.dumps(cache))
    if log_rows is not None:
        log_path.write_text("".join(json.dumps(row) + "\n" for row in log_rows))
    env = os.environ.copy()
    env["GROK_STATUSLINE_CACHE"] = str(cache_path)
    env["GROK_STATUSLINE_AUTH"] = str(auth_path)
    env["GROK_STATUSLINE_LOG"] = str(log_path)
    env["GROK_STATUSLINE_NOW"] = str(now)
    out = subprocess.run(
        ["ruby", str(STATUSLINE)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return out.stdout


def billing_cache(**overrides: Any) -> dict[str, Any]:
    blob: dict[str, Any] = {
        "fetched_at": NOW_TS - 10,
        "used_percent": USED_PERCENT,
        "resets_at": int(RESETS.timestamp()),
    }
    blob.update(overrides)
    return blob


def log_row(used: float, end: str, start: str) -> dict[str, Any]:
    return {
        "ts": "2026-09-02T19:00:00.000Z",
        "msg": "billing: fetched credits config",
        "ctx": {
            "subscriptionTier": "SuperGrok",
            "config": {
                "creditUsagePercent": used,
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "start": start,
                    "end": end,
                },
            },
        },
    }


@unittest.skipUnless(shutil.which("ruby"), "ruby not installed")
class StatuslineRateCardTest(unittest.TestCase):
    def test_the_script_publishes_a_card(self) -> None:
        self.assertTrue(STATUSLINE.exists(), f"missing {STATUSLINE}")
        self.assertTrue(published_card(), "--rates published nothing")

    def test_every_published_rate_matches_native(self) -> None:
        card = published_card()
        for model, tiers in card.items():
            lo = grok_rates(model, 1)
            hi = grok_rates(model, 200_000)
            with self.subTest(model=model):
                self.assertEqual(tiers["<200k"]["input"], lo.input)
                self.assertEqual(tiers["<200k"]["cache_read"], lo.cache_read)
                self.assertEqual(tiers["<200k"]["output"], lo.output)
                self.assertEqual(tiers[">=200k"]["input"], hi.input)
                self.assertEqual(tiers[">=200k"]["cache_read"], hi.cache_read)
                self.assertEqual(tiers[">=200k"]["output"], hi.output)

    def test_the_fallback_model_is_on_the_card(self) -> None:
        self.assertIn(GROK_FALLBACK, published_card())


@unittest.skipUnless(shutil.which("ruby"), "ruby not installed")
class StatuslineRenderTest(unittest.TestCase):
    def test_the_happy_path_is_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            text = plain(render(PAYLOAD, tmp, cache=billing_cache()))
        usd = api_usd("grok-4.6", 135_000, 20_000, 387_000, 18_000)
        self.assertEqual(text.count("\n"), 1)
        self.assertEqual(
            text.rstrip("\n"),
            "Grok 4.6 | xhigh | ctx 73% | 7d 58% ↻3d04h | "
            f"${usd:.2f} | in 412k · out 18k | cache 94%",
        )

    def test_state_runs_keep_the_cache_and_ignore_the_log(self) -> None:
        """A busy turn re-runs the script continuously; it must not walk the log."""
        log = [
            log_row(
                77.0,
                "2026-09-05T22:47:24+00:00",
                "2026-08-29T22:47:24+00:00",
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            text = plain(render(PAYLOAD, tmp, cache=billing_cache(), log_rows=log))
        self.assertIn("7d 58%", text)
        self.assertNotIn("7d 23%", text)

    def test_a_missing_cache_seeds_from_the_log(self) -> None:
        log = [
            log_row(
                77.0,
                "2026-09-05T22:47:24+00:00",
                "2026-08-29T22:47:24+00:00",
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            text = plain(render(PAYLOAD, tmp, log_rows=log))
        self.assertIn("7d 23%", text)
        self.assertIn("↻3d02h", text)

    def test_an_expired_period_is_omitted(self) -> None:
        cache = billing_cache(resets_at=NOW_TS - 60)
        with tempfile.TemporaryDirectory() as tmp:
            text = plain(render(PAYLOAD, tmp, cache=cache))
        self.assertNotIn("7d", text)

    def test_missing_optional_fields_do_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            text = plain(render({"trigger": "state"}, tmp, cache=billing_cache()))
        self.assertIn("?", text)
        self.assertIn("7d 58%", text)
        self.assertEqual(text.count("\n"), 1)

    def test_tiny_costs_are_hidden(self) -> None:
        payload = json.loads(json.dumps(PAYLOAD))
        payload["context_window"]["session_usage"] = {
            "input_tokens": 100,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 100,
        }
        payload["context_window"]["session_input_tokens"] = 100
        payload["context_window"]["session_output_tokens"] = 100
        with tempfile.TemporaryDirectory() as tmp:
            text = plain(render(payload, tmp, cache=billing_cache()))
        self.assertNotIn("$0.00", text)
        self.assertNotRegex(text, r"\$0\.\d{2}")

    def test_the_dollar_is_list_price_not_the_payload_cost(self) -> None:
        """SuperGrok often omits cost.total_cost_usd; when it is present it is not the card."""
        payload = json.loads(json.dumps(PAYLOAD))
        payload["cost"] = {"total_cost_usd": 99.99}
        with tempfile.TemporaryDirectory() as tmp:
            text = plain(render(payload, tmp, cache=billing_cache()))
        usd = api_usd("grok-4.6", 135_000, 20_000, 387_000, 18_000)
        self.assertIn(f"${usd:.2f}", text)
        self.assertNotIn("$99.99", text)
        self.assertNotIn("saved $", text)

    def test_the_200k_cliff_uses_the_high_rate_and_says_so(self) -> None:
        payload = json.loads(json.dumps(PAYLOAD))
        payload["context_window"]["context_tokens"] = 210_000
        payload["context_window"]["remaining_percentage"] = 58
        with tempfile.TemporaryDirectory() as tmp:
            text = plain(render(payload, tmp, cache=billing_cache()))
        self.assertIn("≥200k 2×", text)
        usd = api_usd("grok-4.6", 210_000, 20_000, 387_000, 18_000)
        self.assertIn(f"${usd:.2f}", text)
        lo = api_usd("grok-4.6", 135_000, 20_000, 387_000, 18_000)
        self.assertNotIn(f"${lo:.2f}", text)

    def test_approaching_the_cliff_counts_down(self) -> None:
        payload = json.loads(json.dumps(PAYLOAD))
        payload["context_window"]["context_tokens"] = 190_000
        with tempfile.TemporaryDirectory() as tmp:
            text = plain(render(payload, tmp, cache=billing_cache()))
        self.assertIn("2× in 10k", text)
        self.assertNotIn("≥200k", text)

    def test_ctx_turns_red_when_almost_full(self) -> None:
        payload = json.loads(json.dumps(PAYLOAD))
        payload["context_window"]["remaining_percentage"] = 3
        with tempfile.TemporaryDirectory() as tmp:
            raw = render(payload, tmp, cache=billing_cache())
        self.assertIn("\033[31mctx 3%", raw)

    def test_weekly_turns_amber_when_the_pool_is_thin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = render(PAYLOAD, tmp, cache=billing_cache(used_percent=78.0))
        self.assertIn("\033[33m7d 22%", raw)

    def test_invalid_stdin_exits_quietly(self) -> None:
        env = os.environ.copy()
        with tempfile.TemporaryDirectory() as tmp:
            env["GROK_STATUSLINE_CACHE"] = str(Path(tmp) / "billing.json")
            env["GROK_STATUSLINE_AUTH"] = str(Path(tmp) / "auth.json")
            env["GROK_STATUSLINE_LOG"] = str(Path(tmp) / "unified.jsonl")
            Path(tmp, "auth.json").write_text("{}")
            out = subprocess.run(
                ["ruby", str(STATUSLINE)],
                input="not-json",
                capture_output=True,
                text=True,
                env=env,
            )
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout, "")


if __name__ == "__main__":
    unittest.main()
