from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from quota_gauges.install import install


class InstallTest(unittest.TestCase):
    """GNOME runs a copy out of the repo, refreshed only when asked."""

    ROOT = Path(__file__).resolve().parent.parent

    def _install(self, tmp: str) -> tuple[list[str], Path]:
        dest, extensions = Path(tmp) / "share", Path(tmp) / "extensions"
        return install(self.ROOT, dest, extensions), dest

    def test_the_python_tools_are_copied_and_runnable_from_the_copy(self) -> None:
        """The panel extension runs `quota-gauges --json` from the copy."""
        with tempfile.TemporaryDirectory() as tmp:
            report, dest = self._install(tmp)
            for package in ("token_recap", "quota_gauges"):
                for src in (self.ROOT / package).glob("*.py"):
                    self.assertEqual((dest / package / src.name).read_bytes(), src.read_bytes())
            self.assertFalse(list(dest.rglob("__pycache__")))
            launcher = dest / "quota-gauges"
            self.assertTrue(os.access(launcher, os.X_OK))
            out = subprocess.run([str(launcher), "--help"], capture_output=True, text=True, cwd=tmp)
            self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(all(line.startswith("new ") for line in report))

    def test_the_gnome_extension_is_installed_under_its_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._install(tmp)
            uuid = json.loads((self.ROOT / "quota-gauges-gnome" / "metadata.json").read_text())["uuid"]
            folder = Path(tmp) / "extensions" / uuid
            for name in ("metadata.json", "extension.js"):
                src = self.ROOT / "quota-gauges-gnome" / name
                self.assertEqual((folder / name).read_bytes(), src.read_bytes())

    def test_a_second_run_says_what_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, dest = self._install(tmp)
            (dest / "quota_gauges" / "gauge.py").write_text("tampered")
            report, _ = self._install(tmp)
        changed = [line for line in report if not line.startswith("unchanged ")]
        self.assertGreater(len(report), 1)
        self.assertEqual(len(changed), 1)
        self.assertIn("quota_gauges/gauge.py", changed[0])
        self.assertTrue(changed[0].startswith("changed "))


if __name__ == "__main__":
    unittest.main()
