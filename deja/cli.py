"""The `deja` command-line interface."""
from __future__ import annotations

import argparse
import shutil
import sys

from . import __version__, clip, config, daemon, service, util
from .store import Store

# ------------------------------------------------------------------ output

def _tty() -> bool:
    return sys.stdout.isatty()


def col(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty() else text


DIM, CYAN, YELLOW, GREEN, RED, BOLD = "2", "36", "33", "32", "31", "1"


def print_rows(rows, header=True):
    if not rows:
        print(col("history is empty — is the daemon running? try: deja status", DIM))
        return
    width = shutil.get_terminal_size().columns
    pw = max(20, width - 22)
    if header:
        print(col(f"{'ID':>5}  {'AGE':>4}  {'×':>3}    PREVIEW", DIM))
    for r in rows:
        pin = col("★", YELLOW) if r["pinned"] else " "
        eid = col(f"{r['id']:>5}", CYAN)
        age = col(f"{util.fmt_age(r['last_seen']):>4}", DIM)
        print(f"{eid}  {age}  {r['times_seen']:>3}  {pin} "
              f"{util.preview(r['content'], pw)}")


# ---------------------------------------------------------------- commands

def cmd_list(args):
    s = Store()
    print_rows(s.recent(limit=args.number, pinned_only=args.pinned))
    s.close()


def cmd_find(args):
    s = Store()
    print_rows(s.search(" ".join(args.query), limit=args.number))
    s.close()


def cmd_show(args):
    s = Store()
    row = s.get(args.id)
    s.close()
    if row is None:
        sys.exit(f"deja: no entry #{args.id}")
    if args.raw or not _tty():
        sys.stdout.write(row["content"])
        return
    star = " ★ pinned" if row["pinned"] else ""
    print(col(f"#{row['id']}  copied {row['times_seen']}× · "
              f"first {util.fmt_age(row['first_seen'])} ago · "
              f"last {util.fmt_age(row['last_seen'])} ago · "
              f"{util.human_size(len(row['content'].encode()))}{star}", DIM))
    print(row["content"])


def cmd_copy(args):
    s = Store()
    row = s.get(args.id)
    s.close()
    if row is None:
        sys.exit(f"deja: no entry #{args.id}")
    if not clip.copy_text(row["content"]):
        sys.exit("deja: could not reach the clipboard — is the daemon "
                 "running? (deja status)")
    print(f"{col('✓ copied', GREEN)} {util.preview(row['content'], 50)}")


def cmd_pin(args, pinned=True):
    s = Store()
    ok = s.set_pinned(args.id, pinned)
    s.close()
    if not ok:
        sys.exit(f"deja: no entry #{args.id}")
    print(f"#{args.id} {'★ pinned — it will never be pruned' if pinned else 'unpinned'}")


def cmd_rm(args):
    s = Store()
    for eid in args.ids:
        print(f"#{eid} deleted" if s.delete(eid) else f"deja: no entry #{eid}")
    s.close()


def cmd_purge(args):
    if args.days is None and not args.all:
        sys.exit("deja: purge needs --days N or --all")
    what = (f"unpinned entries older than {args.days} days" if args.days is not None
            else "ALL " + ("entries, including pinned" if args.pinned_too
                           else "unpinned entries"))
    if not args.yes and sys.stdin.isatty():
        if input(f"Delete {what}? [y/N] ").strip().lower() != "y":
            print("nothing deleted")
            return
    s = Store()
    n = s.purge(days=args.days, include_pinned=args.pinned_too)
    s.close()
    print(f"{n} entr{'y' if n == 1 else 'ies'} gone. The past is lighter now.")


def cmd_pause(args):
    config.paused_path().touch()
    print("⏸  recording paused — clipboard is private until `deja resume`")


def cmd_resume(args):
    config.paused_path().unlink(missing_ok=True)
    print("▶  recording resumed")


def cmd_status(args):
    s = Store()
    st = s.stats()
    s.close()
    backend = (clip.watcher_backend()
               or col("none — install python3-gi + gir1.2-gtk-4.0", RED))
    paused = ("⏸  paused" if config.paused_path().exists()
              else col("recording", GREEN))
    svc = service.status()
    svc_col = GREEN if svc == "active" else RED
    print(f"deja {__version__} — the clipboard time machine")
    print(f"  daemon    {col(svc, svc_col)} (systemd user service)")
    print(f"  state     {paused}")
    print(f"  watcher   {backend}")
    print(f"  entries   {st['entries']} ({st['pinned']} pinned)")
    print(f"  database  {config.db_path()} ({util.human_size(st['db_bytes'])})")
    if svc != "active":
        print(col("  hint: `deja setup` installs and starts the daemon", DIM))


def cmd_daemon(args):
    sys.exit(daemon.run())


def cmd_ingest(args):
    daemon.ingest_stdin()


def cmd_setup(args):
    print(f"service: {service.install()}")
    if args.hotkey:
        print(f"hotkey:  {service.set_hotkey(args.hotkey)}")
    else:
        print(col("tip: `deja setup --hotkey '<Control><Alt>v'` binds the GUI "
                  "to a key", DIM))
    print("try copying a few things, then run: deja")


def cmd_teardown(args):
    print(service.uninstall())
    service.remove_hotkey()
    print(f"your history is untouched at {config.db_path()}")


def cmd_gui(args):
    from . import gui  # GTK imports stay out of plain CLI calls
    sys.exit(gui.main())


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deja",
        description="deja — the clipboard time machine. "
                    "Everything you copy, searchable forever.",
        epilog="run `deja <command> -h` for details on a command")
    p.add_argument("--version", action="version", version=f"deja {__version__}")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("list", aliases=["ls"], help="recent clipboard history")
    sp.add_argument("-n", "--number", type=int, default=15, help="rows to show")
    sp.add_argument("-p", "--pinned", action="store_true", help="pinned only")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("find", aliases=["search"],
                        help="full-text search your history")
    sp.add_argument("query", nargs="+")
    sp.add_argument("-n", "--number", type=int, default=15)
    sp.set_defaults(func=cmd_find)

    sp = sub.add_parser("show", help="print one entry in full")
    sp.add_argument("id", type=int)
    sp.add_argument("--raw", action="store_true",
                    help="content only, exactly as copied")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("copy", aliases=["cp"],
                        help="put an old entry back on the clipboard")
    sp.add_argument("id", type=int)
    sp.set_defaults(func=cmd_copy)

    sp = sub.add_parser("pin", help="protect an entry from pruning")
    sp.add_argument("id", type=int)
    sp.set_defaults(func=lambda a: cmd_pin(a, True))

    sp = sub.add_parser("unpin", help="remove pin")
    sp.add_argument("id", type=int)
    sp.set_defaults(func=lambda a: cmd_pin(a, False))

    sp = sub.add_parser("rm", help="delete entries by id")
    sp.add_argument("ids", type=int, nargs="+")
    sp.set_defaults(func=cmd_rm)

    sp = sub.add_parser("purge", help="bulk-delete history")
    sp.add_argument("--days", type=float, help="only entries older than N days")
    sp.add_argument("--all", action="store_true", help="everything unpinned")
    sp.add_argument("--pinned-too", action="store_true",
                    help="with --all: pins are not sacred either")
    sp.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    sp.set_defaults(func=cmd_purge)

    sub.add_parser("pause", help="stop recording (privacy mode)") \
       .set_defaults(func=cmd_pause)
    sub.add_parser("resume", help="start recording again") \
       .set_defaults(func=cmd_resume)
    sub.add_parser("status", help="daemon, backend, and database info") \
       .set_defaults(func=cmd_status)
    sub.add_parser("daemon", help="run the watcher in the foreground") \
       .set_defaults(func=cmd_daemon)
    sub.add_parser("gui", help="open the search popup (GTK4)") \
       .set_defaults(func=cmd_gui)

    sp = sub.add_parser("setup", help="install + start the systemd user service")
    sp.add_argument("--hotkey", metavar="BINDING",
                    help="also bind a GNOME shortcut, e.g. '<Control><Alt>v'")
    sp.set_defaults(func=cmd_setup)
    sub.add_parser("teardown", help="remove the service and hotkey") \
       .set_defaults(func=cmd_teardown)

    sp = sub.add_parser("_ingest")  # internal: wl-paste --watch callback
    sp.set_defaults(func=cmd_ingest)
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["list"]
    args = build_parser().parse_args(argv)
    if not hasattr(args, "func"):
        build_parser().print_help()
        return 1
    args.func(args)
    return 0
