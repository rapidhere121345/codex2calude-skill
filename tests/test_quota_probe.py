from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import threading
import time

import pytest

from claude_research_mcp.quota_probe import (
    CACHE_FILENAME,
    MANUAL_CHECK_URL,
    PROBE_MODEL,
    REASONS,
    SOURCE,
    QuotaError,
    collect_once,
    parse_rate_limit_headers,
    read_quota_cache,
)


BASE_TIME = datetime(2026, 11, 11, 3, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start: datetime = BASE_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    state.chmod(0o700)
    return state


def make_credential(
    tmp_path: Path,
    clock: Clock,
    *,
    token: str = "probe-token-abc",
    expires_in: float = 3600,
    mode: int = 0o600,
    payload: object = None,
) -> Path:
    if payload is None:
        payload = {
            "claudeAiOauth": {
                "accessToken": token,
                "expiresAt": int(
                    (clock() + timedelta(seconds=expires_in)).timestamp() * 1000
                ),
            }
        }
    credential = tmp_path / "credentials.json"
    credential.write_text(json.dumps(payload))
    credential.chmod(mode)
    return credential


def window_headers(
    clock: Clock,
    utilization: str,
    window: str,
    *,
    reset_seconds: float = 7200,
    reset: str | None = None,
) -> list[tuple[str, str]]:
    epoch = reset if reset is not None else str(
        int((clock() + timedelta(seconds=reset_seconds)).timestamp())
    )
    return [
        (f"anthropic-ratelimit-unified-{window}-utilization", utilization),
        (f"anthropic-ratelimit-unified-{window}-reset", epoch),
    ]


def full_headers(clock: Clock, five: str = "0.2", seven: str = "0.1") -> list:
    return window_headers(clock, five, "5h") + window_headers(clock, seven, "7d")


def recording_request(calls: list, clock: Clock, headers=None, status: int = 200):
    def request(token: str):
        calls.append(token)
        return status, headers if headers is not None else full_headers(clock)

    return request


def write_cache(
    state: Path,
    *,
    observed_at: str | None,
    last_attempt_at: str | None,
    last_attempt_status: str = "ok",
    windows: dict | None = None,
) -> Path:
    value = {
        "schema_version": 1,
        "source": SOURCE,
        "probe_model": PROBE_MODEL,
        "last_attempt_at": last_attempt_at,
        "last_attempt_status": last_attempt_status,
        "observed_at": observed_at,
        "windows": windows if windows is not None else {},
    }
    path = state / CACHE_FILENAME
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def iso(clock: Clock, seconds_ago: float = 0) -> str:
    return (clock() - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")


def future_windows(clock: Clock, five: float = 0.2, seven: float = 0.1) -> dict:
    return {
        "five_hour": {
            "utilization": five,
            "resets_at": int((clock() + timedelta(hours=2)).timestamp()),
        },
        "seven_day": {
            "utilization": seven,
            "resets_at": int((clock() + timedelta(days=2)).timestamp()),
        },
    }


# ---------- parse_rate_limit_headers ----------


@pytest.mark.parametrize(
    "raw,expected",
    [("0", 0.0), ("0.2", 0.2), ("1", 1.0), ("1.0", 1.0)],
)
def test_parse_accepts_boundary_utilization_values(clock, raw, expected):
    result = parse_rate_limit_headers(
        window_headers(clock, raw, "5h"), now=clock()
    )
    assert result == {
        "five_hour": {
            "utilization": expected,
            "resets_at": int(
                (clock() + timedelta(seconds=7200)).timestamp()
            ),
        }
    }


@pytest.fixture
def clock():
    return Clock()


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("anthropic-ratelimit-unified-5h-utilization", "0.2")],
        [("anthropic-ratelimit-unified-5h-reset", "1893456000")],
        [
            ("anthropic-ratelimit-unified-5h-utilization", "0.2"),
            ("anthropic-ratelimit-unified-5h-utilization", "0.3"),
            ("anthropic-ratelimit-unified-5h-reset", "1893456000"),
        ],
        [
            ("anthropic-ratelimit-unified-5h-utilization", "nan"),
            ("anthropic-ratelimit-unified-5h-reset", "1893456000"),
        ],
        [
            ("anthropic-ratelimit-unified-5h-utilization", "-0.1"),
            ("anthropic-ratelimit-unified-5h-reset", "1893456000"),
        ],
        [
            ("anthropic-ratelimit-unified-5h-utilization", "1.5"),
            ("anthropic-ratelimit-unified-5h-reset", "1893456000"),
        ],
        [
            ("anthropic-ratelimit-unified-5h-utilization", ".5"),
            ("anthropic-ratelimit-unified-5h-reset", "1893456000"),
        ],
        [
            ("anthropic-ratelimit-unified-5h-utilization", "0."),
            ("anthropic-ratelimit-unified-5h-reset", "1893456000"),
        ],
        [
            ("anthropic-ratelimit-unified-5h-utilization", "0" * 200),
            ("anthropic-ratelimit-unified-5h-reset", "1893456000"),
        ],
        [
            ("anthropic-ratelimit-unified-5h-utilization", "0.2"),
            ("anthropic-ratelimit-unified-5h-reset", "0"),
        ],
        [
            ("anthropic-ratelimit-unified-5h-utilization", "0.2"),
            ("anthropic-ratelimit-unified-5h-reset", "not-a-number"),
        ],
        [
            ("anthropic-ratelimit-unified-5h-utilization", "0.2"),
            ("anthropic-ratelimit-unified-5h-reset", "1" * 15),
        ],
    ],
)
def test_parse_rejects_missing_duplicate_or_malformed_headers(clock, headers):
    with pytest.raises(QuotaError) as caught:
        parse_rate_limit_headers(headers, now=clock())
    assert caught.value.reason in {"probe_headers_missing", "probe_headers_invalid"}


