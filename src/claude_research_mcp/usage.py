"""Sanitized quota observations from documented Claude Code surfaces.

The bridge never reads OAuth credentials and never calls an undocumented usage
API.  It prefers a sanitized statusline observation and can fall back to the
official interactive ``/usage`` screen.  Both paths retain only allowlisted
quota fields and fail closed when data cannot be validated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile


USAGE_SOURCE = "claude_code_statusline"
USAGE_COMMAND_SOURCE = "claude_code_usage_command"
USAGE_CHECK_URL = "https://claude.ai/usage"
MAX_SAMPLE_BYTES = 16_384
MAX_SAMPLE_AGE_SECONDS = 3_600
_WINDOW_NAMES = ("five_hour", "seven_day")
USAGE_CACHE_FILENAME = "usage-command-observation.json"
USAGE_CACHE_TTL_SECONDS = 60
_UNAVAILABLE_REASONS = frozenset(
    {
        "requires_claude_ai_login",
        "statusline_observation_unavailable",
        "statusline_observation_expired",
        "invalid_statusline_observation",
        "usage_command_unavailable",
        "usage_command_failed",
        "invalid_usage_command_output",
    }
)
_SAMPLE_KEYS = frozenset({"source", "observed_at", "five_hour", "seven_day"})
_WINDOW_KEYS = frozenset({"used_percentage", "resets_at"})
_USAGE_TOTAL_LIMIT = 131_072
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_RESET_TIME_RE = re.compile(
    r"^(1[0-2]|0?[1-9])(?::([0-5][0-9]))?\s*(am|pm)$", re.IGNORECASE
)
_RESET_DATE_RE = re.compile(r"^([A-Za-z]{3,9})\s+([1-9]|[12][0-9]|3[01])$")
_USAGE_SECTION_RE = re.compile(
    r"Current\s+(?P<label>session|week\s+\(all\s+models\))(?P<body>.*?)"
    r"(?=Current\s+(?:session|week\s+\(all\s+models\))|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_USAGE_VALUE_RE = re.compile(
    r"(?P<used>100|\d{1,2}(?:\.\d+)?)%\s+used\s+Resets\s+"
    r"(?P<reset>(?:[A-Za-z]{3,9}\s+(?:[1-9]|[12]\d|3[01]),\s*)?"
    r"(?:1[0-2]|0?[1-9])(?::[0-5]\d)?\s*(?:am|pm))\s+\(UTC\)",
    re.IGNORECASE,
)
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_USAGE_FAILURE_RE = re.compile(r"Failed\s+to\s+load\s+usage\s+data", re.IGNORECASE)


class UsageError(Exception):
    """A quota failure carrying only a public-safe reason enum."""

    def __init__(self, reason: str) -> None:
        if reason not in _UNAVAILABLE_REASONS:
            raise AssertionError("unsafe usage error reason")
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ParsedUsageCommand:
    """Only the allowlisted data extracted from the official /usage screen."""

    observed_at: datetime
    five_hour: tuple[float, datetime]
    seven_day: tuple[float, datetime]


def _unavailable(reason: str) -> dict[str, object]:
    source = (
        USAGE_SOURCE
        if reason.startswith("statusline_")
        or reason in {"requires_claude_ai_login", "invalid_statusline_observation"}
        else USAGE_COMMAND_SOURCE
    )
    return {
        "available": False,
        "source": source,
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


def _parse_reset_time(value: str, *, now: datetime, timezone_name: str | None) -> datetime:
    """Parse the two reset formats observed in Claude Code 2.1.236 /usage."""

    zone = _timezone(timezone_name)
    local_now = now.astimezone(zone)
    normalized = " ".join(value.split())
    date_part: str
    time_part: str
    if " " in normalized:
        date_part, time_part = normalized.split(",", 1)
        date_part = date_part.strip()
        time_part = time_part.strip()
    else:
        date_part, time_part = "", normalized
    clock = _RESET_TIME_RE.fullmatch(time_part)
    if clock is None:
        raise ValueError("invalid usage reset time")
    hour = int(clock.group(1)) % 12
    if clock.group(3).lower() == "pm":
        hour += 12
    reset_time = datetime.min.time().replace(
        hour=hour, minute=int(clock.group(2) or 0)
    )
    if date_part:
        date = _RESET_DATE_RE.fullmatch(date_part)
        if date is None:
            raise ValueError("invalid usage reset date")
        month = _MONTHS.get(date.group(1)[:3].lower())
        if month is None:
            raise ValueError("invalid usage reset month")
        candidate = datetime.combine(
            datetime(local_now.year, month, int(date.group(2))).date(), reset_time, zone
        )
        if candidate <= local_now:
            candidate = candidate.replace(year=candidate.year + 1)
        return candidate.astimezone(timezone.utc)
    candidate = datetime.combine(local_now.date(), reset_time, zone)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _timezone(value: str | None) -> timezone:
    # The server deployment has a fixed UTC clock.  A named-zone override is
    # nevertheless accepted so deterministic tests can exercise local rendering.
    if value in (None, "UTC", "Etc/UTC", "Z"):
        return timezone.utc
    match = re.fullmatch(r"UTC([+-])(0[0-9]|1[0-4]):?(00|15|30|45)", value)
    if match is None:
        raise ValueError("unsupported usage timezone")
    sign = 1 if match.group(1) == "+" else -1
    hours, minutes = int(match.group(2)), int(match.group(3))
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def parse_usage_command_output(
    raw: str | bytes,
    *,
    now: datetime | None = None,
    timezone_name: str | None = "UTC",
) -> ParsedUsageCommand:
    """Extract only the two official quota windows from /usage output."""

    current = now or _now()
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UsageError("invalid_usage_command_output") from exc
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _USAGE_TOTAL_LIMIT:
        raise UsageError("invalid_usage_command_output")
    visible = strip_ansi(raw)
    if _USAGE_FAILURE_RE.search(visible) is not None:
        raise UsageError("usage_command_failed")
    windows: dict[str, tuple[float, datetime]] = {}
    for section in _USAGE_SECTION_RE.finditer(visible):
        match = _USAGE_VALUE_RE.search(section.group("body"))
        if match is None:
            continue
        label = (
            "seven_day"
            if "week" in section.group("label").casefold()
            else "five_hour"
        )
        try:
            used = float(match.group("used"))
            if not math.isfinite(used) or not 0.0 <= used <= 100.0:
                raise ValueError("invalid used percentage")
            reset = _parse_reset_time(
                match.group("reset"), now=current, timezone_name=timezone_name
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise UsageError("invalid_usage_command_output") from exc
        if reset <= current:
            raise UsageError("invalid_usage_command_output")
        windows[label] = (used, reset)
    if set(windows) != {"five_hour", "seven_day"}:
        raise UsageError("invalid_usage_command_output")
    return ParsedUsageCommand(
        observed_at=current,
        five_hour=windows["five_hour"],
        seven_day=windows["seven_day"],
    )


def strip_ansi(raw: str) -> str:
    """Remove OSC/CSI sequences, decorations, and collapse all whitespace."""

    visible = _ANSI_OSC_RE.sub(" ", raw)
    visible = _ANSI_CSI_RE.sub(" ", visible)
    visible = "".join(
        character
        for character in visible
        if character not in {"\u2500", "\u2502", "\u250c", "\u2514", "\u2518", "\u2510"}
    )
    return " ".join(visible.split())


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
    value: object, *, now: datetime, source: str = USAGE_SOURCE
) -> tuple[dict[str, object], datetime]:
    if not isinstance(value, dict) or set(value) != _SAMPLE_KEYS:
        raise ValueError("invalid sample")
    if value.get("source") != source:
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
        "source": source,
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


def _usage_command_payload(
    value: object, *, now: datetime
) -> tuple[dict[str, object], datetime]:
    return _validate_sample(value, now=now, source=USAGE_COMMAND_SOURCE)


def read_usage_command_cache(
    cache_path: Path, *, now: datetime | None = None
) -> dict[str, object] | None:
    """Read one recent sanitized /usage sample, rejecting unsafe files."""

    current = now or _now()
    try:
        raw = _read_bounded_regular_file(cache_path)
        text = raw.decode("utf-8")
        payload = json.loads(text)
        normalized, _ = _usage_command_payload(payload, now=current)
        return normalized
    except (UsageError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError):
        return None


def write_usage_command_cache(
    cache_path: Path,
    parsed: ParsedUsageCommand,
    *,
    observed_at: datetime | None = None,
) -> Path:
    """Atomically persist only the normalized weekly /usage observation."""

    current = observed_at or parsed.observed_at
    five_reset = int(parsed.five_hour[1].timestamp())
    seven_reset = int(parsed.seven_day[1].timestamp())
    if five_reset <= int(current.timestamp()) or seven_reset <= int(current.timestamp()):
        raise UsageError("invalid_usage_command_output")
    payload = {
        "source": USAGE_COMMAND_SOURCE,
        "observed_at": current.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "five_hour": {
            "used_percentage": round(parsed.five_hour[0], 2),
            "resets_at": five_reset,
        },
        "seven_day": {
            "used_percentage": round(parsed.seven_day[0], 2),
            "resets_at": seven_reset,
        },
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    if len(encoded) > MAX_SAMPLE_BYTES:
        raise UsageError("invalid_usage_command_output")
    parent = cache_path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_stat = parent.lstat()
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.geteuid()
        or parent_stat.st_mode & 0o077
    ):
        raise OSError("unsafe usage-cache directory")
    try:
        current_stat = cache_path.lstat()
    except FileNotFoundError:
        current_stat = None
    if current_stat is not None and (
        not stat.S_ISREG(current_stat.st_mode)
        or current_stat.st_uid != os.geteuid()
        or current_stat.st_mode & 0o077
    ):
        raise OSError("unsafe usage-cache target")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".usage-command-observation.", dir=parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, cache_path)
        temporary_name = None
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
    return cache_path


def usage_command_cache_path(sample_path: Path) -> Path:
    return sample_path.parent / USAGE_CACHE_FILENAME


def get_cached_usage_command_status(
    cache_path: Path, *, now: datetime | None = None
) -> dict[str, object] | None:
    """Return a fresh short-lived cache entry, or None without exposing data."""

    current = now or _now()
    payload = read_usage_command_cache(cache_path, now=current)
    age = (
        payload.get("age_seconds", USAGE_CACHE_TTL_SECONDS + 1)
        if payload
        else USAGE_CACHE_TTL_SECONDS + 1
    )
    if payload is None or age > USAGE_CACHE_TTL_SECONDS:
        return None
    return payload


def usage_command_unavailable(
    reason: str = "usage_command_unavailable",
) -> dict[str, object]:
    return _unavailable(reason)


__all__ = [
    "USAGE_CHECK_URL",
    "USAGE_CACHE_FILENAME",
    "USAGE_CACHE_TTL_SECONDS",
    "USAGE_COMMAND_SOURCE",
    "USAGE_SOURCE",
    "UsageError",
    "get_cached_usage_command_status",
    "get_opus_quota_status",
    "parse_statusline_observation",
    "parse_usage_command_output",
    "read_usage_command_cache",
    "usage_command_cache_path",
    "usage_command_unavailable",
    "write_usage_command_cache",
]
