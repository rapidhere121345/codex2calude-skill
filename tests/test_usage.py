from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
import urllib.request

import pytest

from claude_research_mcp import usage


TOKEN = "a" * 64


def write_credentials(claude_home: Path, *, mode: int = 0o600, token: object = TOKEN):
    claude_home.mkdir(parents=True, exist_ok=True)
    path = claude_home / ".credentials.json"
    payload = {"claudeAiOauth": {"accessToken": token}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(mode)
    return path


def usage_payload():
    return {
        "five_hour": {"utilization": 25, "resets_at": "2026-09-12T10:00:00+00:00"},
        "seven_day": {"utilization": 40.5, "resets_at": "2026-09-15T10:00:00Z"},
        "seven_day_opus": None,
        "seven_day_sonnet": {"utilization": 1, "resets_at": "2026-09-15T10:00:00Z"},
    }


def expected_windows():
    return {
        "five_hour": {
            "used_percent": 25.0,
            "remaining_percent": 75.0,
            "resets_at": "2026-09-12T10:00:00+00:00",
        },
        "seven_day": {
            "used_percent": 40.5,
            "remaining_percent": 59.5,
            "resets_at": "2026-09-15T10:00:00+00:00",
        },
        "seven_day_opus": None,
    }


def cache_path(tmp_path: Path):
    state = tmp_path / "work" / "state"
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o700)
    return state / "opus-quota-cache.json"


