from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from claude_research_mcp import quota_probe, server as server_module
from claude_research_mcp.config import load_settings
from claude_research_mcp.quota_probe import CACHE_FILENAME, MANUAL_CHECK_URL, SOURCE
from claude_research_mcp.server import create_app


FAKE_CLI = """#!/bin/sh
if [ "$1" = --version ]; then printf '2.1.301\\n'; exit 0; fi
if [ "$1" = auth ] && [ "$2" = status ]; then
  printf '%s\\n' '{"loggedIn":true,"authMethod":"claude.ai","subscriptionType":"pro"}'
  exit 0
fi
cat >/dev/null
printf '%s\\n' '{"result":"ok","sessionId":"s","modelUsage":{}}'
"""

FAKE_CLI_LOGGED_OUT = FAKE_CLI.replace('"loggedIn":true', '"loggedIn":false')


def make_settings(tmp_path: Path, principal: str, work_root: Path, *, logged_in: bool = True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    binary = tmp_path / f"claude-{principal}"
    binary.write_text(FAKE_CLI if logged_in else FAKE_CLI_LOGGED_OUT)
    binary.chmod(0o755)
    work_root.mkdir(parents=True, exist_ok=True)
    return load_settings({
        "CLAUDE_MCP_WORK_ROOT": str(work_root),
        "CLAUDE_MCP_PRINCIPAL": principal,
        "CLAUDE_BIN": str(binary),
    })


def get_tool(app, name):
    return app._tool_manager._tools[name].fn


def status(settings):
    return asyncio.run(get_tool(create_app(settings), "claude_status")())


def write_quota_cache(
    work_root: Path,
    *,
    age_seconds: float = 10,
    last_attempt_status: str = "ok",
    observed: bool = True,
    windows: bool = True,
    raw: str | None = None,
) -> Path:
    state = work_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    if raw is not None:
        path = state / CACHE_FILENAME
        path.write_text(raw)
        path.chmod(0o600)
        return path
    observed_at = (
        (now - timedelta(seconds=age_seconds)).isoformat(timespec="seconds")
        if observed
        else None
    )
    attempt_at = now.isoformat(timespec="seconds")
    value = {
        "schema_version": 1,
        "source": SOURCE,
        "probe_model": quota_probe.PROBE_MODEL,
        "last_attempt_at": attempt_at,
        "last_attempt_status": last_attempt_status,
        "observed_at": observed_at,
        "windows": (
            {
                "five_hour": {
                    "utilization": 0.2,
                    "resets_at": int((now + timedelta(hours=2)).timestamp()),
                },
                "seven_day": {
                    "utilization": 0.1,
                    "resets_at": int((now + timedelta(days=2)).timestamp()),
                },
            }
            if windows and observed
            else {}
        ),
    }
    path = state / CACHE_FILENAME
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


@pytest.fixture
def no_probe_calls(monkeypatch):
    violations = []

    def forbid(name):
        def fail(*args, **kwargs):
            violations.append(name)
            raise AssertionError(f"{name} must not run during claude_status")

        return fail

    monkeypatch.setattr(server_module, "run_usage_command", forbid("run_usage_command"))
    monkeypatch.setattr(server_module, "_run_usage_probe", forbid("_run_usage_probe"))
    monkeypatch.setattr(server_module, "get_opus_quota_status", forbid("get_opus_quota_status"))
    monkeypatch.setattr(quota_probe, "collect_once", forbid("collect_once"))
    monkeypatch.setattr(quota_probe, "request_headers", forbid("request_headers"))
    monkeypatch.setattr(quota_probe, "_credential", forbid("_credential"))
    return violations


def test_status_reads_shared_quota_cache_across_principals_without_probes(
    tmp_path, no_probe_calls
):
    work = tmp_path / "shared-work"
    owner = make_settings(tmp_path / "owner", "owner", work)
    partner = make_settings(tmp_path / "partner", "partner", work)
    write_quota_cache(work, age_seconds=30)

    results = [
        status(owner), status(partner), status(owner), status(partner)
    ]

    for result in results:
        assert result["status"] == "ok"
        quota = result["opus_quota"]
        assert quota["available"] is True
        assert quota["source"] == SOURCE
        assert quota["five_hour"]["used_percent"] == 20.0
        assert quota["five_hour"]["remaining_percent"] == 80.0
        assert quota["seven_day"]["used_percent"] == 10.0
        assert quota["age_seconds"] >= 30
        assert "reason" not in quota
    assert no_probe_calls == []


def test_status_quota_states_stay_distinct(tmp_path, no_probe_calls):
    work = tmp_path / "work"
    settings = make_settings(tmp_path, "owner", work)
    cache = work / "state" / CACHE_FILENAME

    missing = status(settings)
    assert missing["opus_quota"] == {
        "available": False,
        "source": SOURCE,
        "reason": "quota_cache_unavailable",
        "manual_check_url": MANUAL_CHECK_URL,
    }

    write_quota_cache(work, age_seconds=400)
    stale = status(settings)
    assert stale["opus_quota"]["available"] is True
    assert stale["opus_quota"]["stale"] is True
    assert stale["opus_quota"]["age_seconds"] >= 400
    assert "reason" not in stale["opus_quota"]

    write_quota_cache(work, age_seconds=30, last_attempt_status="probe_rate_limited")
    failed = status(settings)
    assert failed["opus_quota"]["available"] is True
    assert failed["opus_quota"]["stale"] is True
    assert failed["opus_quota"]["reason"] == "probe_rate_limited"
    assert failed["opus_quota"]["five_hour"]["used_percent"] == 20.0

    write_quota_cache(work, observed=False, last_attempt_status="probe_timeout")
    unobserved = status(settings)
    assert unobserved["opus_quota"]["available"] is False
    assert unobserved["opus_quota"]["reason"] == "probe_timeout"
    assert "observed_at" not in unobserved["opus_quota"]

    write_quota_cache(work, age_seconds=4000)
    expired = status(settings)
    assert expired["opus_quota"]["available"] is False
    assert expired["opus_quota"]["reason"] == "quota_cache_expired"
    assert "five_hour" not in expired["opus_quota"]

    write_quota_cache(work, raw="{")
    invalid = status(settings)
    assert invalid["opus_quota"]["available"] is False
    assert invalid["opus_quota"]["reason"] == "quota_cache_invalid"

    assert cache.is_file()
    assert no_probe_calls == []


def test_status_survives_unexpected_quota_read_failure(tmp_path, no_probe_calls, monkeypatch):
    work = tmp_path / "work"
    settings = make_settings(tmp_path, "owner", work)

    def explode(path):
        raise RuntimeError("secret filesystem detail")

    monkeypatch.setattr(server_module, "read_quota_cache", explode)
    result = status(settings)

    assert result["status"] == "ok"
    assert result["opus_quota"] == {
        "available": False,
        "source": SOURCE,
        "reason": "quota_cache_invalid",
        "manual_check_url": MANUAL_CHECK_URL,
    }
    assert "secret" not in json.dumps(result).casefold()
    assert no_probe_calls == []


def test_logged_out_status_never_touches_quota_cache(tmp_path, monkeypatch):
    work = tmp_path / "work"
    settings = make_settings(tmp_path, "owner", work, logged_in=False)

    def forbid(name):
        def fail(*args, **kwargs):
            raise AssertionError(f"{name} must not run while logged out")

        return fail

    monkeypatch.setattr(server_module, "read_quota_cache", forbid("read_quota_cache"))
    monkeypatch.setattr(server_module, "run_usage_command", forbid("run_usage_command"))
    monkeypatch.setattr(quota_probe, "collect_once", forbid("collect_once"))
    monkeypatch.setattr(quota_probe, "request_headers", forbid("request_headers"))
    monkeypatch.setattr(quota_probe, "_credential", forbid("_credential"))

    result = status(settings)

    assert result["logged_in"] is False
    assert result["opus_quota"] == {
        "available": False,
        "source": "claude_code_statusline",
        "reason": "requires_claude_ai_login",
        "manual_check_url": MANUAL_CHECK_URL,
    }
