from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3

import pytest

from claude_research_mcp.config import ConfigurationError, ModelPolicy, load_settings
from claude_research_mcp.executor import ExecutionError, build_ask_command
from claude_research_mcp.persistence import get_conversation
from claude_research_mcp.server import create_app


OPUS = {"cli_value": "claude-opus-5-5", "family": "opus", "reasoning_effort": "high"}
FABLE = {"cli_value": "claude-fable-5-2", "family": "fable", "reasoning_effort": "high"}


def make_settings(tmp_path: Path, models: dict, *, principal: str = "owner"):
    binary = tmp_path / "claude"
    if not binary.exists():
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        binary.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return load_settings({
        "CLAUDE_MCP_WORK_ROOT": str(work),
        "CLAUDE_MCP_PRINCIPAL": principal,
        "CLAUDE_BIN": str(binary),
        "CLAUDE_MCP_MODELS": json.dumps(models),
    })


def fake_claude(
    binary: Path, reported_model: str, *, session_id: str = "fake-session-123"
):
    output = json.dumps({
        "result": "fake answer",
        "sessionId": session_id,
        "modelUsage": {reported_model: {"input_tokens": 2, "output_tokens": 3}},
    })
    binary.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = --version ]; then cat >/dev/null; printf '2.1.280\\n'; exit 0; fi\n"
        "if [ \"$1\" = auth ]; then cat >/dev/null; printf '{\"loggedIn\":true}\\n'; exit 0; fi\n"
        f"printf '%s\\n' \"$@\" > {binary}.argv\n"
        f"cat > {binary}.stdin\n"
        f"printf '%s\\n' '{output}'\n",
        encoding="ascii",
    )
    binary.chmod(0o755)


def tool(settings, name: str):
    return create_app(settings)._tool_manager._tools[name].fn


def argv(binary: Path) -> list[str]:
    return Path(str(binary) + ".argv").read_text(encoding="ascii").splitlines()


@pytest.mark.parametrize("effort", [None, "low", "medium", "high", "xhigh", "max"])
def test_model_policy_parses_only_supported_efforts(tmp_path, effort):
    policy = {**OPUS, "reasoning_effort": effort}
    settings = make_settings(tmp_path, {"opus55": policy})
    assert settings.models["opus55"].reasoning_effort == effort
    assert settings.models["opus55"].cli_value == "claude-opus-5-5"
    assert settings.models["opus55"].family == "opus"
    assert ModelPolicy("opus", "opus", "opus", (2, 1, 236)).reasoning_effort is None
    if effort is None:
        assert make_settings(tmp_path, {"opus": {"cli_value": "opus"}}).models["opus"].reasoning_effort is None


@pytest.mark.parametrize("effort", ["", "HIGH", "ultra", "max --model bad", False, True, 2, [], {}])
def test_model_policy_rejects_invalid_efforts(tmp_path, effort):
    with pytest.raises(ConfigurationError, match="CLAUDE_MCP_MODELS"):
        make_settings(tmp_path, {"opus55": {**OPUS, "reasoning_effort": effort}})


def test_native_model_and_effort_use_only_policy_and_never_prompt(
    tmp_path, monkeypatch
):
    settings = make_settings(tmp_path, {"opus55": OPUS})
    fake_claude(settings.claude_bin, "claude-opus-5-5")
    monkeypatch.setenv("CLAUDE_MCP_REASONING_EFFORT", "max")
    prompt = "secret --effort max --model claude-fable-5-2"
    result = asyncio.run(tool(settings, "claude_once")(prompt, model="opus55"))
    command = argv(settings.claude_bin)
    assert command[command.index("--model") + 1] == "claude-opus-5-5"
    assert command[command.index("--effort") + 1] == "high"
    assert "--fallback-model" not in command
    assert prompt not in " ".join(command)
    assert Path(str(settings.claude_bin) + ".stdin").read_text() == prompt
    assert result["model"] == "opus55"
    assert result["configured_model"] == "claude-opus-5-5"
    assert result["reasoning_effort"] == "high"
    assert result["actual_models"] == ["claude-opus-5-5"]


def test_legacy_policy_with_no_effort_preserves_argv_and_result(tmp_path):
    settings = make_settings(tmp_path, {"opus": {"cli_value": "opus", "family": "opus"}})
    fake_claude(settings.claude_bin, "claude-opus-4")
    command = build_ask_command(
        settings, resume_session_id=None, model_cli_value="opus",
        use_web=False, max_turns=3,
    )
    assert "--effort" not in command
    result = asyncio.run(tool(settings, "claude_once")("legacy prompt"))
    assert "--effort" not in argv(settings.claude_bin)
    assert result["configured_model"] == "opus"
    assert "reasoning_effort" not in result


