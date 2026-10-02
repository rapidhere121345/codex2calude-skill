from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from claude_research_mcp import executor
from claude_research_mcp.executor import (
    ExecutionError,
    build_usage_command,
    run_usage_command,
    usage_environment,
)


def test_usage_probe_uses_fixed_argv_and_removes_only_usage_blocking_switch():
    command = build_usage_command(Path("/opt/claude-code/2.1.236/claude"))
    environment = usage_environment()

    assert command == [
        "/opt/claude-code/2.1.236/claude",
        "--no-chrome",
        "--safe-mode",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--permission-mode",
        "dontAsk",
        "--model",
        "opus",
        "--tools",
        "",
    ]
    assert "--disable-slash-commands" not in command
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in environment
    assert environment["TERM"] == "xterm-256color"
    assert environment["TZ"] == "UTC"
    assert environment["CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL"] == "1"
    assert environment["DISABLE_AUTOUPDATER"] == "1"
    assert environment["DISABLE_UPDATES"] == "1"
    assert environment["DISABLE_ERROR_REPORTING"] == "1"
    assert environment["DISABLE_FEEDBACK_COMMAND"] == "1"
    assert environment["DISABLE_TELEMETRY"] == "1"
    assert environment["DO_NOT_TRACK"] == "1"
    assert environment["CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY"] == "1"
    assert environment["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"


def test_usage_probe_captures_screen_without_a_model_prompt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    binary = tmp_path / "claude"
    argv = tmp_path / "argv"
    stdin = tmp_path / "stdin"
    binary.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {argv}\n"
        "IFS= read -r line\n"
        f"printf '%s' \"$line\" > {stdin}\n"
        "printf '\\033[2J\\033[HCurrent session 60%% used Resets 8:30am (UTC)\\n'\n"
        "printf 'Current week (all models) 12%% used Resets Sep 26, 12pm (UTC)\\n'\n"
        "sleep 2\n",
        encoding="ascii",
    )
    binary.chmod(0o755)
    monkeypatch.setattr(executor, "USAGE_COMMAND_INPUT_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(executor, "USAGE_COMMAND_RENDER_SECONDS", 0.05)
    monkeypatch.setattr(executor, "USAGE_COMMAND_TIMEOUT_SECONDS", 1)

    output = asyncio.run(run_usage_command(binary, cwd=tmp_path))

    assert "Current session 60% used" in output
    assert stdin.read_text(encoding="ascii") == "/usage"
    recorded = argv.read_text(encoding="ascii").splitlines()
    assert "--model" in recorded
    assert "--disable-slash-commands" not in recorded


def test_usage_probe_timeout_is_bounded_and_kills_process_group(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    binary = tmp_path / "claude"
    pid_file = tmp_path / "pid"
    binary.write_text(
        "#!/bin/sh\n"
        f"printf '%s' \"$$\" > {pid_file}\n"
        "trap '' TERM\n"
        "sleep 30\n",
        encoding="ascii",
    )
    binary.chmod(0o755)
    monkeypatch.setattr(executor, "USAGE_COMMAND_INPUT_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(executor, "USAGE_COMMAND_RENDER_SECONDS", 1)
    monkeypatch.setattr(executor, "USAGE_COMMAND_TIMEOUT_SECONDS", 0.05)

    async def child() -> None:
        with pytest.raises(ExecutionError, match="timed out"):
            await run_usage_command(binary, cwd=tmp_path)

    asyncio.run(asyncio.wait_for(child(), timeout=2))

    pid = int(pid_file.read_text(encoding="ascii"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
