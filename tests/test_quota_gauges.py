from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from quota_gauges.gauge import (
    Gauge,
    bar,
    elapsed,
    is_stale,
    margin,
    projected,
    runs_out_at,
    tone,
)
from quota_gauges.sources import claude_gauges, codex_gauges, mistral_gauge
from quota_gauges.cli import as_json, render_line
from token_recap.anthropic_usage import ClaudeLimits, limits_from_statusline
from token_recap.codex import CodexLimits
from token_recap.mistral_usage import mistral_key, parse_budget, read_mistral
from token_recap.windows import Window

PARIS = ZoneInfo("Europe/Paris")
# Sunday afternoon, five days and a bit into a Tuesday-noon week.
NOW = datetime(2026, 10, 11, 15, 40, tzinfo=PARIS)
TUESDAY = datetime(2026, 10, 6, 12, 0, tzinfo=PARIS)


def week(used: float | None) -> Gauge:
    return Gauge("claude-week", "Claude week", used, TUESDAY, TUESDAY + timedelta(days=7))


class PaceTest(unittest.TestCase):
    def test_elapsed_is_the_share_of_the_window_behind_us(self) -> None:
        got = elapsed(week(0.5), NOW)
        assert got is not None
        self.assertAlmostEqual(got, (5 * 24 + 3 + 40 / 60) / (7 * 24))

    def test_margin_is_room_left_against_an_even_pace(self) -> None:
        """65% spent with 73.6% of the week gone: 8.6 points to spare."""
        got = margin(week(0.65), NOW)
        assert got is not None
        self.assertAlmostEqual(got, 0.0861, places=3)

    def test_overspending_gives_a_negative_margin(self) -> None:
        got = margin(week(0.90), NOW)
        assert got is not None
        self.assertLess(got, 0)

    def test_projection_extends_the_current_rate_to_the_reset(self) -> None:
        got = projected(week(0.65), NOW)
        assert got is not None
        self.assertAlmostEqual(got, 0.65 / 0.7361, places=3)

    def test_no_projection_this_early_in_a_window(self) -> None:
        """An hour in, one busy session would extrapolate to nonsense."""
        early = TUESDAY + timedelta(hours=1)
        self.assertIsNone(projected(week(0.05), early))
        self.assertIsNone(runs_out_at(week(0.05), early))

    def test_runs_out_before_the_reset_at_the_current_rate(self) -> None:
        """90% in 5d 3h40m: the rest goes in about 13.7 hours."""
        got = runs_out_at(week(0.90), NOW)
        assert got is not None
        spent = NOW - TUESDAY
        self.assertEqual(got, TUESDAY + spent / 0.90)
        self.assertLess(got, TUESDAY + timedelta(days=7))

    def test_an_even_pace_does_not_run_out(self) -> None:
        self.assertIsNone(runs_out_at(week(0.65), NOW))

    def test_a_spent_allowance_has_already_run_out(self) -> None:
        self.assertEqual(runs_out_at(week(1.0), NOW), NOW)

    def test_nothing_is_computed_without_a_reading_or_a_window(self) -> None:
        blind = Gauge("x", "X", None, TUESDAY, TUESDAY + timedelta(days=7))
        open_ended = Gauge("x", "X", 0.3)
        for gauge in (blind, open_ended):
            self.assertIsNone(margin(gauge, NOW))
            self.assertIsNone(projected(gauge, NOW))
            self.assertIsNone(runs_out_at(gauge, NOW))


class ToneTest(unittest.TestCase):
    def test_under_pace_is_fine(self) -> None:
        self.assertEqual(tone(week(0.65), NOW), "ok")

    def test_well_under_pace_says_there_is_room_to_spare(self) -> None:
        """37% used with 74% of the week gone: budget left on the table."""
        self.assertEqual(tone(week(0.37), NOW), "under")

    def test_early_in_a_window_nothing_is_called_underused(self) -> None:
        early = TUESDAY + timedelta(hours=12)  # 7% of the week gone
        gauge = Gauge("w", "W", 0.0, TUESDAY, TUESDAY + timedelta(days=7))
        self.assertEqual(tone(gauge, early), "ok")

    def test_slightly_over_pace_is_a_warning(self) -> None:
        """Any overspend runs out before the reset at the current rate, so
        the tone grades by how far over, not by whether it runs out."""
        self.assertEqual(tone(week(0.80), NOW), "warn")

    def test_well_over_pace_is_over(self) -> None:
        self.assertEqual(tone(week(0.90), NOW), "over")

    def test_a_spent_allowance_is_over_whatever_the_pace(self) -> None:
        late = TUESDAY + timedelta(days=6, hours=23)
        self.assertEqual(tone(week(1.0), late), "over")

    def test_no_reading_is_unknown(self) -> None:
        self.assertEqual(tone(week(None), NOW), "unknown")

    def test_an_old_reading_is_stale_whatever_it_says(self) -> None:
        old = Gauge(
            "m", "M", 0.1, TUESDAY, TUESDAY + timedelta(days=7),
            as_of=NOW - timedelta(hours=2),
        )
        self.assertTrue(is_stale(old, NOW))
        self.assertEqual(tone(old, NOW), "stale")

    def test_an_unpaced_gauge_goes_by_how_full_it_is(self) -> None:
        def five_hour(used: float) -> Gauge:
            return Gauge("c", "C", used, NOW - timedelta(hours=1), NOW + timedelta(hours=4), paced=False)

        self.assertEqual(tone(five_hour(0.5), NOW), "ok")
        self.assertEqual(tone(five_hour(0.75), NOW), "warn")
        self.assertEqual(tone(five_hour(0.95), NOW), "over")


