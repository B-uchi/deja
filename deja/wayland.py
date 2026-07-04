"""Native Wayland data-control client (ext-data-control-v1 / zwlr).

Speaks the raw wire protocol over the compositor socket so the daemon can
watch and set the selection with no window, no focus, and no external
tools. Only the interfaces deja needs are implemented.
"""
from __future__ import annotations

import array
import collections
import os
import select
import socket
import struct
import time


class WaylandError(RuntimeError):
    pass


class DataControlUnavailable(RuntimeError):
    pass


# ext is the standardized protocol, zwlr the wlroots original; both have
# identical requests/events/opcodes.
MANAGERS = ("ext_data_control_manager_v1", "zwlr_data_control_manager_v1")

READ_MIMES = ("text/plain;charset=utf-8", "UTF8_STRING", "text/plain",
              "STRING", "TEXT")
SERVE_MIMES = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING",
               "STRING", "TEXT")

MAX_INCOMING = 16 * 1024 * 1024

# object ids / opcodes (from the protocol XML, in declaration order)
DISPLAY = 1
DISPLAY_SYNC, DISPLAY_GET_REGISTRY = 0, 1
REGISTRY_BIND = 0
EV_DISPLAY_ERROR, EV_DISPLAY_DELETE_ID = 0, 1
EV_REGISTRY_GLOBAL = 0
EV_CALLBACK_DONE = 0
MGR_CREATE_SOURCE, MGR_GET_DEVICE = 0, 1
DEV_SET_SELECTION = 0
EV_DEV_DATA_OFFER, EV_DEV_SELECTION, EV_DEV_FINISHED, EV_DEV_PRIMARY = 0, 1, 2, 3
SRC_OFFER, SRC_DESTROY = 0, 1
EV_SRC_SEND, EV_SRC_CANCELLED = 0, 1
OFFER_RECEIVE, OFFER_DESTROY = 0, 1
EV_OFFER_MIME = 0


def _pad(n: int) -> int:
    return (n + 3) & ~3


def _arg_str(s: str) -> bytes:
    b = s.encode() + b"\0"
    return struct.pack("=I", len(b)) + b + b"\0" * (_pad(len(b)) - len(b))


def _parse_uint(p: bytes, off: int):
    return struct.unpack_from("=I", p, off)[0], off + 4


def _parse_str(p: bytes, off: int):
    ln = struct.unpack_from("=I", p, off)[0]
    s = p[off + 4: off + 4 + ln - 1].decode("utf-8", "replace") if ln else ""
    return s, off + 4 + _pad(ln)


