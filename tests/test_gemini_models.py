from __future__ import annotations

import asyncio
import json
import sqlite3
import stat
import textwrap
from pathlib import Path

import pytest

from gemini_research_mcp.config import (
    DEFAULT_REQUIRED_CLI_VERSION,
    ModelPolicy,
    Settings,
    load_settings,
)
from gemini_research_mcp.executor import ExecutionError, build_ask_command
from gemini_research_mcp.persistence import initialize
from gemini_research_mcp.server import _assert_requested_policy, create_app


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _fake_agy(path: Path, reported_model: str | None = None) -> Path:
    source = """
        #!/usr/bin/env python3
import json
import sys
from pathlib import Path
if "--version" in sys.argv:
    print("1.2.14")
    raise SystemExit(0)
if "/usage" in sys.argv:
    print(json.dumps({"status": "SUCCESS", "result": {"plans": []}}))
    raise SystemExit(0)
init = {"event": "init", "conversation_id": "private-native-id", "init": {"tools": ["search_web", "read_url_content", "ask_permission"]}}
print(json.dumps(init), flush=True)
prompt = sys.stdin.readline()
output = Path(__file__).with_name("invocations.jsonl")
with output.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"argv": sys.argv, "prompt": prompt}) + chr(10))
session_id = sys.argv[sys.argv.index("--conversation") + 1] if "--conversation" in sys.argv else "01234567-89ab-cdef-0123-456789abcdef"
result = {"conversation_id": session_id, "status": "SUCCESS", "response": "answer", "usage": {}}
if __REPORTED_MODEL__ is not None:
    result["stats"] = {"models": {__REPORTED_MODEL__: {"tokens": {"input": 1}}}}
print(json.dumps({"event": "result", "result": result}), flush=True)
sys.stdin.read()
    """
    return _write_executable(path, source.replace("__REPORTED_MODEL__", repr(reported_model)))


def _settings(
    binary: Path,
    *,
    pro: bool = True,
    explicit: bool = True,
) -> Settings:
    work = binary.parent / "work"
    work.mkdir(exist_ok=True)
    policy = (
        ModelPolicy("pro", "fake-pro-model", "gemini", DEFAULT_REQUIRED_CLI_VERSION, "max")
        if pro
        else ModelPolicy("auto", "auto", "gemini", DEFAULT_REQUIRED_CLI_VERSION)
    )
    return Settings(
        work_root=work,
        principal="owner",
        allowed_principals=("owner", "partner"),
        gemini_bin=binary,
        database_path=work / "state" / "mcp.sqlite3",
        lock_dir=work / "state" / "locks",
        models={policy.logical_name: policy},
        prompt_limit=20_000,
        continue_burst_limit=50,
        continue_burst_window_seconds=60,
        concurrency=1,
        output_limit=100_000,
        result_limit=100_000,
        required_cli_version=DEFAULT_REQUIRED_CLI_VERSION,
        default_model=policy.logical_name,
        require_explicit_model=explicit,
    )


def get_tool(app, name: str):
    return app._tool_manager._tools[name].fn


def _invocations(path: Path) -> list[dict]:
    source = path / "invocations.jsonl"
    if not source.exists():
        return []
    return [json.loads(line) for line in source.read_text().splitlines()]


def test_pro_default_builds_fixed_flash_high_argv(tmp_path: Path) -> None:
    binary = _fake_agy(tmp_path / "agy")
    command = build_ask_command(
        _settings(binary),
        resume_session_id=None,
        model_cli_value="fake-pro-model",
        reasoning_effort="max",
        use_web=True,
        max_turns=20,
    )

    assert command[-4:] == ["--model", "gemini-3.8-flash-high", "--effort", "high"]


def test_reported_model_matches_effective_policy_even_with_old_snapshot() -> None:
    policy = ModelPolicy(
        "pro", "gemini-fake-pro-model", "gemini", DEFAULT_REQUIRED_CLI_VERSION, "max"
    )
    unknown = {"actual_models": []}
    exact = {"actual_models": ["gemini-3.8-flash-high"]}
    different = {"actual_models": ["gemini-fake-pro-model"]}

    assert _assert_requested_policy(unknown, policy) == []
    assert _assert_requested_policy(exact, policy) == ["gemini-3.8-flash-high"]
    with pytest.raises(ExecutionError, match="exactly match"):
        _assert_requested_policy(different, policy)


