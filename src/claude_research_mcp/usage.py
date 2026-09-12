"""Sanitized Claude Code quota observations.

The bridge never reads OAuth credentials and never calls an undocumented usage
API.  It only accepts a sanitized observation written by the officially
configured Claude Code statusline integration, then reports that observation
with its source and staleness.  Missing data fails closed as unavailable.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import stat


USAGE_SOURCE = "claude_code_statusline"
USAGE_CHECK_URL = "https://claude.ai/usage"
MAX_SAMPLE_BYTES = 16_384
MAX_SAMPLE_AGE_SECONDS = 3_600
_WINDOW_NAMES = ("five_hour", "seven_day")
_UNAVAILABLE_REASONS = frozenset(
    {
        "requires_claude_ai_login",
        "statusline_observation_unavailable",
        "statusline_observation_expired",
        "invalid_statusline_observation",
    }
)
_SAMPLE_KEYS = frozenset({"source", "observed_at", "five_hour", "seven_day"})
_WINDOW_KEYS = frozenset({"used_percentage", "resets_at"})


class UsageError(Exception):
    """A quota failure carrying only a public-safe reason enum."""

    def __init__(self, reason: str) -> None:
        if reason not in _UNAVAILABLE_REASONS:
            raise AssertionError("unsafe usage error reason")
        super().__init__(reason)
        self.reason = reason


def _unavailable(reason: str) -> dict[str, object]:
    return {
        "available": False,
        "source": USAGE_SOURCE,
        "reason": reason,
        "manual_check_url": USAGE_CHECK_URL,
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_observed_at(value: object) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError("invalid observed_at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid observed_at") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at has no timezone")
    return parsed


def _window(raw: object, *, now: datetime) -> tuple[dict[str, object] | None, bool]:
    if raw is None:
        return None, False
    if not isinstance(raw, dict) or set(raw) != _WINDOW_KEYS:
        raise ValueError("invalid rate-limit window")
    used = raw["used_percentage"]
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        raise ValueError("invalid used_percentage")
    used_float = float(used)
    if not math.isfinite(used_float) or not 0.0 <= used_float <= 100.0:
        raise ValueError("invalid used_percentage")
    resets_at = raw["resets_at"]
    if (
        isinstance(resets_at, bool)
        or not isinstance(resets_at, int)
    ):
        raise ValueError("invalid resets_at")
    if resets_at <= int(now.timestamp()):
        return None, True
    return {
        "used_percent": round(used_float, 2),
        "remaining_percent": round(100.0 - used_float, 2),
        "resets_at": datetime.fromtimestamp(resets_at, timezone.utc).isoformat(),
    }, False


def _validate_sample(
    value: object, *, now: datetime
) -> tuple[dict[str, object], datetime]:
    if not isinstance(value, dict) or set(value) != _SAMPLE_KEYS:
        raise ValueError("invalid sample")
    if value.get("source") != USAGE_SOURCE:
        raise ValueError("invalid sample source")
    observed_at = _parse_observed_at(value.get("observed_at"))
    age = int((now - observed_at).total_seconds())
    if age < 0 or age > MAX_SAMPLE_AGE_SECONDS:
        raise ValueError("sample is stale")
    five_hour, five_expired = _window(value.get("five_hour"), now=now)
    seven_day, seven_expired = _window(value.get("seven_day"), now=now)
    if five_hour is None and seven_day is None:
        if five_expired or seven_expired:
            raise ValueError("all windows expired")
        raise ValueError("all windows are absent")
    normalized = {
        "available": True,
        "source": USAGE_SOURCE,
        "observed_at": observed_at.isoformat(timespec="seconds"),
        "stale": 300 <= age <= MAX_SAMPLE_AGE_SECONDS,
        "age_seconds": age,
        "five_hour": five_hour,
        "seven_day": seven_day,
    }
    return normalized, observed_at


def _read_bounded_regular_file(path: Path) -> bytes:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise OSError("not regular")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise UsageError("statusline_observation_unavailable") from None
    try:
        with os.fdopen(descriptor, "rb") as source:
            file_stat = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.geteuid()
                or file_stat.st_size > MAX_SAMPLE_BYTES
                or file_stat.st_mode & 0o077
            ):
                raise OSError("unsafe file")
            return source.read(MAX_SAMPLE_BYTES + 1)
    except UsageError:
        raise
    except OSError:
        raise UsageError("statusline_observation_unavailable") from None


def parse_statusline_observation(
    raw: str | bytes, *, now: datetime | None = None
) -> dict[str, object]:
    """Parse a sanitized rate_limits observation written by statusline."""

    current = now or _now()
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UsageError("invalid_statusline_observation") from exc
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_SAMPLE_BYTES:
        raise UsageError("invalid_statusline_observation")
    try:
        payload, _ = _validate_sample(json.loads(raw), now=current)
        return payload
    except UsageError:
        raise
    except (
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        OverflowError,
        OSError,
    ) as exc:
        if isinstance(exc, ValueError) and str(exc) in {
            "sample is stale",
            "all windows expired",
        }:
            raise UsageError("statusline_observation_expired") from None
        raise UsageError("invalid_statusline_observation") from None


def get_opus_quota_status(sample_path: Path) -> dict[str, object]:
    """Return the latest official statusline observation, or fail closed."""

    try:
        raw = _read_bounded_regular_file(sample_path)
        return parse_statusline_observation(raw)
    except UsageError as exc:
        return _unavailable(exc.reason)
    except Exception:
        return _unavailable("statusline_observation_unavailable")


__all__ = [
    "USAGE_CHECK_URL",
    "USAGE_SOURCE",
    "UsageError",
    "get_opus_quota_status",
    "parse_statusline_observation",
]
