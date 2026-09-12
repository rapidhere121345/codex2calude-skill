from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from claude_research_mcp import usage


NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)
FIVE_RESET = int((NOW + timedelta(hours=1)).timestamp())
SEVEN_RESET = int((NOW + timedelta(days=2)).timestamp())


def write_observation(
    path: Path,
    *,
    observed_at: datetime = NOW,
    five_hour: object | None = {"used_percentage": 23.5, "resets_at": FIVE_RESET},
    seven_day: object | None = {"used_percentage": 41.2, "resets_at": SEVEN_RESET},
    mode: int = 0o600,
    raw: str | None = None,
) -> Path:
    state = path.parent
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o700)
    payload = {
        "source": "claude_code_statusline",
        "observed_at": observed_at.isoformat(timespec="seconds"),
        "five_hour": five_hour,
        "seven_day": seven_day,
    }
    path.write_text(raw if raw is not None else json.dumps(payload), encoding="utf-8")
    path.chmod(mode)
    return path


def test_uses_official_statusline_observation_without_secret_material(tmp_path):
    sample = write_observation(tmp_path / "state" / "statusline-observation.json")

    result = usage.parse_statusline_observation(
        sample.read_text(encoding="utf-8"), now=NOW
    )

    assert result == {
        "available": True,
        "source": "claude_code_statusline",
        "observed_at": NOW.isoformat(timespec="seconds"),
        "stale": False,
        "age_seconds": 0,
        "five_hour": {
            "used_percent": 23.5,
            "remaining_percent": 76.5,
            "resets_at": datetime.fromtimestamp(FIVE_RESET, timezone.utc).isoformat(),
        },
        "seven_day": {
            "used_percent": 41.2,
            "remaining_percent": 58.8,
            "resets_at": datetime.fromtimestamp(SEVEN_RESET, timezone.utc).isoformat(),
        },
    }
    assert "credentials" not in sample.read_text(encoding="utf-8")


def test_marks_older_sample_stale_and_reports_age(tmp_path):
    sample = write_observation(
        tmp_path / "state" / "statusline-observation.json",
        observed_at=NOW - timedelta(minutes=10),
    )

    result = usage.parse_statusline_observation(
        sample.read_text(encoding="utf-8"), now=NOW
    )

    assert result["stale"] is True
    assert result["age_seconds"] == 600


def test_missing_unsafe_or_expired_samples_fail_closed(tmp_path):
    state = tmp_path / "state"
    state.mkdir(parents=True)
    sample = state / "statusline-observation.json"
    result = usage.get_opus_quota_status(sample)
    assert result == {
        "available": False,
        "source": "claude_code_statusline",
        "reason": "statusline_observation_unavailable",
        "manual_check_url": "https://claude.ai/usage",
    }

    write_observation(sample, mode=0o644)
    assert (
        usage.get_opus_quota_status(sample)["reason"]
        == "statusline_observation_unavailable"
    )

    expired = write_observation(sample, observed_at=NOW - timedelta(seconds=3601))
    assert (
        usage.get_opus_quota_status(expired)["reason"]
        == "statusline_observation_expired"
    )

    invalid = write_observation(sample, raw=json.dumps({"raw": "untrusted"}))
    assert (
        usage.get_opus_quota_status(invalid)["reason"]
        == "invalid_statusline_observation"
    )


@pytest.mark.parametrize("present", ["five_hour", "seven_day"])
def test_accepts_independently_optional_windows(present: str):
    payload = {
        "source": "claude_code_statusline",
        "observed_at": NOW.isoformat(timespec="seconds"),
        "five_hour": (
            {"used_percentage": 1, "resets_at": FIVE_RESET}
            if present == "five_hour"
            else None
        ),
        "seven_day": (
            {"used_percentage": 2, "resets_at": SEVEN_RESET}
            if present == "seven_day"
            else None
        ),
    }

    result = usage.parse_statusline_observation(json.dumps(payload), now=NOW)

    assert result["available"] is True
    assert result[present] is not None
    missing = "seven_day" if present == "five_hour" else "five_hour"
    assert result[missing] is None


@pytest.mark.parametrize(
    "five_hour,seven_day",
    [
        (None, None),
        ({"resets_at": FIVE_RESET}, {"used_percentage": 1, "resets_at": SEVEN_RESET}),
        ({"used_percentage": True, "resets_at": FIVE_RESET}, {"used_percentage": 1, "resets_at": SEVEN_RESET}),
        ({"used_percentage": -0.1, "resets_at": FIVE_RESET}, {"used_percentage": 1, "resets_at": SEVEN_RESET}),
        ({"used_percentage": 100.1, "resets_at": FIVE_RESET}, {"used_percentage": 1, "resets_at": SEVEN_RESET}),
        ({"used_percentage": 1, "resets_at": True}, {"used_percentage": 1, "resets_at": SEVEN_RESET}),
        ({"used_percentage": 1, "resets_at": FIVE_RESET}, {"used_percentage": 1, "resets_at": "later"}),
    ],
)
def test_rejects_absent_or_invalid_windows(five_hour: object, seven_day: object):
    payload = {
        "source": "claude_code_statusline",
        "observed_at": NOW.isoformat(timespec="seconds"),
        "five_hour": five_hour,
        "seven_day": seven_day,
    }
    with pytest.raises(usage.UsageError) as caught:
        usage.parse_statusline_observation(json.dumps(payload), now=NOW)
    assert caught.value.reason == "invalid_statusline_observation"


def test_does_not_expose_oauth_or_undocumented_endpoint(tmp_path):
    forbidden = {
        "untrusted.json": '{"token":"secret"}',
        "statusline-observation.json": "not-json",
    }
    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    for name, value in forbidden.items():
        (claude_home / name).write_text(value, encoding="utf-8")
        (claude_home / name).chmod(0o600)

    result = usage.get_opus_quota_status(
        claude_home / "statusline-observation.json"
    )

    assert result["available"] is False
    assert "secret" not in json.dumps(result)
