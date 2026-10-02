"""Bounded, fixed-argv execution of the non-interactive Gemini CLI."""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess

from .config import Settings
from .diagnostics import attach_raw_error


# Web research commonly needs more than eight search/fetch rounds. This remains
# a per-invocation safety bound, not a bridge call quota.
DEFAULT_MAX_TURNS = 20
DEFAULT_TIMEOUT_SECONDS = 600
MIN_MAX_TURNS = 1
MAX_MAX_TURNS = 100
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 3_600


class ExecutionError(RuntimeError):
    """A sanitized execution failure."""


ERROR_CATEGORIES = (
    "auth",
    "rate_limit",
    "model_unavailable",
    "permission",
    "network",
    "invalid_argument",
    "runtime",
    "timeout",
    "unknown",
)


class GeminiResultError(ExecutionError):
    """A sanitized error reported by the CLI."""

    def __init__(
        self, category: str, http_status: int | None = None, exit_code: int | None = None
    ):
        self.category = category if category in ERROR_CATEGORIES else "unknown"
        self.http_status = (
            http_status
            if type(http_status) is int and http_status in _HTTP_STATUS_CATEGORIES
            else None
        )
        self.exit_code = (
            exit_code if type(exit_code) is int and -255 <= exit_code <= 255 else None
        )
        prefix = "Gemini CLI returned a sanitized error"
        if self.exit_code is not None:
            prefix = f"Gemini CLI failed with exit code {self.exit_code}: {prefix}"
        suffix = f" (category: {self.category}"
        suffix += (
            f", http_status: {self.http_status})" if self.http_status is not None else ")"
        )
        super().__init__(prefix + suffix)


class CommandTimeoutError(ExecutionError):
    """A trusted server-enforced process timeout."""


class OutputLimitExceededError(ExecutionError):
    """A process stream exceeded the configured bounded output limit."""


class OptionsValidationError(ExecutionError):
    """Caller-supplied execution options are invalid."""

# Only these structured names are inspected in bounded stdout.  Values are
# never copied into responses; they are used solely to select a category.
_ERROR_TYPE_FIELDS = ("type", "subtype", "error", "error.type", "error.name")
_ERROR_MESSAGE_FIELDS = ("error.message", "error.msg")
_ERROR_ITEM_FIELDS = ("message", "msg", "type", "name")
_ERROR_STATUS_FIELDS = (
    "error.status",
    "error.status_code",
    "error.statusCode",
    "error.http_status",
)
_MAX_ERROR_JSON_CHARS = 16_384
_MAX_ERROR_TEXT_CHARS = 2_048
_MAX_ERROR_ITEMS = 16
_GENERIC_CATEGORY = "runtime"
_MAX_INIT_LINE_BYTES = 65_536
_REQUIRED_WEB_RESEARCH_TOOLS = frozenset(("search_web", "read_url_content"))
_CONTROLLED_STDIN_COMMANDS = frozenset((("--version",), ("auth", "status")))

# Exact lowercase SDK/API values, matched without inferring from arbitrary
# prose.  Result subtype values are expected to arrive through error.type.
_ERROR_TYPES: dict[str, str] = {
    "invalid_request_error": "invalid_argument",
    "authentication_error": "auth",
    "authenticationerror": "auth",
    "permission_error": "permission",
    "permissionerror": "permission",
    "not_found_error": "runtime",
    "model_not_found_error": "model_unavailable",
    "rate_limit_error": "rate_limit",
    "ratelimiterror": "rate_limit",
    "too_many_requests": "rate_limit",
    "requesterrortimeout": "network",
    "api_error": "runtime",
    "apierror": "runtime",
    "overloaded_error": "runtime",
    "error_during_response": "runtime",
    "timeouterror": "runtime",
    "value_error": "invalid_argument",
}

