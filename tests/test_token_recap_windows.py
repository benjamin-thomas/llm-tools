from __future__ import annotations

import unittest
from datetime import datetime

from zoneinfo import ZoneInfo

from token_recap.windows import RESETS, last_weekly_reset, next_weekly_reset

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


if __name__ == "__main__":
    unittest.main()
