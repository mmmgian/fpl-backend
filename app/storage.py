"""SQLite persistence for league snapshots. Synchronous by design: callers run it off the event loop."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class StoredSnapshot:
    league_id: int
    gw: int
    taken_at: str
    data_json: str          # stored JSON text, served as-is (no parse / re-encode)


class SnapshotRepository:
    def __init__(self, db_path: Path):
        self._path = db_path

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._path)
        con.row_factory = sqlite3.Row
        return con

    def init(self) -> None:
        with closing(self._connect()) as con, con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS league_snapshots (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  league_id INTEGER NOT NULL,
                  gw INTEGER NOT NULL,
                  taken_at TEXT NOT NULL,
                  data TEXT NOT NULL,
                  UNIQUE(league_id, gw)
                )
                """
            )
            # v1 wrote to league_snapshots but read from a legacy `snapshots` table; fold any legacy rows in.
            legacy = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='snapshots'").fetchone()
            if legacy:
                con.execute(
                    "INSERT OR IGNORE INTO league_snapshots (league_id, gw, taken_at, data) "
                    "SELECT league_id, gw, ?, data FROM snapshots WHERE league_id IS NOT NULL AND gw IS NOT NULL",
                    (datetime.now(timezone.utc).isoformat(),),
                )

    def save(self, league_id: int, gw: int, data: dict) -> bool:
        """True if stored, False if a snapshot for (league, gw) already existed."""
        with closing(self._connect()) as con, con:
            cur = con.execute(
                "INSERT OR IGNORE INTO league_snapshots (league_id, gw, taken_at, data) VALUES (?, ?, ?, ?)",
                (league_id, gw, datetime.now(timezone.utc).isoformat(timespec="seconds"), json.dumps(data)),
            )
            return cur.rowcount == 1

    def list(self, league_id: int) -> list[dict]:
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT gw, taken_at FROM league_snapshots WHERE league_id = ? ORDER BY gw ASC", (league_id,)
            ).fetchall()
        return [{"gw": r["gw"], "taken_at": r["taken_at"]} for r in rows]

    def get(self, league_id: int, gw: int) -> StoredSnapshot | None:
        with closing(self._connect()) as con:
            r = con.execute(
                "SELECT gw, taken_at, data FROM league_snapshots WHERE league_id = ? AND gw = ?", (league_id, gw)
            ).fetchone()
        return None if r is None else StoredSnapshot(league_id, r["gw"], r["taken_at"], r["data"])
