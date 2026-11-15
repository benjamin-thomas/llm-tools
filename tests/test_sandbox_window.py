"""The host window a sandbox's private screen is drawn in: its tiling rules,
then the real thing on a disposable Xvfb, never the desktop.

Run with /usr/bin/python3 -B -m unittest discover -s tests -p test_sandbox_window.py.
Requires python3-xlib, xvfb, xserver-xephyr and xdotool.
"""

import importlib.util
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import tempfile
import time
import unittest

from Xlib import X, display


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "sandbox-window.py"
spec = importlib.util.spec_from_file_location("sandbox_window", SCRIPT)
sandbox_window = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sandbox_window)

grid = sandbox_window.grid
primary_area = sandbox_window.primary_area
client_rect = sandbox_window.client_rect
Tiler = sandbox_window.Tiler
View = sandbox_window.View

AREA = (0, 147, 1920, 1053)
EXTENTS = (0, 0, 37, 0)


class GridTest(unittest.TestCase):
    def test_one_window_fills_the_area(self):
        self.assertEqual(grid(AREA, 1), [AREA])

    def test_two_sit_side_by_side(self):
        self.assertEqual(grid(AREA, 2), [(0, 147, 960, 1053), (960, 147, 960, 1053)])

    def test_the_last_row_shares_the_whole_width(self):
        self.assertEqual(grid(AREA, 3), [(0, 147, 960, 526), (960, 147, 960, 526), (0, 673, 1920, 527)])
        self.assertEqual([w for _, _, w, _ in grid(AREA, 5)], [640, 640, 640, 960, 960])

    def test_tiles_meet_without_gaps(self):
        frames = grid((0, 0, 1000, 700), 7)
        for row in sorted({y for _, y, _, _ in frames}):
            spans = sorted((x, x + w) for x, y, w, _ in frames if y == row)
            self.assertEqual(spans[0][0], 0)
            self.assertEqual(spans[-1][1], 1000)
            for (_, end), (start, _) in zip(spans, spans[1:]):
                self.assertEqual(end, start)


class PrimaryAreaTest(unittest.TestCase):
    MONITORS = [(False, 1920, 0, 1920, 1200), (True, 0, 120, 1920, 1080)]

    def test_the_primary_monitor_within_the_work_area(self):
        # GNOME's _NET_WORKAREA spans both monitors, the top bar cut off.
        self.assertEqual(primary_area(self.MONITORS, (0, 147, 3840, 1053)), (0, 147, 1920, 1053))

    def test_without_a_primary_the_first_monitor(self):
        self.assertEqual(primary_area([(False, 0, 0, 1600, 900)], None), (0, 0, 1600, 900))

    def test_without_monitors_the_work_area(self):
        self.assertEqual(primary_area([], (0, 27, 1920, 1053)), (0, 27, 1920, 1053))


class ClientRectTest(unittest.TestCase):
    def test_the_frame_s_position_and_the_size_inside_its_decorations(self):
        self.assertEqual(client_rect((960, 147, 960, 1053), EXTENTS), (960, 147, 960, 1016))


def view(me=1, peers=(1, 2), current_is_last=True, on_last=True, hidden=False, held=False,
         geometry=(0, 147, 1600, 900)):
    return View(current_is_last=current_is_last, on_last=on_last, hidden=hidden, held=held,
                peers=list(peers), me=me, area=AREA, extents=EXTENTS, geometry=geometry)


class TilerTest(unittest.TestCase):
    def test_arriving_on_the_last_desktop_puts_the_window_in_its_tile(self):
        self.assertEqual(Tiler().step(view(me=2)), (960, 147, 960, 1016))

    def test_staying_there_leaves_a_window_resized_by_hand_alone(self):
        tiler = Tiler()
        tiler.step(view())

        self.assertIsNone(tiler.step(view(geometry=(0, 147, 700, 500))))

    def test_a_sandbox_opening_there_while_looking_tiles_again(self):
        tiler = Tiler()
        tiler.step(view())

        self.assertEqual(tiler.step(view(peers=(1, 2, 3))), (0, 147, 960, 489))

    def test_leaving_gives_the_window_its_size_back(self):
        tiler = Tiler()
        tiler.step(view(me=2))

        self.assertEqual(tiler.step(view(me=2, current_is_last=False, geometry=(960, 147, 960, 1016))),
                         (320, 147, 1600, 900))

    def test_a_window_taken_elsewhere_gets_its_size_back_too(self):
        tiler = Tiler()
        tiler.step(view())

        self.assertEqual(tiler.step(view(current_is_last=False, on_last=False, geometry=(0, 147, 960, 1016))),
                         (0, 147, 1600, 900))

    def test_leaving_keeps_a_window_resized_by_hand_as_it_is(self):
        tiler = Tiler()
        tiler.step(view())

        self.assertIsNone(tiler.step(view(current_is_last=False, geometry=(0, 147, 700, 500))))
        self.assertIsNone(tiler.step(view(current_is_last=False, geometry=(0, 147, 960, 1016))))

    def test_a_maximized_window_gets_its_size_back_once_restored(self):
        tiler = Tiler()
        tiler.step(view())

        self.assertIsNone(tiler.step(view(current_is_last=False, held=True, geometry=(0, 147, 1920, 1016))))
        self.assertEqual(tiler.step(view(current_is_last=False, geometry=(0, 147, 960, 1016))),
                         (0, 147, 1600, 900))

    def test_leaving_before_the_tile_landed_still_asks_for_the_size_back(self):
        tiler = Tiler()
        tiler.step(view())

        self.assertEqual(tiler.step(view(current_is_last=False)), (0, 147, 1600, 900))

    def test_coming_back_tiles_again(self):
        tiler = Tiler()
        tiler.step(view())
        tiler.step(view(current_is_last=False, geometry=(0, 147, 960, 1016)))

        self.assertEqual(tiler.step(view()), (0, 147, 960, 1016))

    def test_a_minimized_maximized_or_absent_window_is_not_tiled(self):
        self.assertIsNone(Tiler().step(view(hidden=True)))
        self.assertIsNone(Tiler().step(view(held=True)))
        self.assertIsNone(Tiler().step(view(on_last=False, peers=(2,))))


