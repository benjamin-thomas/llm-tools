"""Every percentage on the status line's first line is a remaining amount,
while /usage shows used ones: the meters share one bracket tagged "left".
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

STATUSLINE = Path(__file__).resolve().parent.parent / "claude-code" / "statusline.rb"
ANSI = re.compile(r"\033\[[0-9;]*m")


def first_line(payload: dict[str, object], cache_home: Path) -> str:
    out = subprocess.run(
        ["ruby", str(STATUSLINE)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
        env={"XDG_CACHE_HOME": str(cache_home), "HOME": str(cache_home)},
    )
    return ANSI.sub("", out.stdout.splitlines()[0])


@unittest.skipUnless(shutil.which("ruby"), "ruby not installed")
class StatuslineFirstLineTest(unittest.TestCase):
    def test_every_quota_reads_as_left(self) -> None:
        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmp:
            cache_home = Path(tmp)
            # A fresh model-windows cache, so the render forks no fetch.
            windows = cache_home / "claude-statusline" / "model-windows.json"
            windows.parent.mkdir(parents=True)
            windows.write_text(
                json.dumps({"fetched_at": now, "windows": [{"name": "fable", "used": 30}]})
            )

            line = first_line(
                {
                    "model": {"display_name": "Opus"},
                    "context_window": {"remaining_percentage": 72},
                    "rate_limits": {
                        "five_hour": {"used_percentage": 66, "resets_at": now + 3600},
                        "seven_day": {"used_percentage": 20, "resets_at": now + 86400},
                    },
                },
                cache_home,
            )

        self.assertRegex(
            line, r"⟨ ctx: 72% · 5h: 34% ↻\S+ · 7d: 80% ↻\S+ · fable: 70% ⟩ left$"
        )


if __name__ == "__main__":
    unittest.main()
