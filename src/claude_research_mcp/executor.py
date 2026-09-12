"""Bounded, fixed-argv execution of non-interactive Claude Code."""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import os
from pathlib import Path
import signal
import subprocess

from .config import Settings


DEFAULT_MAX_TURNS = 8
DEFAULT_TIMEOUT_SECONDS = 600
MIN_MAX_TURNS = 1
MAX_MAX_TURNS = 100
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 3_600
EMPTY_MCP_CONFIG = '{"mcpServers":{}}'


class ExecutionError(RuntimeError):
    """A sanitized execution failure."""


@dataclass(frozen=True)
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int


def validate_options(
    *, model: str, use_web: object, max_turns: object, timeout_seconds: object
) -> tuple[str, bool, int, int]:
    if not isinstance(model, str):
        raise ExecutionError("model must be a string")
    if not isinstance(use_web, bool):
        raise ExecutionError("use_web must be boolean")
    if isinstance(max_turns, bool) or not isinstance(max_turns, int):
        raise ExecutionError("max_turns must be an integer")
    if not MIN_MAX_TURNS <= max_turns <= MAX_MAX_TURNS:
        raise ExecutionError("max_turns must be between 1 and 100")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
        raise ExecutionError("timeout_seconds must be an integer")
    if not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ExecutionError("timeout_seconds must be between 1 and 3600")
    return model, use_web, max_turns, timeout_seconds


def build_version_command(claude_bin: Path) -> list[str]:
    return [str(claude_bin), "--version"]


def build_auth_status_command(claude_bin: Path) -> list[str]:
    return [str(claude_bin), "auth", "status"]


def build_ask_command(
    settings: Settings,
    *,
    resume_session_id: str | None,
    model_cli_value: str,
    use_web: bool,
    max_turns: int,
) -> list[str]:
    """Build the fixed Claude argv; the untrusted prompt is never included."""

    command = [
        str(settings.claude_bin),
        "-p",
        "--output-format",
        "json",
        "--safe-mode",
        "--disable-slash-commands",
        "--no-chrome",
        "--strict-mcp-config",
        "--mcp-config",
        EMPTY_MCP_CONFIG,
        "--permission-mode",
        "dontAsk",
    ]
    if resume_session_id is not None:
        command += ["--resume", resume_session_id]
    command += ["--model", model_cli_value]
    if use_web:
        command += [
            "--tools",
            "WebSearch,WebFetch",
            "--allowedTools",
            "WebSearch,WebFetch",
        ]
    else:
        command += ["--tools", ""]
    command += ["--max-turns", str(max_turns)]
    return command


def truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = "...[truncated]"
    return value[: max(1, limit - len(marker))] + marker


def _sanitize_error(stderr: str) -> str:
    """Classify a failure without returning untrusted CLI diagnostics.

    Claude can echo prompts, account identifiers, paths, or credentials on
    stderr.  Raw diagnostics therefore stay outside both MCP responses and the
    audit database.
    """

    diagnostic = stderr.casefold()
    if any(term in diagnostic for term in ("not logged in", "authentication", "login")):
        return "Claude authentication is unavailable"
    if "model" in diagnostic and any(
        term in diagnostic for term in ("not available", "unsupported", "not supported")
    ):
        return "the selected Claude model is unavailable"
    return "Claude CLI returned an error"


def child_environment() -> dict[str, str]:
    allowed = {"HOME", "PATH", "LANG", "LC_ALL", "TERM", "NO_COLOR"}
    result = {
        key: value
        for key, value in os.environ.items()
        if key in allowed or key.startswith(("LC_", "XDG_"))
    }
    result["NO_COLOR"] = "1"
    return result


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while True:
        chunk = await stream.read(min(65_536, max(1, remaining)))
        if not chunk:
            break
        if remaining > 0:
            chunks.append(chunk[:remaining])
            remaining -= len(chunk)
        if remaining <= 0:
            remaining = 0
    return b"".join(chunks)[: limit + 1]


async def _stop_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def _finish_reads(*tasks: asyncio.Task[bytes]) -> tuple[bytes, ...]:
    for task in tasks:
        task.cancel()
    return await asyncio.gather(*tasks, return_exceptions=True)  # type: ignore[return-value]


async def run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    output_limit: int,
    prompt: str | None = None,
) -> ExecutionResult:
    """Run Claude in a new process group with prompt stdin and bounded pipes."""

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,
            env=child_environment(),
        )
    except (OSError, ValueError) as exc:
        raise ExecutionError("failed to start Claude CLI") from exc

    stdout_task = asyncio.create_task(_read_bounded(process.stdout, output_limit))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, output_limit))
    timed_out = False
    try:
        assert process.stdin is not None
        process.stdin.write((prompt or "").encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()
        outputs = await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task, process.wait()),
            timeout=timeout_seconds,
        )
        stdout_bytes, stderr_bytes = outputs[:2]
    except asyncio.TimeoutError:
        timed_out = True
    except asyncio.CancelledError:
        await _finish_reads(stdout_task, stderr_task)
        await _stop_process_group(process)
        raise
    finally:
        if timed_out:
            await _finish_reads(stdout_task, stderr_task)
            await _stop_process_group(process)

    if timed_out:
        raise ExecutionError(f"Claude CLI timed out after {timeout_seconds} seconds")
    stdout = stdout_bytes[:output_limit].decode("utf-8", errors="replace")
    stderr = stderr_bytes[:output_limit].decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise ExecutionError(
            f"Claude CLI failed with exit code {process.returncode}: "
            f"{_sanitize_error(stderr)}"
        )
    return ExecutionResult(stdout, stderr, process.returncode or 0)
