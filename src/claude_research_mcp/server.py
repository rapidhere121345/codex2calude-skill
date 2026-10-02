"""FastMCP tools for the principal-isolated Claude research bridge."""

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
    Settings,
    clean_claude_session_id,
    load_settings,
    parse_version,
    validate_conversation_id,
    validate_model,
    validate_prompt,
)
from .executor import (
    DEFAULT_MAX_TURNS,
    DEFAULT_TIMEOUT_SECONDS,
    ClaudeResultError,
    CommandTimeoutError,
    ExecutionError,
    OutputLimitExceededError,
    OptionsValidationError,
    build_auth_status_command,
    build_ask_command,
    build_version_command,
    classify_claude_error,
    run_command,
    run_usage_command,
    truncate_text,
    validate_options,
)
from . import diagnostics
from .locking import concurrency_slot
from .persistence import (
    ContinueBurstLimitError,
    SessionOwnershipError,
    attach_claude_session,
    close_conversation,
    create_conversation,
    finish_call,
    get_conversation,
    initialize,
    list_conversations,
    patch_audit_conversation,
    record_audit_event,
    reserve_call,
)
from .usage import (
    get_cached_usage_command_status,
    get_opus_quota_status,
    parse_usage_command_output,
    usage_command_cache_path,
    usage_command_unavailable,
    write_usage_command_cache,
)


INSTRUCTIONS = (
    "Private owner/partner bridge over a shared Claude subscription. Use one "
    "session per logical task; list by title to recover conversation_id, export "
    "to review history, and pass conversation_id to continue. No shell, files, "
    "browser control, or arbitrary commands. Calls may queue; verify important "
    "web results."
)

_STATUS_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_UNSAFE_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_TRANSCRIPT_BYTES = 16_000_000
_MAX_EXPORT_MESSAGES = 500
_EXPORT_FORMATS = frozenset(("markdown", "json"))


def _safe_status_label(value: object) -> str:
    return value if isinstance(value, str) and _STATUS_LABEL_RE.fullmatch(value) else "unknown"


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
    if isinstance(exc, ClaudeResultError):
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


def parse_claude_result(stdout: str, stderr: str = "") -> dict[str, object]:
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError, RecursionError) as exc:
        error = ExecutionError("Claude CLI returned invalid JSON")
        diagnostics.attach_raw_error(error, stdout=stdout, stderr=stderr)
        raise error from exc
    if not isinstance(payload, dict):
        error = ExecutionError("Claude CLI returned invalid JSON")
        diagnostics.attach_raw_error(error, stdout=stdout, stderr=stderr)
        raise error
    result = payload.get("result", "")
    if payload.get("is_error") is True:
        category, status = classify_claude_error(stdout, stderr)
        error = ClaudeResultError(category, status, 0)
        diagnostics.attach_raw_error(error, stdout=stdout, stderr=stderr)
        raise error
    if not isinstance(result, str):
        result = json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    token_keys = {
        "input_tokens": ("input_tokens", "inputTokens"),
        "output_tokens": ("output_tokens", "outputTokens"),
        "cache_creation_input_tokens": (
            "cache_creation_input_tokens",
            "cacheCreationInputTokens",
        ),
        "cache_read_input_tokens": (
            "cache_read_input_tokens",
            "cacheReadInputTokens",
        ),
    }
    raw_usage = payload.get("modelUsage")
    if isinstance(raw_usage, dict):
        sources = [(str(name), value) for name, value in raw_usage.items()]
    elif isinstance(raw_usage, list):
        sources = [
            (str(item.get("model", "unknown")), item)
            for item in raw_usage
            if isinstance(item, dict)
        ]
    else:
        sources = []
    usage: list[dict[str, object]] = []
    for name, value in sources:
        if not isinstance(value, dict):
            continue
        entry: dict[str, object] = {"model": name}
        for normalized, aliases in token_keys.items():
            token_value = next(
                (value[key] for key in aliases if isinstance(value.get(key), int)),
                None,
            )
            if token_value is not None:
                entry[normalized] = token_value
        usage.append(entry)
    if not usage and isinstance(payload.get("usage"), dict):
        entry = {"model": str(payload.get("model") or "unknown")}
        for normalized, aliases in token_keys.items():
            token_value = next(
                (
                    payload["usage"][key]
                    for key in aliases
                    if isinstance(payload["usage"].get(key), int)
                ),
                None,
            )
            if token_value is not None:
                entry[normalized] = token_value
        usage.append(entry)

    actual_models = [str(item["model"]) for item in usage]
    top_model = payload.get("model")
    if isinstance(top_model, str) and top_model and top_model not in actual_models:
        actual_models.insert(0, top_model)
    session_id = clean_claude_session_id(
        payload.get("sessionId") or payload.get("session_id")
    )
    return {
        "result": result,
        "session_id": session_id,
        "actual_models": actual_models,
        "model_usage": usage,
    }


