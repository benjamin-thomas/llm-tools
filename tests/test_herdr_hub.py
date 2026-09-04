"""herdr-hub is driven exactly as the host drives it: a registry directory
of JSON entries, a herdr CLI and a tmux on PATH. Both binaries are stubbed so
the contract (query each sandbox socket, aggregate states, flag tmux, prune
dead entries) holds without a live sandbox or tmux server.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

HUB = Path(__file__).resolve().parent.parent / "herdr-hub"

HERDR_STUB = """#!/bin/sh
# Replies are canned per socket: the file sitting next to each fake socket.
cat "${HERDR_SOCKET_PATH}.reply"
"""

TMUX_STUB = """#!/bin/sh
echo "$@" >> "$TMUX_STUB_LOG"
case "$1" in
  list-sessions) printf 'llm-tools\\nmiriad\\nAdmin\\n' ;;
  list-windows)  printf '@1\\n@2\\n@3\\n' ;;
esac
"""


def agent_list_reply(*statuses: str) -> str:
    agents = [
        {
            "agent_status": s,
            "pane_id": f"w1:p{i}",
            "tab_id": f"w1:t{i}",
            "agent": "claude",
            "terminal_title_stripped": f"task number {i}",
        }
        for i, s in enumerate(statuses, 1)
    ]
    return json.dumps({"id": "cli:agent:list", "result": {"agents": agents, "type": "agent_list"}})


class HerdrHubTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hub-test-"))
        self.state = self.tmp / "state"
        self.bin = self.tmp / "bin"
        self.state.mkdir()
        self.bin.mkdir()
        self.log = self.tmp / "tmux.log"
        self._sockets: list[socket.socket] = []
        for name, body in (("herdr", HERDR_STUB), ("tmux", TMUX_STUB)):
            stub = self.bin / name
            stub.write_text(body)
            stub.chmod(0o755)

    def tearDown(self) -> None:
        for sock in self._sockets:
            sock.close()

    def add_sandbox(self, name: str, session: str, window: str, *statuses: str) -> Path:
        sock_path = self.tmp / f"{name}.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(sock_path))
        self._sockets.append(sock)
        Path(f"{sock_path}.reply").write_text(agent_list_reply(*statuses))
        entry = self.state / f"{name}.json"
        entry.write_text(
            json.dumps(
                {
                    "project": f"/home/benjamin/code/{name}",
                    "tmux_session": session,
                    "tmux_window_id": window,
                    "herdr_socket": str(sock_path),
                    "pid": 1,
                }
            )
        )
        return entry

    def run_hub(self, *args: str) -> str:
        env = os.environ.copy()
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["SANDBOX_AGENT_STATE_DIR"] = str(self.state)
        env["TMUX_STUB_LOG"] = str(self.log)
        out = subprocess.run(
            ["ruby", str(HUB), *args],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        return out.stdout

    def tmux_calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_status_lists_only_sandboxes_needing_attention(self) -> None:
        self.add_sandbox("llm-tools", "llm-tools", "@1", "working")
        self.add_sandbox("miriad", "miriad", "@2", "blocked")
        line = self.run_hub("status").strip()
        self.assertEqual("⚠ miriad", line)

    def test_list_shows_one_row_per_attention_agent(self) -> None:
        self.add_sandbox("miriad", "miriad", "@2", "idle", "blocked", "done")
        out = self.run_hub("list")
        self.assertIn("blocked", out)
        self.assertIn("done", out)
        self.assertIn("task number 2", out)  # the blocked agent is identifiable
        self.assertNotIn("idle", out)  # finished-while-watched is not attention
        self.assertIn("idle", self.run_hub("list", "--all"))

    def test_idle_alone_earns_no_flag_anywhere(self) -> None:
        self.add_sandbox("llm-tools", "llm-tools", "@1", "idle")
        self.assertEqual("", self.run_hub("status").strip())
        self.assertEqual("Nothing needs attention.", self.run_hub("list").strip())
        self.run_hub("refresh")
        self.assertIn("set-option -t llm-tools -u @sandbox_attn", self.tmux_calls())

    def test_refresh_flags_and_sweeps_tmux(self) -> None:
        self.add_sandbox("llm-tools", "llm-tools", "@1", "working")
        self.add_sandbox("miriad", "miriad", "@2", "blocked")
        self.run_hub("refresh")
        calls = self.tmux_calls()
        # Bare session names, no "=" prefix: tmux 3.2a set-option rejects "=".
        self.assertIn("set-option -t miriad @sandbox_attn ⚠ blocked", calls)
        # Windows use their own option name: an unset window option falls back
        # to the session's in format lookup, which would flag every window.
        self.assertIn("set-option -w -t @2 @sandbox_attn_w ⚠ blocked", calls)
        # Everything not earning a flag is swept clean, registered or not.
        self.assertIn("set-option -t llm-tools -u @sandbox_attn", calls)
        self.assertIn("set-option -t Admin -u @sandbox_attn", calls)
        self.assertIn("set-option -w -t @1 -u @sandbox_attn_w", calls)
        self.assertIn("set-option -w -t @3 -u @sandbox_attn_w", calls)

    def test_dead_socket_prunes_the_registry_entry(self) -> None:
        entry = self.state / "gone.json"
        entry.write_text(
            json.dumps(
                {
                    "project": "/tmp/gone",
                    "tmux_session": "dead",
                    "tmux_window_id": "@9",
                    "herdr_socket": str(self.tmp / "missing.sock"),
                    "pid": 1,
                }
            )
        )
        self.assertEqual("Nothing needs attention.", self.run_hub("list").strip())
        self.assertFalse(entry.exists())


if __name__ == "__main__":
    unittest.main()
