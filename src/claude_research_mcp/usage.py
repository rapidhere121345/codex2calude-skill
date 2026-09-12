"""Safe Claude Code OAuth plan-usage status.

This module deliberately returns only sanitized enums and quota fields.  OAuth
credentials and raw endpoint responses never leave this module or enter its
cache.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import ssl
import stat
import tempfile
from urllib.error import HTTPError, URLError
import urllib.request


USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
USAGE_SOURCE = "claude_code_oauth_usage"
USER_AGENT = "claude-research-mcp/0.1"
ANTHROPIC_OAUTH_BETA = "oauth-2025-04-20"
FRESH_TTL_SECONDS = 120
STALE_TTL_SECONDS = 3_600
MAX_CREDENTIAL_BYTES = 16_384
MAX_CACHE_BYTES = 16_384
MAX_RESPONSE_BYTES = 65_536
REQUEST_TIMEOUT_SECONDS = 10
MIN_TOKEN_LENGTH = 32
MAX_TOKEN_LENGTH = 8_192

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WINDOW_NAMES = ("five_hour", "seven_day", "seven_day_opus")
_AVAILABLE_KEYS = frozenset(
    {
        "available",
        "source",
        "fetched_at",
        "cached",
        "stale",
        "cache_age_seconds",
        "five_hour",
        "seven_day",
        "seven_day_opus",
    }
)


class UsageError(Exception):
    """A quota failure carrying only a public-safe reason enum."""

    def __init__(self, reason: str) -> None:
        if reason not in _SAFE_REASONS:
            raise AssertionError("unsafe usage error reason")
        super().__init__(reason)
        self.reason = reason


_SAFE_REASONS = frozenset(
    {
        "credentials_unavailable",
        "authentication_unavailable",
        "rate_limited",
        "usage_endpoint_unavailable",
        "invalid_usage_response",
    }
)


def _unavailable(reason: str) -> dict[str, object]:
    return {"available": False, "source": USAGE_SOURCE, "reason": reason}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError("invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp has no timezone")
    return parsed


def _parse_window(raw: object, *, exact: bool) -> dict[str, object] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("invalid usage window")
    if exact and set(raw) != {"used_percent", "remaining_percent", "resets_at"}:
        raise ValueError("invalid cached window")
    used = raw.get("utilization" if not exact else "used_percent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        raise ValueError("invalid utilization")
    used_float = float(used)
    if not math.isfinite(used_float) or not 0.0 <= used_float <= 100.0:
        raise ValueError("invalid utilization")
    resets_at = _parse_timestamp(raw.get("resets_at")).isoformat()
    normalized_used = round(used_float, 2)
    return {
        "used_percent": normalized_used,
        "remaining_percent": round(100.0 - normalized_used, 2),
        "resets_at": resets_at,
    }


def _parse_live_payload(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError("invalid usage response")
    windows: dict[str, object] = {}
    for name in _WINDOW_NAMES:
        try:
            window = _parse_window(raw.get(name), exact=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid usage response") from exc
        if window is None and name != "seven_day_opus":
            raise ValueError("invalid usage response")
        windows[name] = window
    return windows


def _validate_contract_shape(
    value: object,
) -> tuple[dict[str, object], datetime]:
    if not isinstance(value, dict) or set(value) != _AVAILABLE_KEYS:
        raise ValueError("invalid cache")
    if value.get("available") is not True or value.get("source") != USAGE_SOURCE:
        raise ValueError("invalid cache")
    if value.get("cached") is not False or value.get("stale") is not False:
        raise ValueError("invalid cache")
    age = value.get("cache_age_seconds")
    if age != 0:
        raise ValueError("invalid cache")
    fetched_at = _parse_timestamp(value.get("fetched_at"))
    windows = {}
    for name in _WINDOW_NAMES:
        window = _parse_window(value.get(name), exact=True)
        if window is None and name != "seven_day_opus":
            raise ValueError("invalid cache")
        windows[name] = window
    normalized = {
        "available": True,
        "source": USAGE_SOURCE,
        "fetched_at": value["fetched_at"],
        "cached": value["cached"],
        "stale": value["stale"],
        "cache_age_seconds": age,
        **windows,
    }
    return normalized, fetched_at


def _available(
    windows: dict[str, object], *, fetched_at: str, cached: bool, stale: bool, age: int
) -> dict[str, object]:
    return {
        "available": True,
        "source": USAGE_SOURCE,
        "fetched_at": fetched_at,
        "cached": cached,
        "stale": stale,
        "cache_age_seconds": age,
        **windows,
    }


def _read_bounded_regular_file(
    path: Path, maximum: int, *, no_group_other: bool
) -> bytes:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise OSError("not regular")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except (OSError, ValueError):
        raise UsageError("credentials_unavailable") from None
    try:
        with os.fdopen(descriptor, "rb") as source:
            file_stat = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.geteuid()
                or file_stat.st_size > maximum
                or (no_group_other and file_stat.st_mode & 0o077)
            ):
                raise OSError("unsafe file")
            return source.read(maximum + 1)
    except UsageError:
        raise
    except (OSError, ValueError):
        raise UsageError("credentials_unavailable") from None


def _read_access_token(claude_home: Path) -> str:
    raw = _read_bounded_regular_file(
        claude_home / ".credentials.json",
        MAX_CREDENTIAL_BYTES,
        no_group_other=True,
    )
    if len(raw) > MAX_CREDENTIAL_BYTES:
        raise UsageError("credentials_unavailable")
    try:
        payload = json.loads(raw.decode("utf-8"))
        oauth = payload["claudeAiOauth"]
        token = oauth["accessToken"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        raise UsageError("credentials_unavailable") from None
    if (
        not isinstance(payload, dict)
        or not isinstance(oauth, dict)
        or not isinstance(token, str)
        or not MIN_TOKEN_LENGTH <= len(token) <= MAX_TOKEN_LENGTH
        or _CONTROL_RE.search(token)
    ):
        raise UsageError("credentials_unavailable")
    return token


def _http_opener() -> urllib.request.OpenerDirector:
    class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    context = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        NoRedirectHandler(),
    )


def _request_usage(token: str, opener: urllib.request.OpenerDirector | None = None) -> object:
    request = urllib.request.Request(
        USAGE_ENDPOINT,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "anthropic-beta": ANTHROPIC_OAUTH_BETA,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        response = (opener or _http_opener()).open(
            request, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise UsageError("authentication_unavailable") from None
        if exc.code == 429:
            raise UsageError("rate_limited") from None
        raise UsageError("usage_endpoint_unavailable") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise UsageError("usage_endpoint_unavailable") from None
    try:
        with response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, ValueError):
        raise UsageError("usage_endpoint_unavailable") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise UsageError("invalid_usage_response")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise UsageError("invalid_usage_response") from None


def _fetch_windows(claude_home: Path) -> dict[str, object]:
    token = _read_access_token(claude_home)
    try:
        return _parse_live_payload(_request_usage(token))
    except UsageError:
        raise
    except (TypeError, ValueError):
        raise UsageError("invalid_usage_response") from None


def _load_cache(path: Path, now: datetime) -> tuple[dict[str, object], int] | None:
    try:
        raw = _read_bounded_regular_file(
            path, MAX_CACHE_BYTES, no_group_other=True
        )
    except UsageError:
        return None
    if len(raw) > MAX_CACHE_BYTES:
        return None
    try:
        payload, fetched_at = _validate_contract_shape(json.loads(raw.decode("utf-8")))
        age = int((now - fetched_at).total_seconds())
        if age < 0:
            return None
        payload["cached"] = True
        payload["stale"] = False
        payload["cache_age_seconds"] = age
        return payload, age
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        OverflowError,
    ):
        return None


def _write_cache(path: Path, payload: dict[str, object]) -> None:
    parent = path.parent
    try:
        parent_stat = parent.lstat()
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent.is_symlink()
            or parent_stat.st_uid != os.geteuid()
            or parent_stat.st_mode & 0o077
        ):
            return
    except OSError:
        return
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    if len(encoded) > MAX_CACHE_BYTES:
        return
    temporary: str | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".opus-quota-cache.", dir=parent
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            descriptor = None
            target.write(encoded)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def get_opus_quota_status(claude_home: Path, cache_path: Path) -> dict[str, object]:
    """Return sanitized live/cached quota status; never raise to callers."""
    now = _now()
    cached = _load_cache(cache_path, now)
    if cached is not None:
        payload, age = cached
        if age <= FRESH_TTL_SECONDS:
            return payload
    try:
        windows = _fetch_windows(claude_home)
    except UsageError as exc:
        reason = exc.reason
        if cached is not None and FRESH_TTL_SECONDS < cached[1] <= STALE_TTL_SECONDS:
            payload = dict(cached[0])
            payload["stale"] = True
            payload["refresh_error"] = reason
            return payload
        return _unavailable(reason)
    except Exception:
        # Defensive fail-closed boundary: no implementation detail can escape.
        return _unavailable("usage_endpoint_unavailable")
    fetched_at = _now().isoformat(timespec="seconds")
    payload = _available(
        windows,
        fetched_at=fetched_at,
        cached=False,
        stale=False,
        age=0,
    )
    _write_cache(cache_path, payload)
    return payload


__all__ = ["USAGE_ENDPOINT", "UsageError", "get_opus_quota_status"]
