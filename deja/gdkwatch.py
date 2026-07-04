"""XWayland-bridge clipboard watcher (the GNOME path).

Mutter doesn't expose the Wayland data-control protocol, but it mirrors
the clipboard to XWayland in both directions — and X11 allows focus-less,
event-driven clipboard access (XFixes), which GDK's X11 backend provides.
"""
from __future__ import annotations

import os


class X11BridgeUnavailable(RuntimeError):
    pass


class Watcher:
    def __init__(self):
        display_name = os.environ.get("DISPLAY") or ":0"
        # Must be decided before the first Gdk import in this process.
        os.environ["GDK_BACKEND"] = "x11"
        try:
            import gi
            gi.require_version("Gdk", "4.0")
            from gi.repository import Gdk, GLib, GObject
        except (ImportError, ValueError) as e:
            raise X11BridgeUnavailable(f"GTK4 bindings unavailable: {e}")
        self.Gdk, self.GLib, self.GObject = Gdk, GLib, GObject
        Gdk.set_allowed_backends("x11")
        self.display = Gdk.Display.open(display_name)
        if self.display is None:
            raise X11BridgeUnavailable(
                f"cannot open X display {display_name} (is XWayland running?)")
        self.clipboard = self.display.get_clipboard()
        self.on_text = None            # set by the daemon: fn(text, mimes)
        self._last = (None, 0.0)       # (content hash, monotonic time)
        self.clipboard.connect("changed", self._changed)
        self.loop = GLib.MainLoop()
        self._read_current()           # capture whatever is on it right now

    # ------------------------------------------------------------- watching

    def _mimes(self) -> list[str]:
        formats = self.clipboard.get_formats()
        return list(formats.get_mime_types() or [])

    def _changed(self, cb):
        if cb.is_local():
            return                     # our own set_text; already recorded
        self._read_current()

    def _read_current(self):
        mimes = self._mimes()
        self.clipboard.read_text_async(None, self._got_text, mimes)

    def _got_text(self, cb, result, mimes):
        try:
            text = cb.read_text_finish(result)
        except Exception:
            return                     # non-text (image etc.) — skip
        if not text or not self.on_text:
            return
        # "changed" can fire more than once per copy; debounce so
        # times_seen stays honest without eating real re-copies later.
        import hashlib
        import time
        h = hashlib.sha256(text.encode()).hexdigest()
        now = time.monotonic()
        if h == self._last[0] and now - self._last[1] < 1.0:
            return
        self._last = (h, now)
        self.on_text(text, mimes)

    # -------------------------------------------------------------- setting

    def set_text(self, text: str):
        # Bytes providers, UTF-8 targets only: GDK's own conversions for
        # the legacy targets (STRING/TEXT/text/plain) append a trailing NUL.
        data = self.GLib.Bytes.new(text.encode())
        providers = [self.Gdk.ContentProvider.new_for_bytes(mime, data)
                     for mime in ("text/plain;charset=utf-8", "UTF8_STRING")]
        self.clipboard.set_content(self.Gdk.ContentProvider.new_union(providers))

    def run(self):
        self.loop.run()
