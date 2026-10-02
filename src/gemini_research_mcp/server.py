"""FastMCP tools for the principal-isolated Gemini research bridge."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import time
import unicodedata
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .config import (
    ConfigurationError,
    ModelPolicy,
    REASONING_EFFORTS,
    Settings,
    clean_gemini_session_id,
    load_settings,
    parse_version,
    validate_conversation_id,
    validate_model,
    validate_prompt,
)
from .executor import (
    DEFAULT_MAX_TURNS,
    DEFAULT_TIMEOUT_SECONDS,
    GeminiResultError,
    CommandTimeoutError,
    ExecutionError,
    OutputLimitExceededError,
    OptionsValidationError,
    build_ask_command,
    build_version_command,
    run_command,
    truncate_text,
    validate_options,
)
from . import diagnostics
from .locking import concurrency_slot
from .persistence import (
    ContinueBurstLimitError,
    SessionOwnershipError,
    attach_gemini_session,
    close_conversation,
    create_conversation,
    finish_call,
    get_conversation,
    initialize,
    list_conversation_messages,
    list_conversations,
    count_audit_texts,
    patch_audit_conversation,
    record_audit_event,
    record_conversation_messages,
    reserve_call,
)
INSTRUCTIONS = (
    "Private owner/partner bridge over official Antigravity agy. Use one session per "
    "logical task; list by title to recover conversation_id, export to review "
    "history, and pass conversation_id to continue. The bridge does not expose "
    "shell, file, browser, or arbitrary MCP controls. Web search is enabled "
    "only when the request asks for it; verify important web results."
)

_STATUS_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_UNSAFE_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_USAGE_JSON_CHARS = 16_384
_MAX_USAGE_ACCOUNTS = 8
_USAGE_LOCATION_RULES = (
    "not currently available in your location",
    "not available in your location",
    "not currently available in your region",
    "not available in your region",
    "not currently available in your country",
    "not available in your country",
    "location is not supported",
    "unsupported location",
)
_USAGE_AUTH_RULES = (
    "not authenticated",
    "authentication required",
    "authentication error",
    "login required",
    "oauth token expired",
)
_USAGE_ELIGIBILITY_RULES = (
    "account is not eligible",
    "account is ineligible",
    "you are not eligible",
)
_MAX_TRANSCRIPT_BYTES = 16_000_000
_MAX_TRANSCRIPT_SCAN_FILES = 256
_MAX_TRANSCRIPT_SCAN_BYTES = 32_000_000
_MAX_AUTH_FILE_BYTES = 1_000_000
_MAX_EXPORT_MESSAGES = 500
_EXPORT_FORMATS = frozenset(("markdown", "json"))
_GEMINI_ERROR_TYPES = {
    "fatalauthenticationerror": "auth",
    "unauthorizederror": "auth",
    "changeauthrequestederror": "auth",
    "fatalinputerror": "invalid_argument",
    "badrequesterror": "invalid_argument",
    "forbiddenerror": "permission",
    "accountsuspendederror": "permission",
    "fataluntrustedworkspaceerror": "permission",
    "fatalturnlimitederror": "rate_limit",
    "cancelederror": "cancellation",
}


def _safe_status_label(value: object) -> str:
    return (
        value
        if isinstance(value, str) and _STATUS_LABEL_RE.fullmatch(value)
        else "unknown"
    )


def _validate_title(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigurationError("title must be a short human-readable label")
    title = value.strip()
    if not 1 <= len(title) <= 120:
        raise ConfigurationError("title must be a short human-readable label")
    for character in title:
        codepoint = ord(character)
        if (
            codepoint < 32
            or codepoint == 127
            or 0x80 <= codepoint <= 0x9F
            or 0xD800 <= codepoint <= 0xDFFF
            or unicodedata.category(character).startswith("C")
        ):
            raise ConfigurationError("title contains an invalid character")
    return title


def _validate_list_pagination(
    include_closed: object, limit: object, offset: object
) -> tuple[bool, int, int]:
    if not isinstance(include_closed, bool):
        raise ConfigurationError("include_closed must be boolean")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ConfigurationError("limit must be between 1 and 100")
    if (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset < 0
        or offset > 1_000_000
    ):
        raise ConfigurationError("offset must be between 0 and 1000000")
    return include_closed, limit, offset


def _error(exc: BaseException, prompt: object = None) -> ExecutionError:
    if isinstance(exc, SessionOwnershipError):
        message = "conversation is unavailable"
    else:
        message = (
            str(exc)
            if isinstance(exc, (ConfigurationError, ExecutionError))
            else exc.__class__.__name__
        )
    if isinstance(prompt, str) and len(prompt) >= 16 and prompt in message:
        message = message.replace(prompt, "[prompt redacted]")
    message = truncate_text(message, 500)
    request_id = getattr(exc, "request_id", None)
    if isinstance(request_id, str):
        message += f" [request_id: {request_id}]"
    raw_saved = getattr(exc, "raw_log_saved", None)
    if isinstance(raw_saved, bool):
        message += f" [raw_log_saved: {str(raw_saved).lower()}]"
    return ExecutionError(message)


def _error_metadata(exc: BaseException) -> tuple[str, int | None, int | None]:
    if isinstance(exc, GeminiResultError):
        return exc.category, exc.http_status, exc.exit_code
    if isinstance(exc, CommandTimeoutError):
        return "timeout", None, None
    if isinstance(exc, OutputLimitExceededError):
        return "output_limit", None, None
    if isinstance(exc, asyncio.CancelledError):
        return "cancellation", None, None
    if isinstance(exc, SessionOwnershipError):
        return "permission", None, None
    if isinstance(exc, ContinueBurstLimitError):
        return "rate_limit", None, None
    if isinstance(exc, ConfigurationError):
        return "validation", None, None
    if isinstance(exc, OptionsValidationError):
        return "validation", None, None
    if isinstance(exc, ExecutionError):
        return "runtime", None, None
    return "unknown", None, None


def _failure_status(exc: BaseException) -> str:
    if isinstance(exc, CommandTimeoutError):
        return "timeout"
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    return "error"


def _save_raw_error(
    settings: Settings, exc: BaseException, request_id: str
) -> bool | None:
    data = diagnostics.raw_error_data(exc)
    if data is None:
        return None
    writer = diagnostics.write_raw_error
    if writer is None:
        return False
    try:
        return bool(
            writer(
                settings.work_root,
                principal=settings.principal,
                request_id=request_id,
                stdout=data.stdout,
                stderr=data.stderr,
            )
        )
    except Exception:
        return False


def _annotate_error(exc: BaseException, request_id: str, raw_saved: bool | None) -> None:
    exc.request_id = request_id  # noqa: PYI025
    if raw_saved is not None:
        exc.raw_log_saved = raw_saved  # noqa: PYI025


def _finish_safely(database_path: Path, audit_id: int | None, **kwargs) -> None:
    try:
        if audit_id is None:
            record_audit_event(database_path, **kwargs)
        else:
            finish_keys = {
                "status",
                "duration_ms",
                "error_category",
                "http_status",
                "exit_code",
                "request_id",
                "raw_log_saved",
            }
            finish_call(
                database_path,
                audit_id,
                **{key: kwargs[key] for key in finish_keys if key in kwargs},
            )
    except Exception:
        # Persistence diagnostics must not replace or reveal the caller's
        # original outcome.  Cancellation is a BaseException and still escapes.
        pass


def parse_gemini_result(stdout: str, stderr: str = "") -> dict[str, object]:
    try:
        lines = [line for line in stdout.splitlines() if line.strip()]
        if lines and lines[-1].lstrip().startswith("{"):
            payloads: list[dict[str, object]] = []
            for line in lines:
                try:
                    payload = json.loads(line)
                except (TypeError, ValueError, RecursionError) as exc:
                    raise _gemini_protocol_error(
                        "Gemini CLI returned invalid JSON", stdout, stderr
                    ) from exc
                if not isinstance(payload, dict):
                    raise _gemini_protocol_error(
                        "Gemini CLI returned invalid JSON", stdout, stderr
                    )
                payloads.append(payload)
        else:
            payload = _load_single_json_object(stdout)
            payloads = [] if payload is None else [payload]
    except (TypeError, ValueError, RecursionError) as exc:
        raise _gemini_protocol_error(
            "Gemini CLI returned invalid JSON", stdout, stderr
        ) from exc

    official_events = [item for item in payloads if isinstance(item.get("event"), str)]
    official_result_frames = [
        item for item in official_events if item.get("event") == "result"
    ]
    if official_events:
        final_records = []
        for item in official_result_frames:
            nested = item.get("result")
            if not isinstance(nested, dict):
                raise _gemini_protocol_error(
                    "Gemini CLI returned an invalid result frame", stdout, stderr
                )
            final_records.append(nested)
    elif payloads and any("type" in item for item in payloads):
        final_records = [item for item in payloads if item.get("type") == "result"]
    else:
        final_records = payloads
    if len(final_records) != 1:
        raise _gemini_protocol_error(
            "Gemini CLI did not return exactly one result", stdout, stderr
        )
    payload = final_records[0]

    if payload.get("error") is not None:
        category = _gemini_error_type(payload)
        error = GeminiResultError(category, 0)
        diagnostics.attach_raw_error(error, stdout=stdout, stderr=stderr)
        raise error

    denied_actions = payload.get("denied_actions")
    if denied_actions:
        raise _gemini_sanitized_failure(
            payload, stdout, stderr, "Gemini CLI denied one or more actions"
        )

    failure = payload.get("failure")
    if failure is not None:
        raise _gemini_sanitized_failure(
            payload, stdout, stderr, "Gemini CLI returned a sanitized error"
        )

    status_value = payload.get("status")
    if status_value is not None and status_value != "SUCCESS":
        raise _gemini_sanitized_failure(
            payload, stdout, stderr, "Gemini CLI returned a sanitized error"
        )

    conversation_id = payload.get("conversation_id")
    session_id = conversation_id
    if session_id is None:
        # Gemini CLI 0.62 compatibility fixtures expose the native session
        # identifier under session_id rather than Antigravity's conversation_id.
        session_id = payload.get("session_id")
    if conversation_id is not None and not isinstance(conversation_id, str):
        conversation_id = None
    session_id = clean_gemini_session_id(session_id)
    result = payload.get("response")
    if result is None:
        result = ""
    elif not isinstance(result, str):
        result = json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    usage: list[dict[str, object]] = []
    raw_usage = payload.get("usage")
    if isinstance(raw_usage, dict):
        entry: dict[str, object] = {}
        token_aliases = {
            "input_tokens": (
                "input_tokens",
                "inputTokens",
                "prompt_token_count",
                "promptTokenCount",
            ),
            "output_tokens": ("output_tokens", "outputTokens"),
            "thinking_tokens": ("thinking_tokens", "thinkingTokens"),
            "cached_tokens": (
                "cache_read_tokens",
                "cacheReadTokens",
                "cached_tokens",
                "cachedTokens",
            ),
            "total_tokens": ("total_tokens", "totalTokens"),
        }
        for normalized, aliases in token_aliases.items():
            token_value = next(
                (
                    raw_usage[key]
                    for key in aliases
                    if type(raw_usage.get(key)) is int
                ),
                None,
            )
            if token_value is not None:
                entry[normalized] = token_value
        if entry:
            usage.append(entry)

    stats = payload.get("stats")
    stats_models = stats.get("models") if isinstance(stats, dict) else None
    if isinstance(stats_models, dict):
        for name, values in stats_models.items():
            if not isinstance(values, dict):
                continue
            entry = {"model": str(name)}
            token_source = values.get("tokens")
            if not isinstance(token_source, dict):
                token_source = values
            token_aliases = {
                "input_tokens": ("input_tokens", "inputTokens", "input"),
                "output_tokens": ("output_tokens", "outputTokens", "output"),
                "total_tokens": ("total_tokens", "totalTokens", "total"),
                "cached_tokens": ("cached_tokens", "cachedTokens", "cached"),
            }
            for normalized, aliases in token_aliases.items():
                token_value = next(
                    (
                        token_source[key]
                        for key in aliases
                        if type(token_source.get(key)) is int
                    ),
                    None,
                )
                if token_value is not None:
                    entry[normalized] = token_value
            if len(entry) > 1:
                usage.append(entry)

    actual_models = [
        str(model)
        for item in usage
        if isinstance((model := item.get("model")), str) and model
    ]

    return {
        "result": result,
        "session_id": session_id,
        "actual_models": actual_models,
        "model_usage": usage,
    }


def _load_single_json_object(stdout: str) -> dict[str, object] | None:
    stripped = stdout.strip()
    if not stripped.startswith("{"):
        return None
    payload = json.loads(stripped)
    return payload if isinstance(payload, dict) else None


def _gemini_protocol_error(
    message: str, stdout: str, stderr: str
) -> ExecutionError:
    error = ExecutionError(message)
    diagnostics.attach_raw_error(error, stdout=stdout, stderr=stderr)
    return error


def _gemini_sanitized_failure(
    payload: dict[str, object],
    stdout: str,
    stderr: str,
    message: str,
) -> GeminiResultError:
    category = "runtime"
    failure = payload.get("failure")
    failure_type = (
        failure.get("type")
        if isinstance(failure, dict)
        else payload.get("failure_type")
    )
    if isinstance(failure_type, str):
        candidate = failure_type.strip().casefold()
        candidate = candidate.removeprefix("agent_").removesuffix("_error")
        category = {
            "auth": "auth",
            "authentication": "auth",
            "permission": "permission",
            "permissiondenied": "permission",
            "ratelimit": "rate_limit",
            "modelunavailable": "model_unavailable",
            "invalidargument": "invalid_argument",
            "network": "network",
            "timeout": "timeout",
        }.get(candidate, "runtime")
    error = GeminiResultError(category, 0)
    diagnostics.attach_raw_error(error, stdout=stdout, stderr=stderr)
    return error


def _gemini_error_type(payload: dict[str, object]) -> str:
    error = payload.get("error")
    value = error.get("type") if isinstance(error, dict) else None
    if not isinstance(value, str):
        return "unknown"
    return _GEMINI_ERROR_TYPES.get(value.strip().casefold(), "unknown")


def _assert_requested_family(
    parsed: dict[str, object], family: str
) -> list[str]:
    actual_models = parsed.get("actual_models")
    if not isinstance(actual_models, list):
        actual_models = []
    values = [value for value in actual_models if isinstance(value, str)]
    if not values:
        return values
    if "auto" in values or any(family.lower() in value.lower() for value in values):
        return values
    raise ExecutionError(
        f"requested Gemini model family ({family}) is absent from stats.models; "
        f"actual models: {', '.join(values) or '(none)'}"
    )


def _assert_requested_policy(
    parsed: dict[str, object], policy: ModelPolicy
) -> list[str]:
    actual_models = _assert_requested_family(parsed, policy.family)
    # CLI-reported identifiers are compared verbatim. Unknown (empty) metadata
    # remains unknown; no alias, casing, or display-name normalization is guessed.
    if policy.cli_value != "auto" and any(
        model != policy.cli_value for model in actual_models
    ):
        raise ExecutionError(
            "reported model does not exactly match the configured CLI model"
        )
    return actual_models


def _runtime(settings: Settings) -> None:
    work_dir = settings.principal_work_dir
    if work_dir.is_symlink() or (work_dir.exists() and not work_dir.is_dir()):
        raise ExecutionError("principal working directory is unavailable")
    settings.lock_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    initialize(settings.database_path)


async def _run(
    settings: Settings,
    command: list[str],
    *,
    timeout_seconds: int,
    prompt: str | None,
):
    async with concurrency_slot(settings.lock_dir, settings.concurrency):
        return await run_command(
            command,
            cwd=settings.principal_work_dir,
            timeout_seconds=timeout_seconds,
            output_limit=settings.output_limit,
            prompt=prompt,
        )

def build_agy_usage_command(gemini_bin: Path) -> list[str]:
    """Official read-only slash command used only for authentication status."""

    return [str(gemini_bin), "-p", "/usage", "--output-format", "json"]


def _usage_diagnostic(
    stdout: str, stderr: str = ""
) -> tuple[bool, str | None, str]:
    """Interpret /usage without exposing its account or quota payload.

    The third value is a fixed availability enum. Official error prose is used
    only to select that enum and is never copied into tool responses.
    """

    stripped = stdout.strip()
    if not stripped:
        return False, None, "unknown"
    try:
        if len(stripped) > _MAX_USAGE_JSON_CHARS:
            return False, None, "unknown"
        payload = json.loads(stripped)
    except (TypeError, ValueError, RecursionError):
        return False, None, "unknown"
    if not isinstance(payload, dict):
        return False, None, "unknown"

    error = payload.get("error")
    status = payload.get("status")
    if status == "ERROR":
        message = error if isinstance(error, str) else None
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = error["message"]
        if message is not None and message.strip():
            normalized = " ".join(message.casefold().split())
            if any(rule in normalized for rule in _USAGE_LOCATION_RULES):
                return True, "oauth", "unsupported_location"
            if any(rule in normalized for rule in _USAGE_AUTH_RULES):
                return False, "authentication", "authentication_required"
            if any(rule in normalized for rule in _USAGE_ELIGIBILITY_RULES):
                return True, "oauth", "account_ineligible"
        return False, None, "unknown"

    error_present = isinstance(error, str) and bool(error.strip())
    if isinstance(error, dict):
        error_present = error_present or _has_nonempty_string(error, "message")
    if error_present or status == "ERROR":
        return False, "authentication", "authentication_required"
    if status is not None and not isinstance(status, str):
        return False, None, "unknown"
    if status is not None and not _STATUS_LABEL_RE.fullmatch(status):
        return False, None, "unknown"
    if status is not None and status.upper() not in ("SUCCESS", "OK"):
        return False, "authentication", "authentication_required"
    if status is None:
        # A parseable object alone is not evidence that /usage succeeded.
        return False, None, "unknown"

    command = payload.get("command")
    if isinstance(command, str):
        command_name = command.strip().casefold()
        if command_name and command_name not in ("/usage", "usage", "command usage"):
            return False, None, "unknown"

    embedded = payload.get("result")
    if isinstance(embedded, dict):
        if _has_nonempty_string(embedded, "error"):
            return False, "authentication", "authentication_required"
        embedded_status = embedded.get("status")
        if embedded_status is not None:
            if (
                not isinstance(embedded_status, str)
                or not _STATUS_LABEL_RE.fullmatch(embedded_status)
                or embedded_status.upper() not in ("SUCCESS", "OK")
            ):
                return False, "authentication", "authentication_required"
    return True, "authenticated", "eligible"


def _result(
    parsed: dict[str, object],
    *,
    settings: Settings,
    model: str,
    actual_models: list[str],
    use_web: bool,
) -> dict[str, object]:
    return {
        "status": "ok",
        "model": model,
        "actual_models": actual_models,
        "use_web": use_web,
        "result": truncate_text(str(parsed["result"]), settings.result_limit),
        "model_usage": parsed["model_usage"],
    }


def _start_policy(settings: Settings, logical_model: str) -> ModelPolicy:
    policy = settings.models[logical_model]
    if settings.require_explicit_model and policy.cli_value == "auto":
        raise ConfigurationError(
            "model selection pending: bind GEMINI_MCP_MODELS to an explicit CLI model"
        )
    return policy


def _continue_policy(
    settings: Settings, logical_model: str, record: object
) -> ModelPolicy:
    if logical_model not in settings.models:
        raise ConfigurationError("model is unavailable")
    current = settings.models[logical_model]
    cli_value = getattr(record, "model_cli_value", None)
    family = getattr(record, "model_family", None)
    effort = getattr(record, "reasoning_effort", None)
    snapshot = cli_value is not None or family is not None or effort is not None
    if not snapshot:
        if settings.require_explicit_model:
            raise ConfigurationError(
                "conversation has no model snapshot; start a new explicit-model conversation"
            )
        return current
    if not isinstance(cli_value, str):
        raise ConfigurationError("conversation has an invalid model snapshot")
    if family is not None and not isinstance(family, str):
        raise ConfigurationError("conversation has an invalid model snapshot")
    if effort is not None and (
        not isinstance(effort, str) or effort.casefold() not in REASONING_EFFORTS
    ):
        raise ConfigurationError("conversation has an invalid model snapshot")
    if cli_value in (None, "auto"):
        if not settings.require_explicit_model:
            return ModelPolicy(
                logical_name=logical_model,
                cli_value=cli_value,
                family=family if family is not None else current.family,
                min_cli_version=current.min_cli_version,
                reasoning_effort=effort.casefold() if isinstance(effort, str) else None,
            )
        raise ConfigurationError(
            "conversation model selection is pending; start a new conversation"
        )
    return ModelPolicy(
        logical_name=logical_model,
        cli_value=cli_value,
        family=family if family is not None else current.family,
        min_cli_version=current.min_cli_version,
        reasoning_effort=effort.casefold() if isinstance(effort, str) else None,
    )


def _gemini_home() -> Path:
    return Path.home() / ".gemini"


def _plain_file_exists(path: Path) -> bool:
    try:
        return path.exists() and not path.is_symlink() and path.is_file()
    except OSError:
        return False


def _read_json_object(path: Path) -> dict[str, object] | None:
    raw = _read_regular_file(path, _MAX_AUTH_FILE_BYTES)
    return _decode_json_object(raw) if raw else None


def _has_nonempty_string(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    return isinstance(value, str) and bool(value.strip())


def _environment_auth_method() -> str | None:
    # Match Gemini CLI 0.62.x getAuthTypeFromEnv(), without returning values.
    for variable, method in _GEMINI_FLAG_ENV_AUTH.items():
        if os.environ.get(variable, "").casefold() == "true":
            return method
    if os.environ.get("GOOGLE_GEMINI_BASE_URL"):
        return "gateway"
    for variable, method in _GEMINI_API_ENV_AUTH.items():
        if os.environ.get(variable):
            return method
    for variable, method in _GEMINI_VERTEX_ENV_AUTH.items():
        if variable == "GOOGLE_GENAI_USE_VERTEXAI":
            if os.environ.get(variable, "").casefold() != "true":
                continue
        elif not os.environ.get(variable):
            continue
        return method
    return None


def _validate_export_format(value: object) -> str:
    if not isinstance(value, str) or value not in _EXPORT_FORMATS:
        raise ConfigurationError("format must be 'markdown' or 'json'")
    return value


def _read_version(stdout: str) -> tuple[str, tuple[int, ...] | None]:
    for raw in stdout.splitlines():
        version = parse_version(raw)
        if version is not None:
            return raw.strip(), version
    return "", None


def _read_regular_file(path: Path, maximum_bytes: int) -> bytes | None:
    try:
        if path.is_symlink():
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as source:
            file_stat = os.fstat(source.fileno())
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > maximum_bytes:
                return None
            raw = source.read(maximum_bytes + 1)
        return None if len(raw) > maximum_bytes else raw
    except (OSError, ValueError):
        return None


def _decode_json_object(raw: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _message_text(content: object) -> str:
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n\n".join(part for part in parts if part)


def _conversation_lines(raw: bytes) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in raw.decode("utf-8").splitlines():
        if not line:
            continue
        try:
            record = json.loads(line)
        except (TypeError, ValueError, RecursionError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _load_gemini_conversation(
    raw: bytes, gemini_session_id: str
) -> list[dict[str, object]] | None:
    payload = _decode_json_object(raw)
    if payload is not None:
        records = [payload]
        raw_messages = payload.get("messages")
        if isinstance(raw_messages, list):
            records.extend(item for item in raw_messages if isinstance(item, dict))
    else:
        try:
            records = _conversation_lines(raw)
        except UnicodeDecodeError:
            return None

    metadata: dict[str, object] = {}
    messages: dict[str, dict[str, object]] = {}
    for record in records:
        rewind_id = record.get("$rewindTo")
        if isinstance(rewind_id, str):
            keys = list(messages)
            if rewind_id in keys:
                for key in keys[keys.index(rewind_id) :]:
                    messages.pop(key, None)
            else:
                messages.clear()
            continue

        update = record.get("$set")
        if isinstance(update, dict):
            metadata.update(update)
            replacement = update.get("messages")
            if isinstance(replacement, list):
                messages.clear()
                for index, item in enumerate(replacement):
                    if isinstance(item, dict):
                        message_id = item.get("id")
                        messages[str(message_id or f"replacement-{index}")] = item
            continue

        if isinstance(record.get("sessionId"), str):
            metadata.update(record)
            embedded = record.get("messages")
            if isinstance(embedded, list):
                for index, item in enumerate(embedded):
                    if isinstance(item, dict):
                        message_id = item.get("id")
                        messages[str(message_id or f"embedded-{index}")] = item
            continue

        message_id = record.get("id")
        if isinstance(message_id, str):
            messages[message_id] = record

    if metadata.get("sessionId") != gemini_session_id:
        return None

    output: list[dict[str, object]] = []
    for message in messages.values():
        role = message.get("type")
        if role not in ("user", "gemini"):
            continue
        text = _message_text(message.get("displayContent"))
        if not text:
            text = _message_text(message.get("content"))
        stripped = text.strip()
        if not stripped or stripped.startswith(("<session_context>", "<hook_context>")):
            continue
        timestamp = message.get("timestamp")
        output.append(
            {
                "role": "assistant" if role == "gemini" else "user",
                "timestamp": timestamp if isinstance(timestamp, str) else "",
                "content": text,
            }
        )
    return output


def _read_transcript(work_dir: Path, gemini_session_id: str) -> list[dict[str, object]]:
    del work_dir  # Gemini 0.62.x stores sessions by project hash, not cwd name.
    gemini_home = _gemini_home()
    temporary_root = gemini_home / "tmp"
    if (
        gemini_home.is_symlink()
        or temporary_root.is_symlink()
        or not temporary_root.is_dir()
    ):
        raise ExecutionError("conversation transcript is unavailable")

    directories: list[tuple[int, Path]] = [(0, temporary_root)]
    candidates: list[tuple[int, Path, int]] = []
    scanned = 0
    while directories and scanned < _MAX_TRANSCRIPT_SCAN_FILES:
        depth, directory = directories.pop(0)
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except (OSError, ValueError):
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if depth == 0:
                        directories.append((1, Path(entry.path)))
                    elif depth == 1 and entry.name == "chats":
                        directories.append((2, Path(entry.path)))
                    continue
                if (
                    depth != 2
                    or directory.name != "chats"
                    or entry.name.endswith(".unreadable")
                    or ".tmp-" in entry.name
                    or not entry.name.endswith((".json", ".jsonl"))
                ):
                    continue
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_TRANSCRIPT_BYTES:
                    continue
                candidates.append((info.st_mtime_ns, Path(entry.path), info.st_size))
                scanned += 1
                if scanned >= _MAX_TRANSCRIPT_SCAN_FILES:
                    break
            except OSError:
                continue

    remaining_scan_bytes = _MAX_TRANSCRIPT_SCAN_BYTES
    for _, path, size in sorted(candidates, reverse=True):
        if size > remaining_scan_bytes:
            break
        raw = _read_regular_file(path, _MAX_TRANSCRIPT_BYTES)
        if raw is None:
            continue
        remaining_scan_bytes -= len(raw)
        messages = _load_gemini_conversation(raw, gemini_session_id)
        if messages is not None:
            return messages
    raise ExecutionError("conversation transcript is unavailable")


def _sanitize_export_text(value: str) -> str:
    # Keep the caller's own prose and citations faithful; only remove control
    # characters that are unsafe in JSONL/MCP output.
    return _UNSAFE_TEXT_RE.sub("", value)


def _export_result(
    *,
    conversation_id: str,
    title: str,
    conversation_status: str,
    format_name: str,
    messages: list[dict[str, object]],
    character_limit: int,
) -> dict[str, object]:
    total_messages = len(messages)
    bounded = messages[:_MAX_EXPORT_MESSAGES]
    message_truncated = total_messages > len(bounded)
    rendered: list[dict[str, object]] = []
    markdown_parts: list[str] = []
    used = 0
    rendered_message_count = 0
    content_truncated = False
    for message in bounded:
        role = str(message["role"])
        content = _sanitize_export_text(str(message["content"]))
        timestamp = _sanitize_export_text(str(message.get("timestamp", "")))
        if format_name == "markdown":
            heading = f"## {role.title()}"
            if timestamp:
                heading += f" — {timestamp}"
            block = f"{heading}\n\n{content}\n"
            available = max(0, character_limit - used)
            if available == 0:
                content_truncated = True
                break
            if len(block) > available:
                block = block[:available]
                content_truncated = True
            used += len(block)
            markdown_parts.append(block)
            rendered_message_count += 1
            if content_truncated:
                break
        else:
            item = {"role": role, "timestamp": timestamp, "content": content}
            item_size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            available = max(0, character_limit - used)
            if item_size > available:
                content_truncated = True
                overhead = item_size - len(content)
                if available > overhead:
                    content = content[: available - overhead]
                    rendered.append(
                        {"role": role, "timestamp": timestamp, "content": content}
                    )
                    rendered_message_count += 1
                break
            used += item_size
            rendered.append(item)
            rendered_message_count += 1
    truncated = message_truncated or content_truncated
    base = {
        "status": "ok",
        "conversation_id": conversation_id,
        "title": title,
        "conversation_status": conversation_status,
        "format": format_name,
        "message_count": rendered_message_count,
        "total_message_count": total_messages,
        "truncated": truncated,
        "content_truncated": content_truncated,
        "message_truncated": message_truncated,
    }
    if format_name == "markdown":
        base["content"] = "".join(markdown_parts)
    else:
        base["messages"] = rendered
    return base


def create_app(settings: Settings) -> FastMCP:
    _runtime(settings)
    app = FastMCP("gemini-research", instructions=INSTRUCTIONS)

    @app.tool()
    async def gemini_status() -> dict[str, object]:
        """Sanitized CLI and local auth status without model inference."""
        start = time.monotonic()
        audit: int | None = None
        request_id = diagnostics.new_request_id()
        try:
            audit = reserve_call(
                settings.database_path,
                principal=settings.principal,
                tool="gemini_status",
                conversation_id=None,
                model="-",
                request_id=request_id,
            )
            try:
                version_raw = await _run(
                    settings,
                    build_version_command(settings.gemini_bin),
                    timeout_seconds=5,
                    prompt="",
                )
                version, version_tuple = _read_version(version_raw.stdout)
            except ExecutionError as exc:
                version, version_tuple = "", None
            server_compatible = (
                version_tuple is not None
                and version_tuple >= settings.required_cli_version
            )

            usage_logged_in = False
            usage_method: str | None = None
            usage_availability = "unknown"
            try:
                usage_raw = await _run(
                    settings,
                    build_agy_usage_command(settings.gemini_bin),
                    timeout_seconds=10,
                    prompt="",
                )
                if usage_raw.exit_code == 0:
                    (
                        usage_logged_in,
                        usage_method,
                        usage_availability,
                    ) = _usage_diagnostic(usage_raw.stdout)
            except CommandTimeoutError:
                # A timed-out diagnostic cannot establish availability. Keep
                # the old fields false without guessing that this is OAuth.
                usage_logged_in = False
                usage_method = None
                usage_availability = "unknown"
            except ExecutionError as exc:
                # A failed /usage invocation may contain a structured official
                # error. Keep status usable and classify only allowlisted text.
                raw = diagnostics.raw_error_data(exc)
                if raw is not None:
                    (
                        usage_logged_in,
                        usage_method,
                        usage_availability,
                    ) = _usage_diagnostic(raw.stdout)

            model_status = {}
            for name, policy in settings.models.items():
                model_supported = (
                    server_compatible
                    and version_tuple is not None
                    and version_tuple >= policy.min_cli_version
                )
                model_status[name] = {
                    "cli_supported": model_supported,
                    "selection_ready": policy.cli_value != "auto",
                    "reasoning_effort": policy.reasoning_effort,
                    "min_cli_version": ".".join(
                        str(part) for part in policy.min_cli_version
                    ),
                }
            result = {
                "status": "ok",
                "request_id": request_id,
                "principal": settings.principal,
                "cli_name": "antigravity",
                "cli_version": version,
                "gemini_version": version,
                "cli_compatible": server_compatible,
                "logged_in": usage_logged_in,
                "auth_method": _safe_status_label(usage_method),
                "auth": {
                    "configured": usage_logged_in,
                    "logged_in": usage_logged_in,
                    "method": _safe_status_label(usage_method),
                },
                "availability": {
                    "status": _safe_status_label(usage_availability),
                    "eligible": usage_availability == "eligible",
                },
                "models": model_status,
                "default_model": settings.default_model,
                "reasoning_effort": settings.models[
                    settings.default_model
                ].reasoning_effort,
                "model_selection_ready": (
                    settings.models[settings.default_model].cli_value != "auto"
                ),
                "quota": {
                    "available": False,
                    "reason": "not_reported_by_gemini_cli",
                },
            }
            finish_call(
                settings.database_path,
                audit,
                status="success",
                duration_ms=int((time.monotonic() - start) * 1000),
                request_id=request_id,
            )
            return result
        except BaseException as exc:
            raw_saved = _save_raw_error(settings, exc, request_id)
            _finish_safely(
                settings.database_path,
                audit,
                principal=settings.principal,
                tool="gemini_status",
                conversation_id=None,
                model="-",
                status=_failure_status(exc),
                duration_ms=int((time.monotonic() - start) * 1000),
                error_category=_error_metadata(exc)[0],
                http_status=_error_metadata(exc)[1],
                exit_code=_error_metadata(exc)[2],
                request_id=request_id,
                raw_log_saved=raw_saved,
            )
            _annotate_error(exc, request_id, raw_saved)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise _error(exc) from exc

    async def _execute_new(
        prompt: str, model: str, use_web: bool, max_turns: int, timeout_seconds: int
    ) -> tuple[dict[str, object], list[str]]:
        clean_model, clean_web, clean_turns, clean_timeout = validate_options(
            model=model,
            use_web=use_web,
            max_turns=max_turns,
            timeout_seconds=timeout_seconds,
        )
        policy = _start_policy(settings, clean_model)
        command = build_ask_command(
            settings,
            resume_session_id=None,
            model_cli_value=policy.cli_value,
            reasoning_effort=policy.reasoning_effort,
            use_web=clean_web,
            max_turns=clean_turns,
        )
        raw = await _run(settings, command, timeout_seconds=clean_timeout, prompt=prompt)
        parsed = parse_gemini_result(raw.stdout, raw.stderr)
        return parsed, _assert_requested_policy(parsed, policy)

    @app.tool()
    async def gemini_once(
        prompt: str,
        model: str | None = None,
        use_web: bool = True,
        max_turns: int = DEFAULT_MAX_TURNS,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        """Run one isolated query without creating a conversation mapping."""
        start = time.monotonic()
        audit: int | None = None
        logical_model: str | None = None
        request_id = diagnostics.new_request_id()
        try:
            clean_prompt = validate_prompt(prompt, settings.prompt_limit)
            logical_model = validate_model(model, settings)
            logical_model, clean_web, clean_turns, clean_timeout = validate_options(
                model=logical_model,
                use_web=use_web,
                max_turns=max_turns,
                timeout_seconds=timeout_seconds,
            )
            audit = reserve_call(
                settings.database_path,
                principal=settings.principal,
                tool="gemini_once",
                conversation_id=None,
                model=logical_model,
                request_id=request_id,
            )
            parsed, actual_models = await _execute_new(
                clean_prompt, logical_model, clean_web, clean_turns, clean_timeout
            )
            result = _result(
                parsed,
                settings=settings,
                model=logical_model,
                actual_models=actual_models,
                use_web=clean_web,
            )
            result["request_id"] = request_id
            finish_call(
                settings.database_path,
                audit,
                status="success",
                duration_ms=int((time.monotonic() - start) * 1000),
                request_id=request_id,
            )
            return result
        except BaseException as exc:
            raw_saved = _save_raw_error(settings, exc, request_id)
            metadata = _error_metadata(exc)
            _finish_safely(
                settings.database_path,
                audit,
                principal=settings.principal,
                tool="gemini_once",
                conversation_id=None,
                model=logical_model,
                status=_failure_status(exc),
                duration_ms=int((time.monotonic() - start) * 1000),
                error_category=metadata[0],
                http_status=metadata[1],
                exit_code=metadata[2],
                request_id=request_id,
                raw_log_saved=raw_saved,
            )
            _annotate_error(exc, request_id, raw_saved)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise _error(exc, prompt) from exc

    @app.tool()
    async def gemini_start(
        prompt: str,
        title: str | None = None,
        model: str | None = None,
        use_web: bool = True,
        max_turns: int = DEFAULT_MAX_TURNS,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        """Start one logical task and return its opaque server UUID."""
        start = time.monotonic()
        conversation: str | None = None
        logical_model: str | None = None
        audit: int | None = None
        request_id = diagnostics.new_request_id()
        try:
            clean_prompt = validate_prompt(prompt, settings.prompt_limit)
            clean_title = _validate_title(title)
            logical_model = validate_model(model, settings)
            logical_model, clean_web, clean_turns, clean_timeout = validate_options(
                model=logical_model,
                use_web=use_web,
                max_turns=max_turns,
                timeout_seconds=timeout_seconds,
            )
            policy = _start_policy(settings, logical_model)
            audit = reserve_call(
                settings.database_path,
                principal=settings.principal,
                tool="gemini_start",
                conversation_id=None,
                model=logical_model,
                request_id=request_id,
            )
            record = create_conversation(
                settings.database_path,
                settings.principal,
                logical_model,
                clean_web,
                clean_title,
                policy.cli_value,
                policy.family,
                policy.reasoning_effort,
            )
            conversation = record.conversation_id
            if audit is not None:
                # Created after reserving the audit call, so patch the running
                # audit row without emitting a second record.
                patch_audit_conversation(
                    settings.database_path,
                    audit,
                    record.conversation_id,
                    settings.principal,
                )
            parsed, actual_models = await _execute_new(
                clean_prompt, logical_model, clean_web, clean_turns, clean_timeout
            )
            if parsed["session_id"] is None:
                raise ExecutionError("Gemini CLI did not return a usable session id")
            attach_gemini_session(
                settings.database_path,
                conversation,
                settings.principal,
                str(parsed["session_id"]),
            )
            record_conversation_messages(
                settings.database_path,
                conversation_id=conversation,
                principal=settings.principal,
                request_id=request_id,
                prompt=clean_prompt,
                response=truncate_text(str(parsed["result"]), settings.result_limit),
            )
            result = _result(
                parsed,
                settings=settings,
                model=logical_model,
                actual_models=actual_models,
                use_web=clean_web,
            )
            result["conversation_id"] = conversation
            result["title"] = clean_title
            result["request_id"] = request_id
            finish_call(
                settings.database_path,
                audit,
                status="success",
                duration_ms=int((time.monotonic() - start) * 1000),
                request_id=request_id,
            )
            return result
        except BaseException as exc:
            status = _failure_status(exc)
            duration_ms = int((time.monotonic() - start) * 1000)
            raw_saved = _save_raw_error(settings, exc, request_id)
            metadata = _error_metadata(exc)
            _finish_safely(
                settings.database_path,
                audit,
                principal=settings.principal,
                tool="gemini_start",
                conversation_id=conversation,
                model=logical_model,
                status=status,
                duration_ms=duration_ms,
                error_category=metadata[0],
                http_status=metadata[1],
                exit_code=metadata[2],
                request_id=request_id,
                raw_log_saved=raw_saved,
            )
            _annotate_error(exc, request_id, raw_saved)
            if conversation is not None:
                try:
                    close_conversation(
                        settings.database_path, conversation, settings.principal
                    )
                except BaseException:
                    pass
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise _error(exc, prompt) from exc

    @app.tool()
    async def gemini_continue(
        conversation_id: str,
        prompt: str,
        max_turns: int = DEFAULT_MAX_TURNS,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        """Continue an owned conversation with its stored model and web policy."""
        start = time.monotonic()
        audit: int | None = None
        clean_conversation: str | None = None
        owned_conversation: str | None = None
        logical_model: str | None = None
        request_id = diagnostics.new_request_id()
        try:
            clean_conversation = validate_conversation_id(conversation_id)
            clean_prompt = validate_prompt(prompt, settings.prompt_limit)
            record = get_conversation(
                settings.database_path, clean_conversation, settings.principal
            )
            owned_conversation = clean_conversation
            if record.closed:
                raise ConfigurationError("conversation is closed")
            if record.gemini_session_id is None:
                raise ConfigurationError("conversation has no attached Gemini session")
            logical_model = validate_model(record.model, settings)
            policy = _continue_policy(settings, logical_model, record)
            _, use_web, clean_turns, clean_timeout = validate_options(
                model=logical_model,
                use_web=record.use_web,
                max_turns=max_turns,
                timeout_seconds=timeout_seconds,
            )
            audit = reserve_call(
                settings.database_path,
                principal=settings.principal,
                tool="gemini_continue",
                conversation_id=clean_conversation,
                model=logical_model,
                continue_burst_limit=settings.continue_burst_limit,
                continue_burst_window_seconds=(
                    settings.continue_burst_window_seconds
                ),
            )
            command = build_ask_command(
                settings,
                resume_session_id=record.gemini_session_id,
                model_cli_value=policy.cli_value,
                reasoning_effort=policy.reasoning_effort,
                use_web=use_web,
                max_turns=clean_turns,
            )
            raw = await _run(
                settings,
                command,
                timeout_seconds=clean_timeout,
                prompt=clean_prompt,
            )
            parsed = parse_gemini_result(raw.stdout, raw.stderr)
            actual_models = _assert_requested_policy(parsed, policy)
            if parsed["session_id"] is not None:
                attach_gemini_session(
                    settings.database_path,
                    clean_conversation,
                    settings.principal,
                    str(parsed["session_id"]),
                )
            result = _result(
                parsed,
                settings=settings,
                model=logical_model,
                actual_models=actual_models,
                use_web=use_web,
            )
            record_conversation_messages(
                settings.database_path,
                conversation_id=clean_conversation,
                principal=settings.principal,
                request_id=request_id,
                prompt=clean_prompt,
                response=truncate_text(str(parsed["result"]), settings.result_limit),
            )
            result["conversation_id"] = clean_conversation
            result["request_id"] = request_id
            finish_call(
                settings.database_path,
                audit,
                status="success",
                duration_ms=int((time.monotonic() - start) * 1000),
                request_id=request_id,
            )
            return result
        except ContinueBurstLimitError as exc:
            raw_saved = _save_raw_error(settings, exc, request_id)
            _finish_safely(
                settings.database_path,
                None,
                principal=settings.principal,
                tool="gemini_continue",
                conversation_id=owned_conversation,
                model=logical_model,
                status="burst_limited",
                duration_ms=int((time.monotonic() - start) * 1000),
                error_category="rate_limit",
                http_status=None,
                exit_code=None,
                request_id=request_id,
                raw_log_saved=raw_saved,
            )
            error = ConfigurationError(str(exc))
            _annotate_error(error, request_id, raw_saved)
            raise error from exc
        except BaseException as exc:
            raw_saved = _save_raw_error(settings, exc, request_id)
            metadata = _error_metadata(exc)
            _finish_safely(
                settings.database_path,
                audit,
                principal=settings.principal,
                tool="gemini_continue",
                conversation_id=owned_conversation,
                model=logical_model,
                status=_failure_status(exc),
                duration_ms=int((time.monotonic() - start) * 1000),
                error_category=metadata[0],
                http_status=metadata[1],
                exit_code=metadata[2],
                request_id=request_id,
                raw_log_saved=raw_saved,
            )
            _annotate_error(exc, request_id, raw_saved)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise _error(exc, prompt) from exc

    @app.tool()
    async def gemini_close(conversation_id: str) -> dict[str, object]:
        """Close only the bridge mapping; never delete Gemini transcripts."""
        start = time.monotonic()
        request_id = diagnostics.new_request_id()
        owned_conversation: str | None = None
        try:
            clean_conversation = validate_conversation_id(conversation_id)
            close_conversation(
                settings.database_path, clean_conversation, settings.principal
            )
            owned_conversation = clean_conversation
            record_audit_event(
                settings.database_path,
                principal=settings.principal,
                tool="gemini_close",
                conversation_id=clean_conversation,
                model=None,
                status="success",
                duration_ms=int((time.monotonic() - start) * 1000),
                request_id=request_id,
            )
            return {
                "status": "closed",
                "conversation_id": clean_conversation,
                "transcript_deleted": False,
                "request_id": request_id,
            }
        except BaseException as exc:
            raw_saved = _save_raw_error(settings, exc, request_id)
            metadata = _error_metadata(exc)
            _finish_safely(
                settings.database_path,
                None,
                principal=settings.principal,
                tool="gemini_close",
                conversation_id=owned_conversation,
                model=None,
                status=_failure_status(exc),
                duration_ms=int((time.monotonic() - start) * 1000),
                error_category=metadata[0],
                http_status=metadata[1],
                exit_code=metadata[2],
                request_id=request_id,
                raw_log_saved=raw_saved,
            )
            _annotate_error(exc, request_id, raw_saved)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise _error(exc) from exc

    @app.tool()
    async def gemini_list_conversations(
        include_closed: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        """List owned conversations, newest activity first."""
        start = time.monotonic()
        audit: int | None = None
        request_id = diagnostics.new_request_id()
        try:
            closed, page_limit, page_offset = _validate_list_pagination(
                include_closed, limit, offset
            )
            audit = reserve_call(
                settings.database_path,
                principal=settings.principal,
                tool="gemini_list_conversations",
                conversation_id=None,
                model="-",
                request_id=request_id,
            )
            summaries, total, has_more = list_conversations(
                settings.database_path,
                principal=settings.principal,
                include_closed=closed,
                limit=page_limit,
                offset=page_offset,
            )
            items = [
                {
                    "conversation_id": item.conversation_id,
                    "title": item.title or "Untitled",
                    "model": item.model,
                    "use_web": item.use_web,
                    "created_at": item.created_at,
                    "last_activity_at": item.last_activity_at,
                    "status": "closed" if item.closed_at is not None else "open",
                }
                for item in summaries
            ]
            finish_call(
                settings.database_path,
                audit,
                status="success",
                duration_ms=int((time.monotonic() - start) * 1000),
                request_id=request_id,
            )
            return {
                "status": "ok",
                "request_id": request_id,
                "count": len(items),
                "total_count": total,
                "next_offset": page_offset + len(items) if has_more else None,
                "has_more": has_more,
                "conversations": items,
            }
        except BaseException as exc:
            raw_saved = _save_raw_error(settings, exc, request_id)
            metadata = _error_metadata(exc)
            _finish_safely(
                settings.database_path,
                audit,
                principal=settings.principal,
                tool="gemini_list_conversations",
                conversation_id=None,
                model=None,
                status=_failure_status(exc),
                duration_ms=int((time.monotonic() - start) * 1000),
                error_category=metadata[0],
                http_status=metadata[1],
                exit_code=metadata[2],
                request_id=request_id,
                raw_log_saved=raw_saved,
            )
            _annotate_error(exc, request_id, raw_saved)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise _error(exc) from exc

    @app.tool()
    async def gemini_export_conversation(
        conversation_id: str,
        format: str = "markdown",
    ) -> dict[str, object]:
        """Export readable owned conversation text without internal data."""
        start = time.monotonic()
        clean_conversation: str | None = None
        owned_conversation: str | None = None
        audit: int | None = None
        request_id = diagnostics.new_request_id()
        try:
            clean_conversation = validate_conversation_id(conversation_id)
            format_name = _validate_export_format(format)
            record = get_conversation(
                settings.database_path, clean_conversation, settings.principal
            )
            owned_conversation = clean_conversation
            audit = reserve_call(
                settings.database_path,
                principal=settings.principal,
                tool="gemini_export_conversation",
                conversation_id=owned_conversation,
                model="-",
                request_id=request_id,
            )
            history, total_messages = list_conversation_messages(
                settings.database_path,
                conversation_id=clean_conversation,
                principal=settings.principal,
                limit=_MAX_EXPORT_MESSAGES,
            )
            result = _export_result(
                conversation_id=clean_conversation,
                title=record.title or "Untitled",
                conversation_status="closed" if record.closed else "open",
                format_name=format_name,
                messages=[
                    {
                        "role": message.role,
                        "content": message.content,
                        "timestamp": message.created_at,
                    }
                    for message in history
                ],
                character_limit=settings.result_limit,
            )
            result["total_message_count"] = total_messages
            result["message_truncated"] = total_messages > len(history)
            result["truncated"] = bool(result.get("truncated")) or (
                total_messages > len(history)
            )
            result["request_id"] = request_id
            finish_call(
                settings.database_path,
                audit,
                status="success",
                duration_ms=int((time.monotonic() - start) * 1000),
                request_id=request_id,
            )
            return result
        except BaseException as exc:
            raw_saved = _save_raw_error(settings, exc, request_id)
            metadata = _error_metadata(exc)
            _finish_safely(
                settings.database_path,
                audit,
                principal=settings.principal,
                tool="gemini_export_conversation",
                conversation_id=owned_conversation,
                model=None,
                status=_failure_status(exc),
                duration_ms=int((time.monotonic() - start) * 1000),
                error_category=metadata[0],
                http_status=metadata[1],
                exit_code=metadata[2],
                request_id=request_id,
                raw_log_saved=raw_saved,
            )
            _annotate_error(exc, request_id, raw_saved)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise _error(exc) from exc

    return app


def main() -> None:
    try:
        settings = load_settings()
        app = create_app(settings)
    except ConfigurationError as exc:
        raise SystemExit(f"gemini-research-mcp: {exc}") from exc
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
