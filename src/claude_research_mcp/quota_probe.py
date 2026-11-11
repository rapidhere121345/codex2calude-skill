"""Explicitly enabled Haiku quota collection; status reads only its safe cache.

The collector makes at most one inference attempt per five minutes, including
failed or interrupted attempts. Credentials and response bodies never enter the
cache, logs, command line, or research model-verification path.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
import fcntl
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import tempfile


SOURCE = "anthropic_rate_limit_headers"
PROBE_MODEL = "claude-haiku-4-5-20251001"
INTERVAL_SECONDS = 300
MAX_AGE_SECONDS = 3600
TIMEOUT_SECONDS = 12
MAX_FILE_BYTES = 65_536
CACHE_FILENAME = "haiku-quota-observation.json"
STATE_DIRECTORY = Path("/var/lib/claude-mcp/research/state")
CREDENTIAL_PATH = Path("/var/lib/claude-mcp/.claude/.credentials.json")
MANUAL_CHECK_URL = "https://claude.ai/usage"
REASONS = frozenset({
    "quota_cache_unavailable", "quota_cache_invalid", "quota_cache_expired",
    "quota_windows_expired", "quota_observation_unavailable",
    "oauth_unavailable", "oauth_invalid", "oauth_expired",
    "probe_result_unknown", "probe_timeout", "probe_network_error",
    "probe_auth_rejected", "probe_rate_limited", "probe_http_error",
    "probe_headers_missing", "probe_headers_invalid",
})
_ROOT_KEYS = frozenset({
    "schema_version", "source", "probe_model", "last_attempt_at",
    "last_attempt_status", "observed_at", "windows",
})
_WINDOWS = {"five_hour": "5h", "seven_day": "7d"}
_NUMBER = re.compile(r"^(?:0(?:\.\d+)?|1(?:\.0+)?)$")
_EPOCH = re.compile(r"^[1-9][0-9]{0,11}$")


class QuotaError(Exception):
    def __init__(self, reason: str) -> None:
        if reason not in REASONS:
            raise AssertionError("invalid quota reason")
        super().__init__(reason)
        self.reason = reason


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _date(value: object) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("missing timezone")
    return parsed


def _read_private(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_size > MAX_FILE_BYTES
        ):
            raise ValueError("unsafe private file")
        raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("oversize private file")
        return raw


def _credential(path: Path, now: datetime) -> str:
    try:
        raw = _read_private(path)
    except OSError:
        raise QuotaError("oauth_unavailable") from None
    except ValueError:
        raise QuotaError("oauth_invalid") from None
    try:
        value = json.loads(raw)
        oauth = value["claudeAiOauth"]
        token = oauth["accessToken"]
        expiry = oauth["expiresAt"]
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 16_384
            or not token.isascii()
            or any(ord(c) < 33 or ord(c) > 126 for c in token)
            or type(expiry) not in (int, float)
            or not math.isfinite(expiry)
        ):
            raise ValueError("invalid OAuth metadata")
        if expiry / 1000 <= now.timestamp():
            raise QuotaError("oauth_expired")
        return token
    except QuotaError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        raise QuotaError("oauth_invalid") from None


def parse_rate_limit_headers(
    headers: Iterable[tuple[str, str]], *, now: datetime
) -> dict[str, dict[str, int | float]]:
    """Validate only per-window utilization/reset headers, never guess zeros."""

    wanted = {
        f"anthropic-ratelimit-unified-{suffix}-{field}"
        for suffix in _WINDOWS.values()
        for field in ("utilization", "reset")
    }
    values: dict[str, str] = {}
    for name, value in headers:
        key = name.lower()
        if key not in wanted:
            continue
        if key in values or not isinstance(value, str) or len(value) > 128:
            raise QuotaError("probe_headers_invalid")
        values[key] = value.strip()
    result: dict[str, dict[str, int | float]] = {}
    for window, suffix in _WINDOWS.items():
        prefix = f"anthropic-ratelimit-unified-{suffix}-"
        used, reset = values.get(prefix + "utilization"), values.get(prefix + "reset")
        if used is None and reset is None:
            continue
        if used is None or reset is None or not _NUMBER.fullmatch(used) or not _EPOCH.fullmatch(reset):
            raise QuotaError("probe_headers_invalid")
        epoch = int(reset)
        try:
            datetime.fromtimestamp(epoch, timezone.utc)
        except (ValueError, OverflowError, OSError):
            raise QuotaError("probe_headers_invalid") from None
        if epoch <= now.timestamp():
            raise QuotaError("probe_headers_invalid")
        result[window] = {"utilization": float(used), "resets_at": epoch}
    if not result:
        raise QuotaError("probe_headers_missing")
    return result


def request_headers(token: str) -> tuple[int, list[tuple[str, str]]]:
    """One fixed HTTPS POST; no redirects, retries, proxy overrides, or body log."""

    connection = http.client.HTTPSConnection(
        "api.anthropic.com", timeout=TIMEOUT_SECONDS, context=ssl.create_default_context()
    )
    body = json.dumps({
        "model": PROBE_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }, separators=(",", ":")).encode("ascii")
    try:
        connection.request("POST", "/v1/messages", body=body, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": "remote-research-quota/1",
        })
        response = connection.getresponse()
        try:
            return response.status, response.getheaders()
        finally:
            response.close()
    except (TimeoutError, socket.timeout):
        raise QuotaError("probe_timeout") from None
    except (OSError, http.client.HTTPException):
        raise QuotaError("probe_network_error") from None
    finally:
        connection.close()


def _empty() -> dict:
    return {
        "schema_version": 1, "source": SOURCE, "probe_model": PROBE_MODEL,
        "last_attempt_at": None, "last_attempt_status": "quota_observation_unavailable",
        "observed_at": None, "windows": {},
    }


def _validate_state(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != _ROOT_KEYS:
        raise ValueError("invalid quota state")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("invalid quota schema")
    if value["source"] != SOURCE or value["probe_model"] != PROBE_MODEL:
        raise ValueError("invalid quota source")
    if value["last_attempt_status"] not in REASONS | {"ok"}:
        raise ValueError("invalid quota status")
    if value["last_attempt_at"] is not None:
        _date(value["last_attempt_at"])
    if value["observed_at"] is not None:
        observed = _date(value["observed_at"])
        if value["last_attempt_at"] is None or observed > _date(value["last_attempt_at"]):
            raise ValueError("invalid observation order")
    windows = value["windows"]
    if not isinstance(windows, dict) or set(windows) - _WINDOWS.keys():
        raise ValueError("invalid quota windows")
    if bool(windows) != (value["observed_at"] is not None):
        raise ValueError("missing quota observation")
    for window in windows.values():
        if not isinstance(window, dict) or set(window) != {"utilization", "resets_at"}:
            raise ValueError("invalid quota window")
        used, reset = window["utilization"], window["resets_at"]
        if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 1:
            raise ValueError("invalid quota utilization")
        if type(reset) is not int or reset <= 0:
            raise ValueError("invalid quota reset")
        datetime.fromtimestamp(reset, timezone.utc)
    return value


def _load(path: Path) -> dict:
    try:
        return _validate_state(json.loads(_read_private(path)))
    except FileNotFoundError:
        raise QuotaError("quota_cache_unavailable") from None
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        raise QuotaError("quota_cache_invalid") from None


def _write(path: Path, value: dict) -> None:
    _validate_state(value)
    descriptor, temporary = tempfile.mkstemp(prefix=".haiku-quota.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            json.dump(value, stream, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_quota_cache(path: Path, *, now: datetime | None = None) -> dict:
    """No credential access, subprocess, lock acquisition, or network operation."""

    current = now or utcnow()
    base: dict = {
        "available": False, "source": SOURCE, "manual_check_url": MANUAL_CHECK_URL,
    }
    try:
        state = _load(path)
        attempt = state["last_attempt_at"]
        if attempt is not None and _date(attempt) > current:
            raise QuotaError("quota_cache_invalid")
        observed = state["observed_at"]
        reason = state["last_attempt_status"]
        if observed is None:
            return {**base, "reason": reason if reason != "ok" else "quota_observation_unavailable"}
        age = int((current - _date(observed)).total_seconds())
        if age < 0:
            raise QuotaError("quota_cache_invalid")
        base.update(observed_at=observed, age_seconds=age, stale=True)
        if age > MAX_AGE_SECONDS:
            return {**base, "reason": "quota_cache_expired"}
        windows = {}
        for name, raw in state["windows"].items():
            if raw["resets_at"] <= current.timestamp():
                continue
            used = round(raw["utilization"] * 100, 2)
            windows[name] = {
                "used_percent": used, "remaining_percent": round(100 - used, 2),
                "resets_at": _iso(datetime.fromtimestamp(raw["resets_at"], timezone.utc)),
            }
        if not windows:
            return {**base, "reason": "quota_windows_expired"}
        base.update(available=True, stale=age >= INTERVAL_SECONDS or reason != "ok", **windows)
        if reason != "ok":
            base["reason"] = reason
        return base
    except QuotaError as exc:
        return {**base, "reason": exc.reason}


def collect_once(
    state_dir: Path = STATE_DIRECTORY,
    credential_path: Path = CREDENTIAL_PATH,
    *,
    clock: Callable[[], datetime] = utcnow,
    request: Callable[[str], tuple[int, list[tuple[str, str]]]] = request_headers,
) -> dict:
    """Account-wide lock and persisted attempt time bound even unknown outcomes."""

    metadata = state_dir.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise ValueError("unsafe quota state directory")
    cache = state_dir / CACHE_FILENAME
    descriptor = os.open(
        state_dir / "haiku-quota.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600,
    )
    with os.fdopen(descriptor, "r+") as lock:
        metadata = os.fstat(lock.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise ValueError("unsafe quota lock")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = clock()
        try:
            state = _load(cache)
        except QuotaError:
            state = _empty()
        attempt = state["last_attempt_at"]
        if attempt is not None and (current - _date(attempt)).total_seconds() < INTERVAL_SECONDS:
            return {"attempted": False, "quota": read_quota_cache(cache, now=current)}
        # Persist before the POST: process death or a lost response is not a
        # reason for another worker to replay a possibly completed inference.
        state["last_attempt_at"] = _iso(current)
        state["last_attempt_status"] = "probe_result_unknown"
        _write(cache, state)
        attempted = False
        try:
            token = _credential(credential_path, current)
            attempted = True
            status, headers = request(token)
            if status in (401, 403):
                raise QuotaError("probe_auth_rejected")
            if status not in (200, 429):
                raise QuotaError("probe_http_error")
            completed = clock()
            try:
                windows = parse_rate_limit_headers(headers, now=completed)
            except QuotaError as exc:
                if status == 429 and exc.reason == "probe_headers_missing":
                    raise QuotaError("probe_rate_limited") from None
                raise
            state.update(
                observed_at=_iso(completed), windows=windows,
                last_attempt_at=_iso(completed), last_attempt_status="ok",
            )
        except QuotaError as exc:
            state["last_attempt_status"] = exc.reason
        _write(cache, state)
        return {"attempted": attempted, "quota": read_quota_cache(cache, now=clock())}


def main() -> None:
    try:
        result = collect_once()
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    except Exception:
        # No exception text: HTTP libraries and filesystem exceptions may carry
        # private paths or request details.
        print(json.dumps({"attempted": False, "reason": "collector_failed"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