# Fixed phrases accepted inside a confirmed is_error result/errors envelope.
# They select an enum only; no source text, numbers, paths, or identifiers are
# returned.  More specific evidence takes precedence over generic API errors.
_ERROR_TEXT_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("authentication_error", "authentication error", "oauth token expired"), "auth"),
    (("rate limit reached", "rate limit exceeded", "you've hit your limit", "usage limit reached"), "rate_limit"),
    (("model not found", "model is unavailable", "model is not available"), "model_unavailable"),
    (("permission denied", "permission error", "permission_error", "forbidden", "access denied", "request not allowed"), "permission"),
    (("invalid request error", "invalid request"), "invalid_argument"),
    (("connection error", "network error", "request timed out"), "network"),
    (("api error", "temporarily overloaded"), "runtime"),
)
_ERROR_MESSAGES = {phrase: category for phrases, category in _ERROR_TEXT_RULES for phrase in phrases}

# Bounded, exact stderr fragments.  They classify diagnostics without echoing
# them.  Unknown or ambiguous output remains unknown.
_STDERR_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("not logged in", "authentication error", "authentication failed", "invalid api key", "login required"), "auth"),
    (("rate limit error", "rate limit exceeded", "rate_limit_error"), "rate_limit"),
    (("model not found", "model is unavailable", "model is not available", "model is unsupported", "model is not supported"), "model_unavailable"),
    (("permission denied", "permission error", "permission_error", "permission mode", "forbidden", "access denied", "request not allowed"), "permission"),
    (("network error", "temporary network failure", "connection refused", "connection reset", "connection closed", "connection error", "econnrefused", "econnreset", "enotfound", "etimedout", "fetch failed", "socket disconnected", "network is unreachable"), "network"),
    (("invalid argument", "invalid option", "invalid flag", "unknown option", "unknown flag", "invalid value"), "invalid_argument"),
)

_HTTP_STATUS_CATEGORIES: dict[int, str] = {
    400: "invalid_argument", 401: "auth", 403: "permission", 404: "runtime",
    408: "runtime", 413: "invalid_argument", 422: "invalid_argument",
    429: "rate_limit", 500: "runtime", 502: "network", 503: "runtime", 504: "network",
}


def classify_gemini_error(stdout: str, stderr: str = "") -> tuple[str, int | None]:
    """Classify a Gemini failure with stdout structured evidence first."""

    category, status = _structured_error_category(stdout)
    if category == "unknown":
        category = _stderr_category(stderr)
    return category, status


def _is_error_envelope(payload: dict[object, object]) -> bool:
    return (
        payload.get("is_error") is True
        or isinstance(payload.get("error"), dict)
        or payload.get("type") == "error"
    )


def _text_error_category(value: str) -> tuple[str | None, str | None]:
    normalized = " ".join(value.casefold().translate(str.maketrans({
        "\u2019": "'", "\u2018": "'", "\u02bc": "'", "\uff07": "'",
    })).split())
    specific: str | None = None
    generic: str | None = None
    for phrases, category in _ERROR_TEXT_RULES:
        if not any(phrase in normalized for phrase in phrases):
            continue
        if category == _GENERIC_CATEGORY:
            generic = generic or category
        else:
            specific = category
            break
    return specific, generic


@dataclass(frozen=True)
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int


def validate_options(
    *, model: str, use_web: object, max_turns: object, timeout_seconds: object
) -> tuple[str, bool, int, int]:
    if not isinstance(model, str):
        raise OptionsValidationError("model must be a string")
    if not isinstance(use_web, bool):
        raise OptionsValidationError("use_web must be boolean")
    if isinstance(max_turns, bool) or not isinstance(max_turns, int):
        raise OptionsValidationError("max_turns must be an integer")
    if not MIN_MAX_TURNS <= max_turns <= MAX_MAX_TURNS:
        raise OptionsValidationError("max_turns must be between 1 and 100")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
        raise OptionsValidationError("timeout_seconds must be an integer")
    if not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise OptionsValidationError("timeout_seconds must be between 1 and 3600")
    return model, use_web, max_turns, timeout_seconds


def build_version_command(gemini_bin: Path) -> list[str]:
    return [str(gemini_bin), "--version"]


