from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
import textwrap
import time

import pytest

from claude_research_mcp import server as server_module
from claude_research_mcp.config import (
    ConfigurationError,
    load_settings,
    validate_conversation_id,
    validate_model,
    validate_prompt,
)
from claude_research_mcp.executor import (
    ExecutionError,
    ClaudeResultError,
    CommandTimeoutError,
    build_auth_status_command,
    build_ask_command,
    build_version_command,
    child_environment,
    run_command,
    truncate_text,
    validate_options,
)
from claude_research_mcp.locking import concurrency_slot
from claude_research_mcp.persistence import (
    ContinueBurstLimitError,
    SessionOwnershipError,
    close_conversation,
    create_conversation,
    get_conversation,
    initialize,
    record_audit_event,
    reserve_call,
)
from claude_research_mcp.server import INSTRUCTIONS, create_app, parse_claude_result


TOOLS = [
    "claude_status",
    "claude_once",
    "claude_start",
    "claude_continue",
    "claude_close",
    "claude_list_conversations",
    "claude_export_conversation",
]


def test_child_environment_is_minimal_and_disables_nonessential_traffic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/srv/claude")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("CLAUDE_MCP_STATUSLINE_OBSERVATION", "/state/quota.json")
    monkeypatch.setenv("SSH_CLIENT", "203.0.113.10 12345 22022")
    monkeypatch.setenv("SSH_CONNECTION", "203.0.113.10 12345 10.0.0.1 22022")
    monkeypatch.setenv("XDG_SESSION_ID", "remote-session")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-pass")

    result = child_environment()

    assert result == {
        "HOME": "/srv/claude",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "CLAUDE_MCP_STATUSLINE_OBSERVATION": "/state/quota.json",
        "NO_COLOR": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_UPDATES": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_FEEDBACK_COMMAND": "1",
        "DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    }


def make_settings(tmp_path: Path, principal: str = "owner", **overrides):
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    env = {
        "CLAUDE_MCP_WORK_ROOT": str(work),
        "CLAUDE_MCP_PRINCIPAL": principal,
        "CLAUDE_BIN": str(binary),
    }
    env.update({key: str(value) for key, value in overrides.items()})
    return load_settings(env)


def fake_claude(
    path: Path,
    *,
    fail: bool = False,
    session: str = "claude-session-123",
    logged_in: bool = True,
    auth_method: str = "claude.ai",
) -> None:
    mode = "fail" if fail else "ok"
    output = json.dumps(
        {
            "result": "answer",
            "sessionId": session,
            "modelUsage": {
                "claude-haiku-4-5": {"input_tokens": 2, "output_tokens": 3},
                "claude-opus-5": {"input_tokens": 11, "output_tokens": 22},
            },
        }
    )
    auth_output = json.dumps(
        {
            "loggedIn": logged_in,
            "authMethod": auth_method,
            "subscriptionType": "pro",
            "email": "secret@example.com",
            "orgId": "secret-org",
        },
        separators=(",", ":"),
    )
    # Drain the test pipe before immediate shell responses: these fixtures test
    # response classification, not a child exiting before it accepts stdin.
    body = (
        "#!/bin/sh\n"
        "if [ \"$1\" = --version ]; then cat >/dev/null; printf '2.1.236\\n'; exit 0; fi\n"
        "if [ \"$1\" = auth ] && [ \"$2\" = status ]; then\n"
        "  cat >/dev/null\n"
        f"  printf '%s\\n' '{auth_output}'\n"
        "  exit 0\n"
        "fi\n"
        "prompt=$(cat)\n"
        f"printf '%s\\n' \"$@\" > {path}.argv\n"
        f"printf '%s' \"$prompt\" > {path}.stdin\n"
        f"if [ \"{mode}\" = fail ]; then\n"
        "  printf 'ERROR secret@example.com /home/secret %s\\n' \"$prompt\" >&2\n"
        "  exit 7\n"
        "fi\n"
        f"printf '%s\\n' {json.dumps(output)}\n"
    )
    path.write_text(body)
    path.chmod(0o755)


def get_tool(app, name):
    return app._tool_manager._tools[name].fn


def test_configuration_and_input_validation(tmp_path):
    settings = make_settings(tmp_path, CLAUDE_MCP_PROMPT_LIMIT="10")
    assert settings.principal == "owner"
    assert list(settings.models) == ["opus"]
    assert settings.continue_burst_limit == 50
    assert settings.continue_burst_window_seconds == 60
    minimum_burst = make_settings(
        tmp_path,
        CLAUDE_MCP_CONTINUE_BURST_LIMIT="1",
        CLAUDE_MCP_CONTINUE_BURST_WINDOW_SECONDS="1",
    )
    assert minimum_burst.continue_burst_limit == 1
    assert minimum_burst.continue_burst_window_seconds == 1
    maximum_burst = make_settings(
        tmp_path,
        CLAUDE_MCP_CONTINUE_BURST_LIMIT="100",
        CLAUDE_MCP_CONTINUE_BURST_WINDOW_SECONDS="3600",
    )
    assert maximum_burst.continue_burst_limit == 100
    assert maximum_burst.continue_burst_window_seconds == 3600
    assert validate_model(None, settings) == "opus"
    with pytest.raises(ConfigurationError, match="model is unavailable"):
        validate_model("fable", settings)
    with pytest.raises(ConfigurationError):
        validate_prompt("x" * 11, settings.prompt_limit)
    with pytest.raises(ConfigurationError):
        validate_conversation_id("not-a-uuid")
    with pytest.raises(ExecutionError):
        validate_options(model="opus", use_web="true", max_turns=1, timeout_seconds=1)
    with pytest.raises(ExecutionError):
        validate_options(model="opus", use_web=True, max_turns=0, timeout_seconds=1)

    fable = '{"opus":{"cli_value":"opus","family":"opus"},"fable":{"cli_value":"fable","family":"fable","min_cli_version":"2.1.257"}}'
    enabled = make_settings(tmp_path, CLAUDE_MCP_MODELS=fable)
    assert enabled.models["fable"].min_cli_version == (2, 1, 257)
    for name, value in (
        ("CLAUDE_MCP_CONTINUE_BURST_LIMIT", "0"),
        ("CLAUDE_MCP_CONTINUE_BURST_LIMIT", "101"),
        ("CLAUDE_MCP_CONTINUE_BURST_WINDOW_SECONDS", "0"),
        ("CLAUDE_MCP_CONTINUE_BURST_WINDOW_SECONDS", "3601"),
    ):
        with pytest.raises(ConfigurationError, match=f"invalid {name}"):
            make_settings(tmp_path, **{name: value})


def test_fixed_command_and_prompt_never_in_argv(tmp_path):
    settings = make_settings(tmp_path)
    command = build_ask_command(
        settings,
        resume_session_id="claude-session-123",
        model_cli_value="opus",
        use_web=True,
        max_turns=3,
    )
    assert "--safe-mode" in command
    assert "--disable-slash-commands" in command
    assert "--no-chrome" in command
    assert "--strict-mcp-config" in command
    assert command[command.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert command[command.index("--tools") + 1] == "WebSearch,WebFetch"
    assert command[command.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--resume") + 1] == "claude-session-123"
    assert "--dangerously-skip-permissions" not in command
    assert "SECRET PROMPT" not in " ".join(command)
    no_tools = build_ask_command(
        settings,
        resume_session_id=None,
        model_cli_value="opus",
        use_web=False,
        max_turns=1,
    )
    assert no_tools[no_tools.index("--tools") + 1] == ""
    assert "--allowedTools" not in no_tools
    assert build_version_command(settings.claude_bin)[-1] == "--version"
    assert build_auth_status_command(settings.claude_bin)[-2:] == ["auth", "status"]


def test_five_tools_once_start_continue_close_and_principal_isolation(tmp_path):
    settings = make_settings(tmp_path)
    fake_claude(settings.claude_bin)
    app = create_app(settings)
    assert [tool.name for tool in asyncio.run(app.list_tools())] == TOOLS
    assert len(INSTRUCTIONS) <= 512
    for phrase in ("owner/partner/partner2/partner3", "shared Claude subscription", "one session", "conversation_id", "queue", "verify"):
        assert phrase in INSTRUCTIONS

    once = asyncio.run(get_tool(app, "claude_once")("once secret prompt", model="opus", use_web=True))
    assert once["actual_models"] == ["claude-haiku-4-5", "claude-opus-5"]
    assert once["model_usage"][1] == {"model": "claude-opus-5", "input_tokens": 11, "output_tokens": 22}
    once_argv = Path(str(settings.claude_bin) + ".argv").read_text().splitlines()
    assert once_argv[once_argv.index("--max-turns") + 1] == "50"
    assert "once secret prompt" not in Path(str(settings.claude_bin) + ".argv").read_text()
    assert Path(str(settings.claude_bin) + ".stdin").read_text() == "once secret prompt"

    started = asyncio.run(get_tool(app, "claude_start")("start secret prompt", model="opus", use_web=False))
    conversation = started["conversation_id"]
    validate_conversation_id(conversation)
    assert started["use_web"] is False
    continued = asyncio.run(get_tool(app, "claude_continue")(conversation, "continue secret prompt"))
    assert continued["conversation_id"] == conversation
    assert continued["use_web"] is False
    argv = Path(str(settings.claude_bin) + ".argv").read_text().splitlines()
    assert argv[argv.index("--resume") + 1] == "claude-session-123"

    partner_settings = make_settings(tmp_path, principal="partner")
    partner_app = create_app(partner_settings)
    with pytest.raises(SessionOwnershipError):
        get_conversation(settings.database_path, conversation, "partner")
    with pytest.raises(ExecutionError):
        asyncio.run(get_tool(partner_app, "claude_continue")(conversation, "steal"))

    closed = asyncio.run(get_tool(app, "claude_close")(conversation))
    assert closed["status"] == "closed"
    assert closed["conversation_id"] == conversation
    assert closed["transcript_deleted"] is False
    validate_conversation_id(closed["request_id"])
    with pytest.raises(ExecutionError, match="conversation is closed"):
        asyncio.run(get_tool(app, "claude_continue")(conversation, "after close"))


def test_schema_migration_and_conversation_list_metadata(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE conversations ("
            "conversation_id TEXT PRIMARY KEY, principal TEXT NOT NULL, "
            "claude_session_id TEXT UNIQUE, model TEXT NOT NULL, "
            "use_web INTEGER NOT NULL, created_at TEXT NOT NULL, closed_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "principal TEXT NOT NULL, tool TEXT NOT NULL, conversation_id TEXT, "
            "model TEXT, status TEXT NOT NULL, duration_ms INTEGER, "
            "created_at TEXT NOT NULL, FOREIGN KEY(conversation_id) "
            "REFERENCES conversations(conversation_id))"
        )
        connection.execute(
            "CREATE INDEX audit_quota_window ON audit_log(principal, created_at)"
        )
        connection.execute(
            "INSERT INTO conversations VALUES "
            "(?, 'owner', NULL, 'opus', 1, ?, NULL)",
            ("01935555-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "2026-01-01T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO conversations VALUES "
            "(?, 'owner', NULL, 'opus', 0, ?, ?)",
            (
                "01935555-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "2026-01-02T00:00:00+00:00",
                "2026-01-03T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO audit_log "
            "(principal, tool, conversation_id, model, status, duration_ms, created_at) "
            "VALUES ('owner', 'claude_continue', ?, 'opus', 'success', 1, ?)",
            (
                "01935555-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "2026-01-05T00:00:00+00:00",
            ),
        )

    initialize(database)
    with sqlite3.connect(database) as connection:
        new_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(audit_log)")
        }
        legacy_values = connection.execute(
            "SELECT error_category, http_status, exit_code, request_id, "
            "raw_log_saved FROM audit_log"
        ).fetchall()
    assert {
        "error_category",
        "http_status",
        "exit_code",
        "request_id",
        "raw_log_saved",
    } <= new_columns
    assert legacy_values == [(None, None, None, None, None)]
    initialize(database)
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(conversations)")}
        migrated_activity = connection.execute(
            "SELECT conversation_id, last_activity_at FROM conversations "
            "ORDER BY conversation_id"
        ).fetchall()
        audit_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(audit_log)")
        }
    assert {"title", "last_activity_at"} <= columns
    assert "audit_quota_window" not in audit_indexes
    assert migrated_activity == [
        (
            "01935555-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "2026-01-05T00:00:00+00:00",
        ),
        (
            "01935555-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "2026-01-03T00:00:00+00:00",
        ),
    ]
    initialize(database)

    settings = make_settings(tmp_path)
    initialize(settings.database_path)
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "INSERT INTO conversations "
            "(conversation_id, principal, claude_session_id, model, use_web, "
            "created_at, closed_at, title, last_activity_at) VALUES "
            "(?, 'owner', NULL, 'opus', 1, ?, NULL, ?, ?)",
            (
                "01935555-cccc-4ccc-8ccc-cccccccccccc",
                "2026-02-01T00:00:00+00:00",
                "Open research",
                "2026-02-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO conversations "
            "(conversation_id, principal, claude_session_id, model, use_web, "
            "created_at, closed_at, title, last_activity_at) VALUES "
            "(?, 'partner', NULL, 'opus', 1, ?, NULL, 'Partner only', ?)",
            (
                "01935555-dddd-4ddd-8ddd-dddddddddddd",
                "2026-02-02T00:00:00+00:00",
                "2026-02-02T00:00:00+00:00",
            ),
        )

    app = create_app(settings)
    listed = asyncio.run(get_tool(app, "claude_list_conversations")())
    assert listed["count"] == 1
    assert listed["has_more"] is False
    item = listed["conversations"][0]
    assert item == {
        "conversation_id": "01935555-cccc-4ccc-8ccc-cccccccccccc",
        "title": "Open research",
        "model": "opus",
        "use_web": True,
        "created_at": "2026-02-01T00:00:00+00:00",
        "last_activity_at": "2026-02-01T00:00:00+00:00",
        "status": "open",
    }
    assert "claude_session_id" not in item

    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "INSERT INTO conversations "
            "(conversation_id, principal, claude_session_id, model, use_web, "
            "created_at, closed_at, title, last_activity_at) VALUES "
            "(?, 'owner', NULL, 'opus', 0, ?, ?, NULL, ?)",
            (
                "01935555-eeee-4eee-8eee-eeeeeeeeeeee",
                "2026-01-01T00:00:00+00:00",
                "2026-01-04T00:00:00+00:00",
                "2026-01-04T00:00:00+00:00",
            ),
        )
    first_page = asyncio.run(
        get_tool(app, "claude_list_conversations")(include_closed=True, limit=1)
    )
    assert first_page["count"] == 1
    assert first_page["total_count"] == 2
    assert first_page["next_offset"] == 1
    assert first_page["has_more"] is True
    closed_list = asyncio.run(
        get_tool(app, "claude_list_conversations")(include_closed=True, limit=2)
    )
    assert closed_list["count"] == 2
    assert closed_list["total_count"] == 2
    assert closed_list["next_offset"] is None
    assert closed_list["has_more"] is False
    assert {item["status"] for item in closed_list["conversations"]} == {"open", "closed"}

    with pytest.raises(ExecutionError, match="limit must"):
        asyncio.run(get_tool(app, "claude_list_conversations")(limit=0))


def test_audit_migration_is_idempotent_and_concurrent(tmp_path):
    database = tmp_path / "legacy-concurrent.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "principal TEXT NOT NULL, tool TEXT NOT NULL, conversation_id TEXT, "
            "model TEXT, status TEXT NOT NULL, duration_ms INTEGER, "
            "created_at TEXT NOT NULL)"
        )
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(initialize, database) for _ in range(24)]
        for future in futures:
            future.result()
    initialize(database)
    with sqlite3.connect(database) as connection:
        columns = {
            (row[1], row[2])
            for row in connection.execute("PRAGMA table_info(audit_log)")
        }
    assert {
        ("error_category", "TEXT"),
        ("http_status", "INTEGER"),
        ("exit_code", "INTEGER"),
        ("request_id", "TEXT"),
        ("raw_log_saved", "INTEGER"),
    } <= columns


def test_start_title_validation_and_audit_omits_prompt(tmp_path):
    settings = make_settings(tmp_path)
    fake_claude(settings.claude_bin)
    app = create_app(settings)
    started = asyncio.run(
        get_tool(app, "claude_start")(
            "start secret prompt", title="  Market check ", model="opus", use_web=False
        )
    )
    conversation = started["conversation_id"]
    record = get_conversation(settings.database_path, conversation, "owner")
    assert record.title == "Market check"
    assert started["title"] == "Market check"
    with pytest.raises(ExecutionError, match="title"):
        asyncio.run(get_tool(app, "claude_start")("x", title=""))
    with pytest.raises(ExecutionError, match="invalid character"):
        asyncio.run(get_tool(app, "claude_start")("x", title="bad\x00title"))
    with pytest.raises(ExecutionError, match="short human"):
        asyncio.run(get_tool(app, "claude_start")("x", title="x" * 121))

    with sqlite3.connect(settings.database_path) as connection:
        rows = connection.execute(
            "SELECT tool,conversation_id,model FROM audit_log"
        ).fetchall()
        values = connection.execute("SELECT title FROM conversations").fetchall()
    assert ("claude_start", conversation, "opus") in rows
    assert "start secret prompt" not in json.dumps(rows)
    assert values == [("Market check",)]


def _write_transcript(settings, claude_home, session, records, *, malformed=False):
    encoded = re.sub(r"[^A-Za-z0-9]", "-", str(settings.principal_work_dir))
    project_dir = claude_home / "projects" / encoded
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session}.jsonl"
    if malformed:
        path.write_bytes(b'{"broken":')
    else:
        path.write_text("".join(json.dumps(item) + "\n" for item in records))
    return path


