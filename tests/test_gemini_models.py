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


def _fake_agy(path: Path) -> Path:
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
result = {"conversation_id": "01234567-89ab-cdef-0123-456789abcdef", "status": "SUCCESS", "response": "answer", "usage": {}}
print(json.dumps({"event": "result", "result": result}), flush=True)
sys.stdin.read()
    """
    return _write_executable(path, source)


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


def test_pro_default_builds_model_and_max_effort_argv(tmp_path: Path) -> None:
    binary = _fake_agy(tmp_path / "agy")
    command = build_ask_command(
        _settings(binary),
        resume_session_id=None,
        model_cli_value="fake-pro-model",
        reasoning_effort="max",
        use_web=True,
        max_turns=20,
    )

    assert command[-4:] == ["--model", "fake-pro-model", "--effort", "max"]


def test_reported_model_must_match_concrete_policy_exactly() -> None:
    policy = ModelPolicy(
        "pro", "gemini-fake-pro-model", "gemini", DEFAULT_REQUIRED_CLI_VERSION, "max"
    )
    unknown = {"actual_models": []}
    exact = {"actual_models": ["gemini-fake-pro-model"]}
    different = {"actual_models": ["gemini-fake-flash-model"]}

    assert _assert_requested_policy(unknown, policy) == []
    assert _assert_requested_policy(exact, policy) == ["gemini-fake-pro-model"]
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


def test_pending_auto_blocks_prompt_before_cli_in_explicit_mode(tmp_path: Path) -> None:
    binary = _fake_agy(tmp_path / "agy")
    app = create_app(_settings(binary, pro=False, explicit=True))

    status = asyncio.run(get_tool(app, "gemini_status")())
    assert status["default_model"] == "auto"
    assert status["model_selection_ready"] is False
    assert status["models"]["auto"]["selection_ready"] is False
    with pytest.raises(ExecutionError, match="model selection pending"):
        asyncio.run(get_tool(app, "gemini_once")("secret prompt"))
    with pytest.raises(ExecutionError, match="model selection pending"):
        asyncio.run(get_tool(app, "gemini_start")("secret prompt"))
    assert _invocations(tmp_path) == []


def test_status_reports_default_model_selection_state(tmp_path: Path) -> None:
    binary = _fake_agy(tmp_path / "agy")
    app = create_app(_settings(binary, pro=True, explicit=True))

    status = asyncio.run(get_tool(app, "gemini_status")())

    assert status["default_model"] == "pro"
    assert status["model_selection_ready"] is True
    assert status["models"]["pro"]["selection_ready"] is True


def test_start_snapshot_survives_alias_remap_on_continue(tmp_path: Path) -> None:
    binary = _fake_agy(tmp_path / "agy")
    settings = _settings(binary)
    app = create_app(settings)

    started = asyncio.run(get_tool(app, "gemini_start")("first prompt"))
    conversation = started["conversation_id"]
    first = _invocations(tmp_path)[0]
    assert first["argv"][-4:] == ["--model", "fake-pro-model", "--effort", "max"]

    remapped = ModelPolicy("pro", "remapped-model", "gemini", DEFAULT_REQUIRED_CLI_VERSION, "high")
    remapped_settings = Settings(
        **{
            **settings.__dict__,
            "models": {"pro": remapped},
        }
    )
    remapped_app = create_app(remapped_settings)
    asyncio.run(get_tool(remapped_app, "gemini_continue")(conversation, "second prompt"))

    second = _invocations(tmp_path)[1]
    assert "--conversation" in second["argv"]
    assert second["argv"][-4:] == ["--model", "fake-pro-model", "--effort", "max"]


def test_compat_auto_snapshot_is_not_replaced_by_alias_remap(tmp_path: Path) -> None:
    binary = _fake_agy(tmp_path / "agy")
    settings = _settings(binary, pro=False, explicit=False)
    app = create_app(settings)

    started = asyncio.run(get_tool(app, "gemini_start")("first prompt"))
    conversation = started["conversation_id"]

    remapped = ModelPolicy(
        "auto", "remapped-model", "gemini", DEFAULT_REQUIRED_CLI_VERSION, "high"
    )
    remapped_settings = Settings(
        **{**settings.__dict__, "models": {"auto": remapped}}
    )
    remapped_app = create_app(remapped_settings)
    asyncio.run(get_tool(remapped_app, "gemini_continue")(conversation, "second"))

    second = _invocations(tmp_path)[1]
    assert "--model" not in second["argv"]
    assert "--effort" not in second["argv"]


def test_legacy_snapshot_is_rejected_only_in_explicit_mode(tmp_path: Path) -> None:
    binary = _fake_agy(tmp_path / "agy")
    settings = _settings(binary, explicit=False)
    app = create_app(settings)
    conversation = "01935555-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    initialize(settings.database_path)
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "INSERT INTO conversations "
            "(conversation_id, principal, gemini_session_id, model, use_web, created_at, closed_at, title, last_activity_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                conversation,
                "owner",
                "legacy-session-id",
                "pro",
                1,
                "2026-01-01T00:00:00+00:00",
                "legacy",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    with pytest.raises(ExecutionError, match="no model snapshot"):
        app_explicit = create_app(_settings(binary, explicit=True))
        asyncio.run(get_tool(app_explicit, "gemini_continue")(conversation, "prompt"))
    assert _invocations(tmp_path) == []


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
