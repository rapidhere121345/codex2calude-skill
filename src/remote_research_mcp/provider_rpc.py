"""Provider RPC helper: one bounded JSON request in, one sanitized result out.

Runs as the provider service UID via run-remote-provider-rpc.sh. Rebuilds a
clean environment from the lead catalog and installs it into os.environ
before any backend import or app creation.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path

from gemini_research_mcp.model_evidence import (
    CLI_SESSION_BINDING, EXECUTION_MODEL, binding_matches,
)

from .catalog import PROVIDERS, CatalogError, load_catalog

MAX_REQUEST = 32 * 1024 * 1024
MAX_RESULT = 32 * 1024 * 1024
CATALOG_PATH = Path("/etc/remote-research/catalog.json")

PROVIDER_TOOLS = {
    "claude": {
        "status": "claude_status", "once": "claude_once",
        "start": "claude_start", "continue": "claude_continue",
        "close": "claude_close", "list": "claude_list_conversations",
        "export": "claude_export_conversation",
    },
    "gemini": {
        "status": "gemini_status", "once": "gemini_once",
        "start": "gemini_start", "continue": "gemini_continue",
        "close": "gemini_close", "list": "gemini_list_conversations",
        "export": "gemini_export_conversation",
    },
}
INFER_OPS = {"once", "start", "continue"}

_SHARED_ENV = {
    "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "NO_COLOR": "1",
    "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
}
_CLAUDE_ENV = {
    "HOME": "/var/lib/claude-mcp",
    "PATH": "/opt/claude-code/current:/usr/local/bin:/usr/bin:/bin",
    "CLAUDE_BIN": "/opt/claude-code/current/claude",
    "CLAUDE_MCP_WORK_ROOT": "/var/lib/claude-mcp/research",
    "CLAUDE_MCP_REQUIRED_CLI_VERSION": "2.1.280",
    "CLAUDE_MCP_PROMPT_LIMIT": "20000",
    "CLAUDE_MCP_CONTINUE_BURST_LIMIT": "50",
    "CLAUDE_MCP_CONTINUE_BURST_WINDOW_SECONDS": "60",
    "CLAUDE_MCP_CONCURRENCY": "1",
    "CLAUDE_MCP_OUTPUT_LIMIT": "100000",
    "CLAUDE_MCP_RESULT_LIMIT": "100000",
    "CLAUDE_MCP_STATUSLINE_OBSERVATION":
        "/var/lib/claude-mcp/research/state/statusline-observation.json",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
    "DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1",
    "DISABLE_ERROR_REPORTING": "1", "DISABLE_FEEDBACK_COMMAND": "1",
    "DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1",
    "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
}
_GEMINI_ENV = {
    "HOME": "/var/lib/gemini-mcp",
    "PATH": "/opt/antigravity/current/bin:/usr/local/bin:/usr/bin:/bin",
    "AGY_BIN": "/opt/antigravity/current/bin/agy",
    "GEMINI_MCP_WORK_ROOT": "/var/lib/gemini-mcp/research",
    "GEMINI_MCP_DEFAULT_MODEL": "pro",
    "GEMINI_MCP_REQUIRE_EXPLICIT_MODEL": "1",
    "GEMINI_MCP_REQUIRED_CLI_VERSION": "1.2.14",
    "GEMINI_MCP_PROMPT_LIMIT": "20000",
    "GEMINI_MCP_CONTINUE_BURST_LIMIT": "50",
    "GEMINI_MCP_CONTINUE_BURST_WINDOW_SECONDS": "60",
    "GEMINI_MCP_CONCURRENCY": "1",
    "GEMINI_MCP_OUTPUT_LIMIT": "4194304",
    "GEMINI_MCP_RESULT_LIMIT": "4194304",
}


def _version_key(value: str) -> tuple[int, ...]:
    parts = []
    for piece in value.lstrip("v").split("."):
        digits = "".join(c for c in piece if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _catalog_models(provider: str, catalog_path: Path) -> dict:
    catalog = load_catalog(catalog_path)
    cat = catalog.provider(provider)
    if provider == "claude":
        return {s.backend_model: {
            "cli_value": s.cli_value, "family": s.family,
            "min_cli_version": s.min_cli_version,
            **({"reasoning_effort": s.reasoning_effort}
               if s.reasoning_effort else {}),
        } for s in cat.models.values() if s.enabled}
    return {s.backend_model: {
        "cli_value": s.cli_value, "family": s.family,
        "min_cli_version": s.min_cli_version,
        **({"reasoning_effort": s.reasoning_effort}
           if s.reasoning_effort else {}),
    } for s in cat.models.values() if s.enabled}


def provider_models_env(provider: str, catalog_path: Path) -> dict[str, str]:
    models = _catalog_models(provider, catalog_path)
    catalog = load_catalog(catalog_path)
    cat = catalog.provider(provider)
    if provider == "claude":
        required = max(
            (_version_key(s.min_cli_version) for s in cat.models.values()
             if s.enabled), default=(0,))
        return {
            "CLAUDE_MCP_MODELS": json.dumps(models, separators=(",", ":")),
            "CLAUDE_MCP_REQUIRED_CLI_VERSION": ".".join(map(str, required)),
        }
    return {"GEMINI_MCP_MODELS": json.dumps(models, separators=(",", ":"))}


def provider_env(provider: str, principal: str,
                 catalog_path: Path = CATALOG_PATH) -> dict[str, str]:
    if provider not in PROVIDERS:
        raise CatalogError("unknown provider")
    if principal not in ("owner", "partner", "partner2", "partner3",
                         "partner4"):
        raise CatalogError("invalid principal")
    base = dict(_CLAUDE_ENV if provider == "claude" else _GEMINI_ENV)
    base.update(_SHARED_ENV)
    prefix = "CLAUDE" if provider == "claude" else "GEMINI"
    base[f"{prefix}_MCP_PRINCIPAL"] = principal
    base[f"{prefix}_MCP_PRINCIPALS"] = "owner,partner,partner2,partner3,partner4"
    base[f"{prefix}_MCP_WORK_ROOT"] = (
        "/var/lib/claude-mcp/research" if provider == "claude"
        else "/var/lib/gemini-mcp/research")
    base.update(provider_models_env(provider, catalog_path))
    return base


SELECTION_KEYS = {"public_model", "cli_value", "family",
                  "reasoning_effort"}


def _load_backend(provider: str):
    if provider == "claude":
        from claude_research_mcp import config as claude_config
        from claude_research_mcp import server as claude_server
        return claude_config, claude_server
    from gemini_research_mcp import config as gemini_config
    from gemini_research_mcp import server as gemini_server
    return gemini_config, gemini_server


def _selection_error(request: dict, provider: str, op: str,
                     catalog_path: Path, settings, *, catalog=None) -> str | None:
    """Compare trusted selection against fresh catalog AND the actual
    backend Settings object built once for this call. Fail closed."""
    if op not in INFER_OPS:
        return None
    sel = request.get("selection")
    if not isinstance(sel, dict) or set(sel) != SELECTION_KEYS \
            or not isinstance(sel.get("public_model"), str) \
            or not sel["public_model"]:
        return "missing_selection"
    args = request.get("arguments") or {}
    if op == "continue":
        if "model" in args:
            return "selection_rejected"
    elif args.get("model") is None:
        return "missing_selection"
    try:
        spec = (catalog if catalog is not None else load_catalog(catalog_path)).resolve(
            provider, sel["public_model"])
    except CatalogError:
        return "selection_rejected"
    if op in ("once", "start") and spec.backend_model != args.get("model"):
        return "selection_rejected"
    for key, want in (("cli_value", spec.cli_value),
                      ("family", spec.family),
                      ("reasoning_effort", spec.reasoning_effort)):
        if sel.get(key) != want:
            return "selection_rejected"
    policy = settings.models.get(spec.backend_model)
    if policy is None:
        return "selection_rejected"
    if getattr(policy, "cli_value", None) != spec.cli_value \
            or getattr(policy, "family", None) != spec.family \
            or getattr(policy, "reasoning_effort", None) \
            != spec.reasoning_effort:
        return "selection_rejected"
    return None


def _evidence_error(result: dict, provider: str,
                    cli_value: str | None, *,
                    evidence_policy: str = EXECUTION_MODEL,
                    reasoning_effort: str | None = None) -> str | None:
    """Keep execution proof strict; permit only explicitly configured binding."""
    models = result.get("actual_models")
    if models == [] and type(models) is list \
            and provider == "gemini" and evidence_policy == CLI_SESSION_BINDING:
        if binding_matches(result.get("model_evidence"), cli_value, reasoning_effort):
            return None
        evidence = result.get("model_evidence")
        if isinstance(evidence, dict) and binding_matches(
                evidence, evidence.get("reported_model"), evidence.get("reasoning_effort")):
            return "model_evidence_mismatch"
        return "model_evidence_missing"
    if not isinstance(models, list) or not models \
            or not all(isinstance(m, str) and m for m in models):
        return "model_evidence_missing"
    if cli_value is None:
        return "model_evidence_missing"
    if provider == "gemini":
        return None if all(m == cli_value for m in models) \
            else "model_evidence_mismatch"
    pat = re.compile(re.escape(cli_value) + r"-[0-9]{8}$")
    matched = any(m == cli_value or pat.fullmatch(m) for m in models)
    if not matched:
        return "model_evidence_mismatch"
    return None


def _uuid_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _extract_request_id(exc: BaseException) -> str | None:
    seen = 0
    cur = exc
    while cur is not None and seen < 4:
        rid = _uuid_or_none(getattr(cur, "request_id", None))
        if rid:
            return rid
        cur = cur.__cause__
        seen += 1
    return None


def _error_result(category: str, request_id: object = None) -> dict:
    return {"ok": False,
            "error": {"category": category, "request_id": request_id}}


async def _read_stdin(reader: asyncio.StreamReader) -> dict | None:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=30)
    except (asyncio.TimeoutError, ValueError):
        return None
    if not line or len(line) > MAX_REQUEST:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


async def _wait_eof(reader: asyncio.StreamReader) -> None:
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            return


async def _run(provider: str, principal: str,
               catalog_path: Path) -> dict:
    """One request on stdin; the read transport is always closed."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_REQUEST + 2)
    transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    try:
        return await _dispatch(reader, provider, principal, catalog_path)
    finally:
        transport.close()


