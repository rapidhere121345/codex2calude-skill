from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import stat
import textwrap

import pytest

from gemini_research_mcp import server as server_module
from gemini_research_mcp.config import (
    DEFAULT_REQUIRED_CLI_VERSION,
    ModelPolicy,
    Settings,
)
from gemini_research_mcp.executor import ExecutionError, GeminiResultError
from gemini_research_mcp.persistence import (
    SessionOwnershipError,
    count_audit_texts,
    get_conversation,
    initialize,
)
from gemini_research_mcp.server import (
    _assert_requested_family,
    _read_transcript,
    create_app,
    parse_gemini_result,
)


def _settings(binary: Path, principal: str = "owner") -> Settings:
    work = binary.parent / "work"
    work.mkdir(exist_ok=True)
    return Settings(
        work_root=work,
        principal=principal,
        allowed_principals=("owner", "partner"),
        gemini_bin=binary,
        database_path=work / "state" / "mcp.sqlite3",
        lock_dir=work / "state" / "locks",
        models={
            "auto": ModelPolicy(
                logical_name="auto",
                cli_value="auto",
                family="gemini",
                min_cli_version=DEFAULT_REQUIRED_CLI_VERSION,
            )
        },
        prompt_limit=20_000,
        continue_burst_limit=50,
        continue_burst_window_seconds=60,
        concurrency=1,
        output_limit=100_000,
        result_limit=100_000,
        required_cli_version=DEFAULT_REQUIRED_CLI_VERSION,
    )


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def get_tool(app, name: str):
    return app._tool_manager._tools[name].fn


def test_parse_gemini_json_stats_and_nested_tokens() -> None:
    parsed = parse_gemini_result(
        json.dumps(
            {
                "session_id": "session-12345678",
                "response": "answer",
                "stats": {
                    "models": {
                        "gemini-2.5-pro": {
                            "tokens": {
                                "input": 10,
                                "output": 20,
                                "total": 30,
                                "cached": 4,
                            }
                        }
                    }
                },
            }
        )
    )

    assert parsed["result"] == "answer"
    assert parsed["session_id"] == "session-12345678"
    assert parsed["actual_models"] == ["gemini-2.5-pro"]
    assert parsed["model_usage"] == [
        {
            "model": "gemini-2.5-pro",
            "input_tokens": 10,
            "output_tokens": 20,
            "total_tokens": 30,
            "cached_tokens": 4,
        }
    ]
    assert _assert_requested_family(parsed, "gemini") == ["gemini-2.5-pro"]


def test_parse_official_antigravity_frames_normalize_usage() -> None:
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "event": "init",
                    "conversation_id": "native-secret-init",
                    "init": {"tools": ["search_web"]},
                }
            ),
            json.dumps({"event": "step_update", "step": {"text": "working"}}),
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": "01234567-89ab-cdef-0123-456789abcdef",
                        "status": "SUCCESS",
                        "response": "answer",
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 2,
                            "thinking_tokens": 3,
                            "cache_read_tokens": 4,
                            "total_tokens": 10,
                        },
                        "num_turns": 2,
                    },
                }
            ),
        ]
    )

    parsed = parse_gemini_result(stdout)

    assert parsed["result"] == "answer"
    assert parsed["session_id"] == "01234567-89ab-cdef-0123-456789abcdef"
    assert parsed["actual_models"] == []
    assert parsed["model_usage"] == [
        {
            "input_tokens": 1,
            "output_tokens": 2,
            "thinking_tokens": 3,
            "cached_tokens": 4,
            "total_tokens": 10,
        }
    ]
    assert _assert_requested_family(parsed, "gemini") == []


@pytest.mark.parametrize(
    "stdout",
    [
        "{broken\n",
        json.dumps({"event": "init", "init": {}}) + "\n",
        json.dumps({"event": "result", "result": {"response": "one"}})
        + "\n"
        + json.dumps({"event": "result", "result": {"response": "two"}})
        + "\n",
        json.dumps({"event": "result", "result": "not-an-object"}) + "\n",
    ],
)
def test_invalid_missing_duplicate_and_invalid_result_frames_fail(stdout: str) -> None:
    with pytest.raises(ExecutionError):
        parse_gemini_result(stdout)


@pytest.mark.parametrize(
    ("payload", "category"),
    [
        (
            {
                "event": "result",
                "result": {
                    "status": "FAILURE",
                    "failure": "secret failure detail",
                },
            },
            "runtime",
        ),
        (
            {
                "event": "result",
                "result": {"denied_actions": ["shell(secret path)"]},
            },
            "runtime",
        ),
    ],
)
def test_official_failures_are_sanitized(payload: dict, category: str) -> None:
    with pytest.raises(GeminiResultError) as caught:
        parse_gemini_result(json.dumps(payload))

    assert caught.value.category == category
    assert "secret" not in str(caught.value)


