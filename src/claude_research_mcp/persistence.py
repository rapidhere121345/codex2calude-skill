"""SQLite mappings, rolling quotas, and minimal audit records."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import uuid


class SessionOwnershipError(PermissionError):
    """The conversation belongs to another principal."""


class RateLimitError(RuntimeError):
    """The principal exhausted its rolling hourly quota."""


@dataclass(frozen=True)
class ConversationRecord:
    conversation_id: str
    principal: str
    claude_session_id: str | None
    model: str
    use_web: bool
    closed: bool


@contextmanager
def transaction(database_path: Path):
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize(database_path: Path) -> None:
    with transaction(database_path) as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
              conversation_id TEXT PRIMARY KEY,
              principal TEXT NOT NULL,
              claude_session_id TEXT UNIQUE,
              model TEXT NOT NULL,
              use_web INTEGER NOT NULL CHECK (use_web IN (0, 1)),
              created_at TEXT NOT NULL,
              closed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              principal TEXT NOT NULL,
              tool TEXT NOT NULL,
              conversation_id TEXT,
              model TEXT,
              status TEXT NOT NULL,
              duration_ms INTEGER,
              created_at TEXT NOT NULL,
              FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
            );
            CREATE INDEX IF NOT EXISTS audit_quota_window
              ON audit_log(principal, created_at);
            """
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_conversation(
    database_path: Path, principal: str, model: str, use_web: bool
) -> ConversationRecord:
    conversation_id = str(uuid.uuid4())
    with transaction(database_path) as db:
        db.execute(
            "INSERT INTO conversations "
            "(conversation_id, principal, claude_session_id, model, use_web, created_at, closed_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, NULL)",
            (conversation_id, principal, model, int(use_web), _now()),
        )
    return ConversationRecord(
        conversation_id, principal, None, model, use_web, False
    )


def _record(row: sqlite3.Row) -> ConversationRecord:
    return ConversationRecord(
        row["conversation_id"],
        row["principal"],
        row["claude_session_id"],
        row["model"],
        bool(row["use_web"]),
        row["closed_at"] is not None,
    )


def get_conversation(
    database_path: Path, conversation_id: str, principal: str
) -> ConversationRecord:
    with transaction(database_path) as db:
        row = db.execute(
            "SELECT conversation_id, principal, claude_session_id, model, use_web, created_at, closed_at "
            "FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
    if row is None:
        raise SessionOwnershipError("conversation is unavailable")
    if row["principal"] != principal:
        raise SessionOwnershipError("conversation is unavailable")
    return _record(row)


def attach_claude_session(
    database_path: Path,
    conversation_id: str,
    principal: str,
    claude_session_id: str,
) -> None:
    with transaction(database_path) as db:
        changed = db.execute(
            "UPDATE conversations SET claude_session_id = ? "
            "WHERE conversation_id = ? AND principal = ? AND closed_at IS NULL "
            "AND (claude_session_id IS NULL OR claude_session_id = ?)",
            (claude_session_id, conversation_id, principal, claude_session_id),
        ).rowcount
    if changed != 1:
        raise SessionOwnershipError("conversation cannot be attached")


def close_conversation(
    database_path: Path, conversation_id: str, principal: str
) -> ConversationRecord:
    get_conversation(database_path, conversation_id, principal)
    with transaction(database_path) as db:
        db.execute(
            "UPDATE conversations SET closed_at = ? "
            "WHERE conversation_id = ? AND principal = ? AND closed_at IS NULL",
            (_now(), conversation_id, principal),
        )
    return get_conversation(database_path, conversation_id, principal)


def reserve_call(
    database_path: Path,
    *,
    principal: str,
    tool: str,
    conversation_id: str | None,
    model: str,
    rate_limit: int,
) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with transaction(database_path) as db:
        db.execute("BEGIN IMMEDIATE")
        used = db.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE principal = ? AND created_at > ? AND status != 'rate_limited'",
            (principal, cutoff),
        ).fetchone()[0]
        if used >= rate_limit:
            raise RateLimitError(f"hourly limit reached ({rate_limit} calls)")
        cursor = db.execute(
            "INSERT INTO audit_log "
            "(principal, tool, conversation_id, model, status, duration_ms, created_at) "
            "VALUES (?, ?, ?, ?, 'running', NULL, ?)",
            (principal, tool, conversation_id, model, _now()),
        )
        return int(cursor.lastrowid or 0)


def finish_call(
    database_path: Path, audit_id: int, *, status: str, duration_ms: int
) -> None:
    with transaction(database_path) as db:
        db.execute(
            "UPDATE audit_log SET status = ?, duration_ms = ? WHERE id = ?",
            (status, duration_ms, audit_id),
        )


def record_audit_event(
    database_path: Path,
    *,
    principal: str,
    tool: str,
    conversation_id: str | None,
    model: str | None,
    status: str,
    duration_ms: int = 0,
) -> None:
    with transaction(database_path) as db:
        db.execute(
            "INSERT INTO audit_log "
            "(principal, tool, conversation_id, model, status, duration_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (principal, tool, conversation_id, model, status, duration_ms, _now()),
        )