class BarTest(unittest.TestCase):
    def test_fill_then_empty(self) -> None:
        self.assertEqual(bar(0.5, None, 10), "█████░░░░░")

    def test_the_marker_sits_where_an_even_pace_would_be(self) -> None:
        self.assertEqual(bar(0.2, 0.6, 10), "██░░░░│░░░")

    def test_the_marker_shows_inside_the_fill_too(self) -> None:
        self.assertEqual(bar(0.8, 0.3, 10), "███┃████░░")

    def test_the_marker_never_falls_off_the_end(self) -> None:
        self.assertEqual(bar(0.0, 1.0, 4), "░░░│")

    def test_out_of_range_readings_are_clamped(self) -> None:
        self.assertEqual(bar(1.4, None, 4), "████")
        self.assertEqual(bar(-0.2, None, 4), "░░░░")


def limits(session: tuple[float, datetime | None] | None) -> ClaudeLimits:
    end = TUESDAY + timedelta(days=7)
    return ClaudeLimits(
        Window("Tue 12:00 live", TUESDAY, end), 0.65, "", True, session, NOW
    )


class ClaudeSourceTest(unittest.TestCase):
    def test_a_five_hour_gauge_then_a_paced_weekly_one(self) -> None:
        resets = NOW + timedelta(hours=2)
        five, weekly = claude_gauges(limits((0.4, resets)), NOW)
        self.assertEqual((five.key, five.used, five.paced), ("claude-5h", 0.4, False))
        self.assertEqual((five.start, five.end), (resets - timedelta(hours=5), resets))
        self.assertEqual((weekly.key, weekly.used, weekly.paced), ("claude-week", 0.65, True))
        self.assertEqual((weekly.start, weekly.end), (TUESDAY, TUESDAY + timedelta(days=7)))
        self.assertEqual(weekly.as_of, NOW)

    def test_an_idle_five_hour_window_is_empty_with_no_clock(self) -> None:
        five, _ = claude_gauges(limits((0.0, None)), NOW)
        self.assertEqual(five.used, 0.0)
        self.assertIsNone(five.end)

    def test_no_answer_still_gives_both_gauges_saying_why(self) -> None:
        gauges = claude_gauges(None, NOW, problem="usage endpoint unreachable")
        self.assertEqual([g.key for g in gauges], ["claude-5h", "claude-week"])
        for gauge in gauges:
            self.assertIsNone(gauge.used)
            self.assertEqual(gauge.problem, "usage endpoint unreachable")


class CodexSourceTest(unittest.TestCase):
    def _limits(self, short: tuple[float, datetime] | None) -> CodexLimits:
        window = Window("Tue 12:00 live", TUESDAY, TUESDAY + timedelta(days=7))
        return CodexLimits(window, 0.3, NOW, short)

    def test_a_weekly_only_plan_gives_one_paced_gauge(self) -> None:
        (weekly,) = codex_gauges(self._limits(None), NOW)
        self.assertEqual((weekly.key, weekly.used, weekly.paced), ("codex-week", 0.3, True))
        self.assertEqual((weekly.start, weekly.end), (TUESDAY, TUESDAY + timedelta(days=7)))
        self.assertEqual(weekly.as_of, NOW)

    def test_a_short_window_comes_first_and_unpaced(self) -> None:
        resets = NOW + timedelta(hours=2)
        five, weekly = codex_gauges(self._limits((0.4, resets)), NOW)
        self.assertEqual((five.key, five.used, five.paced), ("codex-5h", 0.4, False))
        self.assertEqual(five.end, resets)
        self.assertEqual(weekly.key, "codex-week")

    def test_no_reading_says_why(self) -> None:
        (weekly,) = codex_gauges(None, NOW, problem="not logged in")
        self.assertIsNone(weekly.used)
        self.assertEqual(weekly.problem, "not logged in")