def write_cache(path: Path, fetched_at: datetime, *, mode: int = 0o600):
    payload = {
        "available": True,
        "source": "claude_code_oauth_usage",
        "fetched_at": fetched_at.isoformat(timespec="seconds"),
        "cached": False,
        "stale": False,
        "cache_age_seconds": 0,
        **expected_windows(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(mode)
    return payload


def test_live_success_null_opus_window_and_remaining(tmp_path, monkeypatch):
    claude_home = Path("/unused")
    path = cache_path(tmp_path)
    calls = []

    def fake_fetch(home):
        calls.append(home)
        return usage._parse_live_payload(usage_payload())

    monkeypatch.setattr(usage, "_fetch_windows", fake_fetch)
    result = usage.get_opus_quota_status(claude_home, path)
    assert calls == [claude_home]
    assert result == {
        "available": True,
        "source": "claude_code_oauth_usage",
        "fetched_at": result["fetched_at"],
        "cached": False,
        "stale": False,
        "cache_age_seconds": 0,
        **expected_windows(),
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "accessToken" not in path.read_text(encoding="utf-8")

    monkeypatch.setattr(
        usage,
        "_fetch_windows",
        lambda home: pytest.fail("the just-written cache must prevent a second fetch"),
    )
    cached = usage.get_opus_quota_status(claude_home, path)
    assert cached["cached"] is True
    assert cached["stale"] is False
    assert cached["cache_age_seconds"] <= 1


def test_fresh_cache_is_reused_without_credentials_or_network(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude"
    path = cache_path(tmp_path)
    original = write_cache(path, datetime.now(timezone.utc) - timedelta(seconds=30))
    monkeypatch.setattr(
        usage,
        "_fetch_windows",
        lambda home: pytest.fail("cache must prevent network access"),
    )
    result = usage.get_opus_quota_status(claude_home, path)
    assert result == {
        **original,
        "cached": True,
        "cache_age_seconds": result["cache_age_seconds"],
    }
    assert 29 <= result["cache_age_seconds"] <= 31


def test_transient_refresh_failure_uses_stale_cache_and_safe_error(monkeypatch):
    now = datetime.now(timezone.utc)
    root = Path("/unused")
    path = Path("/unused-cache")
    stale = {
        "available": True,
        "source": "claude_code_oauth_usage",
        "fetched_at": (now - timedelta(seconds=300)).isoformat(timespec="seconds"),
        "cached": True,
        "stale": False,
        "cache_age_seconds": 300,
        **expected_windows(),
    }
    monkeypatch.setattr(usage, "_load_cache", lambda path, current: (stale, 300))
    monkeypatch.setattr(
        usage,
        "_fetch_windows",
        lambda home: (_ for _ in ()).throw(usage.UsageError("rate_limited")),
    )
    result = usage.get_opus_quota_status(root, path)
    assert result["available"] is True
    assert result["stale"] is True
    assert result["refresh_error"] == "rate_limited"


def test_expired_stale_cache_fails_closed(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude"
    write_credentials(claude_home)
    path = cache_path(tmp_path)
    write_cache(path, datetime.now(timezone.utc) - timedelta(seconds=3601))
    monkeypatch.setattr(
        usage,
        "_fetch_windows",
        lambda home: (_ for _ in ()).throw(usage.UsageError("usage_endpoint_unavailable")),
    )
    result = usage.get_opus_quota_status(claude_home, path)
    assert result == {
        "available": False,
        "source": "claude_code_oauth_usage",
        "reason": "usage_endpoint_unavailable",
    }


def test_malformed_or_unsafe_credentials_and_cache_fail_closed(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude"
    path = cache_path(tmp_path)
    write_credentials(claude_home, mode=0o644)
    write_cache(path, datetime.now(timezone.utc), mode=0o644)
    monkeypatch.setattr(
        usage,
        "_request_usage",
        lambda token, opener=None: pytest.fail("unsafe credentials must not reach the endpoint"),
    )
    result = usage.get_opus_quota_status(claude_home, path)
    assert result["available"] is False
    assert result["reason"] == "credentials_unavailable"

    path.unlink()
    path.write_text('{"available":true,"raw":"secret"}')
    path.chmod(0o600)
    result = usage.get_opus_quota_status(claude_home, path)
    assert result["reason"] == "credentials_unavailable"

    write_credentials(claude_home)
    malformed = [None, "", "x" * 31, "x" * 8193, "bad\ncontrol"]
    for token in malformed:
        write_credentials(claude_home, token=token)
        with pytest.raises(usage.UsageError) as caught:
            usage._read_access_token(claude_home)
        assert caught.value.reason == "credentials_unavailable"


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, size: int) -> bytes:
        return self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return FakeResponse(self.result)


def test_http_headers_status_classification_and_size_limit():
    opener = FakeOpener(b'{"five_hour":{"utilization":0,"resets_at":"2026-01-01T00:00:00Z"},"seven_day":{"utilization":0,"resets_at":"2026-01-01T00:00:00Z"},"seven_day_opus":null}')
    parsed = usage._request_usage(TOKEN, opener)
    request, timeout = opener.requests[0]
    assert request.full_url == usage.USAGE_ENDPOINT
    assert timeout == 10
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Anthropic-beta"] == "oauth-2025-04-20"
    assert request.headers["User-agent"] == "claude-research-mcp/0.1"
    assert parsed["seven_day_opus"] is None

    for code, reason in ((401, "authentication_unavailable"), (403, "authentication_unavailable"), (429, "rate_limited"), (500, "usage_endpoint_unavailable")):
        failing = FakeOpener(__import__("urllib.error", fromlist=["HTTPError"]).HTTPError("safe", code, "safe", {}, None))
        with pytest.raises(usage.UsageError) as caught:
            usage._request_usage(TOKEN, failing)
        assert caught.value.reason == reason

    oversized = FakeOpener(b"x" * (usage.MAX_RESPONSE_BYTES + 1))
    with pytest.raises(usage.UsageError) as caught:
        usage._request_usage(TOKEN, oversized)
    assert caught.value.reason == "invalid_usage_response"


def test_http_opener_disables_redirects_and_environment_proxies(monkeypatch):
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: pytest.fail("the opener must not inspect environment proxies"),
    )
    opener = usage._http_opener()
    redirect = next(
        handler
        for handler in opener.handlers
        if isinstance(handler, urllib.request.HTTPRedirectHandler)
    )
    assert redirect.redirect_request(None, None, 302, "redirect", {}, "https://example.com") is None
    assert not any(
        isinstance(handler, urllib.request.ProxyHandler) for handler in opener.handlers
    )


def test_response_schema_rejects_invalid_utilization_and_timestamps():
    bad_values = [True, False, -0.1, 100.1, float("nan"), float("infinity")]
    for value in bad_values:
        rendered = json.dumps(usage_payload()).replace('"utilization": 25', f'"utilization": {json.dumps(value)}')
        with pytest.raises(ValueError):
            usage._parse_live_payload(json.loads(rendered))

    for timestamp in ("2026-01-01T00:00:00", "not-a-time", None, 12):
        payload = usage_payload()
        payload["five_hour"]["resets_at"] = timestamp
        with pytest.raises(ValueError):
            usage._parse_live_payload(payload)

    missing = usage_payload()
    del missing["seven_day"]
    with pytest.raises(ValueError):
        usage._parse_live_payload(missing)
