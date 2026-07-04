#!/usr/bin/env bash
# Puts `deja` on your PATH and (optionally) sets everything up.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"

chmod +x "${HERE}/bin/deja"
mkdir -p "${BIN_DIR}"
ln -sf "${HERE}/bin/deja" "${BIN_DIR}/deja"
echo "✓ deja linked into ${BIN_DIR}"

if ! python3 -c "import gi; gi.require_version('Gtk','4.0')" 2>/dev/null; then
    echo "! GTK4 Python bindings not found (needed for the GUI and the"
    echo "  GNOME/X11 watcher):  sudo apt install python3-gi gir1.2-gtk-4.0"
fi

case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) echo "! ${BIN_DIR} is not on your PATH — add it to your shell profile" ;;
esac

echo
echo "next steps:"
echo "  deja setup                              # start recording at login"
echo "  deja setup --hotkey '<Control><Alt>v'   # ...and bind the GUI popup"
