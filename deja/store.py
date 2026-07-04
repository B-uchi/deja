"""SQLite storage with FTS5 search. Safe for concurrent daemon/CLI/GUI use (WAL)."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id          INTEGER PRIMARY KEY,
    content     TEXT NOT NULL,
    hash        TEXT NOT NULL UNIQUE,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    times_seen  INTEGER NOT NULL DEFAULT 1,
    pinned      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_last_seen ON entries(last_seen DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts
    USING fts5(content, content='entries', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
END;
"""


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else config.db_path()
        self.db = sqlite3.connect(self.path, timeout=5.0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # ------------------------------------------------------------- ingestion

    def add(self, content: str) -> tuple[int, bool]:
        """Insert content, or bump an existing identical entry.
        Returns (entry id, was_new)."""
        h = hashlib.sha256(content.encode()).hexdigest()
        now = time.time()
        with self.db:
            row = self.db.execute(
                "SELECT id FROM entries WHERE hash = ?", (h,)).fetchone()
            if row:
                self.db.execute(
                    "UPDATE entries SET last_seen = ?, times_seen = times_seen + 1 "
                    "WHERE id = ?", (now, row["id"]))
                return row["id"], False
            cur = self.db.execute(
                "INSERT INTO entries (content, hash, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?)", (content, h, now, now))
            return cur.lastrowid, True

    def prune(self, max_entries: int) -> int:
        """Delete oldest unpinned entries beyond max_entries. Returns count."""
        with self.db:
            cur = self.db.execute(
                "DELETE FROM entries WHERE pinned = 0 AND id IN ("
                "  SELECT id FROM entries WHERE pinned = 0"
                "  ORDER BY last_seen DESC LIMIT -1 OFFSET ?)",
                (max_entries,))
            return cur.rowcount

    # --------------------------------------------------------------- queries

    def recent(self, limit: int = 20, pinned_only: bool = False) -> list[sqlite3.Row]:
        q = ("SELECT * FROM entries {} ORDER BY pinned DESC, last_seen DESC LIMIT ?"
             .format("WHERE pinned = 1" if pinned_only else ""))
        return self.db.execute(q, (limit,)).fetchall()

    def search(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        """FTS5 prefix search; falls back to LIKE for queries FTS can't parse."""
        tokens = re.findall(r"\w+", query)
        if tokens:
            fts = " ".join(f'"{t}"*' for t in tokens)
            try:
                return self.db.execute(
                    "SELECT e.* FROM entries_fts f JOIN entries e ON e.id = f.rowid "
                    "WHERE entries_fts MATCH ? "
                    "ORDER BY e.pinned DESC, bm25(entries_fts), e.last_seen DESC "
                    "LIMIT ?", (fts, limit)).fetchall()
            except sqlite3.OperationalError:
                pass
        like = f"%{query}%"
        return self.db.execute(
            "SELECT * FROM entries WHERE content LIKE ? "
            "ORDER BY pinned DESC, last_seen DESC LIMIT ?", (like, limit)).fetchall()

    def get(self, entry_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()

    def stats(self) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(pinned), 0) AS pins, "
            "COALESCE(SUM(LENGTH(content)), 0) AS chars FROM entries").fetchone()
        return {"entries": row["n"], "pinned": row["pins"], "chars": row["chars"],
                "db_bytes": self.path.stat().st_size if self.path.exists() else 0}

    # --------------------------------------------------------------- editing

    def set_pinned(self, entry_id: int, pinned: bool) -> bool:
        with self.db:
            cur = self.db.execute(
                "UPDATE entries SET pinned = ? WHERE id = ?",
                (1 if pinned else 0, entry_id))
            return cur.rowcount > 0

    def delete(self, entry_id: int) -> bool:
        with self.db:
            return self.db.execute(
                "DELETE FROM entries WHERE id = ?", (entry_id,)).rowcount > 0

    def purge(self, days: float | None = None, include_pinned: bool = False) -> int:
        """Delete entries. days=None means all of them (respecting pins)."""
        cond, args = [], []
        if not include_pinned:
            cond.append("pinned = 0")
        if days is not None:
            cond.append("last_seen < ?")
            args.append(time.time() - days * 86400)
        where = ("WHERE " + " AND ".join(cond)) if cond else ""
        with self.db:
            n = self.db.execute(f"DELETE FROM entries {where}", args).rowcount
        self.db.execute("VACUUM")
        return n
