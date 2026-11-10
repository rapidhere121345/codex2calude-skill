"""Gateway-owned SQLite route map; every lookup is principal-scoped."""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS routes (
  id TEXT PRIMARY KEY,
  principal TEXT NOT NULL,
  provider TEXT NOT NULL CHECK(provider IN ('claude','gemini')),
  backend_id TEXT NOT NULL,
  model TEXT NOT NULL,
  model_cli_value TEXT NOT NULL,
  model_family TEXT NOT NULL,
  reasoning_effort TEXT,
  title TEXT,
  use_web INTEGER NOT NULL CHECK(use_web IN (0,1)),
  created_at TEXT NOT NULL,
  last_activity_at TEXT NOT NULL,
  closed_at TEXT,
  UNIQUE(principal,provider,backend_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RouteStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return closing(db)

    def create(self, principal: str, provider: str, backend_id: str,
               model: str, cli_value: str, family: str,
               reasoning_effort: str | None,
               title: str | None, use_web: bool) -> str:
        rid = str(uuid.uuid4())
        now = _now()
        with self._connect() as db:
            db.execute(
                "INSERT INTO routes(id,principal,provider,backend_id,model,"
                "model_cli_value,model_family,reasoning_effort,title,use_web,"
                "created_at,last_activity_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, principal, provider, backend_id, model,
                 cli_value, family, reasoning_effort, title,
                 1 if use_web else 0, now, now))
            db.commit()
        return rid

    def get(self, route_id: str, principal: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM routes WHERE id=? AND principal=?",
                (route_id, principal)).fetchone()
        return dict(row) if row else None

    def touch(self, route_id: str, principal: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE routes SET last_activity_at=? WHERE id=?"
                " AND principal=?", (_now(), route_id, principal))
            db.commit()

    def close(self, route_id: str, principal: str) -> bool:
        with self._connect() as db:
            cur = db.execute(
                "UPDATE routes SET closed_at=? WHERE id=? AND principal=?"
                " AND closed_at IS NULL",
                (_now(), route_id, principal))
            db.commit()
        return cur.rowcount == 1

    def list_routes(self, principal: str, provider: str,
                    include_closed: bool, limit: int,
                    offset: int) -> tuple[list[dict], int]:
        where = "principal=? AND provider=?"
        params: list = [principal, provider]
        if not include_closed:
            where += " AND closed_at IS NULL"
        with self._connect() as db:
            total = db.execute(
                f"SELECT COUNT(*) c FROM routes WHERE {where}",
                params).fetchone()["c"]
            rows = db.execute(
                f"SELECT * FROM routes WHERE {where}"
                " ORDER BY last_activity_at DESC, id"
                " LIMIT ? OFFSET ?",
                params + [limit, offset]).fetchall()
        return [dict(r) for r in rows], total
