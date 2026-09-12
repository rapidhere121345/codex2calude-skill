"""FastMCP tools for the principal-isolated Claude research bridge."""

from __future__ import annotations

import json
import re
import time

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
    ExecutionError,
    build_auth_status_command,
    build_ask_command,
    build_version_command,
    run_command,
    truncate_text,
    validate_options,
)
from .locking import concurrency_slot
from .persistence import (
    RateLimitError,
    attach_claude_session,
    close_conversation,
    create_conversation,
    finish_call,
    get_conversation,
    initialize,
    record_audit_event,
    reserve_call,
)


INSTRUCTIONS = (
    "Private owner/partner bridge over a shared Claude subscription. Use one "
    "session per logical task; pass conversation_id to continue. No shell, "
    "files, browser control, or arbitrary commands. Calls may queue; verify "
    "important web results."
)

_STATUS_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def _safe_status_label(value: object) -> str:
    return value if isinstance(value, str) and _STATUS_LABEL_RE.fullmatch(value) else "unknown"


def _error(exc: BaseException, prompt: object = None) -> ExecutionError:
    message = (
        str(exc)
        if isinstance(exc, (ConfigurationError, ExecutionError))
        else exc.__class__.__name__
    )
    if isinstance(prompt, str) and len(prompt) >= 16 and prompt in message:
        message = message.replace(prompt, "[prompt redacted]")
    return ExecutionError(truncate_text(message, 500))


def parse_claude_result(stdout: str) -> dict[str, object]:
    try:
        payload = json.loads(stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ExecutionError("Claude CLI returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ExecutionError("Claude CLI returned invalid JSON")
    result = payload.get("result", "")
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
    settings.lock_dir.mkdir(parents=True, exist_ok=True)
    settings.principal_work_dir.mkdir(parents=True, exist_ok=True)
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


def create_app(settings: Settings) -> FastMCP:
    _runtime(settings)
    app = FastMCP("claude-research", instructions=INSTRUCTIONS)

    @app.tool()
    async def claude_status() -> dict[str, object]:
        """Sanitized CLI/model status without account identifiers."""
        start = time.monotonic()
        audit: int | None = None
        try:
            audit = reserve_call(
                settings.database_path,
                principal=settings.principal,
                tool="claude_status",
                conversation_id=None,
                model="-",
                rate_limit=settings.rate_limit,
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
                raise ExecutionError("Claude CLI returned invalid auth status") from exc
            if not isinstance(auth_payload, dict):
                raise ExecutionError("Claude CLI returned invalid auth status")
            result = {
                "status": "ok",
                "principal": settings.principal,
                "claude_version": version,
                "cli_compatible": server_compatible,
                "logged_in": auth_payload.get("loggedIn") is True,
                "auth_method": _safe_status_label(auth_payload.get("authMethod")),
                "subscription_type": _safe_status_label(
                    auth_payload.get("subscriptionType")
                ),
                "default_model": "opus" if "opus" in settings.models else next(iter(settings.models)),
                "models": model_status,
            }
            finish_call(
                settings.database_path,
                audit,
                status="success",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
            return result
        except RateLimitError as exc:
            record_audit_event(
                settings.database_path,
                principal=settings.principal,
                tool="claude_status",
                conversation_id=None,
                model=None,
                status="rate_limited",
            )
            raise ConfigurationError(str(exc)) from exc
        except BaseException as exc:
            if audit is not None:
                finish_call(
                    settings.database_path,
                    audit,
                    status="error",
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
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
        parsed = parse_claude_result(
            (
                await _run(
                    settings,
                    command,
                    timeout_seconds=clean_timeout,
                    prompt=prompt,
                )
            ).stdout
        )
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
                rate_limit=settings.rate_limit,
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
            finish_call(
                settings.database_path,
                audit,
                status="success",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
            return result
        except RateLimitError as exc:
            record_audit_event(
                settings.database_path,
                principal=settings.principal,
                tool="claude_once",
                conversation_id=None,
                model=None,
                status="rate_limited",
            )
            raise ConfigurationError(str(exc)) from exc
        except BaseException as exc:
            if audit is not None:
                finish_call(
                    settings.database_path,
                    audit,
                    status="timeout" if "timed out" in str(exc) else "error",
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            raise _error(exc, prompt) from exc

    @app.tool()
    async def claude_start(
        prompt: str,
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
                tool="claude_start",
                conversation_id=None,
                model=logical_model,
                rate_limit=settings.rate_limit,
            )
            record = create_conversation(
                settings.database_path, settings.principal, logical_model, clean_web
            )
            conversation = record.conversation_id
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
            finish_call(
                settings.database_path,
                audit,
                status="success",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
            return result
        except RateLimitError as exc:
            record_audit_event(
                settings.database_path,
                principal=settings.principal,
                tool="claude_start",
                conversation_id=None,
                model=logical_model,
                status="rate_limited",
            )
            raise ConfigurationError(str(exc)) from exc
        except BaseException as exc:
            status = "timeout" if "timed out" in str(exc) else "error"
            duration_ms = int((time.monotonic() - start) * 1000)
            if audit is not None:
                finish_call(
                    settings.database_path,
                    audit,
                    status=status,
                    duration_ms=duration_ms,
                )
            else:
                record_audit_event(
                    settings.database_path,
                    principal=settings.principal,
                    tool="claude_start",
                    conversation_id=conversation,
                    model=logical_model,
                    status=status,
                    duration_ms=duration_ms,
                )
            if conversation is not None:
                try:
                    close_conversation(
                        settings.database_path, conversation, settings.principal
                    )
                except BaseException:
                    pass
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
        try:
            clean_conversation = validate_conversation_id(conversation_id)
            clean_prompt = validate_prompt(prompt, settings.prompt_limit)
            record = get_conversation(
                settings.database_path, clean_conversation, settings.principal
            )
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
                rate_limit=settings.rate_limit,
            )
            command = build_ask_command(
                settings,
                resume_session_id=record.claude_session_id,
                model_cli_value=policy.cli_value,
                use_web=use_web,
                max_turns=clean_turns,
            )
            parsed = parse_claude_result(
                (
                    await _run(
                        settings,
                        command,
                        timeout_seconds=clean_timeout,
                        prompt=clean_prompt,
                    )
                ).stdout
            )
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
            finish_call(
                settings.database_path,
                audit,
                status="success",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
            return result
        except RateLimitError as exc:
            record_audit_event(
                settings.database_path,
                principal=settings.principal,
                tool="claude_continue",
                conversation_id=None,
                model=None,
                status="rate_limited",
            )
            raise ConfigurationError(str(exc)) from exc
        except BaseException as exc:
            if audit is not None:
                finish_call(
                    settings.database_path,
                    audit,
                    status="timeout" if "timed out" in str(exc) else "error",
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            raise _error(exc, prompt) from exc

    @app.tool()
    async def claude_close(conversation_id: str) -> dict[str, object]:
        """Close only the bridge mapping; never delete Claude transcripts."""
        start = time.monotonic()
        try:
            clean_conversation = validate_conversation_id(conversation_id)
            close_conversation(
                settings.database_path, clean_conversation, settings.principal
            )
            record_audit_event(
                settings.database_path,
                principal=settings.principal,
                tool="claude_close",
                conversation_id=clean_conversation,
                model=None,
                status="success",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
            return {
                "status": "closed",
                "conversation_id": clean_conversation,
                "transcript_deleted": False,
            }
        except BaseException as exc:
            record_audit_event(
                settings.database_path,
                principal=settings.principal,
                tool="claude_close",
                conversation_id=None,
                model=None,
                status="error",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
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
