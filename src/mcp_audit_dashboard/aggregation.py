from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
from typing import Iterable


PRINCIPALS = ("owner", "partner", "total")
BRIDGES = ("claude", "gemini", "total")
PERIODS = {"24h": 24, "7d": 7 * 24, "30d": 30 * 24, "all": None}
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "thinking_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "total_tokens",
)
PAGE_SIZES = (10, 20, 50)
EVENT_STATUSES = ("all", "success", "failed", "running")
EVENT_KINDS = ("all", "inference", "admin")
INFERENCE_TOOLS = frozenset(
    (
        "claude_once",
        "claude_start",
        "claude_continue",
        "gemini_once",
        "gemini_start",
        "gemini_continue",
    )
)


def parse_filters(query: dict[str, list[str]]) -> tuple[str, str, str]:
    if len(query) > 16:
        raise ValueError("too many filters")
    result = []
    for name, allowed in (
        ("bridge", frozenset(("claude", "gemini", "total"))),
        ("principal", frozenset(PRINCIPALS)),
        ("period", frozenset(PERIODS)),
    ):
        values = query.get(name, ["7d" if name == "period" else "total"])
        if len(values) != 1 or values[0] not in allowed:
            raise ValueError("invalid filter")
        result.append(values[0])
    unknown = set(query) - {
        "bridge", "principal", "period", "page", "page_size",
        "event_status", "event_kind",
    }
    if unknown:
        raise ValueError("invalid filter")
    return result[0], result[1], result[2]


def parse_event_filters(query: dict[str, list[str]]) -> dict[str, int | str]:
    result = {}
    for name, default in (
        ("page", "1"), ("page_size", "20"),
        ("event_status", "all"), ("event_kind", "all"),
    ):
        values = query.get(name, [default])
        if len(values) != 1:
            raise ValueError("duplicate event filter")
        value = values[0]
        if name in ("page", "page_size"):
            if not value.isascii() or not value.isdecimal() or len(value) > 7:
                raise ValueError("invalid page")
            value = int(value)
            if name == "page" and not 1 <= value <= 1_000_000:
                raise ValueError("invalid page")
            if name == "page_size" and value not in PAGE_SIZES:
                raise ValueError("invalid page size")
        elif value not in (EVENT_STATUSES if name == "event_status" else EVENT_KINDS):
            raise ValueError("invalid event filter")
        result[name] = value
    return result


def _cutoff(period: str, now: datetime) -> str:
    if period == "all":
        return ""
    return (
        now - timedelta(hours=PERIODS[period])
    ).isoformat()


def _empty_metrics() -> dict[str, object]:
    return {
        "calls": 0,
        "inference_calls": 0,
        "inference_successes": 0,
        "inference_failures": 0,
        "inference_completed": 0,
        "inference_duration_ms_known": 0,
        "inference_duration_ms_total": 0,
        "admin_calls": 0,
        "successes": 0,
        "errors": 0,
        "failures": 0,
        "duration_ms_known": 0,
        "duration_ms_total": 0,
        "status_counts": defaultdict(int),
        "error_categories": defaultdict(int),
        "token_known_calls": 0,
        "token_sources": defaultdict(int),
        **{
            field: {"total": 0, "known_calls": 0}
            for field in TOKEN_FIELDS
        },
    }


def _touch_all(groups: dict[str, dict[str, object]]) -> None:
    for bridge in BRIDGES:
        for principal in PRINCIPALS:
            groups[(bridge, principal)]


def _principal(value: object) -> str:
    return value if value in ("owner", "partner") else "other"


