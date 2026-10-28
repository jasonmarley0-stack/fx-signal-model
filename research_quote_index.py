"""Publisher-owned, disposable SQLite index. Never writes observer files.

Quotes live on disk; only one decision's bounded window enters Python.
The index resumes at a complete-line byte offset, so publication does not
rescan the complete raw log every 15 minutes. It is a cache, not evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path


class QuoteWindowTooLarge(RuntimeError):
    """Refuse explicitly before allocating an unexpectedly dense window."""


class QuoteIndex:
    MAX_WINDOW_ROWS = 100_000

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA cache_size=-2048")  # 2 MiB page cache
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("CREATE TABLE IF NOT EXISTS quotes (pair TEXT, stamp REAL, raw TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS quote_window ON quotes(pair,stamp)")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")

    def close(self):
        self.db.close()

    @staticmethod
    def _anchor(f, offset):
        f.seek(max(0, offset - 4096))
        return hashlib.sha256(f.read(min(offset, 4096))).hexdigest()

    def sync(self, source: Path) -> int:
        """Index complete JSONL lines visible at the beginning of this call.

        Rotation/truncation/change at the checkpoint rebuilds the cache.
        A partial final line is retried next time, never silently lost.
        Checkpoint and inserts commit together; an interrupted sync rolls back.
        Logs are append-only evidence: arbitrary edits away from the checkpoint
        are not supported; delete this disposable cache after such edits.
        """
        if not source.exists():
            with self.db:
                self.db.execute("DELETE FROM quotes")
                self.db.execute("DELETE FROM meta")
            return 0
        with source.open("rb") as f, self.db:
            stat = os.fstat(f.fileno())
            identity = f"{source.resolve()}:{stat.st_dev}:{stat.st_ino}"
            saved = dict(self.db.execute("SELECT key,value FROM meta"))
            offset = int(saved.get("offset", 0))
            if (saved.get("identity") != identity or offset > stat.st_size
                    or saved.get("anchor") != self._anchor(f, offset)):
                self.db.execute("DELETE FROM quotes")
                offset, count = 0, 0
            else:
                count = int(saved.get("count", 0))
            f.seek(offset)
            while f.tell() < stat.st_size:
                start = f.tell()
                line = f.readline(stat.st_size - start)
                if not line.endswith(b"\n"):
                    f.seek(start)
                    break
                offset = f.tell()
                try:
                    q = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(q, dict):
                    continue
                count += 1
                try:
                    dt = datetime.fromisoformat(q["received_at_utc"])
                    if dt.tzinfo is None:
                        continue
                    stamp = dt.timestamp()
                except (KeyError, ValueError, TypeError, OverflowError):
                    continue
                self.db.execute("INSERT INTO quotes VALUES(?,?,?)",
                                (q.get("pair"), stamp, line.decode("utf-8")))
            state = {"identity": identity, "offset": str(offset), "count": str(count),
                     "anchor": self._anchor(f, offset)}
            self.db.executemany("INSERT OR REPLACE INTO meta VALUES(?,?)", state.items())
        return count

    def window(self, pair: str, start: datetime, end: datetime) -> list[dict]:
        args = (pair, start.timestamp(), end.timestamp())
        where = "pair=? AND stamp>=? AND stamp<=?"
        count = self.db.execute(f"SELECT COUNT(*) FROM quotes WHERE {where}", args).fetchone()[0]
        if count > self.MAX_WINDOW_ROWS:
            raise QuoteWindowTooLarge("Single decision quote window exceeds the publisher safety limit; no score published.")
        # Arrival order preserves the frozen scorer's stable ordering on ties.
        return [json.loads(row[0]) for row in self.db.execute(
            f"SELECT raw FROM quotes WHERE {where} ORDER BY rowid", args)]

    def count_windows(self, windows):
        return sum(self.db.execute(
            "SELECT COUNT(*) FROM quotes WHERE pair=? AND stamp>=? AND stamp<=?",
            (pair, start.timestamp(), end.timestamp())).fetchone()[0]
            for pair, intervals in windows.items() for start, end in intervals)
