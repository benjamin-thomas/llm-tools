from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from quota_gauges.native_host import (
    MAX_MESSAGE,
    extension_id,
    host_manifest,
    merge,
    read_message,
    serve,
    write_message,
)

BUDGET = {
    "usage_percentage": 0.6757,
    "initial_budget": 255,
    "currency": "EUR",
    "reset_at": "2026-11-01T00:00:00Z",
    "payg_enabled": False,
}


def framed(message: object) -> bytes:
    body = json.dumps(message).encode()
    return struct.pack("<I", len(body)) + body


class ProtocolTest(unittest.TestCase):
    """Chrome's native messaging: a 4-byte little-endian length, then JSON."""

    def test_a_message_is_read_off_its_length(self) -> None:
        self.assertEqual(read_message(io.BytesIO(framed({"a": 1}))), {"a": 1})

    def test_an_empty_stream_is_no_message(self) -> None:
        self.assertIsNone(read_message(io.BytesIO(b"")))

    def test_an_oversized_message_is_refused_unread(self) -> None:
        stream = io.BytesIO(struct.pack("<I", MAX_MESSAGE + 1) + b"x" * 10)
        self.assertIsNone(read_message(stream))

    def test_a_reply_is_framed_the_same_way(self) -> None:
        out = io.BytesIO()
        write_message(out, {"ok": True})
        self.assertEqual(out.getvalue(), framed({"ok": True}))


class MergeTest(unittest.TestCase):
    def test_a_budget_replaces_whatever_was_kept(self) -> None:
        got = merge({"budget": {"usage_percentage": 1}, "error": "old"},
                    {"budget": BUDGET, "fetched_at": "2026-10-11T14:00:00Z"})
        self.assertEqual(got, {"budget": BUDGET, "fetched_at": "2026-10-11T14:00:00Z"})

    def test_an_error_keeps_the_last_good_budget_and_says_what_failed(self) -> None:
        kept = {"budget": BUDGET, "fetched_at": "2026-10-11T14:00:00Z"}
        got = merge(kept, {"error": "HTTP 401", "fetched_at": "2026-10-11T15:00:00Z"})
        self.assertEqual(got["budget"], BUDGET)
        self.assertEqual(got["fetched_at"], "2026-10-11T14:00:00Z")  # the budget's own age
        self.assertEqual(got["error"], "HTTP 401")
        self.assertEqual(got["error_at"], "2026-10-11T15:00:00Z")

    def test_only_known_budget_fields_are_kept(self) -> None:
        """The page's answer is stored as data, never as whatever it holds."""
        noisy = dict(BUDGET, script="<x>", nested={"deep": 1})
        got = merge(None, {"budget": noisy, "fetched_at": "t"})
        self.assertEqual(got["budget"], BUDGET)

    def test_a_long_error_is_cut_short(self) -> None:
        got = merge(None, {"error": "x" * 5000, "fetched_at": "t"})
        self.assertLessEqual(len(got["error"]), 200)

    def test_a_shapeless_message_changes_nothing(self) -> None:
        kept = {"budget": BUDGET, "fetched_at": "t"}
        self.assertEqual(merge(kept, {"nonsense": 1}), kept)
        self.assertEqual(merge(kept, "not a dict"), kept)


class ServeTest(unittest.TestCase):
    def test_one_message_is_merged_into_the_reading_and_acknowledged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reading = Path(tmp) / "sub" / "mistral.json"
            out = io.BytesIO()
            serve(io.BytesIO(framed({"budget": BUDGET, "fetched_at": "t"})), out, reading)
            self.assertEqual(json.loads(reading.read_text())["budget"], BUDGET)
        self.assertEqual(out.getvalue(), framed({"ok": True}))


class StandaloneTest(unittest.TestCase):
    """The host is copied out of the repo as one file, so it must stand alone."""

    SOURCE = Path(__file__).resolve().parent.parent / "quota_gauges" / "native_host.py"

    def test_it_imports_nothing_from_this_repo(self) -> None:
        text = self.SOURCE.read_text()
        self.assertNotIn("from token_recap", text)
        self.assertNotIn("from quota_gauges", text)

    def test_it_writes_where_the_tools_read(self) -> None:
        from quota_gauges import native_host
        from token_recap import mistral_usage

        self.assertEqual(native_host.READING, mistral_usage.READING)

    def test_it_runs_with_the_system_interpreter(self) -> None:
        self.assertTrue(self.SOURCE.read_text().startswith("#!/usr/bin/python3\n"))


