#!/usr/bin/env python3
"""Persist only Claude Code's documented statusline rate-limit fields.

Claude Code sends a larger statusline JSON document on stdin.  This helper
never logs or stores that document; it writes only the two subscription windows
that the bridge can safely expose through ``claude_status``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile


SOURCE = "claude_code_statusline"
OUTPUT_ENV = "CLAUDE_MCP_STATUSLINE_OBSERVATION"
MAX_INPUT_BYTES = 262_144
MAX_OUTPUT_BYTES = 4_096
WINDOWS = ("five_hour", "seven_day")


def _window(value: object, *, now_epoch: int) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid window")
    used = value.get("used_percentage")
    resets_at = value.get("resets_at")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        raise ValueError("invalid used percentage")
    used_float = float(used)
    if not math.isfinite(used_float) or not 0.0 <= used_float <= 100.0:
        raise ValueError("invalid used percentage")
    if isinstance(resets_at, bool) or not isinstance(resets_at, int):
        raise ValueError("invalid reset timestamp")
    if resets_at <= now_epoch:
        return None
    return {
        "used_percentage": round(used_float, 2),
        "resets_at": resets_at,
    }


def sanitize_statusline_payload(
    value: object, *, observed_at: datetime | None = None
) -> dict[str, object] | None:
    """Return the bounded allowlisted observation, or ``None`` if unavailable."""

    if not isinstance(value, dict):
        return None
    limits = value.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    current = observed_at or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")
    windows = {
        name: _window(limits.get(name), now_epoch=int(current.timestamp()))
        for name in WINDOWS
    }
    if all(window is None for window in windows.values()):
        return None
    return {
        "source": SOURCE,
        "observed_at": current.astimezone(timezone.utc).isoformat(timespec="seconds"),
        **windows,
    }


def _safe_output_path(raw: str | None) -> Path:
    if not raw or "\x00" in raw:
        raise ValueError("missing output path")
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("unsafe output path")
    parent = path.parent
    parent_stat = parent.lstat()
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent.is_symlink()
        or parent_stat.st_uid != os.geteuid()
        or parent_stat.st_mode & 0o077
    ):
        raise ValueError("unsafe output directory")
    if path.exists():
        target_stat = path.lstat()
        if (
            not stat.S_ISREG(target_stat.st_mode)
            or target_stat.st_uid != os.geteuid()
            or target_stat.st_mode & 0o077
        ):
            raise ValueError("unsafe output file")
    return path


def write_observation(path: Path, payload: dict[str, object]) -> None:
    encoded = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise ValueError("observation is too large")
    descriptor: int | None = None
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".statusline-observation.", dir=path.parent
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def main() -> int:
    """Fail silently so a malformed statusline can never break Claude Code."""

    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            return 0
        payload = json.loads(raw.decode("utf-8"))
        sanitized = sanitize_statusline_payload(payload)
        if sanitized is None:
            return 0
        write_observation(_safe_output_path(os.environ.get(OUTPUT_ENV)), sanitized)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
