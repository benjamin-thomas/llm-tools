from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from zoneinfo import ZoneInfo

from token_recap.harness import (
    API_SECTION,
    HYPER_SECTION,
    OLLAMA_SECTION,
    OPENCODE_SECTIONS,
    PI_SECTIONS,
    harness_usd,
    section_for,
)
from token_recap.catalogue import catalogue_rates
from token_recap.harness import display_rates
from token_recap.opencode import collect_opencode
from token_recap.pi import collect_pi
from token_recap.recap import api_window, recap_text

PARIS = ZoneInfo("Europe/Paris")
START = datetime(2026, 8, 21, 12, 0, tzinfo=PARIS)
END = datetime(2026, 8, 22, 12, 0, tzinfo=PARIS)
SPANS = {"codex": (START, END), "grok": (START, END), API_SECTION: (START, END)}


def message(
    entry_id: str,
    provider: str,
    model: str,
    *,
    ts: str = "2026-08-21T18:00:00.000Z",
    started: int = 1,
    usage: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "type": "message",
        "id": entry_id,
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "provider": provider,
            "model": model,
            "timestamp": started,
            "usage": usage
            or {
                "input": 100,
                "output": 10,
                "cacheRead": 900,
                "cacheWrite": 0,
                "cost": {"total": 0.5},
            },
        },
    }


class SectionTest(unittest.TestCase):
    def test_subscription_providers_join_their_plan(self) -> None:
        self.assertEqual(section_for("openai-codex", PI_SECTIONS), "codex")
        self.assertEqual(section_for("xai", PI_SECTIONS), "grok")
        self.assertEqual(section_for("openai", OPENCODE_SECTIONS), "codex")
        self.assertEqual(section_for("xai", OPENCODE_SECTIONS), "grok")

    def test_a_bare_openai_means_opposite_things_per_harness(self) -> None:
        """pi names the Codex OAuth apart, so its "openai" is an API key.

        opencode has one id per vendor, so its "openai" is the ChatGPT OAuth.
        Merging the two maps would misfile one harness or the other.
        """
        self.assertEqual(section_for("openai", PI_SECTIONS), API_SECTION)
        self.assertEqual(section_for("openai", OPENCODE_SECTIONS), "codex")

    def test_everything_else_is_billed_per_token(self) -> None:
        for provider in ("anthropic", "openrouter", "baseten", ""):
            for sections in (PI_SECTIONS, OPENCODE_SECTIONS):
                self.assertEqual(section_for(provider, sections), API_SECTION, provider)

    def test_ollama_is_its_own_subscription_not_a_free_tier(self) -> None:
        for provider in ("ollama", "ollama-cloud"):
            for sections in (PI_SECTIONS, OPENCODE_SECTIONS):
                self.assertEqual(
                    section_for(provider, sections), OLLAMA_SECTION, provider
                )

    def test_hyper_is_its_own_subscription_too(self) -> None:
        """A flat fee, so its calls neither cost money nor priced to nothing."""
        for sections in (PI_SECTIONS, OPENCODE_SECTIONS):
            self.assertEqual(section_for("hyper", sections), HYPER_SECTION)


class DisplayRatesTest(unittest.TestCase):
    def test_our_own_card_answers_for_its_own_provider(self) -> None:
        for model, provider, want in (
            ("claude-opus-5", "anthropic", (5.0, 0.5, 25.0)),
            ("gpt-5.6-sol", "openai", (4.0, 0.4, 20.0)),
            ("grok-4.6", "xai", (2.0, 0.5, 6.0)),
        ):
            rates = display_rates(model, provider)
            assert rates is not None, model
            self.assertEqual(
                (rates.input, rates.cache_read, rates.output), want, model
            )

    def test_grok_is_shown_at_its_under_200k_tier(self) -> None:
        """The tier almost every request bills at; the cliff is per request."""
        rates = display_rates("grok-4.6", "xai")
        assert rates is not None
        self.assertEqual(rates.input, 2.0)

    def test_a_reseller_gets_its_own_price_not_the_first_party_card(self) -> None:
        """The same id through a reseller is a different price. Our card must
        not answer for a provider that did not set it — the catalogue does,
        keyed by that provider."""
        first_party = display_rates("claude-opus-5", "anthropic")
        reseller = display_rates("claude-opus-5", "cortecs")
        assert first_party is not None and reseller is not None
        self.assertEqual(first_party.input, 5.0)
        self.assertNotEqual(reseller.input, first_party.input)

    def test_an_unknown_pair_has_no_rate(self) -> None:
        self.assertIsNone(display_rates("no-such-model", "no-such-provider"))

    def test_a_missing_catalogue_is_not_an_error(self) -> None:
        self.assertIsNone(
            catalogue_rates("openrouter", "anything", Path("/nonexistent.json"))
        )


