"""deja test suite — store logic, ingest filters, and CLI end-to-end.

Run:  python3 test_deja.py
No clipboard or GUI needed; everything runs against a temp database.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from deja import config, daemon, util  # noqa: E402
from deja.store import Store  # noqa: E402

PASS = 0


def ok(cond, label):
    global PASS
    assert cond, f"FAIL: {label}"
    PASS += 1
    print(f"  ✓ {label}")


def test_store(tmp):
    s = Store(tmp / "t.db")
    id1, new1 = s.add("hello world")
    id2, new2 = s.add("second thing entirely")
    ok(new1 and new2 and id1 != id2, "distinct entries insert as new rows")

    id3, new3 = s.add("hello world")
    row = s.get(id3)
    ok(id3 == id1 and not new3 and row["times_seen"] == 2,
       "re-copy dedupes and bumps times_seen")

    ok(s.recent(10)[0]["id"] == id2 or s.recent(10)[0]["id"] == id1,
       "recent() returns rows")
    hits = s.search("hell")
    ok(len(hits) == 1 and hits[0]["id"] == id1, "FTS prefix search finds 'hell'")
    hits = s.search("(unbalanced [chars")
    ok(isinstance(hits, list), "hostile query falls back to LIKE, no crash")

    s.set_pinned(id1, True)
    for i in range(30):
        s.add(f"filler number {i}")
    s.prune(max_entries=5)
    ok(s.get(id1) is not None, "pinned entry survives pruning")
    ok(s.stats()["entries"] == 6, "prune keeps 5 unpinned + 1 pinned")

    n = s.purge(days=None, include_pinned=False)
    ok(s.get(id1) is not None and n == 5, "purge --all spares pins")
    n = s.purge(days=None, include_pinned=True)
    ok(s.stats()["entries"] == 0, "purge --pinned-too empties the db")

    # old-entry purge
    ida, _ = s.add("ancient scroll")
    s.db.execute("UPDATE entries SET last_seen = ? WHERE id = ?",
                 (time.time() - 40 * 86400, ida))
    s.db.commit()
    s.add("fresh coffee")
    s.purge(days=30)
    ok(s.get(ida) is None and s.stats()["entries"] == 1,
       "purge --days 30 removes only the old entry")
    s.close()


def test_ingest(tmp):
    cfg = config.Config(max_entries=100, max_bytes=100, min_chars=3,
                        ignore_patterns=[r"^SECRET-"])
    s = Store(tmp / "i.db")
    kw = dict(cfg=cfg, store=s, check_mime=False)
    ok(daemon.ingest("a good clip", **kw), "normal text is kept")
    ok(not daemon.ingest("hi", **kw), "below min_chars is skipped")
    ok(not daemon.ingest("x" * 200, **kw), "oversized payload is skipped")
    ok(not daemon.ingest("SECRET-abc123", **kw), "ignore_patterns filter works")
    config.paused_path().touch()
    ok(not daemon.ingest("while paused", **kw), "paused flag stops recording")
    config.paused_path().unlink()
    ok(daemon.ingest("after resume", **kw), "resume works")
    ok(s.stats()["entries"] == 2, "exactly the two allowed entries stored")
    s.close()


def test_util():
    now = time.time()
    ok(util.fmt_age(now - 2, now) == "now", "fmt_age: now")
    ok(util.fmt_age(now - 90, now) == "1m", "fmt_age: minutes")
    ok(util.fmt_age(now - 3 * 86400, now) == "3d", "fmt_age: days")
    ok(util.preview("a\nb\t c   d", 50) == "a b c d", "preview flattens whitespace")
    ok(util.preview("x" * 99, 10).endswith("…"), "preview ellipsizes")


def test_cli(tmp):
    env = {**os.environ, "DEJA_DB": str(tmp / "cli.db"),
           "DEJA_STATE_DIR": str(tmp / "state")}

    def run(*args, stdin=None):
        return subprocess.run(
            [sys.executable, "-m", "deja", *args],
            capture_output=True, text=True, input=stdin, env=env, cwd=HERE)

    r = run("_ingest", stdin="the cli test payload")
    ok(r.returncode == 0, "cli: _ingest accepts stdin")
    r = run("list")
    ok("cli test payload" in r.stdout, "cli: list shows the entry")
    r = run("find", "payload")
    ok("cli test payload" in r.stdout, "cli: find matches")
    r = run("show", "1")
    ok(r.stdout == "the cli test payload",
       "cli: show is raw/exact when piped")
    r = run("pin", "1")
    ok("pinned" in r.stdout, "cli: pin")
    r = run("status")
    ok("entries" in r.stdout and r.returncode == 0, "cli: status runs")
    r = run("rm", "1")
    ok("deleted" in r.stdout, "cli: rm")
    r = run("show", "99")
    ok(r.returncode != 0, "cli: show on missing id exits nonzero")
    r = run("--version")
    ok(r.stdout.startswith("deja "), "cli: --version")


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        os.environ["DEJA_STATE_DIR"] = str(tmp / "state")
        print("store:")
        test_store(tmp)
        print("ingest filters:")
        test_ingest(tmp)
        print("formatting:")
        test_util()
        print("cli end-to-end:")
        test_cli(tmp)
    print(f"\nall {PASS} checks passed")


if __name__ == "__main__":
    main()
