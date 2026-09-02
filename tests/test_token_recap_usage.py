from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from zoneinfo import ZoneInfo

from token_recap import anthropic_usage, xai_usage as xai
from token_recap.anthropic_usage import claude_limits
from token_recap.xai_usage import xai_usage

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 8, 29, 22, 0, tzinfo=PARIS)

PAYLOAD: dict[str, Any] = {
    "five_hour": {"utilization": 6.0, "resets_at": "2026-08-30T01:09:59.700136+00:00"},
    "seven_day": {"utilization": 29.0, "resets_at": "2026-09-01T09:59:59.700159+00:00"},
    "seven_day_opus": None,
    "limits": [
        {"kind": "session", "percent": 6, "resets_at": "2026-08-30T01:09:59+00:00"},
        {"kind": "weekly_all", "percent": 29, "scope": None},
        {
            "kind": "weekly_scoped",
            "percent": 32,
            "scope": {"model": {"id": None, "display_name": "Fable"}},
        },
    ],
}


class ClaudeUsageTest(unittest.TestCase):
    @staticmethod
    def _cache(tmp: str) -> Path:
        return Path(tmp) / "cache.json"

    def _creds(self, tmp: str, blob: dict[str, Any] | None = None) -> Path:
        path = Path(tmp) / ".credentials.json"
        payload = blob if blob is not None else {"claudeAiOauth": {"accessToken": "t"}}
        path.write_text(json.dumps(payload))
        return path

    def test_it_reads_the_weekly_meter_and_its_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(anthropic_usage, "_fetch", return_value=PAYLOAD):
                got = claude_limits(self._creds(tmp), NOW, cache=self._cache(tmp))
        assert got is not None
        self.assertAlmostEqual(got.used, 0.29)
        # The window is the seven days ending at the reported reset, not a
        # guessed weekday: 09:59 UTC is 11:59 Paris.
        self.assertEqual(got.window.end, datetime(2026, 9, 1, 11, 59, 59, 700159, tzinfo=PARIS))
        self.assertEqual(got.window.end - got.window.start, anthropic_usage._WEEK)
        self.assertIn("live", got.window.label)

    def test_the_note_carries_the_other_caps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(anthropic_usage, "_fetch", return_value=PAYLOAD):
                got = claude_limits(self._creds(tmp), NOW, cache=self._cache(tmp))
        assert got is not None
        self.assertEqual(got.note, "5h 6% · Fable 32%")

    def test_a_scoped_cap_without_a_model_name_is_skipped(self) -> None:
        payload = dict(PAYLOAD)
        payload["limits"] = [{"kind": "weekly_scoped", "percent": 32, "scope": None}]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(anthropic_usage, "_fetch", return_value=payload):
                got = claude_limits(self._creds(tmp), NOW, cache=self._cache(tmp))
        assert got is not None
        self.assertEqual(got.note, "5h 6%")

    def test_a_network_failure_is_not_an_error(self) -> None:
        """Offline, rate-limited or expired: all just mean "no meter"."""
        with tempfile.TemporaryDirectory() as tmp:
            creds = self._creds(tmp)
            with mock.patch.object(
                anthropic_usage.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("down"),
            ):
                self.assertIsNone(claude_limits(creds, NOW, cache=self._cache(tmp)))

    def test_missing_or_tokenless_credentials_give_no_meter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(claude_limits(Path(tmp) / "nope.json", NOW, cache=self._cache(tmp)))
            self.assertIsNone(claude_limits(self._creds(tmp, {"other": 1}), NOW, cache=self._cache(tmp)))

    def test_a_payload_without_the_weekly_block_gives_no_meter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(anthropic_usage, "_fetch", return_value={"five_hour": {}}):
                self.assertIsNone(claude_limits(self._creds(tmp), NOW, cache=self._cache(tmp)))


XAI_PAYLOAD: dict[str, Any] = {
    "config": {
        "creditUsagePercent": 99.0,
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-08-22T22:47:24.847880+00:00",
            "end": "2026-08-29T22:47:24.847880+00:00",
        },
        "productUsage": [{"product": "GrokBuild", "usagePercent": 99.0}],
    }
}
XAI_AUTH: dict[str, Any] = {
    "https://auth.x.ai::b1a00492": {"key": "tok", "auth_mode": "oidc"}
}