class OllamaCardTest(unittest.TestCase):
    """The card is only worth drawing while there is Ollama work to show."""

    def _collect(self, rows: list[dict[str, object]]):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "a" / "s.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            return collect_pi(root, {**SPANS, OLLAMA_SECTION: (START, END)})

    def test_hosted_and_local_ollama_share_the_card(self) -> None:
        got = self._collect(
            [
                message("1", "ollama", "glm-5.2:cloud"),
                message("2", "ollama-cloud", "deepseek-v4-flash"),
            ]
        )
        self.assertEqual(list(got), [OLLAMA_SECTION])
        self.assertEqual(got[OLLAMA_SECTION].calls, 2)

    def test_no_ollama_work_leaves_no_bucket_to_draw(self) -> None:
        got = self._collect([message("1", "openrouter", "something")])
        self.assertNotIn(OLLAMA_SECTION, got)


class HyperCardTest(unittest.TestCase):
    """Hyper is a plan like Codex or Grok, so its rows name the harness."""

    def _collect(self, rows: list[dict[str, object]]):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "a" / "s.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            return collect_pi(root, {**SPANS, HYPER_SECTION: (START, END)})

    def test_hyper_work_lands_on_a_card_of_its_own(self) -> None:
        """Not the paid section: the flat fee is already spent either way."""
        got = self._collect([message("1", "hyper", "glm-5.3-flash")])
        self.assertEqual(list(got), [HYPER_SECTION])
        self.assertEqual(got[HYPER_SECTION].calls, 1)

    def test_a_row_names_the_harness_eating_the_quota(self) -> None:
        """One provider id serves the whole card, so "hyper/" says nothing."""
        got = self._collect([message("1", "hyper", "glm-5.3-flash")])
        self.assertEqual(list(got[HYPER_SECTION].models), ["glm-5.3-flash (pi)"])

    def test_the_provider_is_kept_for_the_rate_lookup(self) -> None:
        """The row stopped saying it, but the price still has to be keyed on it."""
        got = self._collect([message("1", "hyper", "glm-5.3-flash")])
        use = got[HYPER_SECTION].models["glm-5.3-flash (pi)"]
        self.assertEqual((use.provider, use.model_id), ("hyper", "glm-5.3-flash"))

    def test_no_hyper_work_leaves_no_bucket_to_draw(self) -> None:
        got = self._collect([message("1", "openrouter", "something")])
        self.assertNotIn(HYPER_SECTION, got)


