from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import stat
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


def test_parses_ansi_stripped_usage_command_windows_and_reset_formats():
    screen = (
        "\x1b]0;local session statistics\x07\x1b[2J\x1b[H│ Current session ▸ 60% used Resets 8:30am (UTC)\n"
        "\x1b[38;5;10m Current week (all models) ▸ 12% used Resets Sep 26, 12pm (UTC)\n"
    )

    result = usage.parse_usage_command_output(screen, now=NOW)

    assert result.five_hour == (
        60.0,
        datetime(2026, 9, 13, 8, 30, tzinfo=timezone.utc),
    )
    assert result.seven_day == (
        12.0,
        datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc),
    )

    time_only = usage.parse_usage_command_output(
        "Current session [bars] 7.5% used Resets 1:29am (UTC) "
        "Current week (all models) [bars] 12% used Resets Sep 26, 12pm (UTC)",
        now=datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc),
        timezone_name="UTC+08:00",
    )
    assert time_only.five_hour == (
        7.5,
        datetime(2026, 9, 20, 17, 29, tzinfo=timezone.utc),
    )
    assert time_only.seven_day == (
        12.0,
        datetime(2026, 9, 26, 4, 0, tzinfo=timezone.utc),
    )


def test_ansi_stripping_removes_osc_and_csi_then_collapses_whitespace():
    raw = "\x1b]0;private\x07Current\x1b[2K session  60% used\x1b[0m"

    assert usage.strip_ansi(raw) == "Current session 60% used"


def test_usage_parser_does_not_borrow_values_from_another_window():
    screen = (
        "Current session Loading "
        "Current week (all models) 12% used Resets Sep 26, 12pm (UTC)"
    )

    with pytest.raises(usage.UsageError) as caught:
        usage.parse_usage_command_output(screen, now=NOW)

    assert caught.value.reason == "invalid_usage_command_output"


def test_usage_command_parser_fails_closed_without_diagnostics():
    with pytest.raises(usage.UsageError) as failed:
        usage.parse_usage_command_output("Failed to load usage data\nsecret@example.com")
    assert failed.value.reason == "usage_command_failed"

    with pytest.raises(usage.UsageError) as invalid:
        usage.parse_usage_command_output("Current session: secret")
    assert invalid.value.reason == "invalid_usage_command_output"
    assert "secret" not in str(invalid.value)


def test_usage_command_cache_is_short_lived_private_and_atomic(tmp_path):
    parsed = usage.parse_usage_command_output(
        "Current session 60% used Resets 8:30am (UTC) Current week (all models) 42% used Resets Sep 26, 4:59am (UTC)", now=NOW
    )
    path = usage.usage_command_cache_path(
        tmp_path / "state" / "statusline-observation.json"
    )

    usage.write_usage_command_cache(path, parsed)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = json.loads(path.read_text(encoding="ascii"))
    assert set(saved) == {"source", "observed_at", "five_hour", "seven_day"}
    assert saved["source"] == usage.USAGE_COMMAND_SOURCE
    assert "secret" not in json.dumps(saved)
    result = usage.get_cached_usage_command_status(path, now=NOW)
    assert result is not None
    assert result["source"] == usage.USAGE_COMMAND_SOURCE
    assert result["five_hour"]["used_percent"] == 60.0
    assert result["seven_day"]["used_percent"] == 42.0
    assert usage.get_cached_usage_command_status(
        path, now=NOW + timedelta(seconds=61)
    ) is None


def test_usage_command_cache_rejects_symlinks_and_unsafe_modes(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    target = state / "target"
    target.write_text("sentinel", encoding="ascii")
    target.chmod(0o600)
    cache = usage.usage_command_cache_path(state / "statusline.json")
    cache.symlink_to(target)

    assert usage.read_usage_command_cache(cache, now=NOW) is None
    assert usage.get_cached_usage_command_status(cache, now=NOW) is None
    assert target.read_text(encoding="ascii") == "sentinel"

    cache.unlink()
    payload = {
        "source": usage.USAGE_COMMAND_SOURCE,
        "observed_at": NOW.isoformat(),
        "five_hour": None,
        "seven_day": {"used_percentage": 1, "resets_at": SEVEN_RESET},
    }
    cache.write_text(json.dumps(payload), encoding="ascii")
    cache.chmod(0o644)
    assert usage.read_usage_command_cache(cache, now=NOW) is None


def test_usage_cache_write_replaces_unsafe_target_atomically(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    path = state / usage.USAGE_CACHE_FILENAME
    path.write_text("sentinel", encoding="ascii")
    path.chmod(0o600)
    parsed = usage.parse_usage_command_output(
        "Current session 60% used Resets 8:30am (UTC) "
        "Current week (all models) 42% used Resets Sep 26, 4:59am (UTC)",
        now=NOW,
    )

    usage.write_usage_command_cache(path, parsed)

    assert path.read_text(encoding="ascii") != "sentinel"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(state.glob(".usage-command-observation.*")) == []


def test_usage_cache_write_rejects_symlink_target(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    target = state / "target"
    target.write_text("sentinel", encoding="ascii")
    target.chmod(0o600)
    path = state / usage.USAGE_CACHE_FILENAME
    path.symlink_to(target)
    parsed = usage.parse_usage_command_output(
        "Current session 60% used Resets 8:30am (UTC) "
        "Current week (all models) 42% used Resets Sep 26, 4:59am (UTC)",
        now=NOW,
    )
    with pytest.raises(OSError):
        usage.write_usage_command_cache(path, parsed)
    assert target.read_text(encoding="ascii") == "sentinel"


def test_usage_cache_write_rejects_unsafe_parent_directory(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    path = state / usage.USAGE_CACHE_FILENAME
    parsed = usage.parse_usage_command_output(
        "Current session 60% used Resets 8:30am (UTC) "
        "Current week (all models) 42% used Resets Sep 26, 4:59am (UTC)",
        now=NOW,
    )

    with pytest.raises(OSError, match="unsafe usage-cache directory"):
        usage.write_usage_command_cache(path, parsed)