def test_parse_rejects_expired_reset(clock):
    past = str(int(clock().timestamp()) - 60)
    with pytest.raises(QuotaError) as caught:
        parse_rate_limit_headers(
            window_headers(clock, "0.2", "5h", reset=past), now=clock()
        )
    assert caught.value.reason == "probe_headers_invalid"


def test_parse_accepts_case_insensitive_names_and_partial_window(clock):
    headers = [
        ("Anthropic-RateLimit-Unified-7d-Utilization", "0.4"),
        ("Anthropic-RateLimit-Unified-7d-Reset", "1893456000"),
        ("unrelated-header", "ignored"),
    ]
    result = parse_rate_limit_headers(headers, now=clock())
    assert set(result) == {"seven_day"}
    assert result["seven_day"]["utilization"] == 0.4


# ---------- collect_once ----------


def test_first_collect_requests_once_and_caches_sanitized_windows(tmp_path, clock):
    state = make_state(tmp_path)
    credential = make_credential(tmp_path, clock)
    calls = []

    result = collect_once(
        state, credential, clock=clock,
        request=recording_request(calls, clock),
    )

    assert result["attempted"] is True
    assert calls == ["probe-token-abc"]
    quota = result["quota"]
    assert quota["available"] is True
    assert quota["source"] == SOURCE
    assert quota["manual_check_url"] == MANUAL_CHECK_URL
    assert quota["five_hour"]["used_percent"] == 20.0
    assert quota["five_hour"]["remaining_percent"] == 80.0
    assert quota["seven_day"]["used_percent"] == 10.0
    assert quota["seven_day"]["remaining_percent"] == 90.0
    assert quota["stale"] is False
    assert "reason" not in quota

    cache = state / CACHE_FILENAME
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    persisted = json.loads(cache.read_text())
    assert persisted["probe_model"] == PROBE_MODEL
    assert "probe-token-abc" not in cache.read_text()

    read_back = read_quota_cache(cache, now=clock())
    assert read_back["available"] is True
    assert read_back["five_hour"]["used_percent"] == 20.0


def test_second_collect_within_interval_never_replays(tmp_path, clock):
    state = make_state(tmp_path)
    credential = make_credential(tmp_path, clock)
    calls = []
    request = recording_request(calls, clock)

    first = collect_once(state, credential, clock=clock, request=request)
    assert first["attempted"] is True

    clock.advance(299)
    second = collect_once(state, credential, clock=clock, request=request)
    assert second["attempted"] is False
    assert len(calls) == 1
    assert second["quota"]["available"] is True

    clock.advance(2)
    third = collect_once(state, credential, clock=clock, request=request)
    assert third["attempted"] is True
    assert len(calls) == 2