def test_invalid_effort_is_rejected(tmp_path: Path) -> None:
    environment = {
        "GEMINI_MCP_WORK_ROOT": str(tmp_path),
        "GEMINI_MCP_PRINCIPAL": "owner",
        "GEMINI_MCP_MODELS": json.dumps(
            {"pro": {"cli_value": "safe-model", "reasoning_effort": "ultra"}}
        ),
        "GEMINI_MCP_DEFAULT_MODEL": "pro",
    }

    with pytest.raises(Exception, match="invalid GEMINI_MCP_MODELS"):
        load_settings(environment)


def test_legacy_auto_config_reports_fixed_policy_and_allows_inference(tmp_path: Path) -> None:
    binary = _fake_agy(tmp_path / "agy")
    app = create_app(_settings(binary, pro=False, explicit=True))

    status = asyncio.run(get_tool(app, "gemini_status")())
    assert status["default_model"] == "pro"
    assert status["reasoning_effort"] == "high"
    assert status["model_selection_ready"] is True
    assert status["models"]["pro"]["selection_ready"] is True
    assert status["models"]["auto"]["selection_ready"] is True
    asyncio.run(get_tool(app, "gemini_once")("prompt"))
    assert _invocations(tmp_path)[0]["argv"][-4:] == [
        "--model", "gemini-3.8-flash-high", "--effort", "high"
    ]


@pytest.mark.parametrize("model", [None, "", "pro", "auto", "gemini-3.1-pro-high", "other-model"])
@pytest.mark.parametrize("reported_model", [None, "gemini-3.8-flash-high"])
def test_once_start_continue_share_fixed_policy(
    tmp_path: Path, model: str | None, reported_model: str | None
) -> None:
    binary = _fake_agy(tmp_path / "agy", reported_model)
    settings = _settings(binary)
    app = create_app(settings)
    once = asyncio.run(get_tool(app, "gemini_once")("isolated", model=model))
    started = asyncio.run(get_tool(app, "gemini_start")("first", model=model))
    continued = asyncio.run(get_tool(app, "gemini_continue")(started["conversation_id"], "second"))

    for result in (once, started, continued):
        assert result["model"] == "pro"
        assert result["actual_models"] == ([] if reported_model is None else [reported_model])
    invocations = _invocations(tmp_path)
    assert len(invocations) == 3
    for invocation in invocations:
        assert invocation["argv"][-4:] == ["--model", "gemini-3.8-flash-high", "--effort", "high"]
    assert "--conversation" not in invocations[0]["argv"]
    assert "--conversation" not in invocations[1]["argv"]
    assert "--conversation" in invocations[2]["argv"]
    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute(
            "SELECT model, model_cli_value, reasoning_effort FROM conversations WHERE conversation_id = ?",
            (started["conversation_id"],),
        ).fetchone()
    assert row == ("pro", "gemini-3.8-flash-high", "high")


@pytest.mark.parametrize("legacy_model", ["pro", "auto", "gemini-3.1-pro-high"])
@pytest.mark.parametrize("snapshot", [(None, None, None), ("auto", "gemini", None), ("gemini-3.1-pro-high", "gemini", "high")])
@pytest.mark.parametrize("reported_model", [None, "gemini-3.8-flash-high"])
def test_legacy_sessions_continue_with_fixed_policy(
    tmp_path: Path, legacy_model: str, snapshot: tuple, reported_model: str | None
) -> None:
    binary = _fake_agy(tmp_path / "agy", reported_model)
    settings = _settings(binary, explicit=True)
    conversation = "01935555-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    initialize(settings.database_path)
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "INSERT INTO conversations "
            "(conversation_id, principal, gemini_session_id, model, use_web, created_at, closed_at, title, last_activity_at, model_cli_value, model_family, reasoning_effort) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
            (conversation, "owner", "legacy-session-id", legacy_model, 0,
             "2026-01-01T00:00:00+00:00", "legacy", "2026-01-01T00:00:00+00:00", *snapshot),
        )
    app = create_app(settings)
    result = asyncio.run(get_tool(app, "gemini_continue")(conversation, "prompt"))
    assert result["model"] == "pro"
    assert result["use_web"] is False
    assert result["actual_models"] == ([] if reported_model is None else [reported_model])
    command = _invocations(tmp_path)[0]["argv"]
    assert command[command.index("--conversation") + 1] == "legacy-session-id"
    assert command[command.index("--agent") + 1] == "research-offline"
    assert command[-4:] == ["--model", "gemini-3.8-flash-high", "--effort", "high"]


def test_seven_tool_names_are_unchanged(tmp_path: Path) -> None:
    app = create_app(_settings(_fake_agy(tmp_path / "agy")))

    assert [tool.name for tool in asyncio.run(app.list_tools())] == [
        "gemini_status",
        "gemini_once",
        "gemini_start",
        "gemini_continue",
        "gemini_close",
        "gemini_list_conversations",
        "gemini_export_conversation",
    ]
