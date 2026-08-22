from __future__ import annotations

import unittest

from token_recap.format import bar, compact_count, vis_len


class FormatHelpersTest(unittest.TestCase):
    def test_compact_count(self) -> None:
        self.assertEqual(compact_count(289_839_269), "289.8M")
        self.assertEqual(compact_count(6_881_322), "6.9M")
        self.assertEqual(compact_count(27_837), "27,837")
        self.assertEqual(compact_count(150_000), "150k")

    def test_bar_bounds(self) -> None:
        self.assertEqual(bar(0, 4), "░░░░")
        self.assertEqual(bar(1, 4), "████")
        self.assertEqual(len(bar(0.5, 10)), 10)

    def test_vis_len_strips_ansi(self) -> None:
        raw = "hello"
        colored = "\033[36mhello\033[0m"
        self.assertEqual(vis_len(raw), vis_len(colored))


if __name__ == "__main__":
    unittest.main()
