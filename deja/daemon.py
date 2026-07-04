"""The clipboard watcher.

Backend ladder, best first:
1. Native Wayland data-control (wayland.py) — Sway/KDE/wlroots.
2. XWayland bridge (gdkwatch.py) — GNOME, via GDK/XFixes.
3. `wl-paste --watch` — compositors whose protocol we don't speak.
4. Polling with xclip — X11 sessions only.

Never poll with wl-paste on Wayland: without data-control it opens a
transient focus-stealing window per read.

The daemon also serves a control socket so `deja copy` and the GUI can
set the clipboard through it; the offer then outlives the caller.
"""
from __future__ import annotations

import hashlib
import os
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time

from . import clip, config
from .store import Store

POLL_SECONDS = 0.8
MAX_CONTROL_BYTES = 16 * 1024 * 1024


def ingest(text: str, cfg: config.Config | None = None,
           store: Store | None = None, check_mime: bool = True) -> bool:
    """Apply filters and store one clipboard payload. Returns True if kept."""
    cfg = cfg or config.load_config()
    if config.paused_path().exists():
        return False
    if text is None:
        return False
    stripped = text.strip()
    if len(stripped) < cfg.min_chars:
        return False
    if len(text.encode()) > cfg.max_bytes:
        return False
    if cfg.ignores(text):
        return False
    if check_mime and clip.clipboard_is_sensitive():
        return False
    own = store is None
    s = store or Store()
    try:
        s.add(text)
        s.prune(cfg.max_entries)
    finally:
        if own:
            s.close()
    return True


def ingest_stdin() -> None:
    """Entry point for `deja _ingest` (the wl-paste --watch callback)."""
    data = sys.stdin.buffer.read()
    if data:
        ingest(data.decode("utf-8", errors="replace"))


def _keep(text, mimes, store) -> None:
    """Shared ingest for the event-driven backends (mimes known natively)."""
    if any(h in mimes for h in clip.SENSITIVE_HINTS):
        return
    ingest(text, None, store, check_mime=False)


# ------------------------------------------------------------ control socket

def control_path() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(base, "deja-control.sock")


def _make_listener() -> socket.socket:
    path = control_path()
    try:
        if stat.S_ISSOCK(os.stat(path).st_mode):
            os.unlink(path)
    except OSError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    os.chmod(path, 0o600)
    s.listen(4)
    return s


def _handle_control(conn: socket.socket, set_text, store: Store) -> None:
    """One `deja copy` request: payload in, clipboard set, 'ok' out."""
    try:
        conn.settimeout(2)
        data = b""
        while len(data) <= MAX_CONTROL_BYTES:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        text = data.decode("utf-8", errors="replace")
        if text:
            set_text(text)
            ingest(text, None, store, check_mime=False)  # bump to top
        conn.sendall(b"ok")
    except OSError:
        pass
    finally:
        conn.close()


# ----------------------------------------------------- backend 1: native

def _native_loop() -> None:
    """Wayland data-control. Raises DataControlUnavailable to fall through."""
    from . import wayland
    watcher = wayland.Client()   # raises if the compositor lacks the protocol
    store = Store()
    listener = _make_listener()
    print(f"deja: native Wayland watcher active ({watcher.protocol}), "
          "event-driven, windowless", file=sys.stderr)

    def on_text(text, mimes):
        _keep(text, mimes, store)

    watcher.process(on_text)     # current selection, offered on connect
    while True:
        r, _, _ = select.select([watcher, listener], [], [])
        if watcher in r:
            watcher.process(on_text)
        if listener in r:
            conn, _ = listener.accept()
            _handle_control(conn, watcher.set_text, store)


# ------------------------------------------------- backend 2: xwayland/gdk

def _gdk_loop() -> None:
    """GDK-X11 watcher. Raises X11BridgeUnavailable to fall through."""
    from . import gdkwatch
    watcher = gdkwatch.Watcher()
    from gi.repository import GLib
    store = Store()
    listener = _make_listener()

    watcher.on_text = lambda text, mimes: _keep(text, mimes, store)

    def on_control(_fd, _cond):
        conn, _ = listener.accept()
        _handle_control(conn, watcher.set_text, store)
        return True

    GLib.io_add_watch(listener.fileno(), GLib.PRIORITY_DEFAULT,
                      GLib.IOCondition.IN, on_control)
    print("deja: XWayland-bridge watcher active (event-driven via "
          "GDK/XFixes), windowless", file=sys.stderr)
    watcher.run()


# ---------------------------------------------- backend 3: wl-paste --watch

def _ingest_cmd() -> list[str]:
    exe = shutil.which("deja")
    if exe:
        return [exe, "_ingest"]
    launcher = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "bin", "deja")
    if os.path.exists(launcher):
        return [sys.executable, launcher, "_ingest"]
    return [sys.executable, "-m", "deja", "_ingest"]


def _watch_wayland() -> bool:
    """Event-driven wl-paste. Returns False if the compositor refuses."""
    cmd = ["wl-paste", "-n", "-t", "text", "--watch", *_ingest_cmd()]
    failures = 0
    announced = False
    while True:
        started = time.monotonic()
        try:
            proc = subprocess.run(cmd, capture_output=True)
        except KeyboardInterrupt:
            return True
        if time.monotonic() - started < 3:
            failures += 1
            if failures >= 2:
                return False
        else:
            failures = 0
        if not announced:
            announced = True
            print("deja: wl-paste --watch active", file=sys.stderr)
        time.sleep(1)


# ------------------------------------------------- backend 4: xclip polling

def _poll_loop() -> None:
    cfg = config.load_config()
    store = Store()
    last_hash = None
    print(f"deja: polling the X11 clipboard every {POLL_SECONDS}s (xclip)",
          file=sys.stderr)
    while True:
        try:
            text = clip.read_text()
            if text:
                h = hashlib.sha256(text.encode()).hexdigest()
                if h != last_hash:
                    last_hash = h
                    ingest(text, cfg, store)
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            break
    store.close()


# ------------------------------------------------------------------- runner

def run() -> int:
    Store().close()  # fail early if the DB path is unusable
    print(f"deja: starting (db: {config.db_path()})", file=sys.stderr)
    if clip.is_wayland():
        from . import wayland
        try:
            _native_loop()
            return 0
        except wayland.DataControlUnavailable as e:
            print(f"deja: no data-control protocol ({e}); "
                  "trying the XWayland bridge", file=sys.stderr)
        try:
            _gdk_loop()
            return 0
        except Exception as e:
            print(f"deja: XWayland bridge unavailable ({e})", file=sys.stderr)
        if shutil.which("wl-paste") and _watch_wayland():
            return 0
        print("deja: no event-driven clipboard backend works on this Wayland "
              "session.\n      Refusing to poll with wl-paste — it opens a "
              "flashing window per read on\n      compositors without "
              "data-control. Install GTK4 (python3-gi, gir1.2-gtk-4.0)\n"
              "      so the XWayland bridge can run.", file=sys.stderr)
        return 1
    # plain X11 session
    try:
        _gdk_loop()
        return 0
    except Exception as e:
        print(f"deja: GDK watcher unavailable ({e})", file=sys.stderr)
    if shutil.which("xclip"):
        _poll_loop()
        return 0
    print("deja: no clipboard backend found. Install python3-gi + "
          "gir1.2-gtk-4.0, or xclip.", file=sys.stderr)
    return 1
