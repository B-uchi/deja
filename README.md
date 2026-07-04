# deja — the clipboard time machine

> *You've copied this before.*

Everything you copy, quietly recorded, instantly searchable, forever\* restorable.
A background daemon watches your clipboard; a fast CLI and a GTK4 popup let you
travel back to anything you've ever copied.

\* configurable forever. Default: your last 2000 clips.

```
$ deja find docker
   ID   AGE    ×    PREVIEW
  212    2m    3  ★ docker compose up -d --build
  180    1d    1    docker run --rm -it -v $PWD:/app python:3.12 bash
  164    3d    2    FROM python:3.12-slim
```

## Install

Requirements: Linux, Python 3.11+, and GTK4's Python bindings
(`python3-gi` + `gir1.2-gtk-4.0` — preinstalled on GNOME) for the GUI and
the GNOME/X11 watcher. On Sway/KDE/wlroots not even that: the daemon speaks
the Wayland data-control protocol natively. `wl-clipboard`/`xclip` are
optional fallbacks only. No pip dependencies.

**From the PPA** (recommended):

```bash
sudo add-apt-repository ppa:ibuchukwu/deja
sudo apt install deja
deja setup --hotkey '<Control><Alt>v'  # start at login + bind the GUI popup
```

**Clone & run** (no root needed):

```bash
git clone https://github.com/B-uchi/deja && cd deja
./install.sh                         # links `deja` into ~/.local/bin
deja setup --hotkey '<Control><Alt>v'
```

**pipx**, if that's your thing: `pipx install .` from the checkout, then
`deja setup` as above.

That's it. Copy a few things, then run `deja`.

## Daily driving

| Command | What it does |
|---|---|
| `deja` | your recent clips (same as `deja list`) |
| `deja find <words>` | full-text search, prefix-aware (`deja find "api k"` finds "API key") |
| `deja show 42` | print entry #42 in full (raw when piped: `deja show 42 \| jq`) |
| `deja copy 42` | put #42 back on the clipboard |
| `deja pin 42` / `unpin 42` | pinned entries are never pruned and sort first |
| `deja rm 42 43` | delete entries |
| `deja gui` | the search popup (bind this to a hotkey) |

The GUI: start typing to filter (from anywhere in the window), click to
select, **Enter** or double-click copies and closes, **Ctrl+P** pins,
**Del** deletes, **Esc** vanishes. **Ctrl+S** (or the header toggle) opens
selection mode — checkboxes appear and an action bar handles bulk
pin/unpin/delete, with **Ctrl+A** to select everything.

Copying an old entry bumps it back to the top of your history — the timeline
heals itself.

## Privacy — read this bit

A clipboard historian is a keylogger's cousin. deja treats that seriously:

- **Password managers are ignored.** Anything copied with the
  `x-kde-passwordManagerHint` MIME hint (KeePassXC and friends set this) is
  never recorded.
- **`deja pause` / `deja resume`** — a privacy switch for when you're about to
  copy something you don't want remembered. `deja status` shows which mode
  you're in.
- **`deja purge --days 30`** deletes old history; `deja purge --all` wipes it
  (pins survive unless you add `--pinned-too`). The database is VACUUMed after,
  so the bytes are actually gone.
- **Everything stays local** in one SQLite file:
  `~/.local/share/deja/deja.db`. Nothing ever leaves your machine.
- **Ignore patterns** (config below) let you filter anything matching a regex —
  e.g. entries that look like your company's token format.

## Configuration

Optional. Create `~/.config/deja/config.toml`:

```toml
max_entries = 2000          # unpinned history size (oldest pruned)
max_bytes   = 262144        # skip clipboard payloads bigger than this
min_chars   = 2             # skip single keystrokes
ignore_patterns = [         # regexes; matching clips are never stored
  '^ghp_[A-Za-z0-9]{36}$',  # e.g. GitHub tokens
]
```

