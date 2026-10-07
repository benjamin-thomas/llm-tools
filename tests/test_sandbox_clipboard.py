"""Real X11 clients on two authenticated Xvfb servers; never the desktop clipboard.

Run with /usr/bin/python3 -B -m unittest discover -s tests -p test_sandbox_clipboard.py.
Requires python3-xlib, xvfb, and xclip.
"""

import contextlib
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest

from Xlib import X, Xatom, display, protocol


ROOT = Path(__file__).resolve().parents[1]


def window_id(window):
    return getattr(window, "id", window)


class Server:
    def __init__(self, directory, name):
        self.auth = str(Path(directory) / (name + ".auth"))
        fields = [b"", b"", b"MIT-MAGIC-COOKIE-1", os.urandom(16)]
        Path(self.auth).write_bytes(struct.pack("!H", 65535) + b"".join(
            struct.pack("!H", len(field)) + field for field in fields))
        reader, writer = os.pipe()
        self.process = subprocess.Popen(
            ["Xvfb", "-displayfd", str(writer), "-auth", self.auth,
             "-screen", "0", "800x600x24", "-noreset", "-nolisten", "tcp"],
            pass_fds=(writer,), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        os.close(writer)
        with os.fdopen(reader) as stream:
            if not select.select([stream], [], [], 5)[0]:
                self.close()
                raise RuntimeError("Xvfb did not start")
            self.name = ":" + stream.readline().strip()
        # python-xlib needs a numbered FamilyLocal entry; libXau accepts Wild.
        local = [socket.gethostname().encode(), self.name[1:].encode(), fields[2], fields[3]]
        with open(self.auth, "ab") as auth:
            auth.write(struct.pack("!H", 256) + b"".join(
                struct.pack("!H", len(field)) + field for field in local))
        self.env = {**os.environ, "DISPLAY": self.name, "XAUTHORITY": self.auth}

    def connect(self):
        old_auth = os.environ.get("XAUTHORITY")
        os.environ["XAUTHORITY"] = self.auth
        try:
            return display.Display(self.name)
        finally:
            if old_auth is None:
                os.environ.pop("XAUTHORITY", None)
            else:
                os.environ["XAUTHORITY"] = old_auth

    def close(self):
        self.process.terminate()
        self.process.wait(timeout=5)
        self.process.stderr.close()


class Owner:
    """A native selection owner; payloads are only sent when requested."""
    def __init__(self, server, data, selection="CLIPBOARD", delay=0):
        self.display = server.connect()
        self.display.set_error_handler(lambda _error, _request: None)
        self.window = self.display.screen().root.create_window(
            0, 0, 1, 1, 0, 0, X.InputOnly)
        self.selection = self.display.intern_atom(selection)
        self.data = data
        self.delay = delay
        self.requests = []
        self.outgoing = {}
        self.stopped = threading.Event()
        self.window.set_selection_owner(self.selection, X.CurrentTime)
        self.display.sync()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stopped.is_set():
            if not self.display.pending_events():
                select.select([self.display.fileno()], [], [], .02)
                continue
            event = self.display.next_event()
            if event.type == X.PropertyNotify and event.state == X.PropertyDelete:
                key = (window_id(event.window), event.atom)
                if key in self.outgoing:
                    requestor, prop, kind, remaining = self.outgoing[key]
                    chunk, remaining = remaining[:32768], remaining[32768:]
                    requestor.change_property(prop, kind, 8, chunk)
                    if chunk:
                        self.outgoing[key] = (requestor, prop, kind, remaining)
                    else:
                        del self.outgoing[key]
                    self.display.flush()
            elif event.type == X.SelectionRequest:
                name = self.display.get_atom_name(event.target)
                self.requests.append(name)
                if self.delay:
                    time.sleep(self.delay)
                prop = event.property or event.target
                if name == "TARGETS":
                    atoms = [self.display.intern_atom(t) for t in self.data]
                    atoms += [self.display.intern_atom(t) for t in
                              ["TARGETS", "TIMESTAMP", "SAVE_TARGETS", "DELETE"]]
                    event.requestor.change_property(prop, Xatom.ATOM, 32, atoms)
                elif name in self.data:
                    payload = self.data[name]
                    if len(payload) > 32768:
                        event.requestor.change_attributes(event_mask=X.PropertyChangeMask)
                        event.requestor.change_property(
                            prop, self.display.intern_atom("INCR"), 32, [len(payload)])
                        self.outgoing[(window_id(event.requestor), prop)] = (
                            event.requestor, prop, event.target, payload)
                    else:
                        event.requestor.change_property(prop, event.target, 8, payload)
                else:
                    prop = X.NONE
                event.requestor.send_event(protocol.event.SelectionNotify(
                    time=event.time, requestor=event.requestor,
                    selection=event.selection, target=event.target, property=prop))
                self.display.flush()

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=3)
        self.display.close()


class ClipboardRelayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="clipboard-test-")
        cls.host = Server(cls.directory.name, "host")
        cls.private = Server(cls.directory.name, "private")

    @classmethod
    def tearDownClass(cls):
        cls.private.close()
        cls.host.close()
        cls.directory.cleanup()

    def setUp(self):
        self.owners = []
        self.relay = subprocess.Popen(
            ["/usr/bin/python3", "-I", "-B", str(ROOT / "sandbox-clipboard.py"),
             self.private.name, self.private.auth],
            env=self.host.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_relay)
        self.assertTrue(select.select([self.relay.stdout], [], [], 5)[0], "relay ready timeout")
        self.assertEqual(self.relay.stdout.readline(), b"ready\n")

    def stop_relay(self):
        self.relay.terminate()
        self.relay.wait(timeout=5)
        errors = self.relay.stderr.read().decode()
        self.relay.stdout.close()
        self.relay.stderr.close()
        for owner in self.owners:
            owner.close()
        self.assertEqual(errors, "")

    def own(self, server, data, **kwargs):
        owner = Owner(server, data, **kwargs)
        self.owners.append(owner)
        return owner

    def read(self, server, target="UTF8_STRING", selection="clipboard"):
        return subprocess.run(
            ["xclip", "-o", "-selection", selection, "-t", target],
            env=server.env, capture_output=True, timeout=8)

    def wait_read(self, server, expected, target="UTF8_STRING"):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            result = self.read(server, target)
            if result.returncode == 0 and result.stdout == expected:
                return
            time.sleep(.02)
        self.fail("clipboard did not produce expected dummy payload")

    def clear(self, server):
        with contextlib.closing(server.connect()) as connection:
            protocol.request.SetSelectionOwner(
                display=connection.display, window=X.NONE,
                selection=connection.intern_atom("CLIPBOARD"), time=X.CurrentTime)
            connection.sync()

    def test_host_text_is_only_read_on_demand(self):
        owner = self.own(self.host, {"UTF8_STRING": b"ephemeral dummy password"})
        time.sleep(.15)
        self.assertEqual(owner.requests, [])
        self.wait_read(self.private, b"ephemeral dummy password")
        self.assertEqual(owner.requests, ["UTF8_STRING"])

    def test_private_native_copy_reaches_host_without_focus(self):
        self.own(self.private, {"UTF8_STRING": b"native copy"})
        self.wait_read(self.host, b"native copy")

    def test_all_safe_formats_are_available_and_unsafe_targets_are_not(self):
        png = bytes.fromhex("89504e470d0a1a0a") + b"dummy binary image\x00\xff"
        owner = self.own(self.host, {"UTF8_STRING": b"caption", "image/png": png,
                                     "text/uri-list": b"file:///host/secret"})
        time.sleep(.1)
        targets = self.read(self.private, "TARGETS").stdout.splitlines()
        self.assertIn(b"UTF8_STRING", targets)
        self.assertIn(b"image/png", targets)
        for target in [b"text/uri-list", b"SAVE_TARGETS", b"DELETE", b"MULTIPLE"]:
            self.assertNotIn(target, targets)
            self.assertNotEqual(self.read(self.private, target.decode()).returncode, 0)
        self.wait_read(self.private, png, "image/png")
        self.wait_read(self.private, b"caption")
        self.assertNotIn("SAVE_TARGETS", owner.requests)

    def test_large_image_uses_incremental_transfer_both_ways(self):
        data = bytes(range(256)) * 4096
        self.own(self.host, {"image/png": data})
        self.wait_read(self.private, data, "image/png")
        self.own(self.private, {"image/png": data[::-1]})
        self.wait_read(self.host, data[::-1], "image/png")

    def test_host_clear_retires_private_content(self):
        owner = self.own(self.host, {"UTF8_STRING": b"expires"})
        time.sleep(.1)
        self.clear(self.host)
        time.sleep(.1)
        self.assertNotEqual(self.read(self.private).returncode, 0)
        self.assertEqual(owner.requests, [])

    def test_clear_after_paste_does_not_leave_a_cached_copy(self):
        self.own(self.host, {"UTF8_STRING": b"expires after paste"})
        self.wait_read(self.private, b"expires after paste")
        self.clear(self.host)
        time.sleep(.1)
        self.assertNotEqual(self.read(self.private).returncode, 0)

    def test_private_clear_retires_host_content(self):
        self.own(self.private, {"UTF8_STRING": b"private temporary"})
        self.wait_read(self.host, b"private temporary")
        self.clear(self.private)
        time.sleep(.1)
        self.assertNotEqual(self.read(self.host).returncode, 0)

    def test_owner_exit_retires_content(self):
        owner = self.own(self.host, {"UTF8_STRING": b"owner lifetime"})
        self.wait_read(self.private, b"owner lifetime")
        owner.close()
        self.owners.remove(owner)
        time.sleep(.1)
        self.assertNotEqual(self.read(self.private).returncode, 0)

    def test_clear_cancels_a_read_in_flight(self):
        owner = self.own(self.host, {"UTF8_STRING": b"must not arrive late"}, delay=.3)
        time.sleep(.1)
        reader = subprocess.Popen(["xclip", "-o", "-selection", "clipboard"],
                                  env=self.private.env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        end = time.monotonic() + 2
        while not owner.requests and time.monotonic() < end:
            time.sleep(.01)
        self.assertTrue(owner.requests)
        self.clear(self.host)
        out, _ = reader.communicate(timeout=3)
        self.assertNotEqual(reader.returncode, 0)
        self.assertEqual(out, b"")

    def test_new_copy_replaces_old_content(self):
        self.own(self.host, {"UTF8_STRING": b"first"})
        self.wait_read(self.private, b"first")
        self.own(self.private, {"UTF8_STRING": b"second"})
        self.wait_read(self.host, b"second")
        self.wait_read(self.private, b"second")
        self.own(self.host, {"UTF8_STRING": b"third"})
        self.wait_read(self.private, b"third")

    def test_primary_selection_is_not_shared(self):
        self.own(self.host, {"UTF8_STRING": b"primary stays local"}, selection="PRIMARY")
        self.assertNotEqual(self.read(self.private, selection="primary").returncode, 0)

    def test_two_sessions_share_current_content_without_a_history(self):
        with tempfile.TemporaryDirectory(prefix="clipboard-second-") as directory:
            second = Server(directory, "second")
            relay = subprocess.Popen(
                ["/usr/bin/python3", "-I", "-B", str(ROOT / "sandbox-clipboard.py"),
                 second.name, second.auth], env=self.host.env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            second_owner = None
            try:
                self.assertTrue(select.select([relay.stdout], [], [], 5)[0])
                self.assertEqual(relay.stdout.readline(), b"ready\n")
                self.own(self.private, {"UTF8_STRING": b"first session"})
                self.wait_read(second, b"first session")
                second_owner = self.own(second, {"UTF8_STRING": b"second session"})
                self.wait_read(self.private, b"second session")
                self.clear(second)
                time.sleep(.1)
                self.assertNotEqual(self.read(self.private).returncode, 0)
                self.assertNotEqual(self.read(self.host).returncode, 0)
            finally:
                relay.terminate()
                relay.wait(timeout=5)
                errors = relay.stderr.read()
                relay.stdout.close()
                relay.stderr.close()
                if second_owner is not None:
                    second_owner.close()
                    self.owners.remove(second_owner)
                second.close()
            self.assertEqual(errors, b"")

    def test_native_clients_in_herdr_and_tmux_nested_in_outer_tmux(self):
        # Full sandbox-agent launches, using a disposable HOME and an outer
        # tmux socket of our own. No interaction with the user's mux sessions.
        self.own(self.host, {"UTF8_STRING": b"sandbox input", "image/png": b"dummy PNG"})
        for mux, gui in [("herdr", False), ("tmux", False), ("herdr", True)]:
            with self.subTest(mux=mux, gui=gui), tempfile.TemporaryDirectory(prefix="clipboard-mux-") as directory:
                root = Path(directory)
                home = root / "home"
                project = root / "project"
                runtime = root / "runtime"
                (home / ".local/bin").mkdir(parents=True)
                project.mkdir()
                runtime.mkdir()
                (home / ".gitconfig").write_text("")
                if mux == "herdr":
                    shutil.copy2(Path.home() / ".local/bin/herdr", home / ".local/bin/herdr")
                # This client uses Xlib directly, like a native clipboard
                # library. Its only file output is a test completion marker.
                (project / "probe.py").write_text('''import os, select, sys
from pathlib import Path
from Xlib import X, Xatom, display, error, protocol

try:
    outside = display.Display(sys.argv[1])
except error.DisplayConnectionError:
    pass
else:
    outside.close()
    raise AssertionError("sandbox reached outside X11")

d = display.Display()
w = d.screen().root.create_window(0, 0, 1, 1, 0, 0, X.InputOnly)
clipboard = d.intern_atom("CLIPBOARD")
prop = d.intern_atom("TEST_DATA")
for target, expected in [("UTF8_STRING", b"sandbox input"), ("image/png", b"dummy PNG")]:
    atom = d.intern_atom(target)
    w.convert_selection(clipboard, atom, prop, X.CurrentTime)
    d.flush()
    while True:
        event = d.next_event()
        if event.type == X.SelectionNotify:
            assert event.property == prop
            value = w.get_property(prop, X.AnyPropertyType, 0, 1024, delete=True)
            assert value.property_type == atom and value.format == 8
            assert value.value == expected
            break
w.set_selection_owner(clipboard, X.CurrentTime)
d.sync()
Path(sys.argv[2]).write_text("ready")
while True:
    event = d.next_event()
    if event.type == X.SelectionRequest:
        name = d.get_atom_name(event.target)
        out = event.property or event.target
        if name == "TARGETS":
            event.requestor.change_property(out, Xatom.ATOM, 32, [d.intern_atom("UTF8_STRING")])
        elif name == "UTF8_STRING":
            event.requestor.change_property(out, event.target, 8, b"sandbox native copy")
        else:
            out = X.NONE
        event.requestor.send_event(protocol.event.SelectionNotify(time=event.time,
            requestor=event.requestor, selection=event.selection, target=event.target, property=out))
        d.flush()
''')
                marker = project / "status"
                script = project / "probe.sh"
                script.write_text("#!/bin/sh\nset -eu\n"
                                  "test \"$(command -v xclip)\" = /usr/bin/xclip\n"
                                  "test -z \"${SANDBOX_CLIPBOARD_SOCKET:-}\"\n"
                                  "exec /usr/bin/python3 -I -B " +
                                  shlex.join([str(project / "probe.py"), self.host.name, str(marker)]) + "\n")
                env = {**self.host.env, "HOME": str(home), "XDG_RUNTIME_DIR": str(runtime),
                       "PATH": "/usr/bin:/bin:" + str(home / ".local/bin"), "SHELL": "/bin/sh"}
                env.pop("TMUX", None)
                env.pop("TMUX_PANE", None)
                outer = str(root / "outer.sock")
                args = [str(ROOT / "sandbox-agent")]
                if not gui:
                    args.append("--no-gui")
                command = "exec " + shlex.join(args + ["--mux", mux, str(project), "--", "/bin/sh", str(script)])
                subprocess.run(["tmux", "-S", outer, "-f", "/dev/null", "new-session",
                                "-d", "-s", "clipboard-test", command], env=env, check=True,
                               capture_output=True)
                pid = int(subprocess.check_output(
                    ["tmux", "-S", outer, "display-message", "-p", "#{pane_pid}"], env=env))
                try:
                    subprocess.run(["tmux", "-S", outer, "send-keys", "n", "Enter"],
                                   env=env, check=True, capture_output=True)
                    end = time.monotonic() + 15
                    while not marker.exists() and time.monotonic() < end:
                        time.sleep(.05)
                    if not marker.exists():
                        capture = subprocess.run(["tmux", "-S", outer, "capture-pane", "-p", "-S", "-100"],
                                                 env=env, capture_output=True)
                        self.fail("sandbox probe failed: " + capture.stdout.decode() + capture.stderr.decode())
                    self.wait_read(self.host, b"sandbox native copy")
                    # Reset the source for the next mux, without a payload cache.
                    self.own(self.host, {"UTF8_STRING": b"sandbox input", "image/png": b"dummy PNG"})
                finally:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    end = time.monotonic() + 5
                    while list(runtime.glob("sandbox-agent-*")) and time.monotonic() < end:
                        time.sleep(.05)
                    subprocess.run(["tmux", "-S", outer, "kill-server"], env=env,
                                   capture_output=True)
                self.assertEqual(list(runtime.glob("sandbox-agent-*")), [])


if __name__ == "__main__":
    unittest.main()
