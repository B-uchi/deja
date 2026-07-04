"""Talking to the system clipboard (Wayland via wl-clipboard, X11 via xclip)."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess

SENSITIVE_HINTS = (
    "x-kde-passwordManagerHint",   # KeePassXC & friends mark secrets with this
)


def is_wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def backend() -> str | None:
    """Name of the usable clipboard backend, or None."""
    if is_wayland() and shutil.which("wl-paste"):
        return "wayland"
    if shutil.which("xclip"):
        return "x11"
    return None


def read_text() -> str | None:
    b = backend()
    try:
        if b == "wayland":
            out = subprocess.run(["wl-paste", "-n", "-t", "text"],
                                 capture_output=True, timeout=5)
        elif b == "x11":
            out = subprocess.run(["xclip", "-o", "-selection", "clipboard"],
                                 capture_output=True, timeout=5)
        else:
            return None
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", errors="replace")


def copy_via_daemon(text: str) -> bool:
    """Set the clipboard through the running deja daemon (no window, no
    external tool, and the content outlives whoever asked)."""
    from . import daemon  # late import to avoid a cycle
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(1.0)
        s.connect(daemon.control_path())
        s.sendall(text.encode())
        s.shutdown(socket.SHUT_WR)
        return s.recv(8) == b"ok"
    except OSError:
        return False
    finally:
        s.close()


def copy_text(text: str) -> bool:
    if copy_via_daemon(text):
        return True
    b = backend()
    try:
        if b == "wayland":
            # wl-copy forks and keeps serving the clipboard after we exit.
            subprocess.run(["wl-copy"], input=text.encode(), timeout=5, check=True)
        elif b == "x11":
            subprocess.run(["xclip", "-i", "-selection", "clipboard"],
                           input=text.encode(), timeout=5, check=True)
        else:
            return False
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def clipboard_types() -> list[str]:
    """MIME types currently offered on the clipboard (Wayland only)."""
    if backend() != "wayland":
        return []
    try:
        out = subprocess.run(["wl-paste", "-l"], capture_output=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return []
    return out.stdout.decode(errors="replace").split()


def clipboard_is_sensitive() -> bool:
    """True if the current clipboard is flagged by a password manager."""
    types = clipboard_types()
    return any(h in types for h in SENSITIVE_HINTS)


def have_gtk() -> bool:
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        return True
    except (ImportError, ValueError):
        return False


def watcher_backend() -> str | None:
    """Which watcher the daemon would pick right now (mirrors daemon.run)."""
    if is_wayland():
        try:
            from . import wayland
            c = wayland.Client()
            proto = c.protocol
            c.sock.close()
            return f"native Wayland data-control ({proto})"
        except Exception:
            pass
        if have_gtk() and os.environ.get("DISPLAY"):
            return "XWayland bridge (GDK/XFixes, event-driven)"
        if shutil.which("wl-paste"):
            return None  # wl-paste --watch might work; daemon will try
        return None
    if have_gtk():
        return "X11 (GDK/XFixes, event-driven)"
    if shutil.which("xclip"):
        return "X11 (xclip polling)"
    return None