def build_ask_command(
    settings: Settings,
    *,
    resume_session_id: str | None,
    model_cli_value: str,
    reasoning_effort: str | None = None,
    use_web: bool,
    max_turns: int,
) -> list[str]:
    """Build fixed Antigravity stream-json arguments; prompt stays on stdin."""

    # The prompt is a JSON user event on stdin, so it never enters argv,
    # process listings, or SSH audit records.
    command = [
        str(settings.gemini_bin),
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--disable-slash-commands",
        "--agent",
        "research-web" if use_web else "research-offline",
    ]
    if resume_session_id is not None:
        command += ["--conversation", resume_session_id]
    if model_cli_value != "auto":
        command += ["--model", model_cli_value]
    if reasoning_effort is not None:
        command += ["--effort", reasoning_effort]
    # max_turns is intentionally not translated to a CLI flag: Antigravity
    # 1.2.14 does not expose one. It remains a compatibility input at the MCP
    # layer; timeout_seconds and bounded output pipes are the enforceable
    # process-level bounds.
    return command


def truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = "...[truncated]"
    return value[: max(1, limit - len(marker))] + marker


def _get_path(payload: object, path: str, depth: int = 2) -> object:
    """Return a value from a small, explicitly named JSON path."""

    if depth < 0:
        return None
    if path.startswith("error."):
        child = payload.get("error") if isinstance(payload, dict) else None
        return _get_path(child, path.removeprefix("error."), depth - 1)
    return payload.get(path) if isinstance(payload, dict) else None


def _structured_error_category(stdout: str) -> tuple[str, int | None]:
    """Classify known SDK JSON errors without reading arbitrary text fields.

    Gemini can echo prompts, account identifiers, paths, or credentials on
    stdout.  Parsing is bounded by output_limit in run_command and by the
    explicit field paths inspected here.
    """

    if len(stdout) > _MAX_ERROR_JSON_CHARS:
        return "unknown", None
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError, RecursionError):
        return "unknown", None
    if not isinstance(payload, dict):
        return "unknown", None

    status: int | None = None
    status_category: str | None = None
    for field in _ERROR_STATUS_FIELDS:
        raw_status = _get_path(payload, field)
        if isinstance(raw_status, int) and not isinstance(raw_status, bool):
            if raw_status in _HTTP_STATUS_CATEGORIES:
                status = raw_status
                status_category = _HTTP_STATUS_CATEGORIES[raw_status]
            break
        if isinstance(raw_status, str):
            candidate = _HTTP_STATUS_CATEGORIES.get(int(raw_status, 10)) if (
                raw_status.isascii() and raw_status.isdigit() and len(raw_status) <= 3
            ) else None
            if candidate is not None:
                status = int(raw_status)
                status_category = candidate
            break

    specific_category = status_category if status_category != _GENERIC_CATEGORY else None
    generic_category = status_category

    for field in _ERROR_TYPE_FIELDS:
        value = _get_path(payload, field)
        if isinstance(value, str):
            normalized = value.strip().casefold()
            candidate = _ERROR_TYPES.get(normalized)
            if candidate == _GENERIC_CATEGORY:
                generic_category = generic_category or candidate
            elif candidate is not None:
                specific_category = candidate
                break

    for field in _ERROR_MESSAGE_FIELDS:
        if specific_category is not None:
            break
        value = _get_path(payload, field)
        if not isinstance(value, str):
            continue
        normalized = value.strip().casefold()
        candidate = _ERROR_MESSAGES.get(normalized)
        if candidate == _GENERIC_CATEGORY:
            generic_category = generic_category or candidate
        elif candidate is not None:
            specific_category = candidate
            break
    if specific_category is None and _is_error_envelope(payload):
        result = payload.get("result")
        if isinstance(result, str) and len(result) <= _MAX_ERROR_TEXT_CHARS:
            specific_category, generic_candidate = _text_error_category(result)
            generic_category = generic_category or generic_candidate
        errors = payload.get("errors")
        if isinstance(errors, list):
            for item in errors[:_MAX_ERROR_ITEMS]:
                values: tuple[object, ...]
                if isinstance(item, str):
                    values = (item,)
                elif isinstance(item, dict):
                    values = tuple(item.get(field) for field in _ERROR_ITEM_FIELDS)
                else:
                    continue
                for value in values:
                    if not isinstance(value, str) or len(value) > _MAX_ERROR_TEXT_CHARS:
                        continue
                    exact = _ERROR_TYPES.get(value.strip().casefold())
                    if exact == _GENERIC_CATEGORY:
                        generic_category = generic_category or exact
                    elif exact is not None:
                        specific_category = exact
                        break
                    candidate, generic_candidate = _text_error_category(value)
                    generic_category = generic_category or generic_candidate
                    if candidate is not None:
                        specific_category = candidate
                        break
                if specific_category is not None:
                    break
    return specific_category or generic_category or "unknown", status