Changes apply on the next clipboard event — no restart needed (the daemon
re-reads config per ingest on Wayland).

## Running it

`deja setup` installs a **systemd user service** (`deja.service`) that starts
with your session. Useful bits:

```bash
deja status                        # is everything alive?
systemctl --user status deja      # the long version
journalctl --user -u deja -f      # daemon logs
deja daemon                        # run in the foreground instead
deja teardown                      # remove service + hotkey (keeps your data)
```

## How it works

```
 Sway/KDE/wlroots: native Wayland data-control client   ┌────────────┐
 GNOME:            XWayland bridge (GDK/XFixes)      ──►│ deja daemon│──► SQLite
 plain X11:        GDK/XFixes (xclip poll as backstop)  │  (filters) │    + FTS5
                                                        └─────▲──────┘    (WAL)
        deja copy / GUI ──── control socket ──────────────────┘
```

All watcher backends are **event-driven and windowless** — zero polling,
zero idle CPU, zero focus stealing:

- **Native Wayland**: deja speaks the raw wire protocol for
  `ext-data-control-v1` / `zwlr_data_control_v1` itself (~300 lines, stdlib
  only) — the protocol made for clipboard managers. No wl-clipboard needed.
- **GNOME**: Mutter doesn't ship data-control (yet), but it bridges the
  clipboard to XWayland in both directions — where watching *is* allowed
  without focus. deja attaches there via GDK/XFixes. Same trick covers
  plain X11 sessions.
- deja **refuses to poll with wl-paste**: on compositors without
  data-control each read opens a transient focus-stealing window (the
  infamous flashing "unknown" app).
- **Restoring goes through the daemon** (a control socket): the long-lived
  daemon owns the clipboard offer, so it survives after `deja copy` or the
  GUI exit — on Wayland an offer normally dies with its client.
- **Dedupe by content hash** — copying the same thing twice bumps a counter
  (`×` column) and its timestamp instead of storing a duplicate.
- **FTS5 full-text index** with prefix matching does the searching; queries it
  can't parse fall back to substring match. WAL mode means daemon, CLI, and
  GUI can all hit the DB at once.
- Known cosmetic quirk (all XWayland apps have it, not just deja): on GNOME,
  the *legacy* X targets (`STRING`, `text/plain`) carry a trailing NUL byte
  through Mutter's bridge. Real apps paste via the UTF-8 targets, which are
  byte-exact.

## Troubleshooting

- **`watcher: none` in `deja status`** → install the GTK4 bindings:
  `sudo apt install python3-gi gir1.2-gtk-4.0`.
- **Nothing being recorded** → `deja status` (paused? daemon inactive?), then
  `journalctl --user -u deja -n 20` — the daemon logs which watcher backend
  it picked and why.
- **Hotkey does nothing** → GNOME needs the *command* path to be absolute;
  `deja setup --hotkey` handles that, but a manually created shortcut must use
  the full path shown by `which deja`.
- **I copied a password before pausing** → `deja` to find its id, `deja rm <id>`.

## Development

```bash
python3 test_deja.py     # 31 checks: store, filters, CLI end-to-end
```

Layout: `store.py` (SQLite+FTS5) · `daemon.py` (backend ladder + control
socket) · `wayland.py` (native data-control client) · `gdkwatch.py`
(XWayland/X11 watcher) · `clip.py` (daemon socket + tool fallbacks) ·
`cli.py` · `gui.py` (GTK4) · `service.py` (systemd + GNOME hotkey) ·
`config.py`. Packaging lives in `debian/` and `data/`; build a .deb with
`dpkg-buildpackage -us -uc` (needs debhelper, dh-python,
pybuild-plugin-pyproject).

Roadmap ideas: image support, `deja edit` (re-copy with tweaks), export/import,
fuzzy (not just prefix) ranking.

## License

[MIT](LICENSE). Contributions welcome — run the tests, keep it dependency-free.
