from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from zoneinfo import ZoneInfo

from token_recap.claude import collect_claude
from token_recap.codex import codex_limits, collect_codex
from token_recap.grok import collect_grok, grok_limits

PARIS = ZoneInfo("Europe/Paris")
START = datetime(2026, 8, 21, 12, 0, tzinfo=PARIS)
END = datetime(2026, 8, 22, 12, 0, tzinfo=PARIS)


def turn_context(model: str) -> dict[str, object]:
    return {"type": "turn_context", "payload": {"turn_id": "t", "model": model}}


def usage_event(ts: str, out: int) -> dict[str, object]:
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": {"input_tokens": 0, "output_tokens": out}},
        },
    }


class ClaudeParserTest(unittest.TestCase):
    def test_dedupes_message_id_and_counts_cache_buckets(self) -> None:
        usage = {
            "input_tokens": 10,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 1000,
            "output_tokens": 50,
            "output_tokens_details": {"thinking_tokens": 20},
            "cache_creation": {
                "ephemeral_1h_input_tokens": 100,
                "ephemeral_5m_input_tokens": 0,
            },
        }
        rows = [
            {
                "type": "assistant",
                "timestamp": "2026-08-21T18:00:00.000Z",
                "message": {
                    "id": "msg_1",
                    "model": "claude-opus-5",
                    "usage": usage,
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-08-21T18:00:00.500Z",
                "message": {
                    "id": "msg_1",
                    "model": "claude-opus-5",
                    "usage": usage,
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-08-20T10:00:00.000Z",
                "message": {
                    "id": "msg_old",
                    "model": "claude-opus-5",
                    "usage": usage,
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "proj" / "s.jsonl"
            path.parent.mkdir()
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            (root / "proj" / "s" / "subagents").mkdir(parents=True)
            sub = root / "proj" / "s" / "subagents" / "agent.jsonl"
            sub.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-08-21T19:00:00.000Z",
                        "message": {
                            "id": "msg_sub",
                            "model": "claude-opus-5",
                            "usage": {
                                "input_tokens": 1,
                                "cache_creation_input_tokens": 5,
                                "cache_read_input_tokens": 7,
                                "output_tokens": 3,
                                "cache_creation": {
                                    "ephemeral_5m_input_tokens": 5,
                                    "ephemeral_1h_input_tokens": 0,
                                },
                            },
                        },
                    }
                )
                + "\n"
            )
            got = collect_claude(root, START, END)
        self.assertEqual(got.calls, 2)
        self.assertEqual(got.uncached, 11)
        self.assertEqual(got.cache_write, 105)
        self.assertEqual(got.cache_read, 1007)
        self.assertEqual(got.output, 53)
        self.assertEqual(got.reasoning, 20)
        # opus: 10*5 + 100*10 + 5*6.25 + 1000*0.50 + 7*0.50 + 50*25 + 3*25  / 1e6
        self.assertGreater(got.native_usd, 0.0)
        self.assertAlmostEqual(
            got.native_usd,
            (11 * 5 + 100 * 10 + 5 * 6.25 + 1007 * 0.50 + 53 * 25) / 1_000_000,
        )


class GrokParserTest(unittest.TestCase):
    def test_splits_prompt_into_uncached_and_cache_read(self) -> None:
        rows = [
            {
                "ts": "2026-08-21T22:00:00.000Z",
                "msg": "shell.turn.inference_done",
                "sid": "abc",
                "ctx": {
                    "prompt_tokens": 1000,
                    "cached_prompt_tokens": 800,
                    "completion_tokens": 50,
                    "reasoning_tokens": 40,
                },
            },
            {
                "ts": "2026-08-20T22:00:00.000Z",
                "msg": "shell.turn.inference_done",
                "ctx": {
                    "prompt_tokens": 9999,
                    "cached_prompt_tokens": 1,
                    "completion_tokens": 1,
                    "reasoning_tokens": 1,
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "unified.jsonl"
            log.write_text("".join(json.dumps(r) + "\n" for r in rows))
            got = collect_grok(log, START, END)
        self.assertEqual(got.calls, 1)
        self.assertEqual(got.uncached, 200)
        self.assertEqual(got.cache_read, 800)
        self.assertEqual(got.cache_write, 0)
        self.assertEqual(got.output, 50)
        self.assertEqual(got.reasoning, 40)
        # prompt 1000 < 200k → $2/$0.50/$6
        self.assertAlmostEqual(
            got.native_usd,
            (200 * 2.0 + 800 * 0.50 + 50 * 6.0) / 1_000_000,
        )


class CodexParserTest(unittest.TestCase):
    def test_sums_last_token_usage_in_window(self) -> None:
        def event(ts: str, inp: int, cached: int, out: int) -> dict[str, object]:
            return {
                "timestamp": ts,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": inp,
                            "cached_input_tokens": cached,
                            "cache_write_input_tokens": 0,
                            "output_tokens": out,
                            "reasoning_output_tokens": 1,
                        }
                    },
                },
            }

        rows = [
            event("2026-08-21T18:00:00.000Z", 100, 80, 10),
            event("2026-08-21T19:00:00.000Z", 50, 20, 5),
            event("2026-08-20T18:00:00.000Z", 999, 1, 1),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "2026" / "08" / "21" / "rollout.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            got = collect_codex(root, START, END)
        self.assertEqual(got.calls, 2)
        self.assertEqual(got.uncached, 50)
        self.assertEqual(got.cache_read, 100)
        self.assertEqual(got.output, 15)

    def test_prices_each_turn_with_the_model_named_by_turn_context(self) -> None:
        rows = [
            # No turn_context yet: backfilled with the session's first model.
            usage_event("2026-08-21T18:00:00.000Z", out=1_000_000),
            turn_context("gpt-5.5"),
            usage_event("2026-08-21T18:10:00.000Z", out=1_000_000),
            # Mid-session switch.
            turn_context("gpt-5.1-codex-max"),
            usage_event("2026-08-21T18:20:00.000Z", out=1_000_000),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "2026" / "08" / "21" / "rollout.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            got = collect_codex(root, START, END)
        self.assertEqual(got.calls, 3)
        calls = {name: use.calls for name, use in got.models.items()}
        self.assertEqual(calls, {"gpt-5.5": 2, "gpt-5.1-codex-max": 1})
        # each model carries its own share of the buckets, not just a tally
        self.assertEqual(got.models["gpt-5.5"].output, 2_000_000)
        self.assertAlmostEqual(got.models["gpt-5.5"].native_usd, 60.0)
        # 2M output on gpt-5.5 at $30 + 1M on codex-max at $10
        self.assertAlmostEqual(got.native_usd, 2 * 30.0 + 10.0)


class CodexNotificationTest(unittest.TestCase):
    """A token_count is often notified more than once for the same turn."""

    @staticmethod
    def _event(ts: str, cumulative: int, inp: int, out: int) -> dict[str, object]:
        return {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": cumulative},
                    "last_token_usage": {"input_tokens": inp, "output_tokens": out},
                },
            },
        }

    def _collect(self, rows: list[dict[str, object]]):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "2026" / "08" / "21" / "rollout.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            return collect_codex(root, START, END)

    def test_repeat_notification_is_counted_once(self) -> None:
        ts = "2026-08-21T18:00:00.000Z"
        got = self._collect(
            [
                self._event(ts, 100, 100, 10),
                self._event(ts, 100, 100, 10),  # same cumulative total: a repeat
                self._event(ts, 250, 150, 20),  # the total moved: a real turn
            ]
        )
        self.assertEqual(got.calls, 2)
        self.assertEqual(got.uncached, 250)
        self.assertEqual(got.output, 30)

    def test_identical_turns_still_count_twice(self) -> None:
        """Two turns that spend the same amount still move the total."""
        got = self._collect(
            [
                self._event("2026-08-21T18:00:00.000Z", 100, 100, 10),
                self._event("2026-08-21T18:05:00.000Z", 200, 100, 10),
            ]
        )
        self.assertEqual(got.calls, 2)

    def test_cache_write_is_split_out_of_input(self) -> None:
        """input_tokens is the whole prompt: reads and writes included."""
        rows = [
            {
                "timestamp": "2026-08-21T18:00:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 1000,
                            "cached_input_tokens": 600,
                            "cache_write_input_tokens": 300,
                            "output_tokens": 5,
                        }
                    },
                },
            }
        ]
        got = self._collect(rows)
        self.assertEqual(got.uncached, 100)
        self.assertEqual(got.cache_write, 300)
        self.assertEqual(got.cache_read, 600)


