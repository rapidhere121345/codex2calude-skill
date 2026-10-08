"""SQLite mappings, continue-loop protection, and minimal audit records."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import time
import uuid


AUDIT_STATUSES = frozenset(
    ("running", "success", "error", "timeout", "cancelled", "burst_limited")
)
AUDIT_ERROR_CATEGORIES = frozenset(
    (
        "auth",
        "rate_limit",
        "model_unavailable",
        "permission",
        "network",
        "invalid_argument",
        "runtime",
        "timeout",
        "output_limit",
        "cancellation",
        "validation",
        "unknown",
    )
)
_HTTP_STATUS_ALLOWLIST = frozenset(
    (400, 401, 403, 404, 408, 413, 422, 429, 500, 502, 503, 504)
)
_TOKEN_USAGE_FIELDS = frozenset(
    (
        "input_tokens",
        "output_tokens",
        "thinking_tokens",
        "cache_creation_tokens",
        "cache_read_tokens",
        "total_tokens",
    )
)
_TOKEN_USAGE_SOURCES = frozenset(("gemini_cli_result",))
_TOKEN_USAGE_ALIASES = {
    "cache_creation_tokens": (
        "cache_creation_tokens",
        "cache_creation_input_tokens",
    ),
    "cache_read_tokens": (
        "cache_read_tokens",
        "cache_read_input_tokens",
        "cached_tokens",
    ),
}


def _safe_category(value: object) -> str | None:
    return value if isinstance(value, str) and value in AUDIT_ERROR_CATEGORIES else None


def _safe_http_status(value: object) -> int | None:
    return (
        value
        if type(value) is int and value in _HTTP_STATUS_ALLOWLIST
        else None
    )


def _safe_exit_code(value: object) -> int | None:
    return value if type(value) is int and -255 <= value <= 255 else None


def _safe_request_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError):
        return None
    return value if parsed.version == 4 and str(parsed) == value else None


def _safe_raw_log_saved(value: object) -> int | None:
    if value is None:
        return None
    return int(value) if isinstance(value, bool) else None


def recorded_token_usage(
    model_usage: object, source: str
) -> tuple[dict[str, int], str] | None:
    """Normalize only token counts explicitly present in a CLI result."""

    entries = (
        model_usage
        if isinstance(model_usage, list)
        else [model_usage]
        if isinstance(model_usage, dict)
        else None
    )
    if source not in _TOKEN_USAGE_SOURCES or entries is None:
        return None
    entries = [entry for entry in entries if isinstance(entry, dict)]
    if not entries:
        return None
    totals = {}
    for field in _TOKEN_USAGE_FIELDS:
        values = []
        for entry in entries:
            for key in _TOKEN_USAGE_ALIASES.get(field, (field,)):
                value = entry.get(key)
                if type(value) is int and 0 <= value <= 2**63 - 1:
                    values.append(value)
                    break  # Aliases name the same metric, not extra usage.
        # Missing components stay unknown instead of becoming zero/partial totals.
        if len(values) == len(entries) and sum(values) <= 2**63 - 1:
            totals[field] = sum(values)
    return (totals, source) if totals else None


def _safe_reported_models(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    models = [
        item
        for item in value
        if isinstance(item, str) and item and len(item) <= 200
    ][:8]
    return (
        json.dumps(models, ensure_ascii=False, separators=(",", ":"))
        if models
        else None
    )


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
    gemini_session_id: str | None
    model: str
    model_cli_value: str | None
    model_family: str | None
    reasoning_effort: str | None
    use_web: bool
    closed: bool
    title: str | None
    created_at: str
    last_activity_at: str
    closed_at: str | None


@dataclass(frozen=True)
class ConversationMessage:
    role: str
    content: str
    created_at: str


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


def _enable_wal(connection: sqlite3.Connection) -> None:
    for attempt in range(60):
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 59:
                raise
            time.sleep(0.05)


def initialize(database_path: Path) -> None:
    with transaction(database_path) as db:
        _enable_wal(db)
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
              conversation_id TEXT PRIMARY KEY,
              principal TEXT NOT NULL,
              gemini_session_id TEXT UNIQUE,
              model TEXT NOT NULL,
              model_cli_value TEXT,
              model_family TEXT,
              reasoning_effort TEXT,
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
        # A conversation retains the exact resolved model policy used at its
        # first successful start. Nullable additive columns keep legacy rows
        # readable and explicitly distinguish a missing snapshot from auto.
        if "model_cli_value" not in columns:
            db.execute("ALTER TABLE conversations ADD COLUMN model_cli_value TEXT")
        if "model_family" not in columns:
            db.execute("ALTER TABLE conversations ADD COLUMN model_family TEXT")
        if "reasoning_effort" not in columns:
            db.execute("ALTER TABLE conversations ADD COLUMN reasoning_effort TEXT")
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
                "('gemini_start', 'gemini_continue', 'gemini_close')), "
                "closed_at, created_at) "
                "WHERE last_activity_at IS NULL"
            )
        audit_columns = {
            row["name"]
            for row in db.execute("SELECT name FROM pragma_table_info('audit_log')")
        }
        # Nullable additive columns allow old executables to keep using explicit
        # legacy INSERTs while a new executable rolls forward.  History is not
        # inferred or backfilled.
        if "error_category" not in audit_columns:
            db.execute("ALTER TABLE audit_log ADD COLUMN error_category TEXT")
        if "http_status" not in audit_columns:
            db.execute("ALTER TABLE audit_log ADD COLUMN http_status INTEGER")
        if "exit_code" not in audit_columns:
            db.execute("ALTER TABLE audit_log ADD COLUMN exit_code INTEGER")
        if "request_id" not in audit_columns:
            db.execute("ALTER TABLE audit_log ADD COLUMN request_id TEXT")
        if "raw_log_saved" not in audit_columns:
            db.execute("ALTER TABLE audit_log ADD COLUMN raw_log_saved INTEGER")
        for column in (
            "input_tokens",
            "output_tokens",
            "thinking_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
            "total_tokens",
        ):
            if column not in audit_columns:
                db.execute(f"ALTER TABLE audit_log ADD COLUMN {column} INTEGER")
        if "token_usage_source" not in audit_columns:
            db.execute("ALTER TABLE audit_log ADD COLUMN token_usage_source TEXT")
        if "reported_models_json" not in audit_columns:
            db.execute("ALTER TABLE audit_log ADD COLUMN reported_models_json TEXT")
        db.execute(
            "CREATE INDEX IF NOT EXISTS conversations_principal_activity "
            "ON conversations(principal, last_activity_at, conversation_id)"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              conversation_id TEXT NOT NULL,
              principal TEXT NOT NULL,
              request_id TEXT NOT NULL,
              role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
              content TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(request_id, role),
              FOREIGN KEY(conversation_id)
                REFERENCES conversations(conversation_id)
            )
            """
        )
        db.execute("DROP INDEX IF EXISTS audit_quota_window")
        db.execute(
            "CREATE INDEX IF NOT EXISTS audit_continue_burst_window "
            "ON audit_log(principal, conversation_id, tool, status, created_at)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS conversation_history_export "
            "ON conversation_history(principal, conversation_id, id)"
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_conversation(
    database_path: Path,
    principal: str,
    model: str,
    use_web: bool,
    title: str | None = None,
    model_cli_value: str | None = None,
    model_family: str | None = None,
    reasoning_effort: str | None = None,
) -> ConversationRecord:
    conversation_id = str(uuid.uuid4())
    created_at = _now()
    with transaction(database_path) as db:
        db.execute(
            "INSERT INTO conversations "
            "(conversation_id, principal, gemini_session_id, model, use_web, "
            "created_at, closed_at, title, last_activity_at, model_cli_value, "
            "model_family, reasoning_effort) "
            "VALUES (?, ?, NULL, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                principal,
                model,
                int(use_web),
                created_at,
                title,
                created_at,
                model_cli_value,
                model_family,
                reasoning_effort,
            ),
        )
    return ConversationRecord(
        conversation_id,
        principal,
        None,
        model,
        model_cli_value,
        model_family,
        reasoning_effort,
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
        row["gemini_session_id"],
        row["model"],
        row["model_cli_value"],
        row["model_family"],
        row["reasoning_effort"],
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
            "SELECT conversation_id, principal, gemini_session_id, model, use_web, "
            "created_at, closed_at, title, last_activity_at, model_cli_value, "
            "model_family, reasoning_effort "
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


def attach_gemini_session(
    database_path: Path,
    conversation_id: str,
    principal: str,
    gemini_session_id: str,
) -> None:
    attached_at = _now()
    with transaction(database_path) as db:
        changed = db.execute(
            "UPDATE conversations SET gemini_session_id = ?, last_activity_at = ? "
            "WHERE conversation_id = ? AND principal = ? AND closed_at IS NULL "
            "AND (gemini_session_id IS NULL OR gemini_session_id = ?)",
            (
                gemini_session_id,
                attached_at,
                conversation_id,
                principal,
                gemini_session_id,
            ),
        ).rowcount
    if changed != 1:
        raise SessionOwnershipError("conversation cannot be attached")


def record_conversation_messages(
    database_path: Path,
    *,
    conversation_id: str,
    principal: str,
    request_id: str,
    prompt: str,
    response: str,
) -> None:
    """Persist exactly one user/assistant pair for a successful bridge call.

    Request identifiers are generated by the server and make retries after an
    uncertain persistence outcome idempotent.  Content is never copied into the
    audit log; this table is the only bridge-owned history source.
    """

    if not isinstance(request_id, str) or not request_id:
        raise ValueError("invalid history request id")
    now = _now()
    with transaction(database_path) as db:
        owner = db.execute(
            "SELECT principal FROM conversations "
            "WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if owner is None or owner["principal"] != principal:
            raise SessionOwnershipError("conversation is unavailable")
        for role, content in (("user", prompt), ("assistant", response)):
            existing = db.execute(
                "SELECT conversation_id, principal, content "
                "FROM conversation_history WHERE request_id = ? AND role = ?",
                (request_id, role),
            ).fetchone()
            if existing is not None:
                if (
                    existing["conversation_id"] != conversation_id
                    or existing["principal"] != principal
                    or existing["content"] != content
                ):
                    raise RuntimeError("history request conflict")
                continue
            db.execute(
                "INSERT INTO conversation_history "
                "(conversation_id, principal, request_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    principal,
                    request_id,
                    role,
                    content,
                    now,
                ),
            )


def list_conversation_messages(
    database_path: Path,
    *,
    conversation_id: str,
    principal: str,
    limit: int,
) -> tuple[list[ConversationMessage], int]:
    """Return a bounded history page and the exact total message count."""

    if not isinstance(limit, int) or type(limit) is bool or limit < 1:
        raise ValueError("history limit must be positive")
    with transaction(database_path) as db:
        total = int(
            db.execute(
                "SELECT COUNT(*) FROM conversation_history "
                "WHERE conversation_id = ? AND principal = ?",
                (conversation_id, principal),
            ).fetchone()[0]
        )
        rows = db.execute(
            "SELECT role, content, created_at FROM conversation_history "
            "WHERE conversation_id = ? AND principal = ? "
            "ORDER BY id ASC LIMIT ?",
            (conversation_id, principal, limit),
        ).fetchall()
    return (
        [
            ConversationMessage(
                row["role"],
                row["content"],
                row["created_at"],
            )
            for row in rows
        ],
        total,
    )


def count_audit_texts(database_path: Path) -> int:
    """Test helper proving audit metadata never contains prompt/result text."""

    with transaction(database_path) as db:
        return int(db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])


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
    request_id: str | None = None,
    continue_burst_limit: int | None = None,
    continue_burst_window_seconds: int | None = None,
) -> int:
    with transaction(database_path) as db:
        db.execute("BEGIN IMMEDIATE")
        # Compute the rolling boundary only after acquiring the write lock. A
        # concurrent reservation may otherwise make a pre-lock timestamp stale.
        now = datetime.now(timezone.utc)
        if tool == "gemini_continue":
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
                "AND tool = 'gemini_continue' "
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
            "(principal, tool, conversation_id, model, status, duration_ms, created_at, "
            "error_category, http_status, exit_code, request_id, raw_log_saved) "
            "VALUES (?, ?, ?, ?, 'running', NULL, ?, NULL, NULL, NULL, ?, NULL)",
            (principal, tool, conversation_id, model, _now(), _safe_request_id(request_id)),
        )
        return int(cursor.lastrowid or 0)


