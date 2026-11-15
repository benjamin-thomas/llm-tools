#!/usr/bin/python3
"""The host window a sandbox's private screen is drawn in (Xephyr -parent).

Xephyr's own window shows up on the desktop in use and takes the focus before
anything can move it. This one is made here, already on the last desktop and
asking for no focus, so GNOME maps it there at once; Xephyr draws inside it.
Its screen follows the window's size: Xephyr, run -resizeable, follows its own
window, which is kept filling this one. Typing goes to the private screen once
the window has the focus, wherever the pointer is.

On the last desktop, the sandboxes' windows lie side by side in a grid while
you look at it: arriving there, or a sandbox opening or closing then, lays
them out once; one you resize, maximize or minimize is yours from then on.
Leaving it, a window still at its tile's size gets its own size back, and so
does the private screen and the browser in it.

    sandbox-window.py <title> <width>x<height>

Prints the window's id, then lives until SIGTERM, the window with it.
Run with /usr/bin/python3 -I -B: use the system python3-xlib, not a user package.
"""

from dataclasses import dataclass
import math
import resource
import select
import signal
import sys

from Xlib import X, Xatom, display, error
from Xlib.protocol import event as xevent


def grid(area, count):
    """The frames of count windows over area, row by row; the last row's share its width."""
    x, y, width, height = area
    if count == 0:
        return []
    columns = math.ceil(math.sqrt(count))
    rows = math.ceil(count / columns)
    frames = []
    for index in range(count):
        row, column = divmod(index, columns)
        in_row = min(columns, count - row * columns)
        left, right = x + width * column // in_row, x + width * (column + 1) // in_row
        top, bottom = y + height * row // rows, y + height * (row + 1) // rows
        frames.append((left, top, right - left, bottom - top))
    return frames


def intersection(a, b):
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (left, top, right - left, bottom - top) if right > left and bottom > top else None


def primary_area(monitors, workarea):
    """The primary monitor (else the first) within the work area: GNOME's
    _NET_WORKAREA spans every monitor, and only the primary one has
    workspaces. monitors: (primary, x, y, width, height) each."""
    if not monitors:
        return workarea
    monitor = next((m for m in monitors if m[0]), monitors[0])[1:]
    return (workarea and intersection(monitor, workarea)) or monitor


def client_rect(frame, extents):
    """What to configure for a frame: its position (GNOME places the frame
    there) and the size inside its decorations. extents: left, right, top, bottom."""
    x, y, width, height = frame
    left, right, top, bottom = extents
    return (x, y, width - left - right, height - top - bottom)


@dataclass
class View:
    current_is_last: bool  # the last desktop is the one shown
    on_last: bool          # this window is on it
    hidden: bool           # minimized
    held: bool             # maximized or fullscreen: the user's doing
    peers: list            # the sandboxes' windows to lay out there, in order
    me: int
    area: tuple            # x, y, width, height: the primary monitor's work area
    extents: tuple         # left, right, top, bottom
    geometry: tuple        # x, y of its frame, width, height inside it


class Tiler:
    """Where this window goes, given what the desktop shows: a client rect
    to configure, or None to leave it be."""

    def __init__(self):
        self.natural = None  # its size before it was tiled
        self.tiled = None    # the rect it was tiled at, while it is
        self.layout = None   # the peers and area it was laid out among

    def step(self, view):
        if view.current_is_last:
            if not view.on_last or view.hidden or view.held or view.me not in view.peers:
                return None
            layout = (tuple(view.peers), view.area)
            if layout == self.layout:
                return None
            self.layout = layout
            frame = grid(view.area, len(view.peers))[view.peers.index(view.me)]
            if self.tiled is None:
                self.natural = view.geometry[2:]
            self.tiled = client_rect(frame, view.extents)
            return self.tiled
        self.layout = None
        if self.tiled is None or view.held:
            return None
        tiled, self.tiled = self.tiled, None
        # Still the natural size: the tile is on its way; the size back follows it.
        if view.geometry[2:] not in (tiled[2:], self.natural):
            return None
        return restored(view.geometry[:2], self.natural, view.area, view.extents)


