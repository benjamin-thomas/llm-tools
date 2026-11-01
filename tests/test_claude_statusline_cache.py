"""The caches are written through a per-process temp file, then renamed. A
render killed in between leaves the temp file behind: later renders sweep the
ones over an hour old, never a write still in flight.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

STATUSLINE = Path(__file__).resolve().parent.parent / "claude-code" / "statusline.rb"


@unittest.skipUnless(shutil.which("ruby"), "ruby not installed")
class StatuslineTempSweepTest(unittest.TestCase):
    def test_sweeps_stale_temp_files_only(self) -> None:
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cache = home / "claude-statusline"
            cache.mkdir()
            # A fresh model-windows cache, so the render forks no fetch.
            (cache / "model-windows.json").write_text(
                json.dumps({"fetched_at": int(now), "windows": []})
            )

            stale = [cache / "rate-limits.json.1802", cache / "model-windows.json.4901"]
            kept = [cache / "rate-limits.json.1856", cache / "model-windows.json.lock"]
            for path in stale + kept:
                path.touch()
            for path in stale + [cache / "model-windows.json.lock"]:
                os.utime(path, (now - 7200, now - 7200))

            subprocess.run(
                ["ruby", str(STATUSLINE)],
                input=json.dumps({"model": {"display_name": "Opus"}}),
                capture_output=True,
                text=True,
                check=True,
                env={"XDG_CACHE_HOME": str(home), "HOME": str(home)},
            )

            self.assertEqual([p.name for p in stale if p.exists()], [])
            self.assertEqual([p.name for p in kept if not p.exists()], [])


if __name__ == "__main__":
    unittest.main()