def test_concurrent_collectors_share_a_single_attempt(tmp_path, clock):
    state = make_state(tmp_path)
    credential = make_credential(tmp_path, clock)
    calls = []

    def slow_request(token):
        calls.append(token)
        time.sleep(0.2)
        return 200, full_headers(clock)

    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait(timeout=10)
        results.append(
            collect_once(state, credential, clock=clock, request=slow_request)
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(calls) == 1
    assert sorted(result["attempted"] for result in results) == [False, True]


def test_failed_attempt_persists_cooldown_without_immediate_replay(
    tmp_path, clock
):
    state = make_state(tmp_path)
    credential = make_credential(tmp_path, clock)
    calls = []

    def failing(token):
        calls.append(token)
        raise QuotaError("probe_timeout")

    first = collect_once(state, credential, clock=clock, request=failing)
    assert first["attempted"] is True
    assert first["quota"]["available"] is False
    assert first["quota"]["reason"] == "probe_timeout"
    assert "observed_at" not in first["quota"]

    clock.advance(30)
    second = collect_once(
        state, credential, clock=clock,
        request=recording_request(calls, clock),
    )
    assert second["attempted"] is False
    assert len(calls) == 1

    clock.advance(280)
    third = collect_once(
        state, credential, clock=clock,
        request=recording_request(calls, clock),
    )
    assert third["attempted"] is True
    assert len(calls) == 2
    assert third["quota"]["available"] is True


def test_unknown_attempt_is_persisted_and_not_replayed(tmp_path, clock):
    state = make_state(tmp_path)
    credential = make_credential(tmp_path, clock)
    calls = []

    def lost(token):
        calls.append(token)
        raise RuntimeError("connection reset by peer")

    with pytest.raises(RuntimeError):
        collect_once(state, credential, clock=clock, request=lost)

    persisted = read_quota_cache(state / CACHE_FILENAME, now=clock())
    assert persisted["available"] is False
    assert persisted["reason"] == "probe_result_unknown"

    clock.advance(30)
    result = collect_once(
        state, credential, clock=clock,
        request=recording_request(calls, clock),
    )
    assert result["attempted"] is False
    assert len(calls) == 1
    assert result["quota"]["reason"] == "probe_result_unknown"


@pytest.mark.parametrize(
    "status,headers,reason",
    [
        (401, [], "probe_auth_rejected"),
        (403, full_headers(Clock()), "probe_auth_rejected"),
        (500, [], "probe_http_error"),
        (429, [], "probe_rate_limited"),
        (200, [], "probe_headers_missing"),
        (200, [("anthropic-ratelimit-unified-5h-utilization", "0.2")],
         "probe_headers_invalid"),
    ],
)
def test_probe_status_and_header_failures_map_to_sanitized_reasons(
    tmp_path, clock, status, headers, reason
):
    state = make_state(tmp_path)
    credential = make_credential(tmp_path, clock)
    calls = []
    result = collect_once(
        state, credential, clock=clock,
        request=lambda token: (calls.append(token), (status, headers))[1],
    )
    assert result["attempted"] is True
    assert result["quota"]["available"] is False
    assert result["quota"]["reason"] == reason
    assert result["quota"]["source"] == SOURCE


def test_partial_window_is_preserved(tmp_path, clock):
    state = make_state(tmp_path)
    credential = make_credential(tmp_path, clock)
    headers = window_headers(clock, "0.3", "5h")
    result = collect_once(
        state, credential, clock=clock,
        request=lambda token: (200, headers),
    )
    quota = result["quota"]
    assert quota["available"] is True
    assert "five_hour" in quota
    assert "seven_day" not in quota
    assert quota["five_hour"]["used_percent"] == 30.0


# ---------- credential handling ----------


def test_missing_credential_never_requests_and_stays_cooldown(
    tmp_path, clock
):
    state = make_state(tmp_path)
    credential = tmp_path / "absent.json"
    calls = []
    first = collect_once(
        state, credential, clock=clock,
        request=recording_request(calls, clock),
    )
    assert first["attempted"] is False
    assert first["quota"]["available"] is False
    assert first["quota"]["reason"] == "oauth_unavailable"
    assert calls == []

    clock.advance(30)
    second = collect_once(
        state, credential, clock=clock,
        request=recording_request(calls, clock),
    )
    assert second["attempted"] is False
    assert calls == []


def test_expired_credential_is_rejected_without_request(tmp_path, clock):
    state = make_state(tmp_path)
    credential = make_credential(tmp_path, clock, expires_in=-60)
    calls = []
    result = collect_once(
        state, credential, clock=clock,
        request=recording_request(calls, clock),
    )
    assert result["attempted"] is False
    assert result["quota"]["reason"] == "oauth_expired"
    assert calls == []


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_credential_with_open_permissions_is_rejected(tmp_path, clock, mode):
    state = make_state(tmp_path)
    credential = make_credential(tmp_path, clock, mode=mode)
    calls = []
    result = collect_once(
        state, credential, clock=clock,
        request=recording_request(calls, clock),
    )
    assert result["attempted"] is False
    assert result["quota"]["reason"] == "oauth_invalid"
    assert calls == []


def test_credential_symlink_is_rejected(tmp_path, clock):
    state = make_state(tmp_path)
    target = make_credential(tmp_path, clock)
    link = tmp_path / "linked.json"
    link.symlink_to(target)
    calls = []
    result = collect_once(
        state, link, clock=clock,
        request=recording_request(calls, clock),
    )
    assert result["attempted"] is False
    assert result["quota"]["reason"] == "oauth_unavailable"
    assert calls == []


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        {"claudeAiOauth": {}},
        {"claudeAiOauth": {"accessToken": "tok", "expiresAt": "soon"}},
        {"claudeAiOauth": {"accessToken": "tok\ninjected", "expiresAt": 1}},
        {"claudeAiOauth": {"accessToken": "tökén", "expiresAt": 9999999999999}},
        [],
    ],
)
def test_malformed_credential_is_rejected(tmp_path, clock, payload):
    state = make_state(tmp_path)
    credential = make_credential(tmp_path, clock, payload=payload)
    calls = []
    result = collect_once(
        state, credential, clock=clock,
        request=recording_request(calls, clock),
    )
    assert result["attempted"] is False
    assert result["quota"]["reason"] in {"oauth_invalid", "oauth_expired"}
    assert calls == []