def test_export_conversation_filtering_closed_and_isolation(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude-home"
    monkeypatch.setattr(server_module, "_claude_home", lambda: claude_home)
    settings = make_settings(tmp_path)
    fake_claude(settings.claude_bin)
    app = create_app(settings)
    started = asyncio.run(
        get_tool(app, "claude_start")("public research question", title="Export")
    )
    conversation = started["conversation_id"]
    session = "claude-session-123"
    records = [
        {"type": "queue", "content": "queue-operation"},
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "isMeta": True,
            "message": {"content": "metadata user"},
        },
        {
            "type": "user",
            "timestamp": "2026-01-01T00:01:00Z",
            "message": {"content": "Question /tmp/secret https://example.com/source"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:02:00Z",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "WebSearch", "input": {"q": "x"}},
                    {"type": "text", "text": "Answer https://claude.example/docs and ~/private/file"},
                ]
            },
        },
        {
            "type": "assistant",
            "isSidechain": True,
            "message": {"content": "sidechain"},
        },
        {
            "type": "system",
            "timestamp": "2026-01-01T00:02:00Z",
            "message": {"content": "system record"},
        },
    ]
    _write_transcript(settings, claude_home, session, records)

    activity_before = get_conversation(
        settings.database_path, conversation, "owner"
    ).last_activity_at
    exported = asyncio.run(
        get_tool(app, "claude_export_conversation")(conversation, format="json")
    )
    payload = json.dumps(exported)
    assert exported["message_count"] == 2
    assert exported["truncated"] is False
    assert exported["title"] == "Export"
    assert exported["conversation_status"] == "open"
    assert [item["role"] for item in exported["messages"]] == ["user", "assistant"]
    assert "queue-operation" not in payload
    assert "WebSearch" not in payload
    assert "system record" not in payload
    assert session not in payload
    assert "metadata user" not in payload
    assert "sidechain" not in payload
    assert "system record" not in payload
    assert exported["messages"][0]["content"] == (
        "Question /tmp/secret https://example.com/source"
    )
    assert exported["messages"][1]["content"] == (
        "Answer https://claude.example/docs and ~/private/file"
    )
    assert (
        get_conversation(settings.database_path, conversation, "owner").last_activity_at
        == activity_before
    )

    markdown = asyncio.run(
        get_tool(app, "claude_export_conversation")(conversation, format="markdown")
    )
    assert "## User" in markdown["content"]
    assert session not in json.dumps(markdown)

    asyncio.run(get_tool(app, "claude_close")(conversation))
    closed_export = asyncio.run(
        get_tool(app, "claude_export_conversation")(conversation)
    )
    assert closed_export["status"] == "ok"

    partner_settings = make_settings(tmp_path, principal="partner")
    partner_app = create_app(partner_settings)
    with pytest.raises(ExecutionError, match="unavailable"):
        asyncio.run(
            get_tool(partner_app, "claude_export_conversation")(conversation)
        )
    with sqlite3.connect(settings.database_path) as connection:
        partner_audit = connection.execute(
            "SELECT conversation_id FROM audit_log "
            "WHERE principal = 'partner' AND tool = 'claude_export_conversation'"
        ).fetchall()
    assert partner_audit == [(None,)]

    with pytest.raises(ExecutionError, match="format must"):
        asyncio.run(
            get_tool(app, "claude_export_conversation")(conversation, format="html")
        )


