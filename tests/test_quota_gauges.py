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
from quota_gauges.sources import claude_gauges, hyper_gauge, mistral_gauge, read_mistral, record_balance
from token_recap.hyper_usage import hyper_key
from token_recap.windows import hyper_window
from quota_gauges.cli import as_json, render_line
from token_recap.anthropic_usage import ClaudeLimits, limits_from_statusline
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


class HyperLogTest(unittest.TestCase):
    """Hyper does not say when it refills, so every change of the balance is
    kept with its time: the jump back up shows when the day really turns."""

    def test_a_change_is_appended_and_a_repeat_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "sub" / "hyper-balance.log"
            record_balance(log, NOW, 86)
            record_balance(log, NOW + timedelta(minutes=1), 86)
            record_balance(log, NOW + timedelta(minutes=2), 250)
            lines = log.read_text().splitlines()
        self.assertEqual(lines, [
            f"{NOW.isoformat()} 86",
            f"{(NOW + timedelta(minutes=2)).isoformat()} 250",
        ])

    def test_no_balance_records_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "hyper-balance.log"
            record_balance(log, NOW, None)
            self.assertFalse(log.exists())


class HyperSourceTest(unittest.TestCase):
    def test_the_day_runs_from_the_last_refresh(self) -> None:
        """16:33 UTC, which is 18:33 in Paris in summer."""
        start, end = hyper_window(NOW)
        self.assertEqual(start, datetime(2026, 10, 10, 18, 33, tzinfo=PARIS))
        self.assertEqual(end, datetime(2026, 10, 11, 18, 33, tzinfo=PARIS))

    def test_after_the_refresh_the_new_day_has_begun(self) -> None:
        start, _ = hyper_window(datetime(2026, 10, 11, 18, 40, tzinfo=PARIS))
        self.assertEqual(start, datetime(2026, 10, 11, 18, 33, tzinfo=PARIS))

    def test_the_refresh_keeps_to_utc_across_the_clock_change(self) -> None:
        """From 25 Oct Paris is UTC+1, so the refresh moves to 17:33 there,
        and every Hyper day stays 24 hours long."""
        start, end = hyper_window(datetime(2026, 10, 26, 20, 0, tzinfo=PARIS))
        self.assertEqual(start, datetime(2026, 10, 26, 17, 33, tzinfo=PARIS))
        self.assertEqual(end - start, timedelta(hours=24))

    def test_used_is_what_the_balance_no_longer_holds(self) -> None:
        gauge = hyper_gauge(89, NOW)
        assert gauge.used is not None
        self.assertAlmostEqual(gauge.used, 161 / 250)
        self.assertEqual(gauge.detail, "89 HC left of 250")
        self.assertTrue(gauge.paced)

    def test_a_topped_up_balance_reads_as_nothing_used(self) -> None:
        self.assertEqual(hyper_gauge(400, NOW).used, 0.0)

    def test_no_balance_says_why(self) -> None:
        gauge = hyper_gauge(None, NOW, problem="no API key")
        self.assertIsNone(gauge.used)
        self.assertEqual(gauge.problem, "no API key")

    def test_the_key_comes_from_the_environment_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp) / "auth.json"
            auth.write_text(json.dumps({"hyper": {"type": "api", "key": "from-file"}}))
            self.assertEqual(hyper_key({"HYPER_API_KEY": "from-env"}, auth), "from-env")
            self.assertEqual(hyper_key({}, auth), "from-file")

    def test_an_oauth_entry_is_not_an_api_key(self) -> None:
        """pi's short-lived token: refreshing it is pi's job, not ours."""
        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp) / "auth.json"
            auth.write_text(json.dumps({"hyper": {"type": "oauth", "access": "t"}}))
            self.assertIsNone(hyper_key({}, auth))
            self.assertIsNone(hyper_key({}, Path(tmp) / "missing.json"))


MISTRAL = {
    "fetched_at": "2026-10-11T13:30:00Z",
    "budget": {
        "usage_percentage": 0.6757141233333334,
        "initial_budget": 255,
        "currency": "EUR",
        "reset_at": "2026-11-01T00:00:00Z",
        "payg_enabled": False,
    },
}


class MistralSourceTest(unittest.TestCase):
    def test_the_month_runs_up_to_the_stated_reset(self) -> None:
        gauge = mistral_gauge(MISTRAL, NOW)
        utc = timezone.utc
        self.assertEqual(gauge.start, datetime(2026, 10, 1, tzinfo=utc))
        self.assertEqual(gauge.end, datetime(2026, 11, 1, tzinfo=utc))
        assert gauge.used is not None
        self.assertAlmostEqual(gauge.used, 0.006757, places=6)
        self.assertEqual(gauge.detail, "1.72 € of 255 €")
        self.assertEqual(gauge.as_of, datetime(2026, 10, 11, 13, 30, tzinfo=utc))

    def test_a_january_reset_reaches_back_into_december(self) -> None:
        reading = json.loads(json.dumps(MISTRAL))
        reading["budget"]["reset_at"] = "2027-01-01T00:00:00Z"
        gauge = mistral_gauge(reading, NOW)
        self.assertEqual(gauge.start, datetime(2026, 12, 1, tzinfo=timezone.utc))

    def test_no_reading_points_at_the_extension(self) -> None:
        gauge = mistral_gauge(None, NOW)
        self.assertIsNone(gauge.used)
        self.assertIn("Chrome extension", gauge.problem)

    def test_an_extension_error_is_shown_beside_the_last_good_budget(self) -> None:
        reading = dict(MISTRAL, error="HTTP 401", error_at="2026-10-11T15:00:00Z")
        gauge = mistral_gauge(reading, NOW)
        self.assertIsNotNone(gauge.used)
        self.assertIn("HTTP 401", gauge.detail)

    def test_an_extension_error_with_no_budget_yet_is_the_problem(self) -> None:
        gauge = mistral_gauge({"error": "HTTP 401", "error_at": "t"}, NOW)
        self.assertIsNone(gauge.used)
        self.assertIn("HTTP 401", gauge.problem)
        self.assertIn("chat.mistral.ai", gauge.problem)

    def test_a_malformed_reading_is_no_reading(self) -> None:
        self.assertIsNone(mistral_gauge({"budget": {"usage_percentage": "x"}}, NOW).used)

    def test_the_reading_is_read_from_its_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mistral.json"
            self.assertIsNone(read_mistral(path))
            path.write_text(json.dumps(MISTRAL))
            self.assertEqual(read_mistral(path), MISTRAL)
            path.write_text("{not json")
            self.assertIsNone(read_mistral(path))


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

    def test_an_unpaced_line_has_no_pace(self) -> None:
        five = Gauge("claude-5h", "Claude 5h", 0.4, NOW - timedelta(hours=1), NOW + timedelta(hours=4), paced=False)
        line = render_line(five, NOW)
        self.assertNotIn("pace", line)
        self.assertNotIn("│", line)
        self.assertIn("resets in 4h 0m", line)

    def test_a_missing_reading_says_why(self) -> None:
        line = render_line(mistral_gauge(None, NOW), NOW)
        self.assertIn("Chrome extension", line)

    def test_a_stale_reading_says_how_old(self) -> None:
        gauge = mistral_gauge(MISTRAL, NOW + timedelta(hours=3))
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