def finish_call(
    database_path: Path,
    audit_id: int,
    *,
    status: str,
    duration_ms: int,
    error_category: str | None = None,
    http_status: int | None = None,
    exit_code: int | None = None,
    request_id: str | None = None,
    raw_log_saved: bool | None = None,
    token_usage: tuple[dict[str, int], str] | None = None,
    reported_models: list[str] | None = None,
) -> None:
    if status not in AUDIT_STATUSES:
        raise ValueError("invalid audit status")
    with transaction(database_path) as db:
        normalized = (
            recorded_token_usage(token_usage[0], token_usage[1])
            if token_usage is not None
            else None
        )
        tokens, token_source = normalized if normalized is not None else ({}, None)
        db.execute(
            "UPDATE audit_log SET status = ?, duration_ms = ?, error_category = ?, "
            "http_status = ?, exit_code = ?, request_id = COALESCE(?, request_id), "
            "raw_log_saved = ?, input_tokens = ?, output_tokens = ?, "
            "thinking_tokens = ?, cache_creation_tokens = ?, cache_read_tokens = ?, "
            "total_tokens = ?, token_usage_source = ?, reported_models_json = ? "
            "WHERE id = ?",
            (
                status,
                duration_ms,
                _safe_category(error_category),
                _safe_http_status(http_status),
                _safe_exit_code(exit_code),
                _safe_request_id(request_id),
                _safe_raw_log_saved(raw_log_saved),
                tokens.get("input_tokens"),
                tokens.get("output_tokens"),
                tokens.get("thinking_tokens"),
                tokens.get("cache_creation_tokens"),
                tokens.get("cache_read_tokens"),
                tokens.get("total_tokens"),
                token_source,
                _safe_reported_models(reported_models),
                audit_id,
            ),
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
    error_category: str | None = None,
    http_status: int | None = None,
    exit_code: int | None = None,
    request_id: str | None = None,
    raw_log_saved: bool | None = None,
    token_usage: tuple[dict[str, int], str] | None = None,
) -> None:
    if status not in AUDIT_STATUSES:
        raise ValueError("invalid audit status")
    with transaction(database_path) as db:
        normalized = (
            recorded_token_usage(token_usage[0], token_usage[1])
            if token_usage is not None
            else None
        )
        tokens, token_source = normalized if normalized is not None else ({}, None)
        db.execute(
            "INSERT INTO audit_log "
            "(principal, tool, conversation_id, model, status, duration_ms, created_at, "
            "error_category, http_status, exit_code, request_id, raw_log_saved, "
            "input_tokens, output_tokens, thinking_tokens, cache_creation_tokens, "
            "cache_read_tokens, total_tokens, token_usage_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                principal,
                tool,
                conversation_id,
                model,
                status,
                duration_ms,
                _now(),
                _safe_category(error_category),
                _safe_http_status(http_status),
                _safe_exit_code(exit_code),
                _safe_request_id(request_id),
                _safe_raw_log_saved(raw_log_saved),
                tokens.get("input_tokens"),
                tokens.get("output_tokens"),
                tokens.get("thinking_tokens"),
                tokens.get("cache_creation_tokens"),
                tokens.get("cache_read_tokens"),
                tokens.get("total_tokens"),
                token_source,
            ),
        )
