from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from zoneinfo import ZoneInfo

from token_recap.claude import collect_claude
from token_recap.codex import collect_codex
from token_recap.grok import collect_grok

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
        self.assertEqual(dict(got.models), {"gpt-5.5": 2, "gpt-5.1-codex-max": 1})
        # 2M output on gpt-5.5 at $30 + 1M on codex-max at $10
        self.assertAlmostEqual(got.native_usd, 2 * 30.0 + 10.0)


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
        self.assertEqual(dict(got.models), {"grok-4.5": 1, "grok-4.6": 1})


if __name__ == "__main__":
    unittest.main()