def _assert_requested_family(
    parsed: dict[str, object], family: str
) -> list[str]:
    actual_models = parsed.get("actual_models")
    if not isinstance(actual_models, list):
        actual_models = []
    values = [value for value in actual_models if isinstance(value, str)]
    if not any(family.lower() in value.lower() for value in values):
        raise ExecutionError(
            f"requested Claude model family ({family}) is absent from modelUsage; "
            f"actual models: {', '.join(values) or '(none)'}"
        )
    return values


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


async def _run_usage_probe(settings: Settings) -> dict[str, object]:
    """Try the official /usage screen without ever failing claude_status."""

    sample_path = settings.work_root / "state" / "statusline-observation.json"
    cache_path = usage_command_cache_path(sample_path)
    cached = get_cached_usage_command_status(cache_path)
    if cached is not None:
        return cached
    try:
        async with concurrency_slot(settings.lock_dir, settings.concurrency):
            # The global lock can wait behind another status call; another
            # principal may have refreshed the shared subscription cache.
            cached = get_cached_usage_command_status(cache_path)
            if cached is not None:
                return cached
            output = await run_usage_command(
                settings.claude_bin, cwd=settings.principal_work_dir
            )
            parsed = parse_usage_command_output(output)
            write_usage_command_cache(cache_path, parsed)
            refreshed = get_cached_usage_command_status(cache_path)
            return refreshed if refreshed is not None else usage_command_unavailable()
    except Exception:
        return usage_command_unavailable()


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


def _claude_home() -> Path:
    return Path.home() / ".claude"


def _validate_export_format(value: object) -> str:
    if not isinstance(value, str) or value not in _EXPORT_FORMATS:
        raise ConfigurationError("format must be 'markdown' or 'json'")
    return value


def _project_directory_name(work_dir: Path) -> str:
    encoded = re.sub(r"[^A-Za-z0-9]", "-", str(work_dir.resolve(strict=False)))
    if not encoded or "/" in encoded or encoded in (".", ".."):
        raise ExecutionError("conversation transcript is unavailable")
    return encoded