class XaiUsageTest(unittest.TestCase):
    NOW = datetime(2026, 8, 29, 23, 0, tzinfo=PARIS)

    def _auth(self, tmp: str, blob: dict[str, Any] | None = None) -> Path:
        path = Path(tmp) / "auth.json"
        path.write_text(json.dumps(blob if blob is not None else XAI_AUTH))
        return path

    def test_it_reads_the_weekly_pool_and_its_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(xai, "_fetch", return_value=XAI_PAYLOAD):
                got = xai_usage(self._auth(tmp), self.NOW)
        assert got is not None
        self.assertAlmostEqual(got.used, 0.99)
        self.assertEqual(got.window.end.hour, 0)  # 22:47 UTC is 00:47 in Paris
        self.assertEqual(got.window.end.minute, 47)
        self.assertIn("live", got.window.label)

    def test_the_headline_is_not_the_per_product_breakdown(self) -> None:
        """productUsage splits the same pool; it is not a second meter."""
        payload = {"config": dict(XAI_PAYLOAD["config"])}
        payload["config"]["productUsage"] = [{"product": "GrokBuild", "usagePercent": 12.0}]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(xai, "_fetch", return_value=payload):
                got = xai_usage(self._auth(tmp), self.NOW)
        assert got is not None
        self.assertAlmostEqual(got.used, 0.99)

    def test_only_an_oidc_entry_is_worth_sending(self) -> None:
        """This path rejects an API key, so there is nothing to try with one."""
        with tempfile.TemporaryDirectory() as tmp:
            auth = self._auth(tmp, {"https://auth.x.ai::x": {"key": "k", "auth_mode": "api"}})
            self.assertIsNone(xai._token(auth))
            auth = self._auth(tmp, {"someone-else::x": {"key": "k", "auth_mode": "oidc"}})
            self.assertIsNone(xai._token(auth))

    def test_a_network_failure_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auth = self._auth(tmp)
            with mock.patch.object(
                xai.urllib.request, "urlopen", side_effect=urllib.error.URLError("down")
            ):
                self.assertIsNone(xai_usage(auth, self.NOW))

    def test_a_refilled_period_is_declined(self) -> None:
        payload = {"config": dict(XAI_PAYLOAD["config"])}
        payload["config"]["currentPeriod"] = {
            "start": "2026-08-15T22:47:24+00:00",
            "end": "2026-08-22T22:47:24+00:00",
        }
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(xai, "_fetch", return_value=payload):
                self.assertIsNone(xai_usage(self._auth(tmp), self.NOW))

    def test_a_missing_auth_file_gives_no_meter(self) -> None:
        self.assertIsNone(xai_usage(Path("/nonexistent/auth.json"), self.NOW))


class ClaudeUsageCacheTest(unittest.TestCase):
    """The endpoint 429s readily and has no logged copy to fall back on."""

    def _creds(self, tmp: str) -> Path:
        path = Path(tmp) / ".credentials.json"
        path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t"}}))
        return path

    def test_a_recent_answer_is_reused_without_asking_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache.json"
            creds = self._creds(tmp)
            with mock.patch.object(anthropic_usage, "_fetch", return_value=PAYLOAD) as fetch:
                claude_limits(creds, NOW, cache=cache)
                claude_limits(creds, NOW, cache=cache)
            self.assertEqual(fetch.call_count, 1)

    def test_a_rate_limited_call_falls_back_to_the_last_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache.json"
            creds = self._creds(tmp)
            with mock.patch.object(anthropic_usage, "_fetch", return_value=PAYLOAD):
                first = claude_limits(creds, NOW, cache=cache)
            assert first is not None and first.fresh
            # Much later, with the endpoint refusing: the old reading still shows.
            later = NOW + timedelta(hours=2)
            with mock.patch.object(anthropic_usage, "_fetch", return_value=None):
                stale = claude_limits(creds, later, cache=cache)
            assert stale is not None
            self.assertFalse(stale.fresh)
            self.assertAlmostEqual(stale.used, first.used)
            self.assertIn("as of", stale.note)  # says how old it is

    def test_an_unreadable_answer_is_not_kept(self) -> None:
        """One bad response must not become the fallback for every later run."""
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache.json"
            with mock.patch.object(anthropic_usage, "_fetch", return_value={"five_hour": {}}):
                self.assertIsNone(claude_limits(self._creds(tmp), NOW, cache=cache))
            self.assertFalse(cache.exists())

    def test_no_cache_and_no_answer_gives_no_meter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(anthropic_usage, "_fetch", return_value=None):
                self.assertIsNone(
                    claude_limits(self._creds(tmp), NOW, cache=Path(tmp) / "none.json")
                )


if __name__ == "__main__":
    unittest.main()
