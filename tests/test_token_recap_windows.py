from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

from token_recap.windows import (
    RESETS,
    Window,
    last_weekly_reset,
    next_weekly_reset,
    widen_weeks,
)

PARIS = ZoneInfo("Europe/Paris")


class WeeklyResetTest(unittest.TestCase):
    def test_claude_tuesday_before_noon_uses_previous_week(self) -> None:
        now = datetime(2026, 8, 25, 11, 0, tzinfo=PARIS)  # Tuesday
        got = last_weekly_reset(now, RESETS["claude"])
        self.assertEqual(got, datetime(2026, 8, 18, 12, 0, tzinfo=PARIS))

    def test_claude_tuesday_after_noon_uses_today(self) -> None:
        now = datetime(2026, 8, 25, 12, 1, tzinfo=PARIS)
        got = last_weekly_reset(now, RESETS["claude"])
        self.assertEqual(got, datetime(2026, 8, 25, 12, 0, tzinfo=PARIS))

    def test_grok_sunday_just_after_0047(self) -> None:
        now = datetime(2026, 8, 23, 0, 48, tzinfo=PARIS)
        got = last_weekly_reset(now, RESETS["grok"])
        self.assertEqual(got, datetime(2026, 8, 23, 0, 47, tzinfo=PARIS))

    def test_grok_saturday_night_is_previous_sunday(self) -> None:
        now = datetime(2026, 8, 22, 23, 0, tzinfo=PARIS)
        got = last_weekly_reset(now, RESETS["grok"])
        self.assertEqual(got, datetime(2026, 8, 16, 0, 47, tzinfo=PARIS))

    def test_codex_thursday_1228(self) -> None:
        now = datetime(2026, 8, 20, 12, 29, tzinfo=PARIS)  # Thursday
        got = last_weekly_reset(now, RESETS["codex"])
        self.assertEqual(got, datetime(2026, 8, 20, 12, 28, tzinfo=PARIS))
        self.assertEqual(
            next_weekly_reset(now, RESETS["codex"]),
            datetime(2026, 8, 27, 12, 28, tzinfo=PARIS),
        )


class WidenWeeksTest(unittest.TestCase):
    BASE = Window(
        "Tue 12:00 local",
        datetime(2026, 8, 25, 12, 0, tzinfo=PARIS),
        datetime(2026, 9, 1, 12, 0, tzinfo=PARIS),
    )

    def test_one_week_is_the_window_untouched(self) -> None:
        self.assertIs(widen_weeks(self.BASE, 1), self.BASE)

    def test_four_weeks_reaches_back_three_more(self) -> None:
        got = widen_weeks(self.BASE, 4)
        self.assertEqual(got.start, datetime(2026, 8, 4, 12, 0, tzinfo=PARIS))
        self.assertEqual(got.end, self.BASE.end)  # the end never moves
        self.assertEqual(got.end - got.start, timedelta(weeks=4))

    def test_the_label_says_how_wide(self) -> None:
        self.assertEqual(widen_weeks(self.BASE, 4).label, "Tue 12:00 local ×4wk")

    def test_widening_keeps_the_wall_clock_across_a_dst_change(self) -> None:
        """Paris leaves DST on 25 Oct 2026, so 8 weeks is not 8×168 hours."""
        base = Window(
            "Tue 12:00 local",
            datetime(2026, 11, 3, 12, 0, tzinfo=PARIS),
            datetime(2026, 11, 10, 12, 0, tzinfo=PARIS),
        )
        got = widen_weeks(base, 4)
        self.assertEqual(got.start, datetime(2026, 10, 13, 12, 0, tzinfo=PARIS))


if __name__ == "__main__":
    unittest.main()