def test_export_rejects_malformed_oversized_and_path_surprises(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude-home"
    monkeypatch.setattr(server_module, "_claude_home", lambda: claude_home)
    monkeypatch.setattr(server_module, "_MAX_TRANSCRIPT_BYTES", 64)
    settings = make_settings(tmp_path)
    fake_claude(settings.claude_bin)
    app = create_app(settings)
    started = asyncio.run(get_tool(app, "claude_start")("x", title="Oversize"))
    conversation = started["conversation_id"]
    session = "claude-session-123"
    path = _write_transcript(settings, claude_home, session, [], malformed=True)
    with pytest.raises(ExecutionError, match="transcript is unavailable"):
        asyncio.run(get_tool(app, "claude_export_conversation")(conversation))

    path.unlink()
    path.write_bytes(b"x" * 65)
    with pytest.raises(ExecutionError, match="transcript is unavailable"):
        asyncio.run(get_tool(app, "claude_export_conversation")(conversation))

    path.unlink()
    outside = tmp_path / "outside.jsonl"
    outside.write_text(
        '{"type":"user","message":{"content":"leak"}}\n'
    )
    path.symlink_to(outside)
    with pytest.raises(ExecutionError, match="transcript is unavailable"):
        asyncio.run(get_tool(app, "claude_export_conversation")(conversation))


def test_no_hourly_bridge_quota_setting_or_twenty_call_limit(tmp_path):
    settings = make_settings(tmp_path)
    assert not hasattr(settings, "rate_limit")
    fake_claude(settings.claude_bin)
    app = create_app(settings)

    for index in range(25):
        assert asyncio.run(get_tool(app, "claude_once")(f"inference {index}"))["status"] == "ok"

    with sqlite3.connect(settings.database_path) as connection:
        rows = connection.execute(
            "SELECT tool, status FROM audit_log ORDER BY id"
        ).fetchall()
    assert rows == [("claude_once", "success")] * 25


def test_status_and_fable_version_gate(tmp_path, monkeypatch):
    settings = make_settings(
        tmp_path,
        CLAUDE_MCP_MODELS='{"opus":{"cli_value":"opus","family":"opus"},"fable":{"cli_value":"fable","family":"fable","min_cli_version":"2.1.257"}}',
    )
    fake_claude(settings.claude_bin)
    async def unavailable_probe(_settings):
        return server_module.usage_command_unavailable()

    monkeypatch.setattr(server_module, "_run_usage_probe", unavailable_probe)
    status = asyncio.run(get_tool(create_app(settings), "claude_status")())
    assert status["claude_version"] == "2.1.236"
    assert status["logged_in"] is True
    assert status["auth_method"] == "claude.ai"
    assert status["subscription_type"] == "pro"
    assert "email" not in status
    assert "org" not in json.dumps(status).lower()
    assert status["models"]["opus"]["cli_supported"] is True
    assert status["models"]["fable"] == {
        "cli_supported": False,
        "min_cli_version": "2.1.257",
    }


def test_status_includes_official_quota_observation_without_leaking_identity(
    tmp_path, monkeypatch
):
    settings = make_settings(tmp_path)
    fake_claude(settings.claude_bin)
    monkeypatch.setattr(
        server_module,
        "run_usage_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("secret probe failure")),
    )
    unavailable = asyncio.run(get_tool(create_app(settings), "claude_status")())
    assert unavailable["opus_quota"] == {
        "available": False,
        "source": "claude_code_usage_command",
        "reason": "usage_command_unavailable",
        "manual_check_url": "https://claude.ai/usage",
    }

    sample = {
        "available": True,
        "source": "claude_code_statusline",
        "observed_at": "2026-09-12T00:00:00+00:00",
        "stale": False,
        "age_seconds": 0,
        "five_hour": {
            "used_percent": 20.0,
            "remaining_percent": 80.0,
            "resets_at": "2026-09-12T05:00:00+00:00",
        },
        "seven_day": {
            "used_percent": 30.0,
            "remaining_percent": 70.0,
            "resets_at": "2026-09-16T00:00:00+00:00",
        },
    }
    monkeypatch.setattr(
        server_module,
        "get_opus_quota_status",
        lambda path: {
            **sample,
        },
    )
    app = create_app(settings)
    assert [tool.name for tool in asyncio.run(app.list_tools())] == TOOLS
    status = asyncio.run(get_tool(app, "claude_status")())
    assert status["opus_quota"]["five_hour"]["used_percent"] == 20.0
    rendered = json.dumps(status)
    assert "secret@example.com" not in rendered
    assert "secret-org" not in rendered
    assert "token" not in rendered.casefold()
    assert "credentials" not in rendered.casefold()
    assert "email" not in status
    assert "org" not in status

    monkeypatch.setattr(
        server_module,
        "get_opus_quota_status",
        lambda path: (_ for _ in ()).throw(RuntimeError("secret token")),
    )
    surviving = asyncio.run(get_tool(app, "claude_status")())
    assert surviving["status"] == "ok"
    assert surviving["opus_quota"]["available"] is False
    assert surviving["opus_quota"]["manual_check_url"] == "https://claude.ai/usage"


def test_status_does_not_report_quota_for_missing_login(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    fake_claude(settings.claude_bin, logged_in=False)
    monkeypatch.setattr(
        server_module,
        "get_opus_quota_status",
        lambda path: pytest.fail("quota status requires a Claude login"),
    )

    status = asyncio.run(get_tool(create_app(settings), "claude_status")())

    assert status["logged_in"] is False
    assert status["opus_quota"] == {
        "available": False,
        "source": "claude_code_statusline",
        "reason": "requires_claude_ai_login",
        "manual_check_url": "https://claude.ai/usage",
    }


def test_status_uses_official_usage_fallback_and_shared_cache(
    tmp_path, monkeypatch
):
    settings = make_settings(tmp_path)
    fake_claude(settings.claude_bin)
    state = settings.work_root / "state"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.chmod(0o700)
    now = datetime.now(timezone.utc)
    five_reset = now + timedelta(hours=2)
    seven_reset = now + timedelta(days=2)
    months = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )

    def clock(value):
        return value.strftime("%I:%M%p").lstrip("0").lower()

    screen = (
        "Current session 60% used Resets "
        f"{clock(five_reset)} (UTC) "
        "Current week (all models) 12% used Resets "
        f"{months[seven_reset.month - 1]} {seven_reset.day}, "
        f"{clock(seven_reset)} (UTC) secret@example.com"
    )
    calls = 0

    async def official_usage(_claude_bin, *, cwd):
        nonlocal calls
        calls += 1
        assert cwd == settings.principal_work_dir
        return screen

    monkeypatch.setattr(server_module, "run_usage_command", official_usage)
    status_tool = get_tool(create_app(settings), "claude_status")

    first = asyncio.run(status_tool())
    second = asyncio.run(status_tool())

    assert calls == 1
    assert first["opus_quota"]["source"] == "claude_code_usage_command"
    assert first["opus_quota"]["five_hour"]["used_percent"] == 60.0
    assert first["opus_quota"]["seven_day"]["used_percent"] == 12.0
    assert second["opus_quota"]["age_seconds"] >= 0
    assert "secret@example.com" not in json.dumps(first)
    cache = state / "usage-command-observation.json"
    assert "secret@example.com" not in cache.read_text(encoding="ascii")


def test_model_family_absence_is_rejected_not_silently_ignored():
    payload = {"result": "x", "modelUsage": {"claude-haiku-4-5": {"input_tokens": 1}}}
    parsed = parse_claude_result(json.dumps(payload))
    assert parsed["actual_models"] == ["claude-haiku-4-5"]
    from claude_research_mcp.server import _assert_requested_family

    with pytest.raises(ExecutionError, match="requested Claude model family"):
        _assert_requested_family(parsed, "opus")


def test_limit_output_timeout_kills_group_and_concurrency(tmp_path):
    settings = make_settings(tmp_path)
    from claude_research_mcp.executor import truncate_text

    with pytest.raises(ExecutionError, match="output limit"):
        asyncio.run(run_command([sys.executable, "-c", "print('x' * 100)"], cwd=tmp_path, timeout_seconds=2, output_limit=50, prompt=None))
    assert "truncated" in truncate_text("x" * 100, 50)
    assert "truncated" in truncate_text("abcdef", 3)

    script = textwrap.dedent("""
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        print(child.pid, flush=True)
        time.sleep(30)
    """)
    start = time.monotonic()
    with pytest.raises(ExecutionError, match="timed out"):
        asyncio.run(run_command([sys.executable, "-c", script], cwd=tmp_path, timeout_seconds=1, output_limit=100, prompt=None))
    assert time.monotonic() - start < 5

    async def hold_two():
        async with concurrency_slot(tmp_path / "locks", 1):
            async with concurrency_slot(tmp_path / "locks", 1):
                pass

    async def attempt():
        await asyncio.wait_for(hold_two(), timeout=0.1)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(attempt())

def test_continue_burst_guard_is_per_principal_conversation_and_transactional(
    tmp_path,
):
    database = tmp_path / "burst.sqlite3"
    initialize(database)
    first = create_conversation(database, "owner", "opus", False)
    second = create_conversation(database, "owner", "opus", False)

    def reserve(principal="owner", conversation_id=first.conversation_id):
        return reserve_call(
            database,
            principal=principal,
            tool="claude_continue",
            conversation_id=conversation_id,
            model="opus",
            continue_burst_limit=4,
            continue_burst_window_seconds=60,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(reserve) for _ in range(8)]
    results = []
    blocked = []
    for future in futures:
        try:
            results.append(future.result())
        except ContinueBurstLimitError as exc:
            blocked.append(str(exc))
    assert len(results) == 4
    assert len(blocked) == 4
    assert set(blocked) == {"continue burst limit reached (4 calls in 60 seconds)"}

    reserve(conversation_id=second.conversation_id)
    reserve(principal="partner")
    old = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE audit_log SET created_at = ? "
            "WHERE principal = 'owner' AND conversation_id = ?",
            (old, first.conversation_id),
        )
    reserve()


