from datetime import datetime, timezone
import json

import pytest

from mcp_audit_dashboard.config import config_from_environment
from mcp_audit_dashboard.subscriptions import (
    MAX_SUBSCRIPTION_BYTES,
    read_subscriptions,
)


NOW = datetime(2026, 10, 8, 16, 1, tzinfo=timezone.utc)


def read(tmp_path, payload):
    path = tmp_path / "subscriptions.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return read_subscriptions(path, now=NOW)


@pytest.mark.parametrize(
    "day,remaining,state",
    [("2026-10-08", -1, "past"), ("2026-10-09", 0, "today"),
     ("2026-10-16", 7, "soon"), ("2026-10-17", 8, "scheduled")],
)
def test_calendar_days_and_warning_boundaries(tmp_path, day, remaining, state):
    result = read(tmp_path, {"claude": {"date": day, "kind": "expires"}})
    assert result["timezone"] == "Asia/Shanghai"
    assert result["claude"] == {
        "configured": True, "date": day, "kind": "expires",
        "source": "manual", "days_remaining": remaining, "state": state,
    }
    assert result["gemini"] == {"configured": False, "reason": "not_configured"}


def test_renewal_is_distinct_and_never_derives_account_status(tmp_path):
    result = read(tmp_path, {
        "claude": {"kind": "renews", "date": "2026-10-08"},
        "gemini": {"kind": "expires", "date": "2026-10-16"},
    })
    assert result["claude"]["kind"] == "renews"
    assert result["claude"]["state"] == "past"
    assert result["gemini"]["days_remaining"] == 7
    assert "account_ready" not in json.dumps(result)


@pytest.mark.parametrize("value", [
    {"date": "2026-02-30", "kind": "expires"},
    {"date": "2026-10-09T00:00:00Z", "kind": "expires"},
    {"date": "20261009", "kind": "expires"},
    {"date": "2026-W41-5", "kind": "expires"},
    {"date": {"access_token": "SECRET"}, "kind": "expires"},
    {"date": "2026-10-09", "kind": {"access_token": "SECRET"}},
    {"date": "2026-10-09", "kind": "quota_reset"},
    {"subscriptionCreatedAt": "2026-09-09", "expiresAt": "2026-10-09"},
    [], False,
])
def test_invalid_dates_never_fall_back_to_login_or_quota_dates(tmp_path, value):
    result = read(tmp_path, {
        "claude": value, "gemini": {"date": "2028-02-29", "kind": "expires"},
    })
    assert result["claude"] == {"configured": False, "reason": "invalid"}
    assert result["gemini"]["date"] == "2028-02-29"
    assert "SECRET" not in json.dumps(result)


def test_only_allowlisted_metadata_is_returned(tmp_path):
    result = read(tmp_path, {"claude": {
        "date": "2026-10-12", "kind": "expires", "source": "provider",
        "access_token": "SECRET", "email": "SECRET", "raw": {"secret": "SECRET"},
    }, "secret": "SECRET"})
    assert result["claude"]["source"] == "manual"
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("content", [b"{", b"[]", b"\xff", b" " * (MAX_SUBSCRIPTION_BYTES + 1)])
def test_bad_file_fails_closed_without_paths_or_parser_errors(tmp_path, content):
    path = tmp_path / "private-subscriptions.json"
    path.write_bytes(content)
    result = read_subscriptions(path, now=NOW)
    assert result["claude"] == result["gemini"] == {"configured": False, "reason": "unavailable"}
    assert "private-subscriptions" not in json.dumps(result)


def test_missing_file_and_configuration(tmp_path, monkeypatch):
    path = tmp_path / "subscriptions.json"
    assert read_subscriptions(path)["claude"]["reason"] == "not_configured"
    monkeypatch.setenv("MCP_AUDIT_OWNER_LOGIN", "owner@example.invalid")
    monkeypatch.delenv("MCP_AUDIT_SUBSCRIPTIONS_FILE", raising=False)
    config = config_from_environment(static_dir=tmp_path, subscriptions_file=path)
    assert config.subscriptions_file == path
    override = tmp_path / "override.json"
    monkeypatch.setenv("MCP_AUDIT_SUBSCRIPTIONS_FILE", str(override))
    assert config_from_environment(static_dir=tmp_path, subscriptions_file=path).subscriptions_file == override
