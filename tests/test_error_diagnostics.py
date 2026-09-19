from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from claude_research_mcp.executor import (
    ExecutionError,
    ClaudeResultError,
    classify_claude_error,
    run_command,
)
from claude_research_mcp.server import parse_claude_result


SENSITIVE_MARKERS = (
    "sk-ant-secret",
    "SECRET-PROMPT",
    "https://secret.example/path",
    "/home/rapidhere/secret",
    "01935555-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
)


def sanitized_error(payload: object | None = None, stderr: str = "") -> str:
    stdout = "" if payload is None else json.dumps(payload, ensure_ascii=False)
    return str(ClaudeResultError(*classify_claude_error(stdout, stderr)))


@pytest.mark.parametrize(
    ("payload", "category", "status"),
    [
        (
            {"error": {"type": "authentication_error", "message": "secret token"}},
            "auth",
            None,
        ),
        (
            {"error": {"type": "rate_limit_error", "status": 429}},
            "rate_limit",
            429,
        ),
        (
            {"type": "error", "subtype": "error_during_response", "result": "secret"},
            "runtime",
            None,
        ),
        (
            {"error": {"name": "OverloadedError", "status_code": "503"}},
            "runtime",
            503,
        ),
        (
            {"is_error": True, "result": "Authentication error"},
            "auth",
            None,
        ),
        (
            {
                "is_error": True,
                "errors": [
                    {"message": "ignore me"},
                    {"type": "rate_limit_error", "message": "sk-ant-secret"},
                ],
            },
            "rate_limit",
            None,
        ),
    ],
)
def test_structured_stdout_errors_are_classified(
    payload: object, category: str, status: int | None
) -> None:
    assert classify_claude_error(json.dumps(payload)) == (category, status)
    expected = "Claude CLI returned a sanitized error "
    expected += f"(category: {category}"
    if status is not None:
        expected += f", http_status: {status}"
    assert sanitized_error(payload) == expected + ")"


@pytest.mark.parametrize(
    ("stderr", "category"),
    [
        ("error: unknown option --secret", "invalid_argument"),
        ("TypeError: fetch failed for https://secret.example", "network"),
    ],
)
def test_stderr_errors_are_classified_without_echoing_diagnostics(
    stderr: str, category: str
) -> None:
    message = sanitized_error(stderr=stderr)
    assert f"category: {category}" in message
    assert "secret.example" not in message


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        {"error": {"type": {"nested": "authentication_error"}}},
        {"error": {"type": ["authentication_error"]}},
        {"error": {"status": True}},
        {"error": {"status": "authentication_error"}},
        {"error": {"message": "please talk about authentication and rate limit"}},
        {"error": {"status": "401authentication"}},
        {"error": {"status": 12345}},
        {"error": {"status": "１２３"}},
        {"error": {"status": "9" * 4096}},
        {"nested": {"error": {"type": "authentication_error"}}},
    ],
)
def test_malformed_and_unrecognized_structures_stay_unknown(payload: object) -> None:
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    assert classify_claude_error(stdout) == ("unknown", None)


def test_overlong_error_json_is_not_parsed() -> None:
    payload = {"error": {"type": "authentication_error"}}
    stdout = json.dumps(payload) + " " + ("x" * 16_384)

    assert classify_claude_error(stdout) == ("unknown", None)


def test_only_allowlisted_http_statuses_are_returned() -> None:
    for raw_status in (12345, "12345", "１２３", "9" * 4096, True):
        payload = {"is_error": True, "result": "runtime failure", "error": {}}
        if raw_status is not True:
            payload["error"]["status"] = raw_status
        category, status = classify_claude_error(json.dumps(payload))

        assert status is None
        assert category in ("runtime", "unknown")
        assert "12345" not in str(ClaudeResultError(category, status))


def test_deeply_nested_json_is_rejected_as_unknown() -> None:
    stdout = "[" * 20_000 + "]" * 20_000

    assert classify_claude_error(stdout) == ("unknown", None)


def test_parse_claude_result_rejects_deeply_nested_json_safely() -> None:
    stdout = "[" * 20_000 + "]" * 20_000

    with pytest.raises(ExecutionError, match="invalid JSON"):
        parse_claude_result(stdout)


def test_generic_result_error_does_not_override_specific_http_evidence() -> None:
    payload = {
        "is_error": True,
        "subtype": "error_during_response",
        "error": {"status": 429},
        "result": "api error",
    }

    assert classify_claude_error(json.dumps(payload)) == ("rate_limit", 429)


def test_error_envelope_result_and_errors_are_bounded_and_precise() -> None:
    payload = {
        "is_error": True,
        "result": "SECRET-PROMPT unrelated prose",
        "errors": [
            {"message": "x" * 2049},
            {"message": "Rate limit exceeded"},
        ],
    }

    assert classify_claude_error(json.dumps(payload)) == ("rate_limit", None)


