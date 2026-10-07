#!/usr/bin/python3
"""Host-side, demand-driven CLIPBOARD relay between two separate X servers.

Only ownership metadata is mirrored. Bytes are fetched for an actual paste,
kept in memory for that transfer, then discarded. Replacement, clearing, owner
exit, and timeout cancel pending transfers. PRIMARY and clipboard-manager
handoff/history protocols are deliberately excluded. No clipboard bytes are
written to files or logs; the sandbox never receives the host X connection.

Run with /usr/bin/python3 -I -B: use the system python3-xlib, not a user package.
"""

import os
import resource
import select
import signal
import sys
import time
from dataclasses import dataclass, field

from Xlib import X, Xatom, display, error, protocol
from Xlib.ext import xfixes


MAX_BYTES = 64 * 1024 * 1024
MAX_TRANSFERS = 16
CHUNK_BYTES = 32 * 1024
TIMEOUT = 5
TEXT_TARGETS = frozenset([
    "UTF8_STRING", "STRING", "TEXT", "COMPOUND_TEXT", "text/plain",
    "text/plain;charset=utf-8", "text/plain;charset=UTF-8",
])
IMAGE_TARGETS = frozenset([
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
])
SAFE_TARGETS = TEXT_TARGETS | IMAGE_TARGETS


def xid(window):
    return getattr(window, "id", window)


def clear_selection(endpoint):
    protocol.request.SetSelectionOwner(
        display=endpoint.display.display, window=X.NONE,
        selection=endpoint.clipboard, time=X.CurrentTime)


class Endpoint:
    def __init__(self, name, auth):
        previous = os.environ.get("XAUTHORITY")
        if auth is None:
            os.environ.pop("XAUTHORITY", None)
        else:
            os.environ["XAUTHORITY"] = auth
        try:
            self.display = display.Display(name)
        finally:
            if previous is None:
                os.environ.pop("XAUTHORITY", None)
            else:
                os.environ["XAUTHORITY"] = previous
        # Requestors can disappear mid-transfer. Never log their properties.
        self.display.set_error_handler(lambda _error, _request: None)
        if not self.display.has_extension("XFIXES"):
            raise RuntimeError("XFIXES is required")
        self.display.xfixes_query_version()
        self.ownership_events = {
            self.display.extension_event.SetSelectionOwnerNotify,
            self.display.extension_event.SelectionWindowDestroyNotify,
            self.display.extension_event.SelectionClientCloseNotify,
        }
        self.window = self.new_window()
        self.clipboard = self.atom("CLIPBOARD")
        self.property = self.atom("_SANDBOX_CLIPBOARD_DATA")
        self.timestamp = X.CurrentTime
        self.display.xfixes_select_selection_input(
            self.window, self.clipboard,
            xfixes.XFixesSetSelectionOwnerNotifyMask |
            xfixes.XFixesSelectionWindowDestroyNotifyMask |
            xfixes.XFixesSelectionClientCloseNotifyMask)
        self.display.sync()

    def atom(self, name):
        return self.display.intern_atom(name)

    def new_window(self):
        return self.display.screen().root.create_window(
            0, 0, 1, 1, 0, 0, X.InputOnly, event_mask=X.PropertyChangeMask)

    def owner(self):
        return xid(self.display.get_selection_owner(self.clipboard))


@dataclass
class Transfer:
    source: Endpoint
    destination: Endpoint
    request: object
    window: object
    generation: int
    target: str
    deadline: float = field(default_factory=lambda: time.monotonic() + TIMEOUT)
    data: bytearray = field(default_factory=bytearray)
    kind: str = ""
    receiving_incr: bool = False
    sending_incr: bool = False
    offset: int = 0