def test_parse_gemini_error_is_sanitized() -> None:
    with pytest.raises(GeminiResultError) as caught:
        parse_gemini_result(
            json.dumps(
                {
                    "error": {
                        "type": "FatalAuthenticationError",
                        "message": "secret token must not escape",
                    }
                }
            )
        )
    assert caught.value.category == "auth"
    assert "secret token" not in str(caught.value)


def test_read_gemini_chat_transcript_from_tmp_chats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ".gemini"
    chats = home / "tmp" / "project-hash" / "chats"
    chats.mkdir(parents=True)
    (chats / "session.json").write_text(
        json.dumps(
            {
                "sessionId": "session-12345678",
                "messages": [
                    {
                        "id": "user-1",
                        "type": "user",
                        "timestamp": "2026-10-01T00:00:00Z",
                        "displayContent": "question",
                    },
                    {
                        "id": "gemini-1",
                        "type": "gemini",
                        "timestamp": "2026-10-01T00:00:01Z",
                        "displayContent": [{"text": "answer"}],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(server_module, "_gemini_home", lambda: home)

    messages = _read_transcript(tmp_path, "session-12345678")

    assert messages == [
        {
            "role": "user",
            "timestamp": "2026-10-01T00:00:00Z",
            "content": "question",
        },
        {
            "role": "assistant",
            "timestamp": "2026-10-01T00:00:01Z",
            "content": "answer",
        },
    ]


def _fake_agy(path: Path, *, authenticated: bool, mode: str = "ok") -> Path:
    usage = (
        json.dumps(
            {"status": "SUCCESS", "command": "/usage", "result": {"plans": []}}
        )
        if authenticated
        else json.dumps(
            {
                "status": "ERROR",
                "error": "secret-account is not authenticated",
                "conversation_id": "",
                "num_turns": 0,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "thinking_tokens": 0,
                    "cache_read_tokens": 0,
                    "total_tokens": 0,
                },
            }
        )
    )
    return _write_executable(
        path,
        f'''
        #!/usr/bin/env python3
        import json, os, sys, time
        from pathlib import Path
        if "--version" in sys.argv:
            print("1.2.14")
            raise SystemExit(0)
        if "/usage" in sys.argv:
            print({json.dumps(usage)})
            raise SystemExit({0 if authenticated else 1})
        init = {{"event": "init", "conversation_id": "private-native-id", "init": {{"tools": ["search_web", "read_url_content", "ask_permission"]}}}}
        print(json.dumps(init), flush=True)
        first = sys.stdin.readline()
        Path(__file__).with_name("stdin.jsonl").write_text(first)
        frames = [
            {{"event": "step_update", "step": "working"}},
        ]
        if {json.dumps(mode)} == "fail":
            frames.append({{"event": "result", "result": {{"status": "FAILURE", "failure": "secret failure"}}}})
        elif {json.dumps(mode)} == "denied":
            frames.append({{"event": "result", "result": {{"status": "SUCCESS", "response": "bad", "denied_actions": ["shell(secret)"]}}}})
        else:
            result = {{"conversation_id": "01234567-89ab-cdef-0123-456789abcdef", "status": "SUCCESS", "response": "answer "+first, "usage": {{"input_tokens": 1, "output_tokens": 2, "thinking_tokens": 3, "cache_read_tokens": 4, "total_tokens": 10}}, "num_turns": 1}}
            frames.append({{"event": "result", "result": result}})
        for frame in frames:
            print(json.dumps(frame), flush=True)
        sys.stdin.read()
        ''',
    )


@pytest.mark.parametrize("authenticated", [False, True])
def test_status_usage_true_false_and_version(tmp_path: Path, authenticated: bool) -> None:
    binary = _fake_agy(tmp_path / "agy", authenticated=authenticated)
    app = create_app(_settings(binary))

    status = asyncio.run(get_tool(app, "gemini_status")())

    assert status["status"] == "ok"
    assert status["cli_name"] == "antigravity"
    assert status["cli_version"] == "1.2.14"
    assert status["gemini_version"] == "1.2.14"
    assert status["cli_compatible"] is True
    assert status["models"]["auto"]["cli_supported"] is True
    assert status["auth"]["logged_in"] is authenticated
    assert status["quota"]["available"] is False
    serialized = json.dumps(status)
    assert "secret-account" not in serialized
    assert "private-native-id" not in serialized


def test_five_tools_start_continue_export_close_and_isolation(tmp_path: Path) -> None:
    binary = _fake_agy(tmp_path / "agy", authenticated=True)
    settings = _settings(binary)
    app = create_app(settings)
    tools = [tool.name for tool in asyncio.run(app.list_tools())]
    assert tools == [
        "gemini_status",
        "gemini_once",
        "gemini_start",
        "gemini_continue",
        "gemini_close",
        "gemini_list_conversations",
        "gemini_export_conversation",
    ]

    started = asyncio.run(
        get_tool(app, "gemini_start")("start secret prompt", title="research")
    )
    conversation = started["conversation_id"]
    assert started["result"].startswith("answer ")
    assert started["model_usage"][0]["thinking_tokens"] == 3
    assert started["model_usage"][0]["cached_tokens"] == 4
    assert "private-native-id" not in json.dumps(started)
    stdin_event = json.loads((tmp_path / "stdin.jsonl").read_text())
    assert stdin_event == {
        "event": "user",
        "message": {"content": "start secret prompt"},
    }

    continued = asyncio.run(
        get_tool(app, "gemini_continue")(conversation, "continue secret prompt")
    )
    assert continued["conversation_id"] == conversation
    assert continued["result"].startswith("answer ")

    exported = asyncio.run(
        get_tool(app, "gemini_export_conversation")(conversation, format="json")
    )
    assert exported["total_message_count"] == 4
    assert [message["role"] for message in exported["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert all("secret prompt" in message["content"] for message in exported["messages"] if message["role"] == "user")

    partner_app = create_app(_settings(binary, principal="partner"))
    with pytest.raises(ExecutionError, match="conversation is unavailable"):
        asyncio.run(get_tool(partner_app, "gemini_continue")(conversation, "steal"))
    with pytest.raises(ExecutionError, match="conversation is unavailable"):
        asyncio.run(get_tool(partner_app, "gemini_export_conversation")(conversation))
    with pytest.raises(SessionOwnershipError):
        get_conversation(settings.database_path, conversation, "partner")

    closed = asyncio.run(get_tool(app, "gemini_close")(conversation))
    assert closed["status"] == "closed"
    assert closed["transcript_deleted"] is False
    with pytest.raises(ExecutionError, match="conversation is closed"):
        asyncio.run(get_tool(app, "gemini_continue")(conversation, "after close"))

    closed_export = asyncio.run(
        get_tool(app, "gemini_export_conversation")(conversation)
    )
    assert closed_export["conversation_status"] == "closed"
    assert closed_export["total_message_count"] == 4

    with sqlite3.connect(settings.database_path) as connection:
        audit_rows = connection.execute(
            "SELECT principal, tool, conversation_id, model, status, duration_ms, "
            "error_category, http_status, exit_code, request_id, raw_log_saved "
            "FROM audit_log"
        ).fetchall()
    assert audit_rows
    assert count_audit_texts(settings.database_path) == len(audit_rows)
    flattened = json.dumps([list(row) for row in audit_rows])
    assert "secret prompt" not in flattened
    assert "answer " not in flattened


@pytest.mark.parametrize("mode", ["fail", "denied"])
def test_start_sanitizes_official_failure_and_denied_actions(
    tmp_path: Path, mode: str
) -> None:
    binary = _fake_agy(tmp_path / "agy", authenticated=True, mode=mode)
    app = create_app(_settings(binary))

    with pytest.raises(ExecutionError) as caught:
        asyncio.run(get_tool(app, "gemini_start")("secret prompt", title="bad"))

    assert "secret" not in str(caught.value)
    assert "shell(secret)" not in str(caught.value)


def test_history_export_is_capped_at_500_messages(tmp_path: Path) -> None:
    binary = _fake_agy(tmp_path / "agy", authenticated=True)
    settings = _settings(binary)
    app = create_app(settings)
    conversation_id = "01935555-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    from gemini_research_mcp.persistence import create_conversation

    create_conversation(
        settings.database_path,
        "owner",
        "auto",
        False,
        "bulk",
    )
    # The helper-generated UUID differs; use its returned mapping.
    rows: list[tuple[str, str]] = []
    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute(
            "SELECT conversation_id FROM conversations WHERE principal = 'owner'"
        ).fetchone()
        owned = row[0]
        for index in range(501):
            rows.append((f"u{index}", f"a{index}"))
        connection.executemany(
            "INSERT INTO conversation_history "
            "(conversation_id, principal, request_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (owned, "owner", f"request-{index}-{role}", role, content, "2026-01-01T00:00:00+00:00")
                for index, (prompt, response) in enumerate(rows)
                for role, content in (("user", prompt), ("assistant", response))
            ],
        )
    del conversation_id

    exported = asyncio.run(
        get_tool(app, "gemini_export_conversation")(owned, format="json")
    )

    assert exported["total_message_count"] == 1002
    assert exported["message_count"] == 500
    assert exported["message_truncated"] is True
    assert exported["truncated"] is True