# The shape statusline.rb writes (see its RATE_LIMITS_CACHE).
STATUSLINE_BLOB = {
    "fetched_at": int(NOW.timestamp()) - 60,
    "five_hour": {"used_percentage": 40, "resets_at": int((NOW + timedelta(hours=2)).timestamp())},
    "seven_day": {"used_percentage": 65, "resets_at": int((TUESDAY + timedelta(days=7)).timestamp())},
}


class StatuslineSourceTest(unittest.TestCase):
    """What Claude Code hands its status line, kept on disk: no network."""

    def test_both_windows_and_the_time_they_were_seen(self) -> None:
        got = limits_from_statusline(STATUSLINE_BLOB, NOW)
        assert got is not None and got.session is not None
        self.assertAlmostEqual(got.used, 0.65)
        self.assertEqual(got.window.end, TUESDAY + timedelta(days=7))
        self.assertEqual(got.window.start, TUESDAY)
        self.assertEqual(got.session, (0.40, NOW + timedelta(hours=2)))
        self.assertEqual(got.as_of, NOW - timedelta(minutes=1))

    def test_a_closed_week_says_nothing(self) -> None:
        later = TUESDAY + timedelta(days=8)
        self.assertIsNone(limits_from_statusline(STATUSLINE_BLOB, later))

    def test_a_five_hour_window_past_its_reset_is_empty(self) -> None:
        got = limits_from_statusline(STATUSLINE_BLOB, NOW + timedelta(hours=3))
        assert got is not None
        self.assertEqual(got.session, (0.0, None))

    def test_a_malformed_blob_says_nothing(self) -> None:
        self.assertIsNone(limits_from_statusline({"fetched_at": 1, "seven_day": {}}, NOW))
        self.assertIsNone(limits_from_statusline({}, NOW))


# What /v1/billing/budget answers, trimmed.
MISTRAL = {
    "vibe_budget": {
        "usage_percentage": 0.6757141233333334,
        "initial_budget": 255.0,
        "currency": "EUR",
        "reset_at": "2026-11-01T00:00:00Z",
        "payg_enabled": False,
    },
    "api_budget": {
        "usage_percentage": 50.0,
        "initial_budget": 25.5,
        "currency": "EUR",
        "reset_at": "2026-11-01T00:00:00Z",
    },
}


class MistralSourceTest(unittest.TestCase):
    def _budget(self, body: object = MISTRAL):
        budget = parse_budget(body)
        assert budget is not None
        return budget

    def test_the_month_runs_up_to_the_stated_reset(self) -> None:
        gauge = mistral_gauge(self._budget(), NOW)
        utc = timezone.utc
        self.assertEqual(gauge.start, datetime(2026, 10, 1, tzinfo=utc))
        self.assertEqual(gauge.end, datetime(2026, 11, 1, tzinfo=utc))
        assert gauge.used is not None
        self.assertAlmostEqual(gauge.used, 0.006757, places=6)
        self.assertEqual(gauge.detail, "1.72 € of 255 €")
        self.assertIsNone(gauge.as_of)  # read live

    def test_a_january_reset_reaches_back_into_december(self) -> None:
        body = json.loads(json.dumps(MISTRAL))
        body["vibe_budget"]["reset_at"] = "2027-01-01T00:00:00Z"
        gauge = mistral_gauge(self._budget(body), NOW)
        self.assertEqual(gauge.start, datetime(2026, 12, 1, tzinfo=timezone.utc))

    def test_no_budget_says_why(self) -> None:
        gauge = mistral_gauge("billing endpoint answered HTTP 401", NOW)
        self.assertIsNone(gauge.used)
        self.assertEqual(gauge.problem, "billing endpoint answered HTTP 401")

    def test_the_api_budget_is_not_the_vibe_one(self) -> None:
        self.assertIsNone(parse_budget({"api_budget": MISTRAL["api_budget"]}))

    def test_a_malformed_answer_is_no_budget(self) -> None:
        self.assertIsNone(parse_budget({"vibe_budget": {"usage_percentage": "x"}}))
        self.assertIsNone(parse_budget([]))

    def test_the_key_comes_from_the_environment_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dotenv = Path(tmp) / ".env"
            dotenv.write_text("OTHER=1\nMISTRAL_API_KEY='from-file'\n")
            self.assertEqual(mistral_key({"MISTRAL_API_KEY": "from-env"}, dotenv), "from-env")
            self.assertEqual(mistral_key({}, dotenv), "from-file")
            self.assertIsNone(mistral_key({}, Path(tmp) / "missing"))

    def test_without_a_key_nothing_is_asked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            got = read_mistral({}, Path(tmp) / "missing")
        self.assertIsInstance(got, str)
        self.assertIn("no Mistral API key", str(got))