def _safe_int(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def _add(metrics: dict[str, object], row: dict[str, object]) -> None:
    metrics["calls"] += 1
    inference = row["tool"] in INFERENCE_TOOLS
    if inference:
        metrics["inference_calls"] += 1
    else:
        metrics["admin_calls"] += 1
    status = row["status"]
    if inference and status != "running":
        metrics["inference_completed"] += 1
        metrics["inference_successes"] += status == "success"
        metrics["inference_failures"] += status != "success"
    metrics["status_counts"][status] += 1
    if status == "success":
        metrics["successes"] += 1
    if status == "error":
        metrics["errors"] += 1
    if status != "running":
        metrics["failures"] += status != "success"
    duration = _safe_int(row["duration_ms"])
    if duration is not None:
        metrics["duration_ms_known"] += 1
        metrics["duration_ms_total"] += duration
        if inference:
            metrics["inference_duration_ms_known"] += 1
            metrics["inference_duration_ms_total"] += duration
    if row.get("error_category") is not None:
        metrics["error_categories"][row["error_category"]] += 1
    source = row.get("token_usage_source")
    known = inference and source in ("claude_cli_result", "gemini_cli_result") and any(
        _safe_int(row.get(field)) is not None for field in TOKEN_FIELDS
    )
    if known:
        metrics["token_known_calls"] += 1
        metrics["token_sources"][source] += 1
        for field in TOKEN_FIELDS:
            value = _safe_int(row.get(field))
            if value is not None:
                metrics[field]["total"] += value
                metrics[field]["known_calls"] += 1


def _serialize(metrics: dict[str, object]) -> dict[str, object]:
    duration_known = metrics["duration_ms_known"]
    return {
        "calls": metrics["calls"],
        "inference_calls": metrics["inference_calls"],
        "inference_successes": metrics["inference_successes"],
        "inference_failures": metrics["inference_failures"],
        "inference_success_rate": (
            metrics["inference_successes"] / metrics["inference_completed"]
            if metrics["inference_completed"] else None
        ),
        "inference_duration_ms_average": (
            metrics["inference_duration_ms_total"] / metrics["inference_duration_ms_known"]
            if metrics["inference_duration_ms_known"] else None
        ),
        "admin_calls": metrics["admin_calls"],
        "successes": metrics["successes"],
        "errors": metrics["errors"],
        "failures": metrics["failures"],
        "success_rate": (
            metrics["successes"] / metrics["calls"]
            if metrics["calls"]
            else None
        ),
        "duration_ms_average": (
            metrics["duration_ms_total"] / duration_known
            if duration_known
            else None
        ),
        "duration_ms_known_calls": duration_known,
        "status_counts": dict(metrics["status_counts"]),
        "error_categories": dict(metrics["error_categories"]),
        "token_known_calls": metrics["token_known_calls"],
        "token_known_coverage": (
            metrics["token_known_calls"] / metrics["inference_calls"]
            if metrics["inference_calls"]
            else None
        ),
        "token_coverage_denominator": "inference_calls",
        "token_sources": dict(metrics["token_sources"]),
        "tokens": {
            field: {
                "total": (
                    metrics[field]["total"]
                    if metrics[field]["known_calls"]
                    else None
                ),
                "known_calls": metrics[field]["known_calls"],
            }
            for field in TOKEN_FIELDS
        },
    }


def _read_rows(path: Path, cutoff: str) -> tuple[list[dict[str, object]], str | None]:
    if not path.is_file():
        return [], "unavailable"
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        columns = {
            row["name"] for row in connection.execute(
                "SELECT name FROM pragma_table_info('audit_log')"
            )
        }
        if not {"principal", "tool", "status", "created_at"}.issubset(columns):
            return [], "invalid_schema"
        token_columns = {
            field: field if field in columns else f"NULL AS {field}"
            for field in (*TOKEN_FIELDS, "token_usage_source")
        }
        selected = [
            "id", "principal", "tool", "model", "status", "duration_ms",
            "created_at", "error_category", "http_status", "exit_code",
        ]
        aliases = []
        for field, expression in token_columns.items():
            if expression == field:
                aliases.append(field)
            else:
                aliases.append(f"NULL AS {field}")
        for field in ("reported_models_json",):
            aliases.append(field if field in columns else f"NULL AS {field}")
        rows = connection.execute(
            f"SELECT {', '.join(selected + aliases)} FROM audit_log "
            "WHERE created_at >= ? ORDER BY id DESC",
            (cutoff,),
        ).fetchall()
        connection.close()
        return [dict(row) for row in rows], None
    except (OSError, sqlite3.Error, ValueError):
        return [], "unavailable"
    finally:
        if connection is not None:
            connection.close()


def _sanitized_recent(rows: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    result = []
    for row in rows:
        result.append(
            {
                "bridge": row["bridge"],
                "principal": row["principal"],
                "tool": row["tool"],
                "configured_model": row.get("model"),
                "reported_models": _reported_models(
                    row.get("reported_models_json")
                ),
                "status": row["status"],
                "duration_ms": row.get("duration_ms"),
                "created_at": row.get("created_at"),
                "error_category": row.get("error_category"),
                "http_status": row.get("http_status"),
                "exit_code": row.get("exit_code"),
                "token_usage_source": row.get("token_usage_source"),
                "tokens": {field: _safe_int(row.get(field)) for field in TOKEN_FIELDS},
        }
        )
    return result


def _reported_models(value: object) -> list[str] | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(parsed, list):
        return None
    models = [
        item for item in parsed if isinstance(item, str) and item and len(item) <= 200
    ]
    return models[:8] if models else None


def aggregate_dashboard(
    database_paths: dict[str, Path],
    *,
    bridge_filter: str,
    principal_filter: str,
    period: str,
    now: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
    event_status: str = "all",
    event_kind: str = "all",
) -> dict[str, object]:
    if (type(page) is not int or not 1 <= page <= 1_000_000
            or type(page_size) is not int or page_size not in PAGE_SIZES
            or event_status not in EVENT_STATUSES or event_kind not in EVENT_KINDS):
        raise ValueError("invalid event filters")
    current = now or datetime.now(timezone.utc)
    rows: list[dict[str, object]] = []
    availability = {}
    for bridge, path in database_paths.items():
        bridge_rows, error = _read_rows(path, _cutoff(period, current))
        availability[bridge] = "ok" if error is None else error
        for row in bridge_rows:
            row["bridge"] = bridge
        rows.extend(bridge_rows)
    rows.sort(key=lambda row: (
        str(row.get("created_at") or ""), row["bridge"],
        _safe_int(row.get("id")) or 0,
    ), reverse=True)

    groups: dict[tuple[str, str], dict[str, object]] = defaultdict(_empty_metrics)
    _touch_all(groups)
    included = []
    for row in rows:
        if bridge_filter != "total" and row["bridge"] != bridge_filter:
            continue
        principal = _principal(row["principal"])
        if principal_filter != "total" and principal != principal_filter:
            continue
        included.append(row)
        for bridge_name in ("total", row["bridge"]):
            for principal_name in ("total", principal):
                _add(groups[(bridge_name, principal_name)], row)

    metrics = {
        f"{bridge}:{principal}": _serialize(groups[(bridge, principal)])
        for bridge in BRIDGES
        for principal in PRINCIPALS
    }
    events = [row for row in included if (
        event_status == "all" or row["status"] == event_status
        or (event_status == "failed" and row["status"] not in ("success", "running"))
    ) and (
        event_kind == "all"
        or (row["tool"] in INFERENCE_TOOLS) == (event_kind == "inference")
    )]
    total_rows = len(events)
    total_pages = math.ceil(total_rows / page_size)
    page = min(page, max(total_pages, 1))
    offset = (page - 1) * page_size
    return {
        "filters": {
            "bridge": bridge_filter,
            "principal": principal_filter,
            "period": period,
            "event_status": event_status,
            "event_kind": event_kind,
        },
        "database_availability": availability,
        "metrics": metrics,
        "recent_events": _sanitized_recent(events[offset:offset + page_size]),
        "pagination": {
            "page": page, "page_size": page_size,
            "total_rows": total_rows, "total_pages": total_pages,
            "has_prev": page > 1, "has_next": page < total_pages,
        },
        "token_policy": {
            "history": "null_when_unknown",
            "inference_or_backfill": False,
            "sources": ["claude_cli_result", "gemini_cli_result"],
        },
    }