def _read_transcript(work_dir: Path, claude_session_id: str) -> list[dict[str, object]]:
    claude_home = _claude_home()
    projects_root = claude_home / "projects"
    project_dir = projects_root / _project_directory_name(work_dir)
    transcript = project_dir / f"{claude_session_id}.jsonl"
    try:
        if (
            claude_home.is_symlink()
            or projects_root.is_symlink()
            or project_dir.is_symlink()
            or transcript.is_symlink()
            or not projects_root.is_dir()
            or not project_dir.is_dir()
            or not transcript.is_file()
        ):
            raise OSError("unavailable")
        descriptor = os.open(
            transcript,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb") as source:
            file_stat = os.fstat(source.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise OSError("unavailable")
            if file_stat.st_size > _MAX_TRANSCRIPT_BYTES:
                raise OSError("unavailable")
            raw = source.read(_MAX_TRANSCRIPT_BYTES + 1)
    except (OSError, ValueError):
        raise ExecutionError("conversation transcript is unavailable") from None
    if len(raw) > _MAX_TRANSCRIPT_BYTES:
        raise ExecutionError("conversation transcript is unavailable")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ExecutionError("conversation transcript is unavailable") from exc

    messages: list[dict[str, object]] = []
    for line in lines:
        if not line:
            continue
        try:
            record = json.loads(line)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ExecutionError("conversation transcript is unavailable") from exc
        if not isinstance(record, dict):
            continue
        role = record.get("type") or record.get("role")
        if role not in ("user", "assistant"):
            continue
        if record.get("isMeta") is True or record.get("isSidechain") is True:
            continue
        timestamp = record.get("timestamp")
        timestamp = timestamp if isinstance(timestamp, str) else ""
        content = record.get("message", record.get("content"))
        if isinstance(content, dict):
            content = content.get("content")
        if isinstance(content, list):
            texts = [
                block.get("text", block.get("content"))
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text", block.get("content")), str)
            ]
            text = "\n\n".join(texts)
        elif isinstance(content, str):
            text = content
        else:
            continue
        if not text:
            continue
        messages.append({"role": role, "timestamp": timestamp, "content": text})
    return messages


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
    app = FastMCP("claude-research", instructions=INSTRUCTIONS)

    @app.tool()
    async def claude_status() -> dict[str, object]:
        """Sanitized CLI/model status without account identifiers."""
        start = time.monotonic()
        audit: int | None = None
        request_id = diagnostics.new_request_id()
        try:
            audit = reserve_call(
                settings.database_path,
                principal=settings.principal,
                tool="claude_status",
                conversation_id=None,
                model="-",
                request_id=request_id,
            )
            raw = await _run(
                settings,
                build_version_command(settings.claude_bin),
                timeout_seconds=15,
                prompt=None,
            )
            version = raw.stdout.strip().splitlines()[0] if raw.stdout.strip() else ""
            version_tuple = parse_version(version)
            server_compatible = (
                version_tuple is not None
                and version_tuple >= settings.required_cli_version
            )
            model_status = {}
            for name, policy in settings.models.items():
                model_status[name] = {
                    "cli_supported": server_compatible
                    and version_tuple is not None
                    and version_tuple >= policy.min_cli_version,
                    "min_cli_version": ".".join(str(part) for part in policy.min_cli_version),
                }
            auth_raw = await _run(
                settings,
                build_auth_status_command(settings.claude_bin),
                timeout_seconds=15,
                prompt=None,
            )
            try:
                auth_payload = json.loads(auth_raw.stdout)
            except (TypeError, json.JSONDecodeError) as exc:
                error = ExecutionError("Claude CLI returned invalid auth status")
                diagnostics.attach_raw_error(
                    error, stdout=auth_raw.stdout, stderr=auth_raw.stderr
                )
                raise error from exc
            if not isinstance(auth_payload, dict):
                raise ExecutionError("Claude CLI returned invalid auth status")
            logged_in = auth_payload.get("loggedIn") is True
            auth_method = _safe_status_label(auth_payload.get("authMethod"))
            result = {
                "status": "ok",
                "request_id": request_id,
                "principal": settings.principal,
                "claude_version": version,
                "cli_compatible": server_compatible,
                "logged_in": logged_in,
                "auth_method": auth_method,
                "subscription_type": _safe_status_label(
                    auth_payload.get("subscriptionType")
                ),
                "default_model": "opus" if "opus" in settings.models else next(iter(settings.models)),
                "models": model_status,
            }
            if not logged_in:
                result["opus_quota"] = {
                    "available": False,
                    "source": "claude_code_statusline",
                    "reason": "requires_claude_ai_login",
                    "manual_check_url": "https://claude.ai/usage",
                }
            else:
                try:
                    result["opus_quota"] = await asyncio.to_thread(
                        get_opus_quota_status,
                        settings.work_root / "state" / "statusline-observation.json",
                    )
                    if not result["opus_quota"].get("available"):
                        result["opus_quota"] = await _run_usage_probe(settings)
                except Exception:
                    result["opus_quota"] = usage_command_unavailable()
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
                tool="claude_status",
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
        policy = settings.models[clean_model]
        command = build_ask_command(
            settings,
            resume_session_id=None,
            model_cli_value=policy.cli_value,
            use_web=clean_web,
            max_turns=clean_turns,
        )
        raw = await _run(
            settings, command, timeout_seconds=clean_timeout, prompt=prompt
        )
        parsed = parse_claude_result(raw.stdout, raw.stderr)
        return parsed, _assert_requested_family(parsed, policy.family)

    @app.tool()
    async def claude_once(
        prompt: str,
        model: str = "opus",
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
                tool="claude_once",
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
                tool="claude_once",
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
    async def claude_start(
        prompt: str,
        title: str | None = None,
        model: str = "opus",
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
            audit = reserve_call(
                settings.database_path,
                principal=settings.principal,
                tool="claude_start",
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
                raise ExecutionError("Claude CLI did not return a usable session id")
            attach_claude_session(
                settings.database_path,
                conversation,
                settings.principal,
                str(parsed["session_id"]),
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
                tool="claude_start",
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
    async def claude_continue(
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
            if record.claude_session_id is None:
                raise ConfigurationError("conversation has no attached Claude session")
            logical_model = validate_model(record.model, settings)
            policy = settings.models[logical_model]
            _, use_web, clean_turns, clean_timeout = validate_options(
                model=logical_model,
                use_web=record.use_web,
                max_turns=max_turns,
                timeout_seconds=timeout_seconds,
            )
            audit = reserve_call(
                settings.database_path,
                principal=settings.principal,
                tool="claude_continue",
                conversation_id=clean_conversation,
                model=logical_model,
                continue_burst_limit=settings.continue_burst_limit,
                continue_burst_window_seconds=(
                    settings.continue_burst_window_seconds
                ),
            )
            command = build_ask_command(
                settings,
                resume_session_id=record.claude_session_id,
                model_cli_value=policy.cli_value,
                use_web=use_web,
                max_turns=clean_turns,
            )
            raw = await _run(
                settings, command, timeout_seconds=clean_timeout, prompt=clean_prompt
            )
            parsed = parse_claude_result(raw.stdout, raw.stderr)
            actual_models = _assert_requested_family(parsed, policy.family)
            if parsed["session_id"] is not None:
                attach_claude_session(
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
                tool="claude_continue",
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
                tool="claude_continue",
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
    async def claude_close(conversation_id: str) -> dict[str, object]:
        """Close only the bridge mapping; never delete Claude transcripts."""
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
                tool="claude_close",
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
                tool="claude_close",
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
    async def claude_list_conversations(
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
                tool="claude_list_conversations",
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
                tool="claude_list_conversations",
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
    async def claude_export_conversation(
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
            if record.claude_session_id is None:
                raise ExecutionError("conversation transcript is unavailable")
            claude_session_id = clean_claude_session_id(record.claude_session_id)
            if claude_session_id is None:
                raise ExecutionError("conversation transcript is unavailable")
            audit = reserve_call(
                settings.database_path,
                principal=settings.principal,
                tool="claude_export_conversation",
                conversation_id=owned_conversation,
                model="-",
                request_id=request_id,
            )
            messages = _read_transcript(
                settings.principal_work_dir, claude_session_id
            )
            result = _export_result(
                conversation_id=clean_conversation,
                title=record.title or "Untitled",
                conversation_status="closed" if record.closed else "open",
                format_name=format_name,
                messages=messages,
                character_limit=settings.result_limit,
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
                tool="claude_export_conversation",
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
        raise SystemExit(f"claude-research-mcp: {exc}") from exc
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
