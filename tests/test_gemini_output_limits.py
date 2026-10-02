from __future__ import annotations

import asyncio
import json
import sys
import stat
import textwrap
from pathlib import Path

import pytest

from gemini_research_mcp.config import ConfigurationError, load_settings
from gemini_research_mcp.diagnostics import raw_error_data
from gemini_research_mcp.executor import (
    OutputLimitExceededError,
    build_ask_command,
    run_command,
)


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "GEMINI_MCP_WORK_ROOT": str(tmp_path),
        "GEMINI_MCP_PRINCIPAL": "owner",
        "AGY_BIN": str(tmp_path / "fake-gemini"),
    }


def _configuration_environment(tmp_path: Path) -> dict[str, str]:
    _write_executable(tmp_path / "fake-gemini", "#!/bin/sh\nexit 0\n")
    return _environment(tmp_path)


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_output_and_result_limits_default_to_four_mib(tmp_path: Path) -> None:
    settings = load_settings(_configuration_environment(tmp_path))

    assert settings.output_limit == 4_194_304
    assert settings.result_limit == 4_194_304


@pytest.mark.parametrize(
    "variable",
    ["GEMINI_MCP_OUTPUT_LIMIT", "GEMINI_MCP_RESULT_LIMIT"],
)
def test_output_and_result_limits_accept_four_mib(
    tmp_path: Path, variable: str
) -> None:
    environment = _configuration_environment(tmp_path)
    environment[variable] = "4194304"

    settings = load_settings(environment)

    attribute = variable.removeprefix("GEMINI_MCP_").lower()
    assert getattr(settings, attribute) == 4_194_304


@pytest.mark.parametrize(
    "variable",
    ["GEMINI_MCP_OUTPUT_LIMIT", "GEMINI_MCP_RESULT_LIMIT"],
)
def test_output_and_result_limits_reject_above_max(
    tmp_path: Path, variable: str
) -> None:
    environment = _configuration_environment(tmp_path)
    environment[variable] = "16777217"

    with pytest.raises(ConfigurationError, match=f"invalid {variable}"):
        load_settings(environment)


def test_output_and_result_limits_keep_smaller_explicit_values(
    tmp_path: Path,
) -> None:
    environment = _configuration_environment(tmp_path)
    environment.update(
        GEMINI_MCP_OUTPUT_LIMIT="1000",
        GEMINI_MCP_RESULT_LIMIT="1000",
    )

    settings = load_settings(environment)

    assert settings.output_limit == 1_000
    assert settings.result_limit == 1_000


def test_run_command_accepts_output_above_old_limit_and_below_four_mib(
    tmp_path: Path,
) -> None:
    size = 1_048_577
    binary = _write_executable(
        tmp_path / "fake-gemini",
        f"""
        #!/usr/bin/env python3
        import json
        import sys

        tools = ["search_web", "read_url_content"]
        print(json.dumps({{"event": "init", "conversation_id": "01234567-89ab-cdef-0123-456789abcdef", "init": {{"tools": tools}}}}), flush=True)
        payload = {{"result": "x" * {size}, "session_id": None}}
        print(json.dumps(payload), flush=True)
        """,
    )
    settings = load_settings(_environment(tmp_path))
    command = build_ask_command(
        settings,
        resume_session_id=None,
        model_cli_value="auto",
        use_web=True,
        max_turns=20,
    )

    result = asyncio.run(
        run_command(
            command,
            cwd=tmp_path,
            timeout_seconds=30,
            output_limit=settings.output_limit,
            prompt="bounded regression",
        )
    )

    encoded = result.stdout.encode("utf-8")
    final_result = json.loads(result.stdout.splitlines()[-1])["result"]
    assert 1_048_576 < len(encoded) < 4_194_304
    assert len(final_result) == size


def test_run_command_still_fails_bounded_above_configured_limit(
    tmp_path: Path,
) -> None:
    with pytest.raises(OutputLimitExceededError) as captured:
        asyncio.run(
            run_command(
                [sys.executable, "-c", "print('x' * 4194305)"],
                cwd=tmp_path,
                timeout_seconds=30,
                output_limit=4_194_304,
            )
        )

    raw = raw_error_data(captured.value)
    assert raw is not None
    assert len(raw.stdout.encode("utf-8")) == 4_194_305