def test_blocked_continue_burst_is_sanitized_audited_and_does_not_poison_window(
    tmp_path,
):
    settings = make_settings(
        tmp_path,
        CLAUDE_MCP_CONTINUE_BURST_LIMIT="1",
        CLAUDE_MCP_CONTINUE_BURST_WINDOW_SECONDS="60",
    )
    fake_claude(settings.claude_bin)
    app = create_app(settings)
    started = asyncio.run(get_tool(app, "claude_start")("start"))
    conversation = started["conversation_id"]
    asyncio.run(get_tool(app, "claude_continue")(conversation, "first continue"))

    secret = "secret-burst-prompt"
    with pytest.raises(
        ConfigurationError,
        match=r"continue burst limit reached \(1 calls in 60 seconds\)",
    ) as caught:
        asyncio.run(get_tool(app, "claude_continue")(conversation, secret))
    assert secret not in str(caught.value)

    with sqlite3.connect(settings.database_path) as connection:
        burst_rows = connection.execute(
            "SELECT principal, tool, conversation_id, model, status "
            "FROM audit_log WHERE status = 'burst_limited'"
        ).fetchall()
        stored = json.dumps(
            connection.execute("SELECT * FROM audit_log").fetchall()
        )
    assert burst_rows == [
        ("owner", "claude_continue", conversation, "opus", "burst_limited")
    ]
    assert secret not in stored

    old = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "UPDATE audit_log SET created_at = ? "
            "WHERE tool = 'claude_continue' AND status = 'success'",
            (old,),
        )
    assert (
        asyncio.run(get_tool(app, "claude_continue")(conversation, "after window"))[
            "status"
        ]
        == "ok"
    )


