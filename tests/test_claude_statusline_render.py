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


def lines(payload: dict[str, object], cache_home: Path) -> list[str]:
    out = subprocess.run(
        ["ruby", str(STATUSLINE)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
        env={"XDG_CACHE_HOME": str(cache_home), "HOME": str(cache_home)},
    )
    return ANSI.sub("", out.stdout).splitlines()


def first_line(payload: dict[str, object], cache_home: Path) -> str:
    return lines(payload, cache_home)[0]


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


@unittest.skipUnless(shutil.which("ruby"), "ruby not installed")
class StatuslineDirectoryTest(unittest.TestCase):
    """The launch directory heads the status line, cut to its last two path
    components, starting in the meters' column. A cd away from it follows."""

    def render(self, workspace: dict[str, str], model: str = "Opus 5.5") -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            payload: dict[str, object] = {"model": {"display_name": model}, "workspace": {
                k: v.replace("~", str(home)) for k, v in workspace.items()
            }}
            return lines(payload, home)[:2]

    def test_launch_directory_above_the_model_line(self) -> None:
        out = self.render({"project_dir": "~/code/github.com/me/app", "current_dir": "~/code/github.com/me/app"})
        self.assertEqual(out[0], "Project:    me/app")
        self.assertTrue(out[1].startswith("Opus 5.5  ⟨ ctx:"), out[1])

    def test_path_aligns_with_meters_whichever_label_is_longer(self) -> None:
        for model in ("Haiku", "Sonnet 5.5"):
            out = self.render({"project_dir": "~/code/app"}, model)
            self.assertEqual(out[1].index("ctx:"), out[0].index("code/app"), out)

    def test_short_paths_stay_whole(self) -> None:
        for launch, shown in (("/", "/"), ("/usr", "/usr"), ("~", "~"), ("~/app", "~/app")):
            out = self.render({"project_dir": launch})
            self.assertEqual(out[0], f"Project:    {shown}")

    def test_current_directory_inside_project_is_relative(self) -> None:
        out = self.render({"project_dir": "~/code/me/app", "current_dir": "~/code/me/app/src"})
        self.assertEqual(out[0], "Project:    me/app → src")

    def test_current_directory_outside_project_is_cut_too(self) -> None:
        out = self.render({"project_dir": "~/code/me/app", "current_dir": "~/code/me/lib"})
        self.assertEqual(out[0], "Project:    me/app → me/lib")

    def test_no_workspace_no_directory_line(self) -> None:
        out = self.render({})
        self.assertTrue(out[0].startswith("Opus 5.5  ⟨"), out[0])
        self.assertNotIn("Project", "\n".join(out))


if __name__ == "__main__":
    unittest.main()
