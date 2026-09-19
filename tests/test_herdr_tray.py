"""herdr-tray is driven the way the panel drives it: `herdr-tray once` prints
the state the indicator would paint, so every rule below is checked without a
display, a GTK import or a live sandbox.

Two harnesses. Most cases stub `herdr-hub` with a canned payload, which pins
the state machine. The last three run the *real* Ruby hub over a stubbed
`herdr`, which is what pins the contract between the two programs — above all
that a sandbox which never answers surfaces as unknown rather than as quiet.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests.test_herdr_hub import HERDR_STUB, agent_list_reply

REPO = Path(__file__).resolve().parent.parent
TRAY = REPO / "herdr-tray"
HUB = REPO / "herdr-hub"

# Only the system interpreter carries the PyGObject typelibs, and an
# interactive PATH may shadow it. `once` never imports gi, but run the real
# shebang's interpreter anyway so the tests exercise what the panel exercises.
PYTHON = "/usr/bin/python3"

HUB_STUB = """#!/bin/sh
# `herdr-hub json` with a canned answer: the payload is whatever the test put
# in HUB_JSON, so the state machine is exercised without Ruby or a registry.
printf '%s' "$HUB_JSON"
"""


def sandbox(project: str, *agents: dict[str, str] | None) -> dict[str, Any]:
    """One `herdr-hub json` entry. A lone None agent is the hub saying the
    socket is alive but its herdr never answered."""
    listed: Any = None if agents == (None,) else list(agents)
    return {
        "project": project,
        "tmux_session": os.path.basename(project),
        "tmux_window_id": "@1",
        "herdr_socket": f"/tmp/{os.path.basename(project)}/herdr.sock",
        "pid": 1,
        "agents": listed,
        "marker": None,
    }


def agent(status: str, title: str = "some task") -> dict[str, str]:
    return {"status": status, "tab": "w1:t1", "agent": "claude", "title": title}


class TrayStateTest(unittest.TestCase):
    """The state machine, against a canned herdr-hub."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tray-test-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()

    def write_hub(self, body: str = HUB_STUB) -> None:
        stub = self.bin / "herdr-hub"
        stub.write_text(body)
        stub.chmod(0o755)

    def tray_env(self, payload: Any = None, **extra: str) -> dict[str, str]:
        env = os.environ.copy()
        # Only the stub dir: nothing may fall through to the real herdr-hub.
        env["PATH"] = str(self.bin)
        env["HUB_JSON"] = json.dumps(payload) if payload is not None else ""
        env.update(extra)
        return env

    def run_tray(self, payload: Any = None, **extra: str) -> dict[str, Any]:
        out = subprocess.run(
            [PYTHON, str(TRAY), "once"], capture_output=True, text=True,
            check=True, env=self.tray_env(payload, **extra),
        )
        self.assertEqual("", out.stderr, "once must never leak a traceback")
        parsed: dict[str, Any] = json.loads(out.stdout)
        return parsed

    def test_no_sandboxes_is_a_positive_all_clear(self) -> None:
        self.write_hub()
        st = self.run_tray([])
        self.assertEqual(("quiet", 0, "0", "herdr-quiet"),
                         (st["level"], st["count"], st["label"], st["icon"]))

    def test_working_and_idle_earn_nothing(self) -> None:
        # idle is "finished while you were watching that pane" — herdr-hub's
        # MARKERS deliberately excludes it, and the tray follows its lead.
        self.write_hub()
        st = self.run_tray([sandbox("/code/miriad", agent("working"), agent("idle"))])
        self.assertEqual("quiet", st["level"])
        self.assertEqual(0, st["count"])

    def test_one_done_is_amber(self) -> None:
        self.write_hub()
        st = self.run_tray([sandbox("/code/miriad", agent("done"))])
        self.assertEqual(("done", 1, "1", "herdr-done"),
                         (st["level"], st["count"], st["label"], st["icon"]))
        self.assertEqual("miriad", st["rows"][0]["project"])

    def test_blocked_wins_the_colour_and_the_count_totals(self) -> None:
        self.write_hub()
        st = self.run_tray([sandbox("/code/miriad", agent("done"), agent("blocked"))])
        self.assertEqual(("blocked", 2, "2", "herdr-blocked"),
                         (st["level"], st["count"], st["label"], st["icon"]))
        self.assertEqual(["blocked", "done"], [r["status"] for r in st["rows"]])

    def test_agents_are_counted_not_sandboxes(self) -> None:
        self.write_hub()
        st = self.run_tray([
            sandbox("/code/miriad", agent("blocked"), agent("done")),
            sandbox("/code/llm-tools", agent("done")),
        ])
        self.assertEqual(3, st["count"])
        self.assertEqual({"miriad", "llm-tools"}, {r["project"] for r in st["rows"]})

    def test_a_silent_sandbox_demotes_the_all_clear(self) -> None:
        self.write_hub()
        st = self.run_tray([sandbox("/code/miriad", None)])
        self.assertEqual(("unknown", "?", "herdr-stale"),
                         (st["level"], st["label"], st["icon"]))
        self.assertIn("not answering", st["note"])

    def test_a_silent_sandbox_never_downgrades_an_attention_colour(self) -> None:
        # You already have to go look; grey here would be less information
        # dressed up as more honesty. The "+" says the count is a floor.
        self.write_hub()
        st = self.run_tray([
            sandbox("/code/miriad", None),
            sandbox("/code/llm-tools", agent("blocked")),
        ])
        self.assertEqual(("blocked", 1, "1+"), (st["level"], st["count"], st["label"]))
        self.assertIn("not answering", st["note"])

    def test_unparseable_output_is_unknown(self) -> None:
        self.write_hub("#!/bin/sh\nprintf 'not json'\n")
        self.assertEqual("unknown", self.run_tray()["level"])

    def test_nonzero_exit_is_unknown(self) -> None:
        self.write_hub("#!/bin/sh\nexit 3\n")
        st = self.run_tray()
        self.assertEqual("unknown", st["level"])
        self.assertIn("exited 3", st["note"])

    def test_missing_hub_is_unknown_not_a_crash(self) -> None:
        st = self.run_tray()  # nothing written into the stub dir at all
        self.assertEqual("unknown", st["level"])
        self.assertIn("not on PATH", st["note"])

    def test_a_hung_hub_times_out_into_unknown(self) -> None:
        # Absolute path: PATH holds only the stub dir, and sleep is not a
        # shell builtin, so a bare `sleep` would exit 127 and test the wrong
        # failure mode.
        self.write_hub("#!/bin/sh\n/bin/sleep 30\n")
        st = self.run_tray(HERDR_TRAY_TIMEOUT="1")
        self.assertEqual("unknown", st["level"])
        self.assertIn("timed out", st["note"])

    def test_agent_controlled_text_is_stripped_and_capped(self) -> None:
        # project and title come from inside a sandbox. An ESC would reach a
        # real terminal if one of these lines is ever logged; a 4KB title would
        # stretch the menu off-screen; markup has to stay literal.
        self.write_hub()
        payload = [sandbox("/tmp/a\x1b[31mb",
                           agent("blocked", "<b>x</b>" + "z" * 4000))]
        raw = subprocess.run(
            [PYTHON, str(TRAY), "once"], capture_output=True, text=True,
            check=True, env=self.tray_env(payload),
        ).stdout
        self.assertNotIn("\x1b", raw)
        row = json.loads(raw)["rows"][0]
        self.assertEqual("a[31mb", row["project"])
        self.assertTrue(row["title"].startswith("<b>x</b>"))  # literal, not markup
        self.assertLessEqual(len(row["title"]), 48)

    def test_an_unrecognised_status_is_not_attention(self) -> None:
        self.write_hub()
        st = self.run_tray([sandbox("/code/miriad", agent("weird"))])
        self.assertEqual("quiet", st["level"])

    def test_malformed_payloads_never_crash(self) -> None:
        self.write_hub()
        for payload in ([1, 2, 3], {"not": "a list"}, [{"agents": "nope"}], [None]):
            with self.subTest(payload=payload):
                st = self.run_tray(payload)
                self.assertIn(st["level"], ("quiet", "unknown"))

    def test_shebang_is_the_system_interpreter(self) -> None:
        # Not a style choice: `env python3` finds a version manager's build,
        # which has no PyGObject typelibs, and the indicator never starts.
        self.assertEqual("#!/usr/bin/python3", TRAY.read_text().splitlines()[0])