def test_reserved_continue_failure_counts_toward_burst_guard(tmp_path):
    settings = make_settings(
        tmp_path,
        CLAUDE_MCP_CONTINUE_BURST_LIMIT="1",
        CLAUDE_MCP_CONTINUE_BURST_WINDOW_SECONDS="60",
    )
    fake_claude(settings.claude_bin)
    app = create_app(settings)
    conversation = asyncio.run(get_tool(app, "claude_start")("start"))[
        "conversation_id"
    ]

    fake_claude(settings.claude_bin, fail=True)
    with pytest.raises(ExecutionError, match="Claude CLI failed"):
        asyncio.run(get_tool(app, "claude_continue")(conversation, "fails after reserve"))
    with pytest.raises(ConfigurationError, match="continue burst limit reached"):
        asyncio.run(get_tool(app, "claude_continue")(conversation, "blocked next"))

    with sqlite3.connect(settings.database_path) as connection:
        statuses = connection.execute(
            "SELECT status FROM audit_log WHERE tool = 'claude_continue' ORDER BY id"
        ).fetchall()
    assert statuses == [("error",), ("burst_limited",)]


def test_sensitive_failure_and_audit_never_store_prompt_or_output(tmp_path):
    settings = make_settings(tmp_path)
    fake_claude(settings.claude_bin, fail=True)
    app = create_app(settings)
    prompt = "this-is-a-secret-research-prompt"
    with pytest.raises(ExecutionError) as caught:
        asyncio.run(get_tool(app, "claude_once")(prompt))
    assert prompt not in str(caught.value)
    assert "secret@example.com" not in str(caught.value)
    assert "/home/secret" not in str(caught.value)
    assert str(caught.value).count("Claude CLI failed") == 1

    connection = sqlite3.connect(settings.database_path)
    rows = connection.execute("SELECT principal,tool,conversation_id,model,status,duration_ms FROM audit_log").fetchall()
    connection.close()
    assert len(rows) == 1
    assert rows[0][:4] == ("owner", "claude_once", None, "opus")
    assert "secret" not in json.dumps(rows)


