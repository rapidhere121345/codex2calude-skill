from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

import pytest

from claude_research_mcp.config import ConfigurationError
from claude_research_mcp.diagnostics import raw_error_data
from claude_research_mcp.executor import (
    ExecutionError,
    OptionsValidationError,
    OutputLimitExceededError,
    run_command,
)
from claude_research_mcp.server import _error_metadata, parse_claude_result


CHILD = (
    "import os,sys,time;print('stdout-sentinel',flush=True);"
    "print('stderr-sentinel',file=sys.stderr,flush=True);"
    "print(f'PID:{os.getpid()}',flush=True);time.sleep(30)"
)


def _pid(stdout: str) -> int:
    line = next(line for line in stdout.splitlines() if line.startswith("PID:"))
    return int(line.removeprefix("PID:"))


def _stopped(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_timeout_keeps_partial_streams_and_stops_child(tmp_path: Path) -> None:
    async def scenario() -> ExecutionError:
        with pytest.raises(ExecutionError, match="timed out") as caught:
            await run_command(
                [sys.executable, "-c", CHILD], cwd=tmp_path,
                timeout_seconds=1, output_limit=1000,
            )
        return caught.value

    error = asyncio.run(scenario())
    data = raw_error_data(error)
    assert data is not None
    assert "stdout-sentinel" in data.stdout
    assert "stderr-sentinel" in data.stderr
    assert _stopped(_pid(data.stdout))


def test_external_cancel_keeps_partial_streams_and_stops_child(tmp_path: Path) -> None:
    async def scenario() -> asyncio.CancelledError:
        task = asyncio.create_task(run_command(
            [sys.executable, "-c", CHILD], cwd=tmp_path,
            timeout_seconds=30, output_limit=1000,
        ))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        return caught.value

    error = asyncio.run(scenario())
    data = raw_error_data(error)
    assert data is not None
    assert "stdout-sentinel" in data.stdout
    assert "stderr-sentinel" in data.stderr
    assert _stopped(_pid(data.stdout))


def test_broken_stdin_captures_output(tmp_path: Path) -> None:
    async def scenario() -> ExecutionError:
        script = "import sys;print('stdout-sentinel',flush=True);print('stderr-sentinel',file=sys.stderr,flush=True)"
        with pytest.raises(ExecutionError, match="closed its input stream") as caught:
            await run_command(
                [sys.executable, "-c", script], cwd=tmp_path,
                timeout_seconds=2, output_limit=1000,
                prompt="x" * (1024 * 1024 + 1),
            )
        return caught.value

    error = asyncio.run(scenario())
    data = raw_error_data(error)
    assert data is not None
    assert data.stdout == "stdout-sentinel\n"
    assert data.stderr == "stderr-sentinel\n"


def test_output_limit_keeps_limit_plus_one_evidence(tmp_path: Path) -> None:
    async def scenario() -> OutputLimitExceededError:
        with pytest.raises(OutputLimitExceededError) as caught:
            await run_command(
                [sys.executable, "-c", "print('x' * 100)"], cwd=tmp_path,
                timeout_seconds=2, output_limit=50,
            )
        return caught.value

    data = raw_error_data(asyncio.run(scenario()))
    assert data is not None
    assert data.stdout == "x" * 51


def test_invalid_json_is_runtime_not_validation() -> None:
    with pytest.raises(ExecutionError, match="invalid JSON") as caught:
        parse_claude_result("not-json", "stderr-sentinel")
    assert _error_metadata(caught.value)[0] == "runtime"
    assert raw_error_data(caught.value) is not None


def test_zero_exit_error_classifies_stderr_and_exit_zero() -> None:
    with pytest.raises(ExecutionError) as caught:
        parse_claude_result('{"is_error":true,"result":"secret"}', "connection refused")
    assert caught.value.category == "network"
    assert caught.value.exit_code == 0


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (ConfigurationError("invalid"), "validation"),
        (OptionsValidationError("invalid"), "validation"),
        (ExecutionError("runtime"), "runtime"),
        (OutputLimitExceededError("limit"), "output_limit"),
    ],
)
def test_error_categories_use_trusted_types(exc: Exception, category: str) -> None:
    assert _error_metadata(exc)[0] == category