def test_unsafe_state_directory_is_rejected(tmp_path, clock):
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    state.chmod(0o755)
    credential = make_credential(tmp_path, clock)
    with pytest.raises(ValueError, match="unsafe quota state directory"):
        collect_once(state, credential, clock=clock, request=lambda t: (200, []))


# ---------- read_quota_cache ----------


def test_missing_cache_reports_unavailable(tmp_path, clock):
    quota = read_quota_cache(tmp_path / "absent.json", now=clock())
    assert quota == {
        "available": False,
        "source": SOURCE,
        "reason": "quota_cache_unavailable",
        "manual_check_url": MANUAL_CHECK_URL,
    }


@pytest.mark.parametrize(
    "raw",
    ["{", json.dumps({"schema_version": 2}), "[]", "null", '"text"'],
)
def test_invalid_cache_is_sanitized(tmp_path, clock, raw):
    state = make_state(tmp_path)
    path = state / CACHE_FILENAME
    path.write_text(raw)
    path.chmod(0o600)
    quota = read_quota_cache(path, now=clock())
    assert quota["available"] is False
    assert quota["reason"] == "quota_cache_invalid"
    assert quota["source"] == SOURCE


def test_cache_symlink_is_rejected(tmp_path, clock):
    state = make_state(tmp_path)
    real = tmp_path / "real.json"
    real.write_text("{}")
    real.chmod(0o600)
    link = state / CACHE_FILENAME
    link.symlink_to(real)
    quota = read_quota_cache(link, now=clock())
    assert quota["available"] is False
    assert quota["reason"] == "quota_cache_invalid"


def test_fresh_cache_reports_windows_without_reason(tmp_path, clock):
    state = make_state(tmp_path)
    write_cache(
        state,
        observed_at=iso(clock, 10),
        last_attempt_at=iso(clock, 10),
        windows=future_windows(clock),
    )
    quota = read_quota_cache(state / CACHE_FILENAME, now=clock())
    assert quota["available"] is True
    assert quota["stale"] is False
    assert quota["age_seconds"] == 10
    assert quota["observed_at"] == iso(clock, 10)
    assert "reason" not in quota
    assert quota["five_hour"]["used_percent"] == 20.0
    assert quota["five_hour"]["remaining_percent"] == 80.0
    assert isinstance(quota["five_hour"]["resets_at"], str)
    assert quota["seven_day"]["used_percent"] == 10.0
    assert quota["seven_day"]["remaining_percent"] == 90.0


def test_stale_cache_keeps_windows_and_marks_stale(tmp_path, clock):
    state = make_state(tmp_path)
    write_cache(
        state,
        observed_at=iso(clock, 400),
        last_attempt_at=iso(clock, 400),
        windows=future_windows(clock),
    )
    quota = read_quota_cache(state / CACHE_FILENAME, now=clock())
    assert quota["available"] is True
    assert quota["stale"] is True
    assert quota["age_seconds"] == 400
    assert "reason" not in quota
    assert quota["five_hour"]["used_percent"] == 20.0


