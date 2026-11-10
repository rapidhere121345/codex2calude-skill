"""Unified remote-research gateway: catalog, routes, provider invocation."""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from .catalog import (ALLOWED_PRINCIPALS, Catalog, CatalogError, ModelSpec,
                      PROVIDERS, load_catalog)
from .store import RouteStore

MAX_FRAME = 32 * 1024 * 1024
DEFAULT_STATE_ROOT = Path("/var/lib/remote-research")
DEFAULT_PROVIDER_SOCK = Path("/run/remote-research/provider.sock")
TOOL_OPS = ("status", "once", "start", "continue", "close", "list", "export")


class GatewayError(ValueError):
    """Client-visible gateway failure; message is already sanitized."""


@dataclass(frozen=True)
class GatewaySettings:
    principal: str
    state_root: Path
    catalog_path: Path
    provider_sock: Path

    @property
    def routes_path(self) -> Path:
        return self.state_root / "routes.sqlite3"


def load_settings(env: dict[str, str] | None = None) -> GatewaySettings:
    source = os.environ if env is None else env
    principal = source.get("REMOTE_RESEARCH_PRINCIPAL", "")
    if principal not in ALLOWED_PRINCIPALS:
        raise GatewayError("invalid REMOTE_RESEARCH_PRINCIPAL")
    state_root = Path(source.get(
        "REMOTE_RESEARCH_STATE_ROOT", str(DEFAULT_STATE_ROOT)))
    catalog_path = Path(source.get(
        "REMOTE_RESEARCH_CATALOG", "/etc/remote-research/catalog.json"))
    sock = Path(source.get(
        "REMOTE_RESEARCH_PROVIDER_SOCK", str(DEFAULT_PROVIDER_SOCK)))
    return GatewaySettings(
        principal=principal, state_root=state_root,
        catalog_path=catalog_path, provider_sock=sock)


_WIRE_CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


def _wire_category(value: object) -> str:
    """Accept only a strictly shaped lowercase provider category."""
    if isinstance(value, str) and _WIRE_CATEGORY_RE.fullmatch(value):
        return value
    return "provider_error"


def _wire_request_id(value: object) -> str | None:
    """Normalize an opaque correlation id; anything that is not a UUID is
    dropped rather than echoed back to callers."""
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


class ProviderCallError(RuntimeError):
    """Sanitized provider failure.

    The MCP SDK serializes the exception text, not custom attributes, so
    the (validated) correlation id is carried in the message while the
    structured ``category``/``request_id`` attributes remain available.
    """

    def __init__(self, category: str, request_id: str | None = None,
                 message: str | None = None):
        self.category = _wire_category(category)
        self.request_id = _wire_request_id(request_id)
        self._message = message
        super().__init__(self.category)

    def __str__(self) -> str:
        base = self._message if self._message else self.category
        if self.request_id:
            return f"{base} [request_id: {self.request_id}]"
        return base


