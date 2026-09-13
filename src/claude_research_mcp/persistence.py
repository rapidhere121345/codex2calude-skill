"""SQLite mappings, continue-loop protection, and minimal audit records."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import uuid


class SessionOwnershipError(PermissionError):
    """The conversation belongs to another principal."""


class ContinueBurstLimitError(RuntimeError):
    """The principal continued one conversation too rapidly."""


@dataclass(frozen=True)
class ConversationSummary:
    conversation_id: str
    title: str | None
    model: str
    use_web: bool
    created_at: str
    last_activity_at: str
    closed_at: str | None


@dataclass(frozen=True)
class ConversationRecord:
    conversation_id: str
    principal: str
    claude_session_id: str | None
    model: str
    use_web: bool
    closed: bool
    title: str | None
    created_at: str
    last_activity_at: str
    closed_at: str | None


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
              closed_at TEXT,
              title TEXT,
              last_activity_at TEXT
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
            """
        )
        # Serialize the column check + ALTER sequence across independently
        # spawned stdio servers during the first post-upgrade connection.
        db.execute("BEGIN IMMEDIATE")
        columns = {
            row["name"]
            for row in db.execute(
                "SELECT name FROM pragma_table_info('conversations')"
            ).fetchall()
        }
        # Deployed databases predate optional titles and explicit activity
        # timestamps. Both columns must remain nullable so old rows migrate
        # without rewriting the table.
        if "title" not in columns:
            db.execute("ALTER TABLE conversations ADD COLUMN title TEXT")
        added_last_activity = "last_activity_at" not in columns
        if added_last_activity:
            db.execute(
                "ALTER TABLE conversations ADD COLUMN last_activity_at TEXT"
            )
        if added_last_activity:
            db.execute(
                "UPDATE conversations "
                "SET last_activity_at = COALESCE("
                "(SELECT MAX(audit.created_at) FROM audit_log AS audit "
                " WHERE audit.conversation_id = conversations.conversation_id "
                " AND audit.tool IN "
                "('claude_start', 'claude_continue', 'claude_close')), "
                "closed_at, created_at) "
                "WHERE last_activity_at IS NULL"
            )
        db.execute(
            "CREATE INDEX IF NOT EXISTS conversations_principal_activity "
            "ON conversations(principal, last_activity_at, conversation_id)"
        )
        db.execute("DROP INDEX IF EXISTS audit_quota_window")
        db.execute(
            "CREATE INDEX IF NOT EXISTS audit_continue_burst_window "
            "ON audit_log(principal, conversation_id, tool, status, created_at)"
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_conversation(
    database_path: Path,
    principal: str,
    model: str,
    use_web: bool,
    title: str | None = None,
) -> ConversationRecord:
    conversation_id = str(uuid.uuid4())
    created_at = _now()
    with transaction(database_path) as db:
        db.execute(
            "INSERT INTO conversations "
            "(conversation_id, principal, claude_session_id, model, use_web, "
            "created_at, closed_at, title, last_activity_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, NULL, ?, ?)",
            (
                conversation_id,
                principal,
                model,
                int(use_web),
                created_at,
                title,
                created_at,
            ),
        )
    return ConversationRecord(
        conversation_id,
        principal,
        None,
        model,
        use_web,
        False,
        title,
        created_at,
        created_at,
        None,
    )


def _record(row: sqlite3.Row) -> ConversationRecord:
    title = row["title"]
    return ConversationRecord(
        row["conversation_id"],
        row["principal"],
        row["claude_session_id"],
        row["model"],
        bool(row["use_web"]),
        row["closed_at"] is not None,
        title,
        row["created_at"],
        row["last_activity_at"],
        row["closed_at"],
    )


def get_conversation(
    database_path: Path, conversation_id: str, principal: str
) -> ConversationRecord:
    with transaction(database_path) as db:
        row = db.execute(
            "SELECT conversation_id, principal, claude_session_id, model, use_web, "
            "created_at, closed_at, title, last_activity_at "
            "FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
    if row is None:
        raise SessionOwnershipError("conversation is unavailable")
    if row["principal"] != principal:
        raise SessionOwnershipError("conversation is unavailable")
    return _record(row)


def list_conversations(
    database_path: Path,
    *,
    principal: str,
    include_closed: bool,
    limit: int,
    offset: int,
) -> tuple[list[ConversationSummary], int, bool]:
    closed_filter = "" if include_closed else " AND closed_at IS NULL"
    activity = "COALESCE(last_activity_at, closed_at, created_at)"
    with transaction(database_path) as db:
        total = int(
            db.execute(
                "SELECT COUNT(*) FROM conversations "
                "WHERE principal = ?" + closed_filter,
                (principal,),
            ).fetchone()[0]
        )
        rows = db.execute(
            "SELECT conversation_id, title, model, use_web, created_at, "
            "closed_at, " + activity + " AS last_activity_at "
            "FROM conversations WHERE principal = ?" + closed_filter +
            " ORDER BY last_activity_at DESC, conversation_id ASC "
            "LIMIT ? OFFSET ?",
            (principal, limit, offset),
        ).fetchall()
    summaries = [
        ConversationSummary(
            row["conversation_id"],
            row["title"],
            row["model"],
            bool(row["use_web"]),
            row["created_at"],
            row["last_activity_at"],
            row["closed_at"],
        )
        for row in rows
    ]
    return summaries, total, offset + len(rows) < total


def attach_claude_session(
    database_path: Path,
    conversation_id: str,
    principal: str,
    claude_session_id: str,
) -> None:
    attached_at = _now()
    with transaction(database_path) as db:
        changed = db.execute(
            "UPDATE conversations SET claude_session_id = ?, last_activity_at = ? "
            "WHERE conversation_id = ? AND principal = ? AND closed_at IS NULL "
            "AND (claude_session_id IS NULL OR claude_session_id = ?)",
            (
                claude_session_id,
                attached_at,
                conversation_id,
                principal,
                claude_session_id,
            ),
        ).rowcount
    if changed != 1:
        raise SessionOwnershipError("conversation cannot be attached")


def close_conversation(
    database_path: Path, conversation_id: str, principal: str
) -> ConversationRecord:
    get_conversation(database_path, conversation_id, principal)
    closed_at = _now()
    with transaction(database_path) as db:
        db.execute(
            "UPDATE conversations SET closed_at = ?, last_activity_at = ? "
            "WHERE conversation_id = ? AND principal = ? AND closed_at IS NULL",
            (closed_at, closed_at, conversation_id, principal),
        )
    return get_conversation(database_path, conversation_id, principal)


def reserve_call(
    database_path: Path,
    *,
    principal: str,
    tool: str,
    conversation_id: str | None,
    model: str,
    continue_burst_limit: int | None = None,
    continue_burst_window_seconds: int | None = None,
) -> int:
    with transaction(database_path) as db:
        db.execute("BEGIN IMMEDIATE")
        # Compute the rolling boundary only after acquiring the write lock. A
        # concurrent reservation may otherwise make a pre-lock timestamp stale.
        now = datetime.now(timezone.utc)
        if tool == "claude_continue":
            if (
                conversation_id is None
                or continue_burst_limit is None
                or continue_burst_window_seconds is None
            ):
                raise ValueError("continue burst policy is required")
            burst_cutoff = (
                now - timedelta(seconds=continue_burst_window_seconds)
            ).isoformat()
            burst_used = db.execute(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE principal = ? AND conversation_id = ? "
                "AND tool = 'claude_continue' "
                "AND status IN ('running', 'success', 'error', 'timeout') "
                "AND created_at > ?",
                (principal, conversation_id, burst_cutoff),
            ).fetchone()[0]
            if burst_used >= continue_burst_limit:
                raise ContinueBurstLimitError(
                    "continue burst limit reached "
                    f"({continue_burst_limit} calls in "
                    f"{continue_burst_window_seconds} seconds)"
                )
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


def patch_audit_conversation(
    database_path: Path, audit_id: int, conversation_id: str, principal: str
) -> None:
    with transaction(database_path) as db:
        changed = db.execute(
            "UPDATE audit_log SET conversation_id = ? "
            "WHERE id = ? AND principal = ? AND conversation_id IS NULL",
            (conversation_id, audit_id, principal),
        ).rowcount
    if changed != 1:
        raise RuntimeError("audit call could not be associated")


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
