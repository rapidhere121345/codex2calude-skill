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
_PUBLIC_MODELS = frozenset(
    ("opus-5-5-high", "fable-5-2-high", "gemini-3-8-flash-high")
)
_EFFORTS = frozenset(("low", "medium", "high", "xhigh", "max"))
_QUOTA_SOURCES = frozenset(
    ("claude_code_statusline", "claude_code_usage_command", "antigravity_cli_usage")
)
_QUOTA_REASONS = frozenset(
    (
        "unknown", "requires_claude_ai_login", "statusline_observation_unavailable",
        "statusline_observation_expired", "invalid_statusline_observation",
        "usage_command_unavailable", "usage_command_failed",
        "invalid_usage_command_output", "not_reported_by_agy",
    )
)
_PROVIDER_ERRORS = frozenset(
    (
        "auth", "rate_limit", "model_unavailable", "permission", "network",
        "invalid_argument", "runtime", "timeout", "busy", "unknown", "unavailable",
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
        if key in ("five_hour", "seven_day"):
            if isinstance(item, dict):
                result[key] = _quota(item, depth + 1)
        elif key in ("available", "stale"):
            if type(item) is bool:
                result[key] = item
        elif key in ("used_percent", "remaining_percent", "utilization"):
            if type(item) in (int, float) and math.isfinite(item) and 0 <= item <= 100:
                result[key] = item
        elif key in ("age_seconds", "staleness_seconds"):
            if type(item) in (int, float) and math.isfinite(item) and 0 <= item <= 315_360_000:
                result[key] = item
        elif key in ("reason", "source"):
            if isinstance(item, str) and item in (
                _QUOTA_REASONS if key == "reason" else _QUOTA_SOURCES
            ):
                result[key] = item
        elif key == "manual_check_url":
            if item == "https://claude.ai/usage":
                result[key] = item
        elif key in ("observed_at", "resets_at", "reset_at"):
            if isinstance(item, str) and 1 <= len(item) <= 64:
                try:
                    parsed = datetime.fromisoformat(item.replace("Z", "+00:00"))
                    if parsed.tzinfo is not None:
                        result[key] = item
                except (ValueError, OverflowError):
                    pass
    return result


def _object(value: object, allowed: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {
        key: _scalar(item) for key, item in value.items() if key in allowed
    }


def _bridge(value: object, provider: str) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    result = _object(
        value,
        {"probe_ok", "account_ready", "checked_at", "latency_ms",
         "error_category", "provider_error_category"},
    )
    if not isinstance(result.get("provider_error_category"), str) or (
        result["provider_error_category"] not in _PROVIDER_ERRORS
    ):
        result.pop("provider_error_category", None)
    if result.get("error_category") not in (
        "TimeoutError", "EOFError", "ValueError", "KeyError",
        "BrokenPipeError", "OSError", "TypeError", "JSONDecodeError",
    ):
        result.pop("error_category", None)
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
                "service_available",
                "service_unavailable_reason",
                "service_available_reason",
            },
        )
        clean = result["account"]
        if not isinstance(clean.get("default_model"), str) or clean["default_model"] not in _PUBLIC_MODELS:
            clean.pop("default_model", None)
        if not isinstance(clean.get("reasoning_effort"), str) or clean["reasoning_effort"] not in _EFFORTS:
            clean.pop("reasoning_effort", None)
        if clean.get("status") not in ("ok", "error", "busy", "unknown", "unavailable"):
            clean.pop("status", None)
        for key in ("logged_in", "model_selection_ready", "service_available"):
            if key in clean and type(clean[key]) is not bool and clean[key] is not None:
                clean.pop(key, None)
        if clean.get("service_unavailable_reason") not in (
            "requires_claude_ai_login", "recent_api_auth_failure"
        ):
            clean.pop("service_unavailable_reason", None)
        if clean.get("service_available_reason") != "auth_status_does_not_probe_model_api":
            clean.pop("service_available_reason", None)
        for key in ("cli_version", "claude_version"):
            version = clean.get(key)
            if not isinstance(version, str) or not version or len(version) > 80 or not version[0].isdigit() or not all(
                c.isalnum() or c in " .()+-" for c in version
            ):
                clean.pop(key, None)
        availability = account.get("availability")
        if isinstance(availability, dict):
            clean["availability"] = _object(
                availability, {"status", "eligible"}
            )
            if clean["availability"].get("status") not in (
                "eligible", "unsupported_location", "unknown", "unavailable",
                "not_authenticated", "age_restricted", "ineligible"
            ):
                clean["availability"].pop("status", None)
            if type(clean["availability"].get("eligible")) is not bool:
                clean["availability"].pop("eligible", None)
        if "quota" in account:
            result["account"]["quota"] = _quota(account.get("quota"))
        if provider == "claude" and clean.get("logged_in") is False:
            clean["service_available"] = False
            clean["service_unavailable_reason"] = "requires_claude_ai_login"
            clean.pop("service_available_reason", None)
            clean["quota"] = {
                "available": False,
                "reason": "requires_claude_ai_login",
                "manual_check_url": "https://claude.ai/usage",
            }
        models = account.get("models")
        if isinstance(models, dict):
            clean["models"] = {
                name: {
                    **{
                        key: item[key]
                        for key in ("enabled", "verified", "cli_supported", "selection_ready")
                        if type(item.get(key)) is bool
                    },
                    **(
                        {"reasoning_effort": item["reasoning_effort"]}
                        if isinstance(item.get("reasoning_effort"), str)
                        and item["reasoning_effort"] in _EFFORTS
                        else {}
                    ),
                }
                for name, item in models.items()
                if name in _PUBLIC_MODELS
                and (provider == "gemini") == (name == "gemini-3-8-flash-high")
                and isinstance(item, dict)
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
        transport_result = result["transport"]
        if transport_result.get("probe_kind") not in (
            "local_unified_forced_command", "local_forced_command"
        ):
            transport_result.pop("probe_kind", None)
        version = transport_result.get("server_version")
        if not isinstance(version, str) or not version or not version.isascii() or not version[0].isdigit() or len(version) > 80 or not all(
            c.isalnum() or c in ".+-" for c in version
        ):
            transport_result.pop("server_version", None)
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
            name: _bridge(bridge, name)
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