class Server:
    """An Xvfb with its own cookie, as in test_sandbox_clipboard.py."""
    def __init__(self, directory):
        self.auth = str(Path(directory) / "xvfb.auth")
        fields = [b"", b"", b"MIT-MAGIC-COOKIE-1", os.urandom(16)]
        Path(self.auth).write_bytes(struct.pack("!H", 65535) + b"".join(
            struct.pack("!H", len(field)) + field for field in fields))
        reader, writer = os.pipe()
        self.process = subprocess.Popen(
            ["Xvfb", "-displayfd", str(writer), "-auth", self.auth,
             "-screen", "0", "1280x1024x24", "-noreset", "-nolisten", "tcp"],
            pass_fds=(writer,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(writer)
        with os.fdopen(reader) as stream:
            if not select.select([stream], [], [], 5)[0]:
                self.close()
                raise RuntimeError("Xvfb did not start")
            self.name = ":" + stream.readline().strip()
        local = [socket.gethostname().encode(), self.name[1:].encode(), fields[2], fields[3]]
        with open(self.auth, "ab") as auth:
            auth.write(struct.pack("!H", 256) + b"".join(
                struct.pack("!H", len(field)) + field for field in local))
        self.env = {**os.environ, "DISPLAY": self.name, "XAUTHORITY": self.auth}

    def connect(self):
        os.environ["XAUTHORITY"] = self.auth
        return display.Display(self.name)

    def close(self):
        self.process.terminate()
        self.process.wait(timeout=5)


def wait_until(check, seconds=5):
    end = time.time() + seconds
    while time.time() < end:
        found = check()
        if found:
            return found
        time.sleep(.05)
    return check()


class OnARealServerTest(unittest.TestCase):
    """No window manager here: the window is mapped as made, nothing tiles."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.server = Server(self.directory.name)
        self.helper = subprocess.Popen(
            ["/usr/bin/python3", "-I", "-B", str(SCRIPT), "sandbox: api/erp-workspace", "800x600"],
            env=self.server.env, stdout=subprocess.PIPE, text=True)
        ready = select.select([self.helper.stdout], [], [], 5)[0]
        self.window = int(self.helper.stdout.readline()) if ready else None
        self.xephyr = None
        self.host = self.server.connect()

    def tearDown(self):
        if self.xephyr:
            self.xephyr.terminate()
            self.xephyr.wait(timeout=5)
        self.helper.terminate()
        self.helper.wait(timeout=5)
        self.helper.stdout.close()
        self.host.close()
        self.server.close()
        self.directory.cleanup()

    def start_xephyr(self):
        reader, writer = os.pipe()
        self.xephyr = subprocess.Popen(
            ["Xephyr", "-displayfd", str(writer), "-parent", str(self.window), "-resizeable",
             "-ac", "-noreset", "-nolisten", "tcp"],
            pass_fds=(writer,), env=self.server.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(writer)
        with os.fdopen(reader) as stream:
            self.assertTrue(select.select([stream], [], [], 5)[0], "Xephyr did not start")
            return ":" + stream.readline().strip()

    def test_it_says_its_window_named_after_the_session(self):
        self.assertIsNotNone(self.window, "no window id printed")
        window = self.host.create_resource_object("window", self.window)

        self.assertEqual(window.get_wm_class(), ("sandbox: api/erp-workspace", "sandbox: api/erp-workspace"))
        self.assertEqual(window.get_wm_name(), "sandbox: api/erp-workspace")
        geometry = window.get_geometry()
        self.assertEqual((geometry.width, geometry.height), (800, 600))

    def test_the_private_screen_follows_the_window_s_size(self):
        screen = self.start_xephyr()
        inner = wait_until(lambda: display.Display(screen))

        window = self.host.create_resource_object("window", self.window)
        window.configure(width=1000, height=700)
        self.host.sync()

        def follows():
            root = inner.screen().root.get_geometry()
            return (root.width, root.height) == (1000, 700)
        self.assertTrue(wait_until(follows), "the private screen kept its size")
        inner.close()

    def test_keys_typed_in_the_window_reach_the_private_screen(self):
        screen = self.start_xephyr()
        inner = wait_until(lambda: display.Display(screen))
        catcher = inner.screen().root.create_window(0, 0, 800, 600, 0, X.CopyFromParent,
                                                    event_mask=X.KeyPressMask)
        catcher.map()
        inner.sync()
        # The pointer away from the window: the key goes where the focus is.
        subprocess.run(["xdotool", "mousemove", "1200", "1000"], env=self.server.env, check=True)
        window = self.host.create_resource_object("window", self.window)
        wait_until(lambda: window.query_tree().children)
        window.set_input_focus(X.RevertToParent, X.CurrentTime)
        self.host.sync()
        time.sleep(.3)

        subprocess.run(["xdotool", "key", "a"], env=self.server.env, check=True)

        def pressed():
            while inner.pending_events():
                if inner.next_event().type == X.KeyPress:
                    return True
            return False
        self.assertTrue(wait_until(pressed), "the key never reached the private screen")
        inner.close()


if __name__ == "__main__":
    unittest.main()
