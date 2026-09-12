from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import typing as t


SCRIPT = (
    Path(__file__).parents[1] / "scripts" / "capture-statusline-rate-limits.py"
)
SPEC = importlib.util.spec_from_file_location("capture_statusline_rate_limits", SCRIPT)
assert SPEC and SPEC.loader
capture = importlib.util.module_from_spec(SPEC)
t.cast(t.Any, SPEC.loader).exec_module(capture)

NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)


def raw_statusline() -> dict[str, object]:
    return {
        "session_id": "must-not-be-persisted",
        "transcript_path": "/private/transcript.jsonl",
        "cwd": "/private/worktree",
        "rate_limits": {
            "five_hour": {
                "used_percentage": 23.5,
                "resets_at": int((NOW + timedelta(hours=1)).timestamp()),
                "future_field": "ignored",
            },
            "seven_day": {
                "used_percentage": 41.2,
                "resets_at": int((NOW + timedelta(days=2)).timestamp()),
            },
            "spend_limit": {
                "used_percentage": 150,
                "resets_at": int((NOW + timedelta(days=30)).timestamp()),
            },
        },
    }


def test_sanitizer_keeps_only_documented_subscription_windows() -> None:
    result = capture.sanitize_statusline_payload(raw_statusline(), observed_at=NOW)

    assert result == {
        "source": "claude_code_statusline",
        "observed_at": NOW.isoformat(timespec="seconds"),
        "five_hour": {
            "used_percentage": 23.5,
            "resets_at": int((NOW + timedelta(hours=1)).timestamp()),
        },
        "seven_day": {
            "used_percentage": 41.2,
            "resets_at": int((NOW + timedelta(days=2)).timestamp()),
        },
    }
    rendered = json.dumps(result)
    for forbidden in ("session_id", "transcript", "cwd", "/private", "spend_limit"):
        assert forbidden not in rendered


def test_sanitizer_accepts_one_window_and_rejects_no_live_windows() -> None:
    payload = raw_statusline()
    payload["rate_limits"]["seven_day"] = None  # type: ignore[index]
    result = capture.sanitize_statusline_payload(payload, observed_at=NOW)
    assert result is not None
    assert result["five_hour"] is not None
    assert result["seven_day"] is None

    payload["rate_limits"]["five_hour"] = None  # type: ignore[index]
    assert capture.sanitize_statusline_payload(payload, observed_at=NOW) is None


def test_command_writes_private_sanitized_file_and_never_echoes_input(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    target = state / "statusline-observation.json"
    env = {"PATH": os.environ["PATH"], capture.OUTPUT_ENV: str(target)}

    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(raw_statusline()),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    saved = json.loads(target.read_text(encoding="ascii"))
    assert set(saved) == {"source", "observed_at", "five_hour", "seven_day"}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_command_fails_silently_without_overwriting_on_unsafe_input(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    target = state / "statusline-observation.json"
    target.write_text("sentinel", encoding="ascii")
    target.chmod(0o600)
    env = {"PATH": os.environ["PATH"], capture.OUTPUT_ENV: str(target)}

    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input='{"prompt":"secret","rate_limits":null}',
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert target.read_text(encoding="ascii") == "sentinel"


def test_command_rejects_symlink_target_and_nonprivate_directory(
    tmp_path: Path,
) -> None:
    payload = json.dumps(raw_statusline())
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("unchanged", encoding="ascii")
    target = state / "statusline-observation.json"
    target.symlink_to(sentinel)
    env = {"PATH": os.environ["PATH"], capture.OUTPUT_ENV: str(target)}

    symlink_result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert symlink_result.returncode == 0
    assert symlink_result.stdout == symlink_result.stderr == ""
    assert sentinel.read_text(encoding="ascii") == "unchanged"

    target.unlink()
    target.write_text("sentinel", encoding="ascii")
    target.chmod(0o600)
    state.chmod(0o755)
    directory_result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert directory_result.returncode == 0
    assert directory_result.stdout == directory_result.stderr == ""
    assert target.read_text(encoding="ascii") == "sentinel"