def _stderr_category(stderr: str) -> str:
    """Classify bounded stderr using exact allowlisted fragments."""

    diagnostic = stderr.casefold()
    for terms, category in _STDERR_RULES:
        if any(term in diagnostic for term in terms):
            return category
    return "unknown"


def child_environment() -> dict[str, str]:
    allowed = {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_PROJECT_ID",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_GENAI_USE_GCA",
        "GEMINI_CLI_USE_COMPUTE_ADC",
    }
    result = {
        key: value
        for key, value in os.environ.items()
        if key in allowed
    }
    result["NO_COLOR"] = "1"
    result["GEMINI_CLI_SYSTEM_DEFAULTS_PATH"] = "/etc/gemini-cli/system-defaults.json"
    return result


class _BoundedAccumulator:
    """A cancellation-safe buffer retaining at most limit + 1 bytes."""

    def __init__(self, limit: int):
        self._remaining = limit + 1
        self._chunks: list[bytes] = []

    def extend(self, chunk: bytes) -> None:
        if self._remaining <= 0:
            return
        retained = chunk[: self._remaining]
        self._chunks.append(retained)
        self._remaining -= len(retained)

    @property
    def remaining(self) -> int:
        return self._remaining

    @property
    def value(self) -> bytes:
        return b"".join(self._chunks)


async def _read_bounded(
    stream: asyncio.StreamReader,
    accumulator: _BoundedAccumulator,
    validator: _StreamInitValidator | None = None,
) -> None:
    while True:
        chunk = await stream.read(65_536)
        if not chunk:
            break
        accumulator.extend(chunk)
        if validator is not None:
            validator.feed(chunk)