def test_error_envelope_accepts_common_bounded_error_phrases() -> None:
    payloads = (
        {
            "is_error": True,
            "result": "API Error: 401 authentication_error sk-ant-secret",
        },
        {
            "is_error": True,
            "result": "OAuth token expired; visit https://secret.example/path",
        },
        {
            "is_error": True,
            "errors": ["You've hit your limit. SECRET-PROMPT"],
        },
        {
            "is_error": True,
            "errors": [{"message": "usage limit reached: 12345"}],
        },
    )

    categories = [classify_claude_error(json.dumps(payload))[0] for payload in payloads]
    messages = [sanitized_error(payload) for payload in payloads]

    assert categories == ["auth", "auth", "rate_limit", "rate_limit"]
    assert not any(marker in message for message in messages for marker in SENSITIVE_MARKERS)
    assert not any("12345" in message for message in messages)


def test_success_result_is_never_used_as_error_evidence() -> None:
    payload = {"result": "rate limit exceeded and authentication_error", "modelUsage": {}}

    parsed = parse_claude_result(json.dumps(payload))

    assert parsed["result"] == "rate limit exceeded and authentication_error"


def test_sensitive_error_fields_are_never_returned() -> None:
    payload = {
        "is_error": True,
        "result": "SECRET-PROMPT",
        "session_id": "01935555-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "error": {
            "type": "authentication_error",
            "message": "sk-ant-secret https://secret.example/path",
        },
    }
    message = sanitized_error(payload)

    assert "category: auth" in message
    assert not any(marker in message for marker in SENSITIVE_MARKERS)


def test_success_payload_prose_is_not_error_classified() -> None:
    payload = {"result": "authentication and rate limit are unrelated prose"}

    parsed = parse_claude_result(json.dumps(payload))

    assert parsed["result"] == "authentication and rate limit are unrelated prose"


def test_exit_zero_is_error_is_failure_not_success() -> None:
    payload = {
        "is_error": True,
        "result": "SECRET-PROMPT",
        "error": {"type": "rate_limit_error"},
    }

    with pytest.raises(ClaudeResultError) as caught:
        parse_claude_result(json.dumps(payload))

    assert caught.value.category == "rate_limit"
    assert caught.value.http_status is None
    assert not any(marker in str(caught.value) for marker in SENSITIVE_MARKERS)


def test_run_command_prefers_stdout_json_and_preserves_exit_code(
    tmp_path: Path,
) -> None:
    script = (
        "import json, sys; "
        "print(json.dumps({'error': {'type': 'authentication_error', "
        "'message': 'sk-ant-secret'}})); "
        "print('connection refused', file=sys.stderr); sys.exit(9)"
    )

    with pytest.raises(ClaudeResultError) as caught:
        asyncio.run(
            run_command(
                ["python3", "-c", script],
                cwd=tmp_path,
                timeout_seconds=2,
                output_limit=1000,
            )
        )

    assert caught.value.category == "auth"
    assert caught.value.exit_code == 9
    assert str(caught.value) == (
        "Claude CLI failed with exit code 9: Claude CLI returned a sanitized error "
        "(category: auth)"
    )
    assert "sk-ant-secret" not in str(caught.value)
    assert "connection refused" not in str(caught.value)


def test_exception_constructor_also_enforces_output_allowlist() -> None:
    error = ClaudeResultError("SECRET-PROMPT", "sk-ant-secret", "private-path")
    assert str(error) == "Claude CLI returned a sanitized error (category: unknown)"
    assert error.http_status is None
    assert error.exit_code is None


def test_oversized_integer_and_deep_json_fail_closed() -> None:
    for raw in ('{"error":{"status":' + "9" * 5000 + '}}', "[" * 1500 + "]" * 1500):
        assert classify_claude_error(raw) == ("unknown", None)
        with pytest.raises(ExecutionError, match="invalid JSON"):
            parse_claude_result(raw)


def test_structured_evidence_takes_precedence_over_error_prose() -> None:
    payload = {
        "is_error": True,
        "error": {"status": 401, "type": "api_error"},
        "result": "You've hit your limit",
    }
    assert classify_claude_error(json.dumps(payload)) == ("auth", 401)
    payload["error"] = {"type": "authentication_error", "message": "rate limit reached"}
    assert classify_claude_error(json.dumps(payload)) == ("auth", None)


@pytest.mark.parametrize("phrase", ["forbidden", "Access denied", "permission_error", "Request not allowed"])
def test_cli_result_rejection_is_permission_not_inferred_http_status(phrase: str) -> None:
    payload = {"type": "result", "subtype": "success", "is_error": True,
               "result": "API Error: 403 " + phrase + " SECRET-PROMPT"}
    with pytest.raises(ClaudeResultError) as caught:
        parse_claude_result(json.dumps(payload))
    assert caught.value.category == "permission"
    assert caught.value.http_status is None
    assert "SECRET-PROMPT" not in str(caught.value)
    payload["is_error"] = False
    assert parse_claude_result(json.dumps(payload))["result"] == payload["result"]


@pytest.mark.parametrize("apostrophe", ["\u2019", "\u2018", "\u02bc", "\uff07"])
def test_typographic_cli_limit_notice_is_sanitized(apostrophe: str) -> None:
    payload = {"is_error": True, "subtype": "success",
               "result": f"You{apostrophe}ve\u00a0hit your limit · resets PRIVATE-RESET"}
    with pytest.raises(ClaudeResultError) as caught:
        parse_claude_result(json.dumps(payload))
    assert caught.value.category == "rate_limit"
    assert caught.value.http_status is None
    assert "PRIVATE-RESET" not in str(caught.value)