def test_failed_attempt_with_live_windows_reports_reason_and_stale(
    tmp_path, clock
):
    state = make_state(tmp_path)
    write_cache(
        state,
        observed_at=iso(clock, 100),
        last_attempt_at=iso(clock, 10),
        last_attempt_status="probe_rate_limited",
        windows=future_windows(clock),
    )
    quota = read_quota_cache(state / CACHE_FILENAME, now=clock())
    assert quota["available"] is True
    assert quota["stale"] is True
    assert quota["reason"] == "probe_rate_limited"
    assert quota["five_hour"]["used_percent"] == 20.0


def test_expired_cache_keeps_metadata_but_drops_windows(tmp_path, clock):
    state = make_state(tmp_path)
    write_cache(
        state,
        observed_at=iso(clock, 4000),
        last_attempt_at=iso(clock, 4000),
        windows=future_windows(clock),
    )
    quota = read_quota_cache(state / CACHE_FILENAME, now=clock())
    assert quota["available"] is False
    assert quota["reason"] == "quota_cache_expired"
    assert quota["stale"] is True
    assert quota["age_seconds"] == 4000
    assert "five_hour" not in quota
    assert "seven_day" not in quota


def test_all_windows_expired_reports_distinct_reason(tmp_path, clock):
    state = make_state(tmp_path)
    past = int(clock().timestamp()) - 60
    write_cache(
        state,
        observed_at=iso(clock, 10),
        last_attempt_at=iso(clock, 10),
        windows={
            "five_hour": {"utilization": 0.2, "resets_at": past},
            "seven_day": {"utilization": 0.1, "resets_at": past},
        },
    )
    quota = read_quota_cache(state / CACHE_FILENAME, now=clock())
    assert quota["available"] is False
    assert quota["reason"] == "quota_windows_expired"
    assert quota["observed_at"] == iso(clock, 10)


def test_failed_attempt_without_observation_reports_reason(tmp_path, clock):
    state = make_state(tmp_path)
    write_cache(
        state,
        observed_at=None,
        last_attempt_at=iso(clock, 10),
        last_attempt_status="probe_timeout",
    )
    quota = read_quota_cache(state / CACHE_FILENAME, now=clock())
    assert quota == {
        "available": False,
        "source": SOURCE,
        "reason": "probe_timeout",
        "manual_check_url": MANUAL_CHECK_URL,
    }


def test_ok_attempt_without_observation_is_unavailable_not_zero(
    tmp_path, clock
):
    state = make_state(tmp_path)
    write_cache(
        state,
        observed_at=None,
        last_attempt_at=iso(clock, 10),
        last_attempt_status="ok",
    )
    quota = read_quota_cache(state / CACHE_FILENAME, now=clock())
    assert quota["available"] is False
    assert quota["reason"] == "quota_observation_unavailable"


def test_attempt_timestamp_in_future_invalidates_cache(tmp_path, clock):
    state = make_state(tmp_path)
    write_cache(
        state,
        observed_at=iso(clock, -60),
        last_attempt_at=iso(clock, -60),
        windows=future_windows(clock),
    )
    quota = read_quota_cache(state / CACHE_FILENAME, now=clock())
    assert quota["available"] is False
    assert quota["reason"] == "quota_cache_invalid"


def test_observation_after_attempt_invalidates_cache(tmp_path, clock):
    state = make_state(tmp_path)
    write_cache(
        state,
        observed_at=iso(clock, 10),
        last_attempt_at=iso(clock, 20),
        windows=future_windows(clock),
    )
    quota = read_quota_cache(state / CACHE_FILENAME, now=clock())
    assert quota["available"] is False
    assert quota["reason"] == "quota_cache_invalid"


def test_every_declared_reason_is_public(tmp_path, clock):
    assert REASONS == frozenset({
        "quota_cache_unavailable", "quota_cache_invalid", "quota_cache_expired",
        "quota_windows_expired", "quota_observation_unavailable",
        "oauth_unavailable", "oauth_invalid", "oauth_expired",
        "probe_result_unknown", "probe_timeout", "probe_network_error",
        "probe_auth_rejected", "probe_rate_limited", "probe_http_error",
        "probe_headers_missing", "probe_headers_invalid",
    })
    for reason in sorted(REASONS):
        state = tmp_path / reason
        state.mkdir(mode=0o700)
        state.chmod(0o700)
        write_cache(
            state,
            observed_at=None,
            last_attempt_at=iso(clock, 5),
            last_attempt_status=reason,
        )
        quota = read_quota_cache(state / CACHE_FILENAME, now=clock())
        assert quota["reason"] == reason
        assert quota["source"] == SOURCE