class Client:
    """Connect, find the data-control global, then watch_fd()/process()
    to observe selections and set_text() to own the clipboard."""

    def __init__(self):
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        if not runtime and not os.path.isabs(display):
            raise DataControlUnavailable("no XDG_RUNTIME_DIR")
        path = display if os.path.isabs(display) else os.path.join(runtime, display)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.connect(path)
        except OSError as e:
            raise DataControlUnavailable(f"cannot connect to {path}: {e}")
        self._buf = b""
        self._fds = collections.deque()
        self._next_id = 2
        self.globals = []              # (name, interface, version)
        self.offers = {}               # offer id -> [mime, ...]
        self.sources = {}              # source id -> bytes to serve
        self._pending = None           # offer id of new selection (0 = cleared)
        self._done = set()             # finished wl_callback ids

        self._registry = self._new_id()
        self._request(DISPLAY, DISPLAY_GET_REGISTRY,
                      struct.pack("=I", self._registry))
        self._roundtrip()

        seat = next((g for g in self.globals if g[1] == "wl_seat"), None)
        mgr = None
        for want in MANAGERS:
            mgr = next((g for g in self.globals if g[1] == want), None)
            if mgr:
                break
        if seat is None or mgr is None:
            raise DataControlUnavailable(
                "compositor does not advertise ext/zwlr data-control")
        self.protocol = mgr[1]
        self._seat = self._bind(seat[0], "wl_seat", 1)
        self._manager = self._bind(mgr[0], mgr[1], 1)
        self._device = self._new_id()
        self._request(self._manager, MGR_GET_DEVICE,
                      struct.pack("=II", self._device, self._seat))
        # the compositor answers get_data_device with the current selection
        self._roundtrip()

    # ------------------------------------------------------------- plumbing

    def _new_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    def _request(self, obj: int, opcode: int, payload: bytes = b"", fds=()):
        msg = struct.pack("=II", obj, ((8 + len(payload)) << 16) | opcode) + payload
        if fds:
            self.sock.sendmsg([msg], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                       array.array("i", fds).tobytes())])
        else:
            self.sock.sendall(msg)

    def _bind(self, name: int, interface: str, version: int) -> int:
        nid = self._new_id()
        self._request(self._registry, REGISTRY_BIND,
                      struct.pack("=I", name) + _arg_str(interface)
                      + struct.pack("=II", version, nid))
        return nid

    def _read_once(self, timeout) -> bool:
        """Read one chunk from the socket and dispatch complete messages.
        Returns False on timeout with nothing read."""
        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return False
        data, anc, _flags, _addr = self.sock.recvmsg(65536, 4096)
        if not data:
            raise WaylandError("compositor closed the connection")
        for level, ctype, cdata in anc:
            if level == socket.SOL_SOCKET and ctype == socket.SCM_RIGHTS:
                usable = len(cdata) - (len(cdata) % 4)
                self._fds.extend(array.array("i", cdata[:usable]))
        self._buf += data
        while len(self._buf) >= 8:
            obj, szop = struct.unpack_from("=II", self._buf, 0)
            size = szop >> 16
            if len(self._buf) < size:
                break
            self._dispatch(obj, szop & 0xFFFF, self._buf[8:size])
            self._buf = self._buf[size:]
        return True

    def _roundtrip(self):
        cb = self._new_id()
        self._request(DISPLAY, DISPLAY_SYNC, struct.pack("=I", cb))
        deadline = time.monotonic() + 5
        while cb not in self._done:
            if not self._read_once(max(0.0, deadline - time.monotonic())):
                raise WaylandError("roundtrip timed out")
        self._done.discard(cb)

    # ------------------------------------------------------------- dispatch

    def _dispatch(self, obj: int, opcode: int, p: bytes):
        if obj == DISPLAY:
            if opcode == EV_DISPLAY_ERROR:
                _oid, off = _parse_uint(p, 0)
                _code, off = _parse_uint(p, off)
                msg, _ = _parse_str(p, off)
                raise WaylandError(f"compositor error: {msg}")
            return  # delete_id — nothing to do, we don't recycle ids
        if obj == self._registry:
            if opcode == EV_REGISTRY_GLOBAL:
                name, off = _parse_uint(p, 0)
                iface, off = _parse_str(p, off)
                ver, _ = _parse_uint(p, off)
                self.globals.append((name, iface, ver))
            return
        if obj in self.offers:
            if opcode == EV_OFFER_MIME:
                mime, _ = _parse_str(p, 0)
                self.offers[obj].append(mime)
            return
        if obj in self.sources:
            if opcode == EV_SRC_SEND:
                _mime, _ = _parse_str(p, 0)
                fd = self._fds.popleft() if self._fds else None
                if fd is not None:
                    try:
                        os.write(fd, self.sources[obj])
                    except OSError:
                        pass
                    finally:
                        os.close(fd)
            elif opcode == EV_SRC_CANCELLED:
                self._request(obj, SRC_DESTROY)
                del self.sources[obj]
            return
        if hasattr(self, "_device") and obj == self._device:
            if opcode == EV_DEV_DATA_OFFER:
                oid, _ = _parse_uint(p, 0)
                self.offers[oid] = []
            elif opcode == EV_DEV_SELECTION:
                oid, _ = _parse_uint(p, 0)
                self._pending = oid
            elif opcode == EV_DEV_PRIMARY:
                oid, _ = _parse_uint(p, 0)
                # we don't track the primary selection; drop its offer
                if oid and oid in self.offers and oid != self._pending:
                    self._destroy_offer(oid)
            elif opcode == EV_DEV_FINISHED:
                raise WaylandError("data-control device finished (seat gone?)")
            return
        # anything left untracked with opcode 0 is a wl_callback firing
        if opcode == EV_CALLBACK_DONE and len(p) == 4:
            self._done.add(obj)

    def _destroy_offer(self, oid: int):
        self._request(oid, OFFER_DESTROY)
        self.offers.pop(oid, None)

    # ------------------------------------------------------------ receiving

    def _receive_text(self, oid: int):
        """Read the selection offer's text. Returns (text|None, mimes)."""
        mimes = self.offers.get(oid, [])
        mime = next((m for m in READ_MIMES if m in mimes), None)
        if mime is None:
            mime = next((m for m in mimes if m.startswith("text/")), None)
        if mime is None:                       # image or other non-text
            self._destroy_offer(oid)
            return None, mimes
        r, w = os.pipe()
        self._request(oid, OFFER_RECEIVE, _arg_str(mime), fds=(w,))
        os.close(w)
        chunks, total = [], 0
        deadline = time.monotonic() + 4
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                ready, _, _ = select.select([r, self.sock.fileno()], [], [], left)
                if not ready:
                    break
                # keep dispatching (we may be serving our own source here)
                if self.sock.fileno() in ready:
                    self._read_once(0)
                if r in ready:
                    d = os.read(r, 65536)
                    if not d:
                        break
                    total += len(d)
                    if total > MAX_INCOMING:
                        chunks = []
                        break
                    chunks.append(d)
        finally:
            os.close(r)
            self._destroy_offer(oid)
        if not chunks and total:
            return None, mimes                 # oversized: treat as skip
        return b"".join(chunks).decode("utf-8", "replace"), mimes

    # ------------------------------------------------------------ public api

    def fileno(self) -> int:
        return self.sock.fileno()

    def process(self, on_text=None):
        """Drain events; on a new selection, read it and call
        on_text(text, mimes). Non-blocking."""
        while self._read_once(0):
            pass
        while self._pending is not None:
            oid, self._pending = self._pending, None
            if oid == 0:
                continue                       # selection cleared
            text, mimes = self._receive_text(oid)
            if text and on_text:
                on_text(text, mimes)

    def set_text(self, text: str):
        """Own the clipboard with `text` (serves pastes until replaced).
        Caller must keep processing events for the offer to be served."""
        src = self._new_id()
        self.sources[src] = text.encode()
        self._request(self._manager, MGR_CREATE_SOURCE, struct.pack("=I", src))
        for mime in SERVE_MIMES:
            self._request(src, SRC_OFFER, _arg_str(mime))
        self._request(self._device, DEV_SET_SELECTION, struct.pack("=I", src))