@pytest.mark.parametrize(
    "changed_models",
    [
        {"opus55": {"cli_value": "claude-opus-6-0", "family": "different", "reasoning_effort": "max"}},
        {"fable52": FABLE},
    ],
)
def test_start_snapshots_native_model_and_effort_for_continue(tmp_path, changed_models):
    settings = make_settings(tmp_path, {"opus55": OPUS})
    fake_claude(settings.claude_bin, "claude-opus-5-5")
    started = asyncio.run(tool(settings, "claude_start")("start", model="opus55"))
    record = get_conversation(
        settings.database_path, started["conversation_id"], "owner"
    )
    assert (record.model, record.model_cli_value, record.model_family, record.reasoning_effort) == (
        "opus55", "claude-opus-5-5", "opus", "high"
    )
    assert started["configured_model"] == "claude-opus-5-5"
    assert started["reasoning_effort"] == "high"
    assert argv(settings.claude_bin)[argv(settings.claude_bin).index("--effort") + 1] == "high"

    revised = make_settings(tmp_path, changed_models)
    continued = asyncio.run(
        tool(revised, "claude_continue")(started["conversation_id"], "continue")
    )
    command = argv(revised.claude_bin)
    assert command[command.index("--model") + 1] == "claude-opus-5-5"
    assert command[command.index("--effort") + 1] == "high"
    assert command[command.index("--resume") + 1] == "fake-session-123"
    assert continued["model"] == "opus55"
    assert continued["configured_model"] == "claude-opus-5-5"
    assert continued["reasoning_effort"] == "high"
    assert continued["actual_models"] == ["claude-opus-5-5"]

    partner = make_settings(tmp_path, changed_models, principal="partner")
    with pytest.raises(ExecutionError, match="conversation is unavailable"):
        asyncio.run(
            tool(partner, "claude_continue")(started["conversation_id"], "not mine")
        )
    assert argv(settings.claude_bin) == command


def test_new_snapshot_with_no_effort_stays_without_effort_after_policy_change(tmp_path):
    settings = make_settings(tmp_path, {"opus": {"cli_value": "opus", "family": "opus"}})
    fake_claude(settings.claude_bin, "claude-opus-4")
    started = asyncio.run(tool(settings, "claude_start")("start"))
    assert "reasoning_effort" not in started
    revised = make_settings(
        tmp_path, {"opus": {"cli_value": "claude-opus-5-5", "family": "opus", "reasoning_effort": "high"}}
    )
    continued = asyncio.run(
        tool(revised, "claude_continue")(started["conversation_id"], "continue")
    )
    command = argv(revised.claude_bin)
    assert command[command.index("--model") + 1] == "opus"
    assert "--effort" not in command
    assert continued["configured_model"] == "opus"
    assert "reasoning_effort" not in continued


def test_migrated_legacy_row_resumes_without_invented_effort(tmp_path):
    settings = make_settings(tmp_path, {"opus": {**OPUS, "reasoning_effort": "high"}})
    settings.database_path.parent.mkdir(parents=True)
    with sqlite3.connect(settings.database_path) as db:
        db.executescript(
            "CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY, "
            "principal TEXT NOT NULL, claude_session_id TEXT UNIQUE, "
            "model TEXT NOT NULL, use_web INTEGER NOT NULL, "
            "created_at TEXT NOT NULL, closed_at TEXT);"
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "principal TEXT NOT NULL, tool TEXT NOT NULL, "
            "conversation_id TEXT, model TEXT, status TEXT NOT NULL, "
            "duration_ms INTEGER, created_at TEXT NOT NULL);"
        )
        db.execute(
            "INSERT INTO conversations VALUES (?, 'owner', 'legacy-session-123', "
            "'opus', 1, '2026-01-01T00:00:00+00:00', NULL)",
            ("11111111-1111-4111-8111-111111111111",),
        )
    fake_claude(
        settings.claude_bin, "claude-opus-5-5", session_id="legacy-session-123"
    )
    app = create_app(settings)
    record = get_conversation(
        settings.database_path, "11111111-1111-4111-8111-111111111111", "owner"
    )
    assert (record.model_cli_value, record.model_family, record.reasoning_effort) == (
        None, None, None
    )
    response = asyncio.run(
        app._tool_manager._tools["claude_continue"].fn(
            "11111111-1111-4111-8111-111111111111", "legacy"
        )
    )
    command = argv(settings.claude_bin)
    assert command[command.index("--model") + 1] == "claude-opus-5-5"
    assert "--effort" not in command
    assert response["configured_model"] == "claude-opus-5-5"
    assert "reasoning_effort" not in response
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute(
            "SELECT model_cli_value, model_family, reasoning_effort FROM conversations"
        ).fetchone() == (None, None, None)


def test_fable_candidate_requires_actual_fable_family_in_fake_result(tmp_path):
    settings = make_settings(tmp_path, {"fable52": FABLE})
    fake_claude(settings.claude_bin, "claude-fable-5-2")
    result = asyncio.run(tool(settings, "claude_once")("fake candidate", model="fable52"))
    command = argv(settings.claude_bin)
    assert command[command.index("--model") + 1] == "claude-fable-5-2"
    assert command[command.index("--effort") + 1] == "high"
    assert result["actual_models"] == ["claude-fable-5-2"]
    assert result["configured_model"] == "claude-fable-5-2"
    assert "verified" not in result
    assert "service_available" not in result

    fake_claude(settings.claude_bin, "claude-opus-5-5")
    with pytest.raises(ExecutionError, match="requested Claude model family"):
        asyncio.run(tool(settings, "claude_once")("wrong family", model="fable52"))
