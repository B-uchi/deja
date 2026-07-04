"""systemd user service + GNOME hotkey registration for `deja setup`."""
from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "deja.service"
PACKAGED_UNIT = Path("/usr/lib/systemd/user") / UNIT_NAME

UNIT_TEMPLATE = """[Unit]
Description=deja — clipboard time machine daemon
After=graphical-session.target
PartOf=graphical-session.target

[Service]
ExecStart={exec_start}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=graphical-session.target
"""


def launcher_path() -> Path:
    return Path(__file__).resolve().parent.parent / "bin" / "deja"


def exec_start(args: str) -> str:
    """Command line that runs deja from anywhere: the installed console
    script if there is one, else this checkout's launcher."""
    exe = shutil.which("deja")
    if exe:
        return f"{exe} {args}"
    return f"{sys.executable} {launcher_path()} {args}"


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args],
                          capture_output=True, text=True)


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def _packaged_version() -> str | None:
    try:
        out = subprocess.run(["/usr/bin/deja", "--version"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip().removeprefix("deja ").strip() or None
    except OSError:
        return None


def install() -> str:
    if not PACKAGED_UNIT.exists():
        # git-checkout / pipx install: write a user unit pointing at us
        unit_path().parent.mkdir(parents=True, exist_ok=True)
        unit_path().write_text(
            UNIT_TEMPLATE.format(exec_start=exec_start("daemon")))
        _systemctl("daemon-reload")
    r = _systemctl("enable", "--now", UNIT_NAME)
    if r.returncode != 0:
        return f"systemd said no: {r.stderr.strip()}"
    if not PACKAGED_UNIT.exists():
        return "installed and started"
    from . import __version__
    pkg = _packaged_version()
    msg = f"enabled the packaged service (deja {pkg or '?'} at /usr/bin/deja)"
    if pkg and pkg != __version__:
        msg += (f"\n  WARNING: you ran setup from deja {__version__}, but the "
                f"daemon will run the SYSTEM package (deja {pkg}).\n"
                "  Rebuild and reinstall the .deb (or `sudo apt remove deja`) "
                "to use this code.")
    return msg


def uninstall() -> str:
    _systemctl("disable", "--now", UNIT_NAME)
    unit_path().unlink(missing_ok=True)
    _systemctl("daemon-reload")
    return "service removed"


def status() -> str:
    r = _systemctl("is-active", UNIT_NAME)
    return r.stdout.strip() or "unknown"


# ------------------------------------------------------------- GNOME hotkey

KEYS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
BINDING_DIR = ("/org/gnome/settings-daemon/plugins/media-keys/"
               "custom-keybindings/deja/")
BINDING_SCHEMA = f"{KEYS_SCHEMA}.custom-keybinding"


def _gsettings(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gsettings", *args], capture_output=True, text=True)


def set_hotkey(binding: str = "<Control><Alt>v") -> str:
    """Register a GNOME custom shortcut that opens the deja GUI."""
    current = _gsettings("get", KEYS_SCHEMA, "custom-keybindings").stdout.strip()
    try:
        paths = ast.literal_eval(current.removeprefix("@as ")) or []
    except (ValueError, SyntaxError):
        paths = []
    if BINDING_DIR not in paths:
        paths.append(BINDING_DIR)
        _gsettings("set", KEYS_SCHEMA, "custom-keybindings", str(paths))
    slot = f"{BINDING_SCHEMA}:{BINDING_DIR}"
    _gsettings("set", slot, "name", "deja clipboard history")
    _gsettings("set", slot, "command", exec_start("gui"))
    r = _gsettings("set", slot, "binding", binding)
    if r.returncode != 0:
        return f"could not set hotkey: {r.stderr.strip()}"
    return f"hotkey {binding} → deja gui"


def remove_hotkey() -> None:
    current = _gsettings("get", KEYS_SCHEMA, "custom-keybindings").stdout.strip()
    try:
        paths = ast.literal_eval(current.removeprefix("@as ")) or []
    except (ValueError, SyntaxError):
        return
    if BINDING_DIR in paths:
        paths.remove(BINDING_DIR)
        _gsettings("set", KEYS_SCHEMA, "custom-keybindings", str(paths))