def test_raw_failure_hook_and_safe_metadata(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    fake_claude(settings.claude_bin, fail=True)
    calls = []

    def writer(work_root, *, principal, request_id, stdout, stderr):
        calls.append((work_root, principal, request_id, stdout, stderr))
        return True

    monkeypatch.setattr(server_module.diagnostics, "write_raw_error", writer)
    app = create_app(settings)
    with pytest.raises(ExecutionError) as caught:
        asyncio.run(get_tool(app, "claude_once")("raw failure prompt"))
    rendered = str(caught.value)
    assert "raw_log_saved: true" in rendered
    assert "secret@example.com" not in rendered
    assert len(calls) == 1
    work_root, principal, request_id, stdout, stderr = calls[0]
    assert (work_root, principal) == (settings.work_root, "owner")
    validate_conversation_id(request_id)
    assert "secret@example.com" in stderr
    assert "raw failure prompt" in stderr

    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute(
            "SELECT error_category, exit_code, request_id, raw_log_saved "
            "FROM audit_log"
        ).fetchone()
    assert row[0] == "unknown"
    assert row[1] == 7
    assert row[2] == request_id
    assert row[3] == 1


def test_raw_failure_storage_failure_reports_false(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    fake_claude(settings.claude_bin, fail=True)
    monkeypatch.setattr(server_module.diagnostics, "write_raw_error", lambda *a, **k: False)
    app = create_app(settings)
    with pytest.raises(ExecutionError) as caught:
        asyncio.run(get_tool(app, "claude_once")("raw failure prompt"))
    assert "raw_log_saved: false" in str(caught.value)
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT raw_log_saved FROM audit_log").fetchone() == (0,)


@pytest.mark.parametrize(
    ("exc", "category", "status"),
    [
        (ClaudeResultError("auth", 401), "auth", "error"),
        (ClaudeResultError("rate_limit", 429), "rate_limit", "error"),
        (ClaudeResultError("permission", 403), "permission", "error"),
        (ClaudeResultError("model_unavailable"), "model_unavailable", "error"),
        (ClaudeResultError("runtime", 500), "runtime", "error"),
        (CommandTimeoutError("timed out"), "timeout", "timeout"),
        (asyncio.CancelledError(), "cancellation", "cancelled"),
    ],
)
def test_failure_metadata_classifications(exc, category, status):
    from claude_research_mcp.executor import CommandTimeoutError
    from claude_research_mcp.server import _error_metadata, _failure_status

    assert _error_metadata(exc)[0] == category
    assert _failure_status(exc) == status


def test_zero_exit_is_error_and_model_mismatch_never_raw_logged(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    calls = []
    monkeypatch.setattr(server_module.diagnostics, "write_raw_error", lambda *a, **k: calls.append((a, k)) or True)
    app = create_app(settings)
    payload = json.dumps(
        {"is_error": True, "result": "secret-zero-exit-error", "error": {"status": 429}}
    )
    settings.claude_bin.write_text("#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' '" + payload + "'\n")
    settings.claude_bin.chmod(0o755)
    with pytest.raises(ExecutionError, match="rate_limit") as caught:
        asyncio.run(get_tool(app, "claude_once")("zero exit"))
    assert "secret-zero-exit-error" not in str(caught.value)
    assert len(calls) == 1
    assert calls[0][1]["stdout"] == payload + "\n"

    calls.clear()
    settings.claude_bin.write_text(
        "#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' '"
        + json.dumps({"result": "valid secret success", "sessionId": "x", "modelUsage": {}})
        + "'\n"
    )
    settings.claude_bin.chmod(0o755)
    with pytest.raises(ExecutionError, match="requested Claude model family"):
        asyncio.run(get_tool(app, "claude_once")("family mismatch"))
    assert calls == []


def test_request_ids_match_audit_for_all_seven_tools_and_rejections(
    tmp_path, monkeypatch
):
    claude_home = tmp_path / "claude-home"
    monkeypatch.setattr(server_module, "_claude_home", lambda: claude_home)
    async def unavailable_probe(_settings):
        return server_module.usage_command_unavailable()

    monkeypatch.setattr(server_module, "_run_usage_probe", unavailable_probe)
    settings = make_settings(tmp_path)
    fake_claude(settings.claude_bin)
    app = create_app(settings)

    status = asyncio.run(get_tool(app, "claude_status")())
    once = asyncio.run(get_tool(app, "claude_once")("question"))
    started = asyncio.run(get_tool(app, "claude_start")("start"))
    conversation = started["conversation_id"]
    _write_transcript(
        settings, claude_home, "claude-session-123", [{"type": "user", "message": {"content": "q"}}]
    )
    continued = asyncio.run(get_tool(app, "claude_continue")(conversation, "more"))
    listed = asyncio.run(get_tool(app, "claude_list_conversations")())
    exported = asyncio.run(get_tool(app, "claude_export_conversation")(conversation))
    closed = asyncio.run(get_tool(app, "claude_close")(conversation))
    success_ids = [
        result["request_id"]
        for result in (status, once, started, continued, listed, exported, closed)
    ]
    assert len(set(success_ids)) == 7
    for value in success_ids:
        validate_conversation_id(value)

    rejection_cases = [
        (get_tool(app, "claude_once")("x", model="secret-model"), "model is unavailable"),
        (get_tool(app, "claude_start")("x", use_web="true"), "use_web"),
        (
            get_tool(app, "claude_continue")(
                "01935555-eeee-4eee-8eee-eeeeeeeeeeee", "x"
            ),
            "unavailable",
        ),
        (get_tool(app, "claude_close")("not-a-uuid"), "server-issued UUID"),
        (get_tool(app, "claude_list_conversations")(limit=0), "limit"),
        (get_tool(app, "claude_export_conversation")("not-a-uuid"), "server-issued UUID"),
    ]
    error_ids = []
    error_messages = []
    for case, marker in rejection_cases:
        with pytest.raises(ExecutionError, match=marker) as caught:
            asyncio.run(case)
        rendered = str(caught.value)
        error_messages.append(rendered)
        request_match = re.search(r"request_id: ([0-9a-f-]{36})", rendered)
        assert request_match
        error_ids.append(request_match.group(1))
    assert len(set(error_ids)) == len(error_ids)

    with sqlite3.connect(settings.database_path) as connection:
        rows = connection.execute(
            "SELECT status, request_id FROM audit_log ORDER BY id"
        ).fetchall()
        new_fields = json.dumps(
            connection.execute(
                "SELECT error_category, http_status, exit_code, request_id, "
                "raw_log_saved FROM audit_log"
            ).fetchall()
        )
    assert set(success_ids + error_ids) == {row[1] for row in rows}
    assert all(row[0] != "running" for row in rows)
    assert "secret-model" not in " ".join(error_messages)
    assert "not-a-uuid" not in " ".join(error_messages)
    assert "secret-model" not in new_fields
    assert "not-a-uuid" not in new_fields


def test_safe_audit_metadata_rejects_tampered_attributes(tmp_path):
    settings = make_settings(tmp_path)
    database = settings.database_path
    initialize(database)
    request_id = "6850492e-06f4-4be0-8e52-cdcba1a26c38"
    record_audit_event(
        database,
        principal="owner",
        tool="claude_once",
        conversation_id=None,
        model="opus",
        status="error",
        duration_ms=3,
        error_category="arbitrary-secret-path",
        http_status=999,
        exit_code=999999,
        request_id=request_id.upper(),
        raw_log_saved="yes",
    )
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT error_category, http_status, exit_code, request_id, "
            "raw_log_saved FROM audit_log"
        ).fetchone()
    assert row == (None, None, None, None, None)


def test_invalid_start_creates_no_conversation_and_failed_start_is_single_audit(tmp_path):
    settings = make_settings(tmp_path)
    fake_claude(settings.claude_bin)
    app = create_app(settings)
    with pytest.raises(ExecutionError, match="use_web must be boolean"):
        asyncio.run(get_tool(app, "claude_start")("prompt", use_web="true"))

    connection = sqlite3.connect(settings.database_path)
    assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    assert connection.execute("SELECT status FROM audit_log").fetchall() == [("error",)]
    connection.close()

    fake_claude(settings.claude_bin, fail=True)
    with pytest.raises(ExecutionError):
        asyncio.run(get_tool(app, "claude_start")("failing prompt"))
    connection = sqlite3.connect(settings.database_path)
    rows = connection.execute(
        "SELECT status FROM audit_log WHERE tool = 'claude_start'"
    ).fetchall()
    closed = connection.execute(
        "SELECT closed_at IS NOT NULL FROM conversations"
    ).fetchall()
    connection.close()
    assert rows == [("error",), ("error",)]
    assert ("running",) not in rows
    assert closed == [(1,)]