async def _stop_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=0.25)
    except asyncio.TimeoutError:
        pass
    # The session leader can exit while a descendant retains its pipes.  Kill
    # the original process group unconditionally so that case cannot block the
    # final bounded drain (or leave an ignored-SIGTERM descendant running).
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def _finish_reads(*tasks: asyncio.Task[None]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _captured_stream(value: bytes, limit: int) -> str:
    return (
            value[: limit + 1].decode("utf-8", errors="replace")
            if len(value) > limit
            else value.decode("utf-8", errors="replace")
    )


def _stream_ask_mode(command: list[str]) -> str | None:
    """Return the research mode for commands built by build_ask_command."""

    is_stream_ask = (
        len(command) >= 8
        and command[0] != ""
        and command[1:3] == ["--input-format", "stream-json"]
        and command[3:5] == ["--output-format", "stream-json"]
        and command[5] == "--disable-slash-commands"
        and command[6] == "--agent"
        and command[7] in ("research-web", "research-offline")
    )
    return command[7] if is_stream_ask else None


def _tool_names(raw_tools: object) -> frozenset[str] | None:
    if not isinstance(raw_tools, list):
        return None
    names: set[str] = set()
    for item in raw_tools:
        name = item if isinstance(item, str) else (
            item.get("name") if isinstance(item, dict) else None
        )
        if not isinstance(name, str) or not name:
            return None
        names.add(name)
    return frozenset(names)


class _StreamInitValidator:
    """Fail-closed parser for the pre-prompt initialization NDJSON frame."""

    def __init__(self, use_web: bool):
        self.required_tools = _REQUIRED_WEB_RESEARCH_TOOLS if use_web else frozenset()
        self.ready = False
        self.failed = False
        self.event: asyncio.Event | None = None
        self._buffer = b""

    def feed(self, chunk: bytes) -> None:
        if self.ready or self.failed:
            return
        self._buffer += chunk
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                if len(self._buffer) > _MAX_INIT_LINE_BYTES:
                    self._fail()
                return
            line = self._buffer[:newline]
            self._buffer = self._buffer[newline + 1 :]
            if not line.strip():
                continue
            if len(line) > _MAX_INIT_LINE_BYTES or not self._accept(line):
                self._fail()
                return

    def _fail(self) -> None:
        self.failed = True
        if self.event is not None:
            self.event.set()

    def _accept(self, line: bytes) -> bool:
        try:
            payload = json.loads(line)
        except (TypeError, ValueError, RecursionError):
            return False
        if not isinstance(payload, dict):
            return False
        raw_tools: object = None
        is_init = (
            payload.get("event") == "init"
            and isinstance(payload.get("init"), dict)
            and (raw_tools := payload["init"].get("tools")) is not None
        ) or (
            payload.get("event") == "system"
            and payload.get("type") == "init"
            and (raw_tools := payload.get("tools")) is not None
        ) or (
            payload.get("type") == "system"
            and payload.get("subtype") == "init"
            and (raw_tools := payload.get("tools")) is not None
        )
        if not is_init:
            return False
        tools = _tool_names(raw_tools)
        if tools is None or not self.required_tools <= tools:
            return False
        self.ready = True
        if self.event is not None:
            self.event.set()
        return True


async def _wait_for_stream_init(
    process: asyncio.subprocess.Process, validator: _StreamInitValidator
) -> None:
    process_task = asyncio.create_task(process.wait())
    assert validator.event is not None
    ready_task = asyncio.create_task(validator.event.wait())
    done, pending = await asyncio.wait(
        (process_task, ready_task), return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if validator.failed:
        raise ExecutionError("Gemini CLI sent an invalid tool initialization event")
    if validator.ready:
        return
    raise ExecutionError("Gemini CLI did not send its tool initialization event")


def _stream_prompt(prompt: str) -> bytes:
    event = json.dumps(
        {"event": "user", "message": {"content": prompt}},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (event + "\n").encode("utf-8")


async def _capture_failure(
    process: asyncio.subprocess.Process,
    *,
    tasks: tuple[asyncio.Task[None], ...],
    streams: tuple[asyncio.StreamReader, ...],
    accumulators: tuple[_BoundedAccumulator, ...],
) -> tuple[bytes, ...]:
    """Stop the whole process group, then drain each dead pipe once."""

    await _stop_process_group(process)
    await _finish_reads(*tasks)
    drains = tuple(
        asyncio.create_task(_read_bounded(stream, accumulator))
        for stream, accumulator in zip(streams, accumulators)
    )
    try:
        await asyncio.wait_for(asyncio.gather(*drains), timeout=0.75)
    except asyncio.TimeoutError:
        for drain in drains:
            drain.cancel()
        await asyncio.gather(*drains, return_exceptions=True)
    return tuple(accumulator.value for accumulator in accumulators)


async def run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    output_limit: int,
    prompt: str | None = None,
) -> ExecutionResult:
    """Run Gemini in a new process group with bounded pipes and stdin."""

    stream_mode = _stream_ask_mode(command)
    stream_protocol = stream_mode is not None
    if stream_protocol and (not isinstance(prompt, str) or not prompt):
        raise OptionsValidationError("prompt must be a non-empty string")

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
        raise ExecutionError("failed to start Gemini CLI") from exc

    stdout_accumulator = _BoundedAccumulator(output_limit)
    stderr_accumulator = _BoundedAccumulator(output_limit)
    validator = (
        _StreamInitValidator(use_web=stream_mode == "research-web")
        if stream_protocol
        else None
    )
    if validator is not None:
        validator.event = asyncio.Event()
    stdout_task = asyncio.create_task(
        _read_bounded(process.stdout, stdout_accumulator, validator)
    )
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, stderr_accumulator))
    timed_out = False
    failure_output: tuple[bytes, ...] | None = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    try:
        assert process.stdin is not None
        if validator is not None:
            remaining = max(0.0, deadline - loop.time())
            try:
                await asyncio.wait_for(
                    _wait_for_stream_init(process, validator), remaining
                )
            except asyncio.TimeoutError:
                raise
            except ExecutionError as exc:
                failure_output = await _capture_failure(
                    process,
                    tasks=(stdout_task, stderr_task),
                    streams=(process.stdout, process.stderr),
                    accumulators=(stdout_accumulator, stderr_accumulator),
                )
                attach_raw_error(
                    exc,
                    stdout=_captured_stream(failure_output[0], output_limit),
                    stderr=_captured_stream(failure_output[1], output_limit),
                )
                raise
        try:
            if stream_protocol:
                process.stdin.write(_stream_prompt(prompt))
                await process.stdin.drain()
            elif tuple(command[1:]) in _CONTROLLED_STDIN_COMMANDS:
                process.stdin.write((prompt or "").encode("utf-8"))
                await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            failure_output = await _capture_failure(
                process,
                tasks=(stdout_task, stderr_task),
                streams=(process.stdout, process.stderr),
                accumulators=(stdout_accumulator, stderr_accumulator),
            )
            error = ExecutionError("Gemini CLI closed its input stream")
            attach_raw_error(
                error,
                stdout=_captured_stream(failure_output[0], output_limit),
                stderr=_captured_stream(failure_output[1], output_limit),
            )
            raise error
        outputs = await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task, process.wait()),
            timeout=max(0.0, deadline - loop.time()),
        )
    except asyncio.TimeoutError:
        timed_out = True
    except asyncio.CancelledError as exc:
        failure_output = await _capture_failure(
            process,
            tasks=(stdout_task, stderr_task),
            streams=(process.stdout, process.stderr),
            accumulators=(stdout_accumulator, stderr_accumulator),
        )
        attach_raw_error(
            exc,
            stdout=_captured_stream(failure_output[0], output_limit),
            stderr=_captured_stream(failure_output[1], output_limit),
        )
        raise
    finally:
        if timed_out and failure_output is None:
            failure_output = await _capture_failure(
                process,
                tasks=(stdout_task, stderr_task),
                streams=(process.stdout, process.stderr),
                accumulators=(stdout_accumulator, stderr_accumulator),
            )

    if timed_out:
        error = CommandTimeoutError(
            f"Gemini CLI timed out after {timeout_seconds} seconds"
        )
        assert failure_output is not None
        attach_raw_error(
            error,
            stdout=_captured_stream(failure_output[0], output_limit),
            stderr=_captured_stream(failure_output[1], output_limit),
        )
        raise error
    stdout_bytes = stdout_accumulator.value
    stderr_bytes = stderr_accumulator.value
    stdout = stdout_bytes[:output_limit].decode("utf-8", errors="replace")
    stderr = stderr_bytes[:output_limit].decode("utf-8", errors="replace")
    if process.returncode != 0:
        category, status = classify_gemini_error(stdout, stderr)
        error = GeminiResultError(category, status, process.returncode)
        attach_raw_error(
            error,
            stdout=_captured_stream(stdout_bytes, output_limit),
            stderr=_captured_stream(stderr_bytes, output_limit),
        )
        raise error
    if len(stdout_bytes) > output_limit or len(stderr_bytes) > output_limit:
        error = OutputLimitExceededError("Gemini CLI exceeded its output limit")
        attach_raw_error(
            error,
            stdout=_captured_stream(stdout_bytes, output_limit),
            stderr=_captured_stream(stderr_bytes, output_limit),
        )
        raise error
    return ExecutionResult(stdout, stderr, process.returncode or 0)
