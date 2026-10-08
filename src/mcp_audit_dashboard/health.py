from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path


MAX_SNAPSHOT_BYTES = 1_048_576
_QUOTA_KEYS = frozenset(
    (
        "available",
        "reason",
        "source",
        "observed_at",
        "stale",
        "staleness_seconds",
        "age_seconds",
        "used_percent",
        "remaining_percent",
        "utilization",
        "resets_at",
        "reset_at",
        "five_hour",
        "seven_day",
        "manual_check_url",
    )
)


def _scalar(value: object) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str) and len(value) <= 240:
        return value
    return None


def _quota(value: object, depth: int = 0) -> object:
    if not isinstance(value, dict) or depth > 3:
        return {}
    result = {}
    for key, item in value.items():
        if key not in _QUOTA_KEYS:
            continue
        result[key] = _quota(item, depth + 1) if isinstance(item, dict) else _scalar(item)
    return result


def _object(value: object, allowed: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {
        key: _scalar(item) for key, item in value.items() if key in allowed
    }


def _bridge(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    result = _object(
        value,
        {"probe_ok", "account_ready", "checked_at", "latency_ms", "error_category"},
    )
    account = value.get("account")
    if isinstance(account, dict):
        result["account"] = _object(
            account,
            {
                "logged_in",
                "status",
                "default_model",
                "model_selection_ready",
                "cli_version",
                "claude_version",
                "reasoning_effort",
            },
        )
        availability = account.get("availability")
        if isinstance(availability, dict):
            result["account"]["availability"] = _object(
                availability, {"status", "eligible"}
            )
        if "quota" in account:
            result["account"]["quota"] = _quota(account.get("quota"))
        models = account.get("models")
        if isinstance(models, dict):
            result["account"]["models"] = {
                name: _object(
                    item,
                    {"cli_supported", "selection_ready", "reasoning_effort"},
                )
                for name, item in models.items()
                if name in ("opus", "pro") and isinstance(item, dict)
            }
    transport = value.get("transport")
    if isinstance(transport, dict):
        result["transport"] = _object(
            transport,
            {
                "probe_kind",
                "initialize_ok",
                "tools_count",
                "server_version",
                "ssh_banner_ok",
            },
        )
    return result


def _system(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    result = _object(
        value,
        {
            "memory_total_bytes",
            "memory_available_bytes",
            "swap_total_bytes",
            "swap_used_bytes",
            "idle_timeout_seconds",
        },
    )
    workers = value.get("workers")
    if isinstance(workers, dict):
        result["workers"] = _object(workers, {"claude", "gemini"})
    return result


def _snapshot_age(value: object, now: datetime) -> tuple[str, int | None]:
    if not isinstance(value, str):
        return "unknown", None
    try:
        observed = datetime.fromisoformat(value)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        age = max(0, int((now - observed).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return "unknown", None
    return ("fresh" if age <= 1200 else "warning" if age <= 1800 else "stale"), age


def read_health_snapshot(path: Path, *, now: datetime | None = None) -> dict[str, object]:
    current = now or datetime.now(timezone.utc)
    if not path.is_file() or path.stat().st_size > MAX_SNAPSHOT_BYTES:
        return {
            "status": "unknown",
            "reason": "snapshot_absent",
            "source": "cached_file_only",
            "bridges": {},
            "system": {},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {
            "status": "unknown",
            "reason": "snapshot_invalid",
            "source": "cached_file_only",
            "bridges": {},
            "system": {},
        }
    if not isinstance(payload, dict):
        return {
            "status": "unknown",
            "reason": "snapshot_invalid",
            "source": "cached_file_only",
            "bridges": {},
            "system": {},
        }
    status, age = _snapshot_age(payload.get("observed_at"), current)
    return {
        "observed_at": payload.get("observed_at") if isinstance(payload.get("observed_at"), str) else None,
        "staleness": status,
        "age_seconds": age,
        "source": "cached_file_only",
        "bridges": {
            name: _bridge(bridge)
            for name, bridge in (
                payload.get("bridges", {}).items()
                if isinstance(payload.get("bridges"), dict)
                else []
            )
            if name in ("claude", "gemini")
        },
        "system": _system(
            payload.get("system"),
        ),
        "status": status if status != "unknown" else "unknown",
        **({"reason": "observed_at_invalid"} if status == "unknown" else {}),
    }