class Relay:
    def __init__(self, host, private):
        self.endpoints = (host, private)
        self.source = None
        self.owner = X.NONE
        self.generation = 0
        self.transfers = []
        self.running = True
        # A new session exposes the current host selection, without reading it.
        if host.owner():
            self.follow(host, host.owner())
        elif private.owner():
            self.follow(private, private.owner())

    def notify(self, endpoint, request, prop):
        request.requestor.send_event(protocol.event.SelectionNotify(
            time=request.time, requestor=request.requestor,
            selection=request.selection, target=request.target, property=prop))
        endpoint.display.flush()

    def finish(self, transfer, failed=False):
        destination = transfer.destination
        if failed:
            if transfer.sending_incr:
                transfer.request.requestor.change_property(
                    transfer.request.property or transfer.request.target,
                    destination.atom(transfer.kind), 8, b"")
                destination.display.flush()
            else:
                self.notify(destination, transfer.request, X.NONE)
        transfer.window.destroy()
        transfer.source.display.flush()
        transfer.data.clear()
        self.transfers.remove(transfer)

    def follow(self, source, owner):
        self.generation += 1
        for transfer in self.transfers[:]:
            self.finish(transfer, failed=True)
        self.source, self.owner = source, owner
        destination = self.endpoints[1] if source is self.endpoints[0] else self.endpoints[0]
        if owner:
            destination.window.set_selection_owner(destination.clipboard, X.CurrentTime)
        elif destination.owner() == destination.window.id:
            clear_selection(destination)
        destination.display.flush()

    def ownership(self, endpoint, event):
        if event.send_event or event.selection != endpoint.clipboard:
            return
        owner = xid(event.owner)
        # Ignore obsolete notifications and our own mirrored ownership.
        if endpoint.owner() != owner:
            return
        if owner == endpoint.window.id:
            endpoint.timestamp = event.selection_timestamp
            return
        if owner or endpoint is self.source:
            self.follow(endpoint, owner)

    def valid(self, transfer):
        return (transfer.generation == self.generation and self.owner and
                transfer.source.owner() == self.owner and
                transfer.destination.owner() == transfer.destination.window.id)

    def request(self, endpoint, event):
        if (event.selection != endpoint.clipboard or not self.source or not self.owner or
                endpoint is self.source or endpoint.owner() != endpoint.window.id):
            self.notify(endpoint, event, X.NONE)
            return
        target = endpoint.display.get_atom_name(event.target)
        if target == "TIMESTAMP":
            prop = event.property or event.target
            event.requestor.change_property(prop, Xatom.INTEGER, 32, [endpoint.timestamp])
            self.notify(endpoint, event, prop)
            return
        if target not in SAFE_TARGETS | {"TARGETS"} or len(self.transfers) >= MAX_TRANSFERS:
            self.notify(endpoint, event, X.NONE)
            return
        transfer = Transfer(self.source, endpoint, event, self.source.new_window(),
                            self.generation, target)
        self.transfers.append(transfer)
        transfer.window.convert_selection(
            self.source.clipboard, self.source.atom(target),
            self.source.property, X.CurrentTime)
        self.source.display.flush()

    def receive(self, transfer, incremental=False):
        if not self.valid(transfer):
            self.finish(transfer, failed=True)
            return
        source = transfer.source
        # get_full_property would allocate an unbounded reply from a bad owner.
        limit = 4096 if transfer.target == "TARGETS" else MAX_BYTES
        prop = transfer.window.get_property(source.property, X.AnyPropertyType,
                                            0, limit // 4 + 1, delete=True)
        if prop is None or prop.bytes_after:
            self.finish(transfer, failed=True)
            return
        kind = source.display.get_atom_name(prop.property_type)
        if kind == "INCR" and not incremental:
            if prop.format != 32 or len(prop.value) != 1 or prop.value[0] > limit:
                self.finish(transfer, failed=True)
                return
            transfer.receiving_incr = True
            source.display.flush()  # deleting the marker acknowledges INCR
            return
        if transfer.target == "TARGETS":
            if incremental or prop.format != 32 or kind not in {"ATOM", "TARGETS"}:
                self.finish(transfer, failed=True)
                return
            names = {source.display.get_atom_name(atom) for atom in prop.value}
            names = (names & SAFE_TARGETS) | {"TARGETS", "TIMESTAMP"}
            destination = transfer.destination
            out = [destination.atom(name) for name in sorted(names)]
            transfer.request.requestor.change_property(
                transfer.request.property or transfer.request.target, Xatom.ATOM, 32, out)
            self.notify(destination, transfer.request,
                        transfer.request.property or transfer.request.target)
            self.finish(transfer)
            return
        if (prop.format != 8 or kind not in SAFE_TARGETS or
                (transfer.target in IMAGE_TARGETS and kind != transfer.target) or
                (transfer.kind and kind != transfer.kind)):
            self.finish(transfer, failed=True)
            return
        transfer.kind = kind
        if sum(len(t.data) for t in self.transfers) + len(prop.value) > MAX_BYTES:
            self.finish(transfer, failed=True)
            return
        transfer.data.extend(prop.value)
        transfer.deadline = time.monotonic() + TIMEOUT
        source.display.flush()
        if not incremental or not prop.value:
            self.deliver(transfer)

    def deliver(self, transfer):
        if not self.valid(transfer):
            self.finish(transfer, failed=True)
            return
        destination = transfer.destination
        request = transfer.request
        prop = request.property or request.target
        if len(transfer.data) > CHUNK_BYTES:
            request.requestor.change_attributes(event_mask=X.PropertyChangeMask)
            request.requestor.change_property(
                prop, destination.atom("INCR"), 32, [len(transfer.data)])
            transfer.sending_incr = True
        else:
            request.requestor.change_property(
                prop, destination.atom(transfer.kind), 8, bytes(transfer.data))
        self.notify(destination, request, prop)
        if not transfer.sending_incr:
            self.finish(transfer)

    def send_chunk(self, transfer):
        if not self.valid(transfer):
            self.finish(transfer, failed=True)
            return
        end = transfer.offset + CHUNK_BYTES
        chunk = bytes(transfer.data[transfer.offset:end])
        transfer.offset = end
        transfer.request.requestor.change_property(
            transfer.request.property or transfer.request.target,
            transfer.destination.atom(transfer.kind), 8, chunk)
        transfer.destination.display.flush()
        transfer.deadline = time.monotonic() + TIMEOUT
        if not chunk:
            self.finish(transfer)

    def event(self, endpoint, event):
        if event.type == X.SelectionRequest:
            self.request(endpoint, event)
        elif event.type == X.SelectionNotify:
            for transfer in self.transfers[:]:
                if endpoint is transfer.source and xid(event.requestor) == transfer.window.id:
                    if (event.selection != endpoint.clipboard or
                            event.target != endpoint.atom(transfer.target) or
                            event.property != endpoint.property):
                        self.finish(transfer, failed=True)
                    else:
                        self.receive(transfer)
                    break
        elif event.type == X.PropertyNotify:
            for transfer in self.transfers[:]:
                if (endpoint is transfer.source and transfer.receiving_incr and
                        not transfer.sending_incr and xid(event.window) == transfer.window.id and
                        event.atom == endpoint.property and event.state == X.PropertyNewValue):
                    self.receive(transfer, incremental=True)
                elif (endpoint is transfer.destination and transfer.sending_incr and
                      xid(event.window) == xid(transfer.request.requestor) and
                      event.atom == (transfer.request.property or transfer.request.target) and
                      event.state == X.PropertyDelete):
                    self.send_chunk(transfer)

    def run(self):
        while self.running:
            events = []
            for endpoint in self.endpoints:
                while endpoint.display.pending_events():
                    events.append((endpoint, endpoint.display.next_event()))
            # Invalidate expired content before handling queued paste responses.
            for endpoint, event in events:
                if (event.type, getattr(event, "sub_code", None)) in endpoint.ownership_events:
                    self.ownership(endpoint, event)
            for endpoint, event in events:
                if (event.type, getattr(event, "sub_code", None)) not in endpoint.ownership_events:
                    try:
                        self.event(endpoint, event)
                    except (error.BadWindow, error.BadAtom, error.BadMatch):
                        for transfer in self.transfers[:]:
                            self.finish(transfer, failed=True)
            for transfer in self.transfers[:]:
                if time.monotonic() >= transfer.deadline:
                    self.finish(transfer, failed=True)
            if not events:
                select.select([e.display.fileno() for e in self.endpoints], [], [], .1)


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if len(sys.argv) != 3:
        sys.exit("Usage: sandbox-clipboard.py <private-display> <private-xauthority>")
    host = Endpoint(os.environ.get("DISPLAY"), os.environ.get("XAUTHORITY"))
    private = Endpoint(sys.argv[1], sys.argv[2])
    relay = Relay(host, private)
    def stop(_signum, _frame):
        relay.running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print("ready", flush=True)
    try:
        relay.run()
    finally:
        for transfer in relay.transfers[:]:
            relay.finish(transfer, failed=True)
        private.display.close()
        host.display.close()


if __name__ == "__main__":
    try:
        main()
    except (error.DisplayConnectionError, error.ConnectionClosedError, OSError, RuntimeError):
        sys.exit("Clipboard relay could not connect to its X servers (XFIXES required).")