class HyperReportTest(unittest.TestCase):
    """The card is drawn only when it holds work, and it is drawn with prices."""

    NOW = datetime(2026, 8, 29, 22, 0, tzinfo=PARIS)

    @staticmethod
    def _row(provider: str) -> tuple[str, int, str]:
        data = {
            "role": "assistant",
            "providerID": provider,
            "modelID": "glm-5.3-flash",
            "cost": 0.0017,
            "tokens": {
                "input": 10_000,
                "output": 100,
                "reasoning": 20,
                "cache": {"read": 5_000, "write": 0},
            },
        }
        when = HyperReportTest.NOW - timedelta(days=1)
        return ("m1", int(when.timestamp() * 1000), json.dumps(data))

    def _report(self, provider: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE message (id text PRIMARY KEY,"
                " time_created integer NOT NULL, data text NOT NULL)"
            )
            con.execute("INSERT INTO message VALUES (?, ?, ?)", self._row(provider))
            con.commit()
            con.close()
            gone = Path(tmp) / "nothing-here"
            return recap_text(
                self.NOW,
                None,
                None,
                gone,  # claude root
                gone,  # claude credentials
                gone,  # grok log
                gone,  # grok auth
                gone,  # codex root
                gone,  # pi root
                db,
                color=False,
                offline=True,
            )

    @staticmethod
    def _card(report: str, title: str) -> list[str]:
        lines = report.splitlines()
        top = next(i for i, ln in enumerate(lines) if ln.startswith("┌") and title in ln)
        end = next(i for i, ln in enumerate(lines[top:], top) if ln.startswith("└"))
        return lines[top : end + 1]

    def test_the_card_is_drawn_under_its_own_name(self) -> None:
        report = self._report("hyper")
        card = self._card(report, "Charm Hyper")
        self.assertIn("glm-5.3-flash (opencode)", "\n".join(card))
        self.assertIn("Hyper", report)  # and a row of its own in the snapshot

    def test_the_card_keeps_its_price_columns(self) -> None:
        """Hyper publishes a rate card, so the list price still means something."""
        card = self._card(self._report("hyper"), "Charm Hyper")
        header = next(ln for ln in card if "model" in ln and "calls" in ln)
        self.assertIn("$in/M", header)

    def test_work_elsewhere_draws_no_hyper_card(self) -> None:
        self.assertNotIn("Charm Hyper", self._report("openrouter"))


class CatalogueTest(unittest.TestCase):
    CATALOGUE = {
        "runinfra": {
            "models": {
                "some-model": {"cost": {"input": 9.0, "output": 9.0, "cache_read": 9.0}}
            }
        },
        "cortecs": {
            "models": {"claude-opus-5": {"cost": {"input": 5.5, "output": 27.5}}}
        },
    }
    OVERRIDES = {
        "providers": {
            "runinfra": {
                "models": [
                    {
                        "id": "some-model",
                        "cost": {"input": 0.13, "output": 0.27, "cacheRead": 0.01},
                    }
                ]
            }
        }
    }

    def _files(self, tmp: str) -> tuple[Path, Path]:
        cat = Path(tmp) / "models.json"
        ovr = Path(tmp) / "pi.json"
        cat.write_text(json.dumps(self.CATALOGUE))
        ovr.write_text(json.dumps(self.OVERRIDES))
        return cat, ovr

    def test_the_users_own_price_beats_the_public_one(self) -> None:
        """What they configured is what they are actually charged."""
        with tempfile.TemporaryDirectory() as tmp:
            cat, ovr = self._files(tmp)
            rates = catalogue_rates("runinfra", "some-model", cat, ovr)
        assert rates is not None
        self.assertEqual(rates.input, 0.13)

    def test_the_catalogue_answers_where_no_override_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cat, ovr = self._files(tmp)
            rates = catalogue_rates("cortecs", "claude-opus-5", cat, ovr)
        assert rates is not None
        self.assertEqual(rates.input, 5.5)

    def test_a_missing_write_rate_bills_as_ordinary_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cat, ovr = self._files(tmp)
            rates = catalogue_rates("cortecs", "claude-opus-5", cat, ovr)
        assert rates is not None
        self.assertEqual(rates.cache_write, rates.input)

    def test_an_unknown_provider_for_a_known_model_is_not_a_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cat, ovr = self._files(tmp)
            self.assertIsNone(catalogue_rates("elsewhere", "some-model", cat, ovr))


class HarnessPricingTest(unittest.TestCase):
    def test_our_own_card_wins_over_the_harness_figure(self) -> None:
        self.assertAlmostEqual(
            harness_usd("gpt-5.6-sol", 0, 0, 0, 1_000_000, 999.0), 20.0
        )
        self.assertAlmostEqual(harness_usd("grok-4.6", 0, 0, 0, 1_000_000, 999.0), 6.0)

    def test_a_model_we_hold_no_card_for_keeps_its_own_figure(self) -> None:
        self.assertAlmostEqual(harness_usd("MiniMax-M2.5", 0, 0, 0, 1_000, 1.25), 1.25)

    def test_the_one_hour_write_split_is_used_when_given(self) -> None:
        """1h writes bill at 2× input, 5m at 1.25×; opus input is $5."""
        both = harness_usd("claude-opus-5", 0, 1_000_000, 0, 0, 0.0, write_1h=1_000_000)
        neither = harness_usd("claude-opus-5", 0, 1_000_000, 0, 0, 0.0)
        self.assertAlmostEqual(both, 10.0)
        self.assertAlmostEqual(neither, 6.25)


class ApiWindowTest(unittest.TestCase):
    NOW = datetime(2026, 8, 29, 22, 0, tzinfo=PARIS)

    def test_it_rolls_back_from_now_by_the_week_count(self) -> None:
        got = api_window(self.NOW, None, None, 4)
        self.assertEqual(got.end, self.NOW)
        self.assertEqual(got.start, datetime(2026, 8, 1, 22, 0, tzinfo=PARIS))

    def test_an_explicit_range_governs_it_like_every_other_section(self) -> None:
        since = datetime(2026, 8, 28, 0, 0, tzinfo=PARIS)
        got = api_window(self.NOW, since, None, 1)
        self.assertEqual(got.label, "custom window")
        self.assertEqual(got.start, since)
        self.assertEqual(got.end, self.NOW)

    def test_an_until_alone_still_narrows_it(self) -> None:
        until = datetime(2026, 8, 29, 12, 0, tzinfo=PARIS)
        got = api_window(self.NOW, None, until, 1)
        self.assertEqual(got.end, until)
        self.assertEqual(got.start, datetime(2026, 8, 22, 22, 0, tzinfo=PARIS))


class PiCollectorTest(unittest.TestCase):
    def _collect(self, files: dict[str, list[dict[str, object]]]):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, rows in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            return collect_pi(root, SPANS)

    def test_calls_are_filed_under_the_plan_that_paid(self) -> None:
        got = self._collect(
            {
                "a/s.jsonl": [
                    message("1", "openai-codex", "gpt-5.6-sol"),
                    message("2", "xai", "grok-4.6"),
                    message("3", "openrouter", "deepseek/deepseek-v4-flash"),
                ]
            }
        )
        self.assertEqual(sorted(got), ["api", "codex", "grok"])
        self.assertEqual(got["codex"].calls, 1)
        self.assertEqual(got["grok"].calls, 1)
        self.assertEqual(got[API_SECTION].calls, 1)

    def test_a_plan_row_names_the_harness_eating_the_quota(self) -> None:
        got = self._collect({"a/s.jsonl": [message("1", "xai", "grok-4.6")]})
        self.assertEqual(list(got["grok"].models), ["grok-4.6 (pi)"])

    def test_a_paid_row_names_the_provider_setting_the_price(self) -> None:
        """The bill does not care which tool placed the call."""
        got = self._collect({"a/s.jsonl": [message("1", "openrouter", "some-model")]})
        self.assertEqual(list(got[API_SECTION].models), ["openrouter/some-model"])

    def test_one_model_on_two_providers_stays_two_rows(self) -> None:
        """They are priced apart, so merging them would pick one at random."""
        got = self._collect(
            {
                "a/s.jsonl": [
                    message("1", "openrouter", "MiniMax-M2.5"),
                    message("2", "minimax", "MiniMax-M2.5"),
                ]
            }
        )
        self.assertEqual(
            sorted(got[API_SECTION].models),
            ["minimax/MiniMax-M2.5", "openrouter/MiniMax-M2.5"],
        )

    def test_input_is_already_uncached_so_nothing_is_subtracted(self) -> None:
        got = self._collect({"a/s.jsonl": [message("1", "xai", "grok-4.6")]})
        b = got["grok"]
        self.assertEqual(b.uncached, 100)  # not 100 - 900
        self.assertEqual(b.cache_read, 900)
        self.assertEqual(b.output, 10)

    def test_a_forked_copy_is_not_a_second_call(self) -> None:
        """/fork copies entries verbatim into a new file, id included."""
        row = message("dup", "xai", "grok-4.6")
        got = self._collect({"a/s.jsonl": [row], "b/fork.jsonl": [row]})
        self.assertEqual(got["grok"].calls, 1)

    def test_two_real_calls_are_not_confused_for_a_copy(self) -> None:
        got = self._collect(
            {
                "a/s.jsonl": [
                    message("x", "xai", "grok-4.6", started=1),
                    message("x", "xai", "grok-4.6", started=2),  # same id, later run
                ]
            }
        )
        self.assertEqual(got["grok"].calls, 2)

    def test_records_outside_the_window_are_skipped(self) -> None:
        got = self._collect(
            {"a/s.jsonl": [message("1", "xai", "grok-4.6", ts="2026-08-01T00:00:00Z")]}
        )
        self.assertEqual(got, {})

    def test_a_throughput_sample_is_not_a_request(self) -> None:
        sample: dict[str, object] = {
            "type": "custom",
            "customType": "tps-sample",
            "id": "t",
            "timestamp": "2026-08-21T18:00:00.000Z",
            "message": {"role": "assistant", "usage": {"input": 1, "output": 1}},
        }
        self.assertEqual(self._collect({"a/s.jsonl": [sample]}), {})


class OpencodeCollectorTest(unittest.TestCase):
    """opencode stores one JSON blob per message in SQLite, keyed by id."""

    @staticmethod
    def _row(
        provider: str,
        model: str,
        tokens: dict[str, object],
        *,
        cost: float = 0.0,
        created: datetime = datetime(2026, 8, 21, 18, 0, tzinfo=PARIS),
    ) -> tuple[str, int, str]:
        data = {
            "role": "assistant",
            "providerID": provider,
            "modelID": model,
            "cost": cost,
            "tokens": tokens,
        }
        return (f"msg{id(tokens)}", int(created.timestamp() * 1000), json.dumps(data))

    def _collect(self, rows: list[tuple[str, int, str]]):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE message (id text PRIMARY KEY,"
                " time_created integer NOT NULL, data text NOT NULL)"
            )
            con.executemany("INSERT INTO message VALUES (?, ?, ?)", rows)
            con.commit()
            con.close()
            return collect_opencode(db, SPANS)

    def test_a_priced_xai_call_is_still_a_subscription_call(self) -> None:
        """opencode does not zero xAI prices under OAuth, so cost proves nothing."""
        got = self._collect(
            [
                self._row(
                    "xai",
                    "grok-4.5",
                    {"input": 100, "output": 10, "cache": {"read": 900, "write": 0}},
                    cost=0.024,
                )
            ]
        )
        self.assertEqual(list(got), ["grok"])

    def test_input_is_already_uncached(self) -> None:
        got = self._collect(
            [
                self._row(
                    "openai",
                    "gpt-5.6-sol",
                    {"input": 417, "output": 50, "cache": {"read": 96896, "write": 0}},
                )
            ]
        )
        b = got["codex"]
        self.assertEqual(b.uncached, 417)  # not 417 - 96896
        self.assertEqual(b.cache_read, 96896)

    def test_reasoning_is_added_back_only_when_stored_apart(self) -> None:
        """Current opencode stores output net of reasoning; old builds did not."""
        apart = self._collect(
            [
                self._row(
                    "xai",
                    "grok-4.5",
                    # total counts reasoning separately: output is net of it
                    {"input": 11367, "output": 93, "reasoning": 62,
                     "total": 13442, "cache": {"read": 1920, "write": 0}},
                )
            ]
        )
        self.assertEqual(apart["grok"].output, 93 + 62)

        included = self._collect(
            [
                self._row(
                    "openai",
                    "gpt-5.3-codex",
                    # total omits reasoning: output already contains it
                    {"input": 320, "output": 247, "reasoning": 62,
                     "total": 9911 - 9911 + 320 + 247 + 9344,
                     "cache": {"read": 9344, "write": 0}},
                )
            ]
        )
        self.assertEqual(included["codex"].output, 247)

    def test_a_plan_row_names_the_harness_eating_the_quota(self) -> None:
        got = self._collect(
            [self._row("xai", "grok-4.5", {"input": 1, "output": 1, "cache": {}})]
        )
        self.assertEqual(list(got["grok"].models), ["grok-4.5 (opencode)"])

    def test_two_harnesses_on_one_paid_model_merge_into_one_row(self) -> None:
        """Which tool placed the call is not what the paid row is about."""
        got = self._collect(
            [
                self._row("openrouter", "shared", {"input": 1, "output": 1, "cache": {}}, cost=0.5),
                self._row("openrouter", "shared", {"input": 2, "output": 2, "cache": {}}, cost=0.5),
            ]
        )
        self.assertEqual(list(got[API_SECTION].models), ["openrouter/shared"])
        self.assertEqual(got[API_SECTION].models["openrouter/shared"].calls, 2)

    def test_a_missing_database_is_not_an_error(self) -> None:
        self.assertEqual(collect_opencode(Path("/nope/opencode.db"), SPANS), {})


if __name__ == "__main__":
    unittest.main()