def restored(position, size, area, extents):
    """size again at position, moved back into area when it starts there and would overflow it."""
    x, y = position
    width, height = size
    ax, ay, aw, ah = area
    left, right, top, bottom = extents
    if ax <= x < ax + aw and ay <= y < ay + ah:
        x = max(ax, min(x, ax + aw - (width + left + right)))
        y = max(ay, min(y, ay + ah - (height + top + bottom)))
    return (x, y, width, height)


class Window:
    WATCHED = ("_NET_CURRENT_DESKTOP", "_NET_NUMBER_OF_DESKTOPS", "_NET_CLIENT_LIST", "_NET_WORKAREA",
               "_NET_WM_DESKTOP", "_NET_WM_STATE", "_NET_FRAME_EXTENTS")

    def __init__(self, title, width, height):
        self.display = display.Display()
        self.root = self.display.screen().root
        self.atom = self.display.intern_atom
        self.watched = {self.atom(name) for name in self.WATCHED}
        self.tiler = Tiler()
        area = self.area() or (0, 0, width, height)
        last = self.last_desktop()
        self.window = self.root.create_window(
            area[0], area[1], width, height, 0, X.CopyFromParent,
            background_pixel=self.display.screen().black_pixel,
            event_mask=X.StructureNotifyMask | X.SubstructureNotifyMask | X.PropertyChangeMask
            | X.FocusChangeMask)
        self.window.set_wm_name(title)
        self.window.change_property(self.atom("_NET_WM_NAME"), self.atom("UTF8_STRING"), 8, title.encode())
        self.window.set_wm_class(title, title)
        # PPosition | PSize: GNOME honours the position, on the primary monitor.
        self.window.set_wm_normal_hints(flags=4 | 8, x=area[0], y=area[1], width=width, height=height)
        self.window.set_wm_hints(flags=1, input=1)
        self.window.set_wm_protocols([self.atom("WM_DELETE_WINDOW")])
        if last is not None:
            self.window.change_property(self.atom("_NET_WM_DESKTOP"), Xatom.CARDINAL, 32, [last])
        self.window.change_property(self.atom("_NET_WM_USER_TIME"), Xatom.CARDINAL, 32, [0])
        self.root.change_attributes(event_mask=X.PropertyChangeMask)
        self.window.map()
        self.display.sync()
        self.size = (width, height)
        self.running = True

    # --- what the desktop says ---------------------------------------------

    def cardinals(self, window, name):
        prop = window.get_full_property(self.atom(name), X.AnyPropertyType)
        return list(prop.value) if prop else None

    def last_desktop(self):
        count = self.cardinals(self.root, "_NET_NUMBER_OF_DESKTOPS")
        return count[0] - 1 if count else None

    def area(self):
        workarea = self.cardinals(self.root, "_NET_WORKAREA")
        try:
            resources = self.root.xrandr_get_screen_resources_current()
            primary = self.root.xrandr_get_output_primary().output
            monitors = []
            for output in resources.outputs:
                info = self.display.xrandr_get_output_info(output, resources.config_timestamp)
                if info.crtc:
                    crtc = self.display.xrandr_get_crtc_info(info.crtc, resources.config_timestamp)
                    monitors.append((output == primary, crtc.x, crtc.y, crtc.width, crtc.height))
        except (AttributeError, error.XError):
            monitors = []
        return primary_area(monitors, tuple(workarea[:4]) if workarea else None)

    def view(self):
        """None without a window manager speaking EWMH: nothing to tile."""
        current = self.cardinals(self.root, "_NET_CURRENT_DESKTOP")
        last = self.last_desktop()
        clients = self.cardinals(self.root, "_NET_CLIENT_LIST")
        extents = self.cardinals(self.window, "_NET_FRAME_EXTENTS")
        mine = self.cardinals(self.window, "_NET_WM_DESKTOP")
        area = self.area()
        if current is None or last is None or clients is None or extents is None or mine is None or area is None:
            return None
        hidden, maximized, fullscreen = (self.atom(f"_NET_WM_STATE_{s}") for s in ("HIDDEN", "MAXIMIZED_VERT", "FULLSCREEN"))
        peers = []
        for client in clients:
            try:
                window = self.display.create_resource_object("window", client)
                wm_class = window.get_wm_class()
                if not wm_class or not wm_class[1].startswith("sandbox: "):
                    continue
                if client != self.window.id:
                    # Told when another sandbox's window moves to or from the last desktop.
                    window.change_attributes(event_mask=X.PropertyChangeMask)
                if self.cardinals(window, "_NET_WM_DESKTOP") == [last] \
                        and hidden not in (self.cardinals(window, "_NET_WM_STATE") or []):
                    peers.append(client)
            except error.XError:
                continue
        state = self.cardinals(self.window, "_NET_WM_STATE") or []
        position = self.window.translate_coords(self.root, 0, 0)
        geometry = self.window.get_geometry()
        left, _, top, _ = extents
        return View(current_is_last=current[0] == last, on_last=mine[0] == last, hidden=hidden in state,
                    held=maximized in state or fullscreen in state, peers=sorted(peers), me=self.window.id,
                    area=area, extents=tuple(extents),
                    geometry=(-position.x - left, -position.y - top, geometry.width, geometry.height))

    # --- events ------------------------------------------------------------

    def fill(self):
        """Xephyr's window fills this one: its screen follows."""
        for child in self.window.query_tree().children:
            child.configure(x=0, y=0, width=self.size[0], height=self.size[1])

    def lay_out(self):
        view = self.view()
        target = view and self.tiler.step(view)
        if target:
            x, y, width, height = target
            self.window.configure(x=x, y=y, width=width, height=height)

    def handle(self, event):
        """Whether the layout must be looked at again."""
        if event.type == X.ConfigureNotify and event.window == self.window:
            if (event.width, event.height) != self.size:
                self.size = (event.width, event.height)
                self.fill()
        elif event.type in (X.CreateNotify, X.MapNotify) and getattr(event, "parent", self.window) == self.window \
                and event.window != self.window:
            self.fill()
        elif event.type == X.FocusIn and event.window == self.window and event.detail != X.NotifyInferior:
            children = self.window.query_tree().children
            if children:
                children[-1].set_input_focus(X.RevertToParent, X.CurrentTime)
        elif event.type == X.ClientMessage and event.client_type == self.atom("WM_PROTOCOLS"):
            # Closing it would lose the private screen for the rest of the session: minimize it instead.
            if event.data[1][0] == self.atom("WM_DELETE_WINDOW"):
                self.iconify()
        elif event.type == X.PropertyNotify:
            return event.atom in self.watched
        return False

    def iconify(self):
        message = xevent.ClientMessage(window=self.window, client_type=self.atom("WM_CHANGE_STATE"),
                                       data=(32, [3, 0, 0, 0, 0]))  # IconicState
        self.root.send_event(message, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)

    def run(self):
        self.lay_out()
        self.display.flush()
        while self.running:
            again = False
            while self.display.pending_events():
                try:
                    again = self.handle(self.display.next_event()) or again
                except error.XError:
                    pass
            if again:
                try:
                    self.lay_out()
                except error.XError:
                    pass
            self.display.flush()
            select.select([self.display.fileno()], [], [], .5)


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if len(sys.argv) != 3 or "x" not in sys.argv[2]:
        sys.exit("Usage: sandbox-window.py <title> <width>x<height>")
    width, height = (int(n) for n in sys.argv[2].split("x"))
    window = Window(sys.argv[1], width, height)

    def stop(_signum, _frame):
        window.running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(window.window.id, flush=True)
    try:
        window.run()
    finally:
        window.display.close()


if __name__ == "__main__":
    try:
        main()
    except (error.DisplayConnectionError, error.ConnectionClosedError, OSError):
        sys.exit("The sandbox's window could not reach the X server.")