class CodexLimitWindowTest(unittest.TestCase):
    @staticmethod
    def _event(ts: str, limits: dict[str, object]) -> dict[str, object]:
        return {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": limits},
        }

    def _write(self, tmp: str, limits: dict[str, object]) -> Path:
        root = Path(tmp)
        path = root / "2026" / "08" / "21" / "rollout.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self._event("2026-08-21T09:00:00.000Z", limits)) + "\n")
        return root

    def test_reads_the_weekly_bucket_and_its_meter(self) -> None:
        now = datetime(2026, 8, 21, 12, 0, tzinfo=PARIS)
        weekly = int(datetime(2026, 8, 25, 12, 0, tzinfo=PARIS).timestamp())
        short = int(datetime(2026, 8, 21, 17, 0, tzinfo=PARIS).timestamp())
        limits = {
            "primary": {"window_minutes": 300, "resets_at": short, "used_percent": 53},
            "secondary": {
                "window_minutes": 10080,
                "resets_at": weekly,
                "used_percent": 42,
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            got = codex_limits(self._write(tmp, limits), now)
        assert got is not None
        self.assertEqual(got.window.end - got.window.start, timedelta(minutes=10080))
        self.assertEqual(got.window.end.timestamp(), weekly)
        self.assertAlmostEqual(got.used, 0.42)  # the meter, not time elapsed
        assert got.short is not None
        self.assertAlmostEqual(got.short[0], 0.53)

    def test_the_newest_event_wins_even_when_the_window_moved(self) -> None:
        """The server re-anchors: a later event replaces an unexpired bucket."""
        now = datetime(2026, 8, 21, 12, 0, tzinfo=PARIS)
        old_reset = int(datetime(2026, 8, 24, 12, 0, tzinfo=PARIS).timestamp())
        new_reset = int(datetime(2026, 8, 28, 9, 0, tzinfo=PARIS).timestamp())
        rows = [
            self._event(
                "2026-08-21T09:00:00.000Z",
                {"a": {"window_minutes": 10080, "resets_at": old_reset, "used_percent": 24}},
            ),
            self._event(
                "2026-08-21T10:00:00.000Z",
                {"a": {"window_minutes": 10080, "resets_at": new_reset, "used_percent": 7}},
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "2026" / "08" / "21" / "rollout.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            got = codex_limits(root, now)
        assert got is not None
        self.assertEqual(got.window.end.timestamp(), new_reset)
        self.assertAlmostEqual(got.used, 0.07)  # never the older maximum

    def test_a_stale_window_is_declined(self) -> None:
        """Already past its reset: window and percentage are both stale."""
        now = datetime(2026, 8, 21, 12, 0, tzinfo=PARIS)
        gone = int(datetime(2026, 8, 20, 12, 0, tzinfo=PARIS).timestamp())
        limits = {"secondary": {"window_minutes": 10080, "resets_at": gone}}
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(codex_limits(self._write(tmp, limits), now))

    def test_no_rate_limits_logged_is_none(self) -> None:
        now = datetime(2026, 8, 21, 12, 0, tzinfo=PARIS)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "a" / "r.jsonl").write_text(json.dumps(turn_context("gpt-5.5")) + "\n")
            self.assertIsNone(codex_limits(root, now))


class GrokLimitsTest(unittest.TestCase):
    """The CLI logs the billing answer it renders in its own /usage panel."""

    NOW = datetime(2026, 8, 29, 23, 0, tzinfo=PARIS)

    @staticmethod
    def _billing(ts: str, percent: float, end: str, start: str) -> dict[str, object]:
        return {
            "ts": ts,
            "msg": "billing: fetched credits config",
            "ctx": {
                "subscriptionTier": "SuperGrok",
                "config": {
                    "creditUsagePercent": percent,
                    "currentPeriod": {
                        "type": "USAGE_PERIOD_TYPE_WEEKLY",
                        "start": start,
                        "end": end,
                    },
                },
            },
        }

    def _read(self, rows: list[dict[str, object]]):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "unified.jsonl"
            log.write_text("".join(json.dumps(r) + "\n" for r in rows))
            return grok_limits(log, self.NOW)

    def test_it_reads_the_weekly_pool_and_its_period(self) -> None:
        got = self._read(
            [
                self._billing(
                    "2026-08-29T21:08:57.142Z",
                    96.0,
                    "2026-08-29T22:47:24.847880+00:00",
                    "2026-08-22T22:47:24.847880+00:00",
                )
            ]
        )
        assert got is not None
        self.assertAlmostEqual(got.used, 0.96)
        self.assertEqual(got.tier, "SuperGrok")
        # 22:47 UTC is 00:47 the next day in Paris, the observed reset
        self.assertEqual(got.window.end.hour, 0)
        self.assertEqual(got.window.end.minute, 47)
        self.assertEqual(got.window.end - got.window.start, timedelta(days=7))

    def test_the_newest_reading_wins(self) -> None:
        old = self._billing(
            "2026-08-28T10:00:00.000Z", 51.0,
            "2026-08-29T22:47:24+00:00", "2026-08-22T22:47:24+00:00",
        )
        new = self._billing(
            "2026-08-29T21:08:57.142Z", 96.0,
            "2026-08-29T22:47:24+00:00", "2026-08-22T22:47:24+00:00",
        )
        got = self._read([new, old])  # order in the file must not matter
        assert got is not None
        self.assertAlmostEqual(got.used, 0.96)

    def test_a_refilled_pool_is_declined(self) -> None:
        """Past its end the percentage describes a week that already closed."""
        self.assertIsNone(
            self._read(
                [
                    self._billing(
                        "2026-08-22T10:00:00Z", 51.0,
                        "2026-08-22T22:47:24+00:00", "2026-08-15T22:47:24+00:00",
                    )
                ]
            )
        )

    def test_no_billing_line_and_no_file_are_both_quiet(self) -> None:
        self.assertIsNone(self._read([{"msg": "something else", "ts": "2026-08-29T21:00:00Z"}]))
        self.assertIsNone(grok_limits(Path("/nonexistent/unified.jsonl"), self.NOW))


class GrokModelTest(unittest.TestCase):
    def test_tracks_the_model_per_session(self) -> None:
        rows = [
            {
                "ts": "2026-08-21T20:00:00.000Z",
                "msg": "model changed",
                "sid": "abc",
                "ctx": {"model": "grok-4.5"},
            },
            {
                "ts": "2026-08-21T22:00:00.000Z",
                "msg": "shell.turn.inference_done",
                "sid": "abc",
                "ctx": {
                    "prompt_tokens": 10,
                    "cached_prompt_tokens": 0,
                    "completion_tokens": 1,
                },
            },
            {
                "ts": "2026-08-21T22:30:00.000Z",
                "msg": "shell.turn.inference_done",
                "sid": "xyz",
                "ctx": {
                    "prompt_tokens": 10,
                    "cached_prompt_tokens": 0,
                    "completion_tokens": 1,
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "unified.jsonl"
            log.write_text("".join(json.dumps(r) + "\n" for r in rows))
            got = collect_grok(log, START, END)
        calls = {name: use.calls for name, use in got.models.items()}
        self.assertEqual(calls, {"grok-4.5": 1, "grok-4.6": 1})


if __name__ == "__main__":
    unittest.main()