class TrayOverRealHubTest(unittest.TestCase):
    """The tray against the real Ruby hub, with only `herdr` faked. This is
    what pins the contract between the two programs."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tray-hub-"))
        self.state = self.tmp / "state"
        self.bin = self.tmp / "bin"
        self.state.mkdir()
        self.bin.mkdir()
        self._sockets: list[socket.socket] = []
        self.write_herdr(HERDR_STUB)

    def tearDown(self) -> None:
        for sock in self._sockets:
            sock.close()

    def write_herdr(self, body: str) -> None:
        stub = self.bin / "herdr"
        stub.write_text(body)
        stub.chmod(0o755)

    def add_sandbox(self, name: str, *statuses: str) -> Path:
        sock_path = self.tmp / f"{name}.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(sock_path))
        self._sockets.append(sock)
        Path(f"{sock_path}.reply").write_text(agent_list_reply(*statuses))
        entry = self.state / f"{name}.json"
        entry.write_text(json.dumps({
            "project": f"/home/benjamin/code/{name}",
            "tmux_session": name,
            "tmux_window_id": "@1",
            "herdr_socket": str(sock_path),
            "pid": 1,
        }))
        return entry

    def run_tray(self) -> dict[str, Any]:
        env = os.environ.copy()
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["SANDBOX_AGENT_STATE_DIR"] = str(self.state)
        env["HERDR_HUB"] = str(HUB)  # the real hub, so no shim is needed
        out = subprocess.run(
            [PYTHON, str(TRAY), "once"], capture_output=True, text=True,
            check=True, env=env,
        )
        parsed: dict[str, Any] = json.loads(out.stdout)
        return parsed

    def test_a_blocked_agent_reaches_the_panel(self) -> None:
        self.add_sandbox("miriad", "working", "blocked")
        st = self.run_tray()
        self.assertEqual(("blocked", 1), (st["level"], st["count"]))
        self.assertEqual("miriad", st["rows"][0]["project"])

    def test_a_dead_socket_is_pruned_and_leaves_the_panel_quiet(self) -> None:
        # Polling makes the tray a caller of `entries`, so it prunes too.
        # Racing a `bind s` refresh is harmless: both delete idempotently.
        entry = self.state / "gone.json"
        entry.write_text(json.dumps({
            "project": "/tmp/gone", "tmux_session": "dead", "tmux_window_id": "@9",
            "herdr_socket": str(self.tmp / "missing.sock"), "pid": 1,
        }))
        st = self.run_tray()
        self.assertEqual("quiet", st["level"])
        self.assertFalse(entry.exists())

    def test_a_live_socket_that_never_answers_is_unknown_not_quiet(self) -> None:
        # The case the grey state exists for, and the reason herdr-hub grew
        # keep_unreachable: without it this sandbox vanishes from the payload
        # and the panel paints a reassuring green 0.
        self.add_sandbox("miriad", "working")
        self.write_herdr("#!/bin/sh\nexit 1\n")
        st = self.run_tray()
        self.assertEqual(("unknown", "?"), (st["level"], st["label"]))
        self.assertIn("not answering", st["note"])


if __name__ == "__main__":
    unittest.main()