class Gateway:
    def __init__(self, settings: GatewaySettings):
        self.settings = settings
        self.catalog = load_catalog(settings.catalog_path)
        self.store = RouteStore(settings.routes_path)
        self._semaphores = {
            p: asyncio.Semaphore(1) for p in PROVIDERS}

    # ---------------- provider transport ----------------

    async def _rpc_once(self, request: dict, timeout: float) -> dict:
        """One bounded JSON envelope over the provider Unix socket.

        Failures opening the socket — before any write attempt — map to
        ``provider_unavailable``. From the moment the write is attempted,
        the outcome is uncertain (a partial delivery may already execute),
        so any write/drain/read transport failure, EOF or timeout becomes
        ``provider_result_unknown`` with the request id in the message
        text; the request is never replayed automatically.
        """
        envelope = {"provider": request["provider"],
                    "principal": self.settings.principal,
                    "tool": request["tool"],
                    "arguments": request["arguments"],
                    "request_id": request.get("request_id") or str(uuid.uuid4())}
        if request.get("selection") is not None:
            envelope["selection"] = request["selection"]
        raw = (json.dumps(envelope, separators=(",", ":")) + "\n").encode()
        if len(raw) > MAX_FRAME:
            raise GatewayError("request too large")
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(self.settings.provider_sock), limit=MAX_FRAME + 2)
        except (OSError, RuntimeError):
            raise ProviderCallError("provider_unavailable") from None
        # Built before the first write attempt: everything after that is
        # conservative uncertainty, never a clean unavailable.
        unknown = ProviderCallError(
            "provider_result_unknown", envelope["request_id"],
            message="Provider result unknown after request write; "
                    "not replayed")
        try:
            try:
                writer.write(raw)
                await writer.drain()
            except (OSError, RuntimeError):
                raise unknown from None
            try:
                line = await asyncio.wait_for(
                    reader.readline(), timeout=timeout)
            except (asyncio.TimeoutError, OSError):
                # Includes ConnectionResetError mid-read after the write.
                raise unknown from None
            except ValueError:
                raise ProviderCallError("response_too_large") from None
            if not line:
                # EOF after a written request: the outcome is unknown.
                raise unknown from None
            if len(line) > MAX_FRAME:
                raise ProviderCallError("response_too_large") from None
            try:
                payload = json.loads(line)
            except ValueError:
                raise ProviderCallError("malformed_response") from None
            if not isinstance(payload, dict):
                raise ProviderCallError("malformed_response") from None
            if payload.get("ok") is True and isinstance(
                    payload.get("result"), dict):
                return payload["result"]
            err = payload.get("error")
            if not isinstance(err, dict):
                raise ProviderCallError("malformed_response") from None
            raise ProviderCallError(
                _wire_category(err.get("category")),
                _wire_request_id(err.get("request_id")))
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, RuntimeError):
                pass

    async def call(self, provider: str, op: str, arguments: dict,
                   timeout: float, selection: dict | None = None) -> dict:
        """Invoke one allowlisted provider tool through the RPC broker."""
        if provider not in PROVIDERS or op not in TOOL_OPS:
            raise GatewayError("unknown provider operation")
        # Map gateway op names onto provider tool function names.
        tool_name = {
            "status": f"{provider}_status",
            "once": f"{provider}_once",
            "start": f"{provider}_start",
            "continue": f"{provider}_continue",
            "close": f"{provider}_close",
            "list": f"{provider}_list_conversations",
            "export": f"{provider}_export_conversation",
        }[op]
        request = {"provider": provider, "tool": tool_name,
                   "arguments": arguments, "selection": selection,
                   "request_id": str(uuid.uuid4())}
        async with self._semaphores[provider]:
            return await self._rpc_once(request, timeout)

    # ---------------- shared helpers ----------------

    def _model_meta(self, spec: ModelSpec) -> dict:
        return {"label": spec.label, "enabled": spec.enabled,
                "verified": spec.verified,
                "reasoning_effort": spec.reasoning_effort,
                "evidence_policy": spec.evidence_policy}

    def _annotate(self, result: dict, provider: str,
                  public_model: str | None,
                  effort: str | None) -> dict:
        out = dict(result)
        out["provider"] = provider
        if public_model is not None:
            out["model"] = public_model
        if effort is not None:
            out["reasoning_effort"] = effort
        return out

    def _route_public_model(self, route: dict) -> str | None:
        model = route.get("model") or ""
        spec = self.catalog.backend_for_public(model)
        if spec is not None and spec.provider == route["provider"]:
            return spec.public_id
        mapped = self.catalog.public_for_backend(
            route["provider"], model)
        return mapped or (model if model != "unknown" else None)

    def _resolve_route(self, conversation_id: str) -> dict:
        route = self.store.get(conversation_id, self.settings.principal)
        if route is None:
            raise GatewayError("conversation not found")
        if route.get("closed_at"):
            raise GatewayError("conversation is closed")
        return route

    # ---------------- tool implementations ----------------

    async def status(self, provider: str | None = None) -> dict:
        names = [provider] if provider else list(PROVIDERS)
        fresh = self._fresh_catalog()

        async def one(name):
            cat = fresh.provider(name)
            entry = {
                "default_model": cat.default_model,
                "models": {mid: self._model_meta(s)
                           for mid, s in cat.models.items()},
            }
            try:
                result = await asyncio.wait_for(
                    self.call(name, "status", {}, timeout=60), timeout=60)
                sanitized = dict(result) if isinstance(result, dict) else {}
                for reserved in ("models", "default_model", "principal"):
                    sanitized.pop(reserved, None)
                quota = sanitized.pop("opus_quota", None)
                if quota is not None and "quota" not in sanitized:
                    sanitized["quota"] = quota
                sanitized.pop("request_id", None)
                entry.update(sanitized)
                entry["status"] = "ok"
            except Exception as exc:
                entry["status"] = "error"
                entry["error"] = getattr(exc, "category", "unknown")
                entry["request_id"] = getattr(exc, "request_id", None)
                entry["quota"] = None
            return name, entry

        pairs = await asyncio.gather(*(one(n) for n in names))
        providers = dict(pairs)
        return {"principal": self.settings.principal,
                "providers": providers}

    def _fresh_catalog(self) -> Catalog:
        return load_catalog(self.settings.catalog_path)

    async def once(self, prompt: str, provider: str,
                   model: str | None = None, use_web: bool = True,
                   max_turns: int = 50,
                   timeout_seconds: int = 900) -> dict:
        spec = self._fresh_catalog().resolve(provider, model)
        result = await self.call(provider, "once", {
            "prompt": prompt, "model": spec.backend_model,
            "use_web": use_web, "max_turns": max_turns,
            "timeout_seconds": timeout_seconds,
        }, timeout=timeout_seconds + 120, selection={
            "public_model": spec.public_id, "cli_value": spec.cli_value,
            "family": spec.family, "reasoning_effort": spec.reasoning_effort})
        return self._annotate(result, provider, spec.public_id,
                              spec.reasoning_effort)

    async def start(self, prompt: str, provider: str,
                    model: str | None = None, title: str | None = None,
                    use_web: bool = True, max_turns: int = 50,
                    timeout_seconds: int = 900) -> dict:
        spec = self._fresh_catalog().resolve(provider, model)
        arguments = {"prompt": prompt, "title": title,
                     "model": spec.backend_model, "use_web": use_web,
                     "max_turns": max_turns,
                     "timeout_seconds": timeout_seconds}
        result = await self.call(provider, "start", arguments,
                                 timeout=timeout_seconds + 120,
                                 selection={
            "public_model": spec.public_id, "cli_value": spec.cli_value,
            "family": spec.family,
            "reasoning_effort": spec.reasoning_effort})
        backend_id = result.get("conversation_id")
        if not isinstance(backend_id, str) or not backend_id:
            raise ProviderCallError("malformed_response")
        route_id = self.store.create(
            self.settings.principal, provider, backend_id,
            spec.public_id, spec.cli_value, spec.family,
            spec.reasoning_effort, title, use_web)
        out = self._annotate(result, provider, spec.public_id,
                             spec.reasoning_effort)
        out["conversation_id"] = route_id
        return out

    async def continue_conversation(self, conversation_id: str, prompt: str,
                                    max_turns: int = 50,
                                    timeout_seconds: int = 900) -> dict:
        route = self._resolve_route(conversation_id)
        provider = route["provider"]
        cat = self._fresh_catalog().provider(provider)
        public = route.get("model")
        spec = cat.models.get(public or "")
        if spec is None or not spec.enabled                 or spec.cli_value != route.get("model_cli_value")                 or spec.family != route.get("model_family")                 or spec.reasoning_effort != route.get("reasoning_effort"):
            raise GatewayError("conversation model policy changed")
        result = await self.call(provider, "continue", {
            "conversation_id": route["backend_id"], "prompt": prompt,
            "max_turns": max_turns, "timeout_seconds": timeout_seconds,
        }, timeout=timeout_seconds + 120, selection={
            "public_model": public,
            "cli_value": route.get("model_cli_value"),
            "family": route.get("model_family"),
            "reasoning_effort": route.get("reasoning_effort")})
        self.store.touch(conversation_id, self.settings.principal)
        out = dict(result)
        out["conversation_id"] = conversation_id
        public = self._route_public_model(route)
        if public:
            out["model"] = public
        if route.get("reasoning_effort"):
            out["reasoning_effort"] = route["reasoning_effort"]
        out["provider"] = provider
        return out

    async def list_conversations(self, provider: str,
                                 include_closed: bool = False,
                                 limit: int = 50,
                                 offset: int = 0) -> dict:
        if provider not in PROVIDERS:
            raise GatewayError("unknown provider")
        if not isinstance(limit, int) or isinstance(limit, bool) \
                or not 1 <= limit <= 200:
            raise GatewayError("invalid limit")
        if not isinstance(offset, int) or isinstance(offset, bool) \
                or offset < 0:
            raise GatewayError("invalid offset")
        rows, total = self.store.list_routes(
            self.settings.principal, provider, include_closed, limit, offset)
        items = []
        for row in rows:
            public = self._route_public_model(row)
            items.append({
                "conversation_id": row["id"],
                "title": row.get("title") or "Untitled",
                "model": public or row.get("model"),
                "reasoning_effort": row.get("reasoning_effort"),
                "use_web": bool(row.get("use_web")),
                "created_at": row.get("created_at"),
                "last_activity_at": row.get("last_activity_at"),
                "status": "closed" if row.get("closed_at") else "open",
            })
        has_more = offset + len(items) < total
        return {
            "status": "ok", "provider": provider, "count": len(items),
            "total_count": total,
            "next_offset": offset + len(items) if has_more else None,
            "has_more": has_more, "conversations": items,
        }

    async def export_conversation(self, conversation_id: str,
                                  format: str = "markdown") -> dict:
        route = self.store.get(conversation_id, self.settings.principal)
        if route is None:
            raise GatewayError("conversation not found")
        result = await self.call(route["provider"], "export", {
            "conversation_id": route["backend_id"], "format": format},
            timeout=120)
        out = dict(result)
        out["conversation_id"] = conversation_id
        out["provider"] = route["provider"]
        return out

    async def close(self, conversation_id: str) -> dict:
        route = self.store.get(conversation_id, self.settings.principal)
        if route is None:
            raise GatewayError("conversation not found")
        provider = route["provider"]
        result = await self.call(provider, "close", {
            "conversation_id": route["backend_id"]}, timeout=60)
        self.store.close(conversation_id, self.settings.principal)
        out = dict(result)
        out["conversation_id"] = conversation_id
        out["provider"] = provider
        out["closed"] = True
        return out
