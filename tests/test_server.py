from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import textwrap
import time

import pytest

from claude_research_mcp.config import (
    ConfigurationError,
    load_settings,
    validate_conversation_id,
    validate_model,
    validate_prompt,
)
from claude_research_mcp.executor import (
    ExecutionError,
    build_auth_status_command,
    build_ask_command,
    build_version_command,
    run_command,
    truncate_text,
    validate_options,
)
from claude_research_mcp.locking import concurrency_slot
from claude_research_mcp.persistence import (
    RateLimitError,
    SessionOwnershipError,
    close_conversation,
    create_conversation,
    get_conversation,
    initialize,
    reserve_call,
)
from claude_research_mcp.server import INSTRUCTIONS, create_app, parse_claude_result


TOOLS = ["claude_status", "claude_once", "claude_start", "claude_continue", "claude_close"]


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
        "CLAUDE_MCP_RATE_LIMIT_PER_HOUR": "20",
    }
    env.update({key: str(value) for key, value in overrides.items()})
    return load_settings(env)


def fake_claude(path: Path, *, fail: bool = False, session: str = "claude-session-123") -> None:
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
    body = (
        "#!/bin/sh\n"
        "if [ \"$1\" = --version ]; then printf '2.1.236\\n'; exit 0; fi\n"
        "if [ \"$1\" = auth ] && [ \"$2\" = status ]; then\n"
        "  printf '%s\\n' '{\"loggedIn\":true,\"authMethod\":\"claude.ai\",\"subscriptionType\":\"pro\",\"email\":\"secret@example.com\",\"orgId\":\"secret-org\"}'\n"
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
    for phrase in ("owner/partner", "shared Claude subscription", "one session", "conversation_id", "queue", "verify"):
        assert phrase in INSTRUCTIONS

    once = asyncio.run(get_tool(app, "claude_once")("once secret prompt", model="opus", use_web=True))
    assert once["actual_models"] == ["claude-haiku-4-5", "claude-opus-5"]
    assert once["model_usage"][1] == {"model": "claude-opus-5", "input_tokens": 11, "output_tokens": 22}
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
    assert closed == {"status": "closed", "conversation_id": conversation, "transcript_deleted": False}
    with pytest.raises(ExecutionError, match="conversation is closed"):
        asyncio.run(get_tool(app, "claude_continue")(conversation, "after close"))


def test_status_and_fable_version_gate(tmp_path):
    settings = make_settings(
        tmp_path,
        CLAUDE_MCP_MODELS='{"opus":{"cli_value":"opus","family":"opus"},"fable":{"cli_value":"fable","family":"fable","min_cli_version":"2.1.257"}}',
    )
    fake_claude(settings.claude_bin)
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


def test_model_family_absence_is_rejected_not_silently_ignored():
    payload = {"result": "x", "modelUsage": {"claude-haiku-4-5": {"input_tokens": 1}}}
    parsed = parse_claude_result(json.dumps(payload))
    assert parsed["actual_models"] == ["claude-haiku-4-5"]
    from claude_research_mcp.server import _assert_requested_family

    with pytest.raises(ExecutionError, match="requested Claude model family"):
        _assert_requested_family(parsed, "opus")


def test_limit_output_timeout_kills_group_concurrency_and_rate(tmp_path):
    settings = make_settings(tmp_path)
    from claude_research_mcp.executor import truncate_text

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

    database = tmp_path / "quota.sqlite3"
    initialize(database)
    reserve_call(database, principal="owner", tool="claude_once", conversation_id=None, model="opus", rate_limit=1)
    with pytest.raises(RateLimitError):
        reserve_call(database, principal="owner", tool="claude_once", conversation_id=None, model="opus", rate_limit=1)
    reserve_call(database, principal="partner", tool="claude_once", conversation_id=None, model="opus", rate_limit=1)


def test_sensitive_failure_and_audit_never_store_prompt_or_output(tmp_path):
    settings = make_settings(tmp_path, CLAUDE_MCP_RATE_LIMIT_PER_HOUR="1")
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
