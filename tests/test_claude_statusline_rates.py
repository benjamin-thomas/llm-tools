"""The status line keeps its own copy of the rate card -- a render cannot
afford to start Python. This holds what the script publishes against native.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from token_recap.native import (
    CLAUDE_WRITE_1H,
    CLAUDE_WRITE_5M,
    claude_cache_read,
    claude_card,
)

STATUSLINE = Path(__file__).resolve().parent.parent / "claude-code" / "statusline.rb"


def published_card() -> dict[str, dict[str, float]]:
    out = subprocess.run(
        ["ruby", str(STATUSLINE), "--rates"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("ruby"), "ruby not installed")
class StatuslineRateCardTest(unittest.TestCase):
    """Published spreads (write price minus read price) must match native.py."""

    def test_the_script_publishes_a_card(self) -> None:
        self.assertTrue(STATUSLINE.exists(), f"missing {STATUSLINE}")
        self.assertTrue(published_card(), "--rates published nothing")

    def test_every_published_spread_matches_native(self) -> None:
        for model, spreads in published_card().items():
            card = claude_card(model)
            self.assertIsNotNone(card, f"native.py prices no card for {model}")
            assert card is not None
            base = card[0]
            read = claude_cache_read(model)
            for ttl, write in (("1h", CLAUDE_WRITE_1H), ("5m", CLAUDE_WRITE_5M)):
                with self.subTest(model=model, ttl=ttl):
                    self.assertAlmostEqual(
                        spreads[ttl], (write - read) * base, places=9
                    )

    def test_fable_5_1_is_priced_apart_from_fable_5(self) -> None:
        """The bug this exists for: one family, two cache-read rates."""
        card = published_card()
        self.assertGreater(card["claude-fable-5-1"]["1h"], card["claude-fable-5"]["1h"])


if __name__ == "__main__":
    unittest.main()