class RenderTest(unittest.TestCase):
    def test_a_paced_line_shows_the_pace_room_and_reset(self) -> None:
        line = render_line(week(0.65), NOW)
        self.assertIn("Claude week", line)
        self.assertIn(" 65%", line)
        self.assertIn("│", line)  # the even-pace tick
        self.assertIn("pace 74%", line)
        self.assertIn("+9 room", line)
        self.assertIn("→ 88% at reset", line)
        self.assertIn("resets in 1d 20h 20m", line)

    def test_an_overspent_line_says_when_it_runs_out(self) -> None:
        line = render_line(week(0.90), NOW)
        self.assertIn("-16 over", line)
        self.assertIn("out Mon 05:24", line)

    def test_an_unpaced_line_marks_the_even_pace_but_says_nothing_of_it(self) -> None:
        """The tick still shows a session going too fast; words like "room"
        would only tell off a window that opened on an idle hour."""
        five = Gauge("claude-5h", "Claude 5h", 0.4, NOW - timedelta(hours=1), NOW + timedelta(hours=4), paced=False)
        line = render_line(five, NOW)
        self.assertNotIn("pace", line)
        self.assertIn("┃", line)  # 20% of the window gone, inside the 40% fill
        self.assertIn("resets in 4h 0m", line)

    def test_json_gives_an_unpaced_gauge_its_elapsed_share_and_no_verdict_on_it(self) -> None:
        five = Gauge("claude-5h", "Claude 5h", 0.4, NOW - timedelta(hours=1), NOW + timedelta(hours=4), paced=False)
        got = as_json([five], NOW)[0]
        self.assertAlmostEqual(got["elapsed"], 0.2)
        self.assertIsNone(got["margin"])
        self.assertEqual(got["tone"], "ok")  # by how full it is, not by pace

    def test_a_missing_reading_says_why(self) -> None:
        line = render_line(mistral_gauge("billing endpoint unreachable", NOW), NOW)
        self.assertIn("billing endpoint unreachable", line)

    def test_a_stale_reading_says_how_old(self) -> None:
        gauge = Gauge(
            "claude-week", "Claude week", 0.5, TUESDAY, TUESDAY + timedelta(days=7),
            as_of=datetime(2026, 10, 11, 13, 30, tzinfo=timezone.utc),
        )
        self.assertIn("as of 11 Oct 15:30", render_line(gauge, NOW + timedelta(hours=3)))

    def test_json_carries_the_verdict_not_just_the_reading(self) -> None:
        got = as_json([week(0.90)], NOW)[0]
        self.assertEqual(got["key"], "claude-week")
        self.assertEqual(got["tone"], "over")
        self.assertAlmostEqual(got["margin"], 0.736 - 0.90, places=2)
        self.assertEqual(got["resets_at"], "2026-10-13T12:00:00+02:00")
        self.assertIsNotNone(got["runs_out_at"])
        json.dumps(got)  # every field must serialise

    def test_json_carries_each_phrase_apart_for_the_panel_to_lay_out(self) -> None:
        got = as_json([week(0.65)], NOW)[0]["phrases"]
        self.assertEqual(got["pace"], "pace 74%")
        self.assertEqual(got["room"], "+9 room")
        self.assertEqual(got["outlook"], "→ 88% at reset")
        self.assertEqual(got["resets"], "resets in 1d 20h 20m")
        self.assertEqual(got["as_of"], "")

    def test_json_says_in_plain_words_how_the_pace_stands(self) -> None:
        said = [as_json([week(u)], NOW)[0]["phrases"]["status"] for u in (0.37, 0.65, 0.80, 0.95, None)]
        self.assertEqual(said, ["room to spare", "on track", "a bit fast", "too fast", "no reading"])

    def test_json_gives_the_reset_to_the_hour_or_minute(self) -> None:
        """A glance needs "1d 20h", not "1d 20h 20m"."""
        self.assertEqual(as_json([week(0.65)], NOW)[0]["phrases"]["resets_short"], "resets in 1d 20h")
        soon = Gauge("h", "H", 0.5, NOW - timedelta(hours=23), NOW + timedelta(minutes=8))
        self.assertEqual(as_json([soon], NOW)[0]["phrases"]["resets_short"], "resets in 8m")

    def test_an_overspent_gauge_says_when_it_runs_out(self) -> None:
        got = as_json([week(0.90)], NOW)[0]["phrases"]
        self.assertEqual((got["room"], got["outlook"]), ("-16 over", "out Mon 05:24"))

    def test_json_carries_the_line_a_menu_shows(self) -> None:
        """The panel extension shows these lines as they are, so the wording
        lives in one place, here."""
        got = as_json([week(0.65)], NOW)[0]
        self.assertEqual(got["line"], render_line(week(0.65), NOW))


if __name__ == "__main__":
    unittest.main()