async def _dispatch(reader: asyncio.StreamReader, provider: str,
                    principal: str, catalog_path: Path) -> dict:
    request = await _read_stdin(reader)
    if request is None:
        return _error_result("invalid_request")
    tool = request.get("tool")
    arguments = request.get("arguments", {})
    # Every accepted request carries one opaque normalized id, including
    # bounded setup failures, so callers can always correlate outcomes.
    request_id = _uuid_or_none(request.get("request_id")) or str(uuid.uuid4())
    allowed = PROVIDER_TOOLS.get(provider, {})
    op = next((k for k, v in allowed.items() if v == tool), None)
    if op is None or not isinstance(arguments, dict):
        return _error_result("invalid_request", request_id)
    try:
        env = provider_env(provider, principal, catalog_path)
        # Install the rebuilt environment BEFORE backend imports/app creation so
        # executors' child_environment() sees provider HOME/policy, not the
        # wrapper's HOME=/nonexistent.
        os.environ.clear()
        os.environ.update(env)
        config_mod, server_mod = _load_backend(provider)
        settings = config_mod.load_settings(env)
        request_catalog = load_catalog(catalog_path)
        bad = _selection_error(request, provider, op, catalog_path, settings,
                               catalog=request_catalog)
        if bad is not None:
            return _error_result(bad, request_id)
        evidence_policy = EXECUTION_MODEL
        if op in INFER_OPS:
            selected_spec = request_catalog.resolve(
                provider, request["selection"]["public_model"])
            evidence_policy = selected_spec.evidence_policy
        app = server_mod.create_app(settings)
        fn = app._tool_manager._tools[tool].fn
    except Exception:
        # Bounded generic setup failure: no import/config text is exposed.
        return _error_result("provider_error", request_id)
    eof = asyncio.ensure_future(_wait_eof(reader))
    task = asyncio.ensure_future(fn(**arguments))
    try:
        done, _ = await asyncio.wait(
            {task, eof}, return_when=asyncio.FIRST_COMPLETED)
        if eof in done:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            return _error_result("cancelled", request_id)
        eof.cancel()
        try:
            await eof
        except BaseException:
            pass
        result = task.result()
        if op == "status" and isinstance(result, dict):
            cat = request_catalog.provider(provider)
            result["model_evidence_policy"] = \
                cat.models[cat.default_model].evidence_policy
        if op in INFER_OPS:
            sel = request.get("selection") or {}
            ev = _evidence_error(result if isinstance(result, dict) else {},
                                 provider, sel.get("cli_value"),
                                 evidence_policy=evidence_policy,
                                 reasoning_effort=sel.get("reasoning_effort"))
            if ev is not None:
                rid = (_uuid_or_none(result.get("request_id"))
                       if isinstance(result, dict) else None) \
                    or _uuid_or_none(request_id)
                return _error_result(ev, rid)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        category = "".join(c if c.isalnum() or c == "_" else "_"
                          for c in type(exc).__name__.lower())[:48]
        rid = _extract_request_id(exc) or _uuid_or_none(request_id)
        return _error_result(category or "provider_error", rid)
    finally:
        # Cancel any unfinished work AND await it: the dispatch never
        # leaves orphaned tasks behind at interpreter shutdown.
        for pending in (eof, task):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(eof, task, return_exceptions=True)
    encoded = json.dumps({"ok": True, "result": result},
                         separators=(",", ":"), default=str) + "\n"
    if len(encoded) > MAX_RESULT:
        return _error_result("result_too_large", request_id)
    return {"ok": True, "result": result}


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in PROVIDER_TOOLS \
            or argv[2] not in ("owner", "partner", "partner2",
                              "partner3", "partner4"):
        sys.stdout.write(json.dumps(_error_result("invalid_request")) + "\n")
        sys.stdout.flush()
        return 64
    provider, principal = argv[1], argv[2]
    catalog_path = Path(os.environ.get(
        "REMOTE_RESEARCH_CATALOG", str(CATALOG_PATH)))
    try:
        outcome = asyncio.run(_run(provider, principal, catalog_path))
    except Exception:
        outcome = _error_result("provider_error")
    sys.stdout.write(json.dumps(outcome, separators=(",", ":"),
                                default=str) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(["provider_rpc", *sys.argv[1:]]))