class ManifestTest(unittest.TestCase):
    def test_the_id_is_chromes_hash_of_the_public_key(self) -> None:
        """First 16 bytes of the key's SHA-256, each hex digit shifted to a-p."""
        key = base64.b64encode(b"any public key bytes").decode()
        digest = hashlib.sha256(b"any public key bytes").hexdigest()[:32]
        expected = "".join(chr(ord("a") + int(c, 16)) for c in digest)
        self.assertEqual(extension_id(key), expected)

    def test_the_host_manifest_lets_only_our_extension_in(self) -> None:
        got = host_manifest("abcdefghijklmnopabcdefghijklmnop", Path("/opt/host"))
        self.assertEqual(got["name"], "dev.benjamin.quota_gauges")
        self.assertEqual(got["path"], "/opt/host")
        self.assertEqual(got["type"], "stdio")
        self.assertEqual(got["allowed_origins"], ["chrome-extension://abcdefghijklmnopabcdefghijklmnop/"])


class InstallTest(unittest.TestCase):
    """Chrome runs a copy out of the repo, refreshed only when asked."""

    ROOT = Path(__file__).resolve().parent.parent

    def _install(self, tmp: str) -> tuple[list[str], Path, Path]:
        from quota_gauges.install import install

        dest, hosts = Path(tmp) / "share", Path(tmp) / "hosts"
        extensions = Path(tmp) / "extensions"
        return install(self.ROOT, dest, hosts, extensions), dest, hosts

    def test_the_python_tools_are_copied_and_runnable_from_the_copy(self) -> None:
        """The panel extension runs `quota-gauges --json` from the copy."""
        with tempfile.TemporaryDirectory() as tmp:
            _, dest, _ = self._install(tmp)
            for package in ("token_recap", "quota_gauges"):
                for src in (self.ROOT / package).glob("*.py"):
                    self.assertEqual((dest / package / src.name).read_bytes(), src.read_bytes())
            self.assertFalse(list(dest.rglob("__pycache__")))
            launcher = dest / "quota-gauges"
            self.assertTrue(os.access(launcher, os.X_OK))
            out = subprocess.run([str(launcher), "--help"], capture_output=True, text=True, cwd=tmp)
            self.assertEqual(out.returncode, 0, out.stderr)

    def test_the_gnome_extension_is_installed_under_its_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._install(tmp)
            uuid = json.loads((self.ROOT / "quota-gauges-gnome" / "metadata.json").read_text())["uuid"]
            folder = Path(tmp) / "extensions" / uuid
            for name in ("metadata.json", "extension.js"):
                src = self.ROOT / "quota-gauges-gnome" / name
                self.assertEqual((folder / name).read_bytes(), src.read_bytes())

    def test_the_extension_and_host_are_copied_out_of_the_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, dest, _ = self._install(tmp)
            for name in ("manifest.json", "background.js"):
                src = self.ROOT / "quota-gauges-chrome" / name
                self.assertEqual((dest / "chrome" / name).read_bytes(), src.read_bytes())
            host = dest / "native-host"
            self.assertEqual(host.read_bytes(), (self.ROOT / "quota_gauges" / "native_host.py").read_bytes())
            self.assertTrue(os.access(host, os.X_OK))
        self.assertTrue(all(line.startswith("new ") for line in report))

    def test_chrome_is_pointed_at_the_copy_for_our_extension_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, dest, hosts = self._install(tmp)
            manifest = json.loads((hosts / "dev.benjamin.quota_gauges.json").read_text())
        key = json.loads((self.ROOT / "quota-gauges-chrome" / "manifest.json").read_text())["key"]
        self.assertEqual(manifest["path"], str(dest / "native-host"))
        self.assertEqual(manifest["allowed_origins"], [f"chrome-extension://{extension_id(key)}/"])

    def test_a_second_run_says_what_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, dest, _ = self._install(tmp)
            (dest / "chrome" / "background.js").write_text("tampered")
            report, _, _ = self._install(tmp)
        changed = [line for line in report if not line.startswith("unchanged ")]
        self.assertGreater(len(report), 1)
        self.assertEqual(len(changed), 1)
        self.assertIn("chrome/background.js", changed[0])
        self.assertTrue(changed[0].startswith("changed "))


if __name__ == "__main__":
    unittest.main()
