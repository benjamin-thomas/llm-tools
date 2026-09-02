from __future__ import annotations

import unittest
from datetime import datetime

from zoneinfo import ZoneInfo

from token_recap.buckets import TokenBuckets
from token_recap.format import (
    MIN_BOX,
    Palette,
    ProviderView,
    bar,
    compact_count,
    models_lines,
    render_report,
    vis_len,
)
from token_recap.windows import Window


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


class ModelLinesTest(unittest.TestCase):
    PLAIN = Palette(on=False)

    @staticmethod
    def _buckets(models: dict[str, int]) -> TokenBuckets:
        b = TokenBuckets()
        for model, n in models.items():
            for _ in range(n):
                b.add(model=model)
        return b

    def test_one_row_per_model_none_truncated(self) -> None:
        """A joined list lost its tail to the box edge; rows do not."""
        b = self._buckets(
            {
                "claude-opus-5": 5,
                "claude-fable-5": 4,
                "glm-5.3-flash": 3,
                "claude-haiku-4-5-20251001": 2,
                "claude-sonnet-5": 1,
            }
        )
        rows = models_lines(b, self.PLAIN, 30)
        counts = [r for r in rows[1:] if "$/MTok" not in r]
        self.assertEqual(len(counts), 5)  # one count row per model, none merged
        self.assertNotIn("…", "".join(rows))
        for name in ("opus-5", "fable-5", "glm-5.3-flash", "haiku-4-5", "sonnet-5"):
            self.assertTrue(any(name in row for row in rows), name)

    def test_a_name_is_never_abbreviated(self) -> None:
        """The box grows to fit the name; the name is not cut to fit the box."""
        long = "openrouter/deepseek-ai/DeepSeek-V4-Pro-0813"
        rows = models_lines(self._buckets({long: 1}), self.PLAIN, len(long))
        self.assertIn(long, rows[1])
        self.assertNotIn("…", rows[1])

    def test_every_row_shares_one_width(self) -> None:
        rows = models_lines(self._buckets({"gpt-5.1-codex-mini": 123_456}), self.PLAIN, 30)
        self.assertEqual(len({vis_len(r) for r in rows}), 1)
        self.assertGreaterEqual(MIN_BOX, 80)

    def test_a_free_section_drops_the_price_columns(self) -> None:
        b = TokenBuckets()
        b.add(model="ollama/x", provider="ollama", output=5)
        rows = models_lines(b, self.PLAIN, 20, prices=False)
        for gone in ("total", "$in/M", "$"):
            self.assertNotIn(gone, rows[0])
        self.assertIn("out", rows[0])

    def test_every_column_is_labelled(self) -> None:
        rows = models_lines(self._buckets({"claude-opus-5": 2, "grok-4.6": 1}), self.PLAIN, 20)
        for column in ("model", "calls", "in", "cached", "out", "total"):
            self.assertIn(column, rows[0])

    def test_rows_are_ordered_by_cost(self) -> None:
        b = TokenBuckets()
        b.add(model="cheap", output=1, native_usd=1.0)
        b.add(model="dear", output=1, native_usd=9.0)
        rows = models_lines(b, self.PLAIN, 30)
        self.assertIn("dear", rows[1])
        self.assertIn("cheap", rows[2])

    def test_the_unit_price_sits_on_the_model_row(self) -> None:
        b = TokenBuckets()
        b.add(model="claude-opus-5", provider="anthropic", output=1)
        rows = models_lines(b, self.PLAIN, 30)
        self.assertEqual(len(rows), 2)  # header plus one row, no second line
        # input $5.00, cache read $0.50 (0.1x), output $25.00
        for rate in ("$5.00", "$0.50", "$25.00"):
            self.assertIn(rate, rows[1])

    def test_an_unknown_model_gets_dashes_not_an_invented_rate(self) -> None:
        b = TokenBuckets()
        b.add(model="something-local", provider="ollama", output=1)
        rows = models_lines(b, self.PLAIN, 30)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].split()[-3:], ["-", "-", "-"])  # the rate columns

    def test_a_free_card_reads_as_zero_not_as_unknown(self) -> None:
        b = TokenBuckets()
        b.add(model="x-preview-f-free", provider="opencode", output=1)
        rows = models_lines(b, self.PLAIN, 30)
        # "$0" is a published rate of nothing; "-" would mean none was found.
        self.assertEqual(rows[1].split()[-3:], ["$0", "$0", "$0"])

    def test_the_header_names_every_column(self) -> None:
        b = TokenBuckets()
        b.add(model="claude-opus-5", provider="anthropic", output=1)
        header = models_lines(b, self.PLAIN, 30)[0]
        for column in ("model", "calls", "in", "cached", "out", "total",
                       "$in/M", "$cache/M", "$out/M"):
            self.assertIn(column, header)

    def test_a_model_carries_its_own_buckets(self) -> None:
        b = TokenBuckets()
        b.add(model="m", uncached=10, cache_read=20, cache_write=5, output=3, native_usd=1.5)
        b.add(model="m", uncached=1, output=1)
        use = b.models["m"]
        self.assertEqual((use.calls, use.uncached, use.output), (2, 11, 4))
        self.assertEqual(use.cached, 25)  # reads and writes together
        self.assertAlmostEqual(use.native_usd, 1.5)

    def test_synthetic_is_not_a_request(self) -> None:
        rows = models_lines(self._buckets({"<synthetic>": 3}), self.PLAIN, 20)
        self.assertEqual(rows, ["  no model breakdown"])


class BoxWidthTest(unittest.TestCase):
    NOW = datetime(2026, 8, 29, 22, 0, tzinfo=ZoneInfo("Europe/Paris"))

    def _view(self, key: str, models: dict[str, int]) -> ProviderView:
        b = TokenBuckets()
        for model, n in models.items():
            for _ in range(n):
                b.add(model=model, provider="anthropic", output=1)
        window = Window("w", self.NOW, self.NOW)
        return ProviderView(key=key, title=key, window=window, buckets=b)

    def _report(self) -> list[str]:
        views = [
            self._view("short", {"claude-opus-5": 1}),
            self._view("long", {"openrouter/deepseek-ai/DeepSeek-V4-Pro-0813": 1}),
        ]
        combined = TokenBuckets()
        text = render_report(self.NOW, False, 1, views, combined, color=False)
        return text.splitlines()

    def test_every_box_shares_one_width(self) -> None:
        widths = {vis_len(line) for line in self._report() if line.strip()}
        self.assertEqual(len(widths), 1)

    def test_a_short_table_is_not_stretched_by_a_long_one(self) -> None:
        """Trailing space is fine; a name column padded to another section's
        longest model is not."""
        lines = self._report()
        headers = [ln for ln in lines if "model " in ln and "calls" in ln]
        self.assertEqual(len(headers), 2)
        short, long = (ln.index("calls") for ln in headers)
        self.assertLess(short, long)

    def test_the_long_name_is_still_whole(self) -> None:
        joined = "\n".join(self._report())
        self.assertIn("openrouter/deepseek-ai/DeepSeek-V4-Pro-0813", joined)


if __name__ == "__main__":
    unittest.main()
