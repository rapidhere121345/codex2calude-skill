from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import uuid
from pathlib import Path

import pytest

from remote_research_mcp.catalog import CatalogError
from remote_research_mcp.gateway import (Gateway, GatewayError, ProviderCallError,
                                         load_settings)
from remote_research_mcp.server import create_app

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "deploy" / "remote-research-catalog.json"


def _result_for(tool: str, args: dict, provider: str) -> dict:
    if tool.endswith("_status"):
        return {"status": "ok", "authenticated": True,
                "opus_quota": {"used": 1}, "quota": {"used": 2}}
    if tool.endswith("_once"):
        return {"status": "ok", "answer": "done", "model": args.get("model")}
    if tool.endswith("_start"):
        return {"status": "ok",
                "conversation_id": f"native-{provider}-1",
                "title": args.get("title")}
    if tool.endswith("_continue"):
        return {"status": "ok",
                "conversation_id": args.get("conversation_id"),
                "answer": "continued"}
    if tool.endswith("_close"):
        if os.environ.get("FAKE_CLOSE_FAIL"):
            return {"ok": False, "error": {"category": "close_failed"}}
        return {"status": "ok",
                "conversation_id": args.get("conversation_id"),
                "closed": True}
    if tool.endswith("_list_conversations"):
        return {"status": "ok", "count": 1, "total_count": 1,
                "has_more": False, "next_offset": None,
                "conversations": [{
                    "conversation_id": f"native-{provider}-9",
                    "title": "Legacy",
                    "model": "opus55" if provider == "claude" else "pro",
                    "use_web": True, "created_at": "t",
                    "last_activity_at": "t", "status": "open"}]}
    if tool.endswith("_export_conversation"):
        return {"status": "ok",
                "conversation_id": args.get("conversation_id"),
                "format": args.get("format", "markdown"), "content": "text"}
    return {"status": "ok"}


class FakeBroker:
    """Unix-socket stand-in for privileged_rpc: logs envelopes, replies canned."""

    def __init__(self, path: Path, log: Path):
        self.path = path
        self.log = log
        self.stop = threading.Event()
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(path))
        self.srv.listen(16)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        self.srv.settimeout(0.2)
        while not self.stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, conn):
        try:
            data = b""
            while b"\n" not in data and len(data) < 40 * 1024 * 1024:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            env = json.loads(data.split(b"\n", 1)[0])
            with open(self.log, "a", encoding="utf-8") as f:
                f.write(json.dumps(env) + "\n")
            provider = env.get("provider")
            fail = os.environ.get("FAKE_FAIL_PROVIDER")
            if fail == provider:
                payload = {"ok": False,
                           "error": {"category": "auth_error"}}
            elif os.environ.get("FAKE_EVIL_ERROR") == provider:
                payload = {"ok": False, "error": {
                    "category": "EVIL Category/Text!!",
                    "request_id": "../../secret",
                    "detail": "hide this too"}}
            elif os.environ.get("FAKE_CORRELATED") == provider:
                payload = {"ok": False, "error": {
                    "category": "quota_exceeded",
                    "request_id": "8B3D4C5E-1A2F-4C3D-9E2F-0A1B2C3D4E5F"}}
            elif os.environ.get("FAKE_MALFORMED") == provider:
                conn.sendall(b"this is not json\n")
                return
            elif os.environ.get("FAKE_HUGE") == provider:
                conn.sendall(b"x" * (40 * 1024 * 1024) + b"\n")
                return
            else:
                tool = env.get("tool", "")
                args = env.get("arguments") or {}
                result = _result_for(tool, args, provider)
                if isinstance(result, dict) and result.get("ok") is False:
                    payload = result
                else:
                    payload = {"ok": True, "result": result}
            conn.sendall(json.dumps(payload).encode() + b"\n")
        except (OSError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self.stop.set()
        self.srv.close()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sock = tmp_path / "provider.sock"
    log = tmp_path / "rpc.jsonl"
    broker = FakeBroker(sock, log)
    state = tmp_path / "state"

    def make(principal="owner"):
        return Gateway(load_settings({
            "REMOTE_RESEARCH_PRINCIPAL": principal,
            "REMOTE_RESEARCH_STATE_ROOT": str(state),
            "REMOTE_RESEARCH_CATALOG": str(CATALOG),
            "REMOTE_RESEARCH_PROVIDER_SOCK": str(sock),
        }))

    yield make, log, tmp_path, broker
    broker.close()


def logged(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(x) for x in log.read_text().splitlines()]


def test_exactly_seven_tools(env):
    make, _, _, _ = env
    app = create_app(make())
    names = set(app._tool_manager._tools)
    assert names == {
        "research_status", "research_once", "research_start",
        "research_continue", "research_list_conversations",
        "research_export_conversation", "research_close"}


def test_once_routes_backend_model(env):
    make, log, _, _ = env
    gw = make()
    out = asyncio.run(gw.once("hi", "claude", "opus-5-5-high"))
    assert out["provider"] == "claude" and out["model"] == "opus-5-5-high"
    assert out["reasoning_effort"] == "high"
    entry = logged(log)[-1]
    assert entry["provider"] == "claude"
    assert entry["principal"] == "owner"
    assert entry["tool"] == "claude_once"
    assert entry["arguments"]["model"] == "opus55"


def test_gemini_backend_mapping(env):
    make, log, _, _ = env
    out = asyncio.run(make().once("hi", "gemini"))
    assert out["model"] == "gemini-3-8-flash-high"
    entry = logged(log)[-1]
    assert entry["tool"] == "gemini_once"
    assert entry["arguments"]["model"] == "pro"


def test_disabled_and_unknown_model_rejected(env):
    make, log, _, _ = env
    gw = make()
    with pytest.raises(CatalogError):
        asyncio.run(gw.once("hi", "claude", "fable-5-2-high"))
    with pytest.raises(CatalogError):
        asyncio.run(gw.once("hi", "claude", "bogus-model"))
    assert logged(log) == []


def test_provider_error_does_not_hide_other_status(env, monkeypatch):
    make, log, _, _ = env
    monkeypatch.setenv("FAKE_FAIL_PROVIDER", "claude")
    out = asyncio.run(make().status())
    assert out["principal"] == "owner"
    assert out["providers"]["claude"]["status"] == "error"
    assert out["providers"]["gemini"]["status"] == "ok"
    assert out["providers"]["gemini"]["quota"] == {"used": 2}


def test_principal_isolation(env):
    make, log, _, _ = env
    owner = make("owner")
    rid = owner.store.create("owner", "claude", "native-x",
                             "opus-5-5-high", "claude-opus-5-5", "opus", "high", "T", True)
    other = make("partner")
    with pytest.raises(GatewayError):
        asyncio.run(other.continue_conversation(rid, "hi"))
    with pytest.raises(GatewayError):
        asyncio.run(other.close(rid))
    out = asyncio.run(owner.continue_conversation(rid, "hi"))
    assert out["conversation_id"] == rid


def test_continue_default_change_ok_policy_change_rejected(env, tmp_path):
    make, log, _, _ = env
    owner = make("owner")
    started = asyncio.run(owner.start("do it", "claude", "opus-5-5-high"))
    rid = started["conversation_id"]
    # Default-only catalog change: continuation still works. Switch the
    # claude default to the (temporarily enabled) fable preset.
    cat = json.loads(CATALOG.read_text())
    cat["providers"]["claude"]["default_model"] = "fable-5-2-high"
    cat["providers"]["claude"]["models"]["fable-5-2-high"]["enabled"] = True
    cat["providers"]["claude"]["models"]["fable-5-2-high"]["verified"] = True
    changed = tmp_path / "catalog2.json"
    changed.write_text(json.dumps(cat))
    gw2 = Gateway(load_settings({
        "REMOTE_RESEARCH_PRINCIPAL": "owner",
        "REMOTE_RESEARCH_STATE_ROOT": str(owner.settings.state_root),
        "REMOTE_RESEARCH_CATALOG": str(changed),
        "REMOTE_RESEARCH_PROVIDER_SOCK": str(owner.settings.provider_sock),
    }))
    out = asyncio.run(gw2.continue_conversation(rid, "more"))
    assert out["model"] == "opus-5-5-high"
    # Disabled model: continuation rejected before any provider call.
    cat["providers"]["claude"]["models"]["opus-5-5-high"]["enabled"] = False
    changed.write_text(json.dumps(cat))
    gw3 = Gateway(load_settings({
        "REMOTE_RESEARCH_PRINCIPAL": "owner",
        "REMOTE_RESEARCH_STATE_ROOT": str(owner.settings.state_root),
        "REMOTE_RESEARCH_CATALOG": str(changed),
        "REMOTE_RESEARCH_PROVIDER_SOCK": str(owner.settings.provider_sock),
    }))
    before = len(logged(log))
    with pytest.raises(GatewayError):
        asyncio.run(gw3.continue_conversation(rid, "more"))
    assert len(logged(log)) == before
    # Native cli_value change also rejects before any provider call.
    cat["providers"]["claude"]["models"]["opus-5-5-high"]["enabled"] = True
    cat["providers"]["claude"]["models"]["opus-5-5-high"]["cli_value"] =         "claude-opus-4-6"
    changed.write_text(json.dumps(cat))
    gw4 = Gateway(load_settings({
        "REMOTE_RESEARCH_PRINCIPAL": "owner",
        "REMOTE_RESEARCH_STATE_ROOT": str(owner.settings.state_root),
        "REMOTE_RESEARCH_CATALOG": str(changed),
        "REMOTE_RESEARCH_PROVIDER_SOCK": str(owner.settings.provider_sock),
    }))
    with pytest.raises(GatewayError):
        asyncio.run(gw4.continue_conversation(rid, "more"))
    assert len(logged(log)) == before


def test_list_reads_gateway_routes_only(env):
    make, log, _, _ = env
    gw = make()
    # Unknown legacy/native ID is not listable: list shows only own routes.
    started = asyncio.run(gw.start("task", "claude", "opus-5-5-high",
                                   title="My task", use_web=False))
    rid = started["conversation_id"]
    out = asyncio.run(gw.list_conversations("claude"))
    assert out["total_count"] == 1
    item = out["conversations"][0]
    assert item["conversation_id"] == rid
    assert item["title"] == "My task"
    assert item["use_web"] is False
    assert item["model"] == "opus-5-5-high"
    assert item["status"] == "open"
    # No provider list call is made for listing.
    assert all(e["tool"] != "claude_list_conversations" for e in logged(log))
    # Closed routes hide unless requested; unknown old ID still fails.
    asyncio.run(gw.close(rid))
    assert asyncio.run(gw.list_conversations("claude"))["total_count"] == 0
    assert asyncio.run(gw.list_conversations(
        "claude", include_closed=True))["total_count"] == 1
    with pytest.raises(GatewayError):
        asyncio.run(gw.export_conversation("native-claude-9"))


def test_close_failure_keeps_mapping_open(env, monkeypatch):
    make, log, _, _ = env
    monkeypatch.setenv("FAKE_CLOSE_FAIL", "1")
    gw = make()
    started = asyncio.run(gw.start("x", "gemini"))
    rid = started["conversation_id"]
    with pytest.raises(ProviderCallError):
        asyncio.run(gw.close(rid))
    assert gw.store.get(rid, "owner")["closed_at"] is None


def test_closed_rejects_continue(env):
    make, log, _, _ = env
    gw = make()
    started = asyncio.run(gw.start("x", "claude"))
    rid = started["conversation_id"]
    asyncio.run(gw.close(rid))
    assert gw.store.get(rid, "owner")["closed_at"] is not None
    with pytest.raises(GatewayError):
        asyncio.run(gw.continue_conversation(rid, "again"))


def test_export_rewrites_conversation_id(env):
    make, log, _, _ = env
    gw = make()
    rid = gw.store.create("owner", "claude", "native-exp",
                          "opus-5-5-high", "claude-opus-5-5", "opus", "high", "T", True)
    out = asyncio.run(gw.export_conversation(rid))
    assert out["conversation_id"] == rid
    assert "native-exp" not in json.dumps(out)


def test_error_envelope_sanitizes_malicious_category_and_id(
        env, monkeypatch):
    """A hostile broker error category/id is normalized away: category
    falls back to provider_error, the id is dropped, and neither appears
    in the exception text."""
    make, log, _, _ = env
    monkeypatch.setenv("FAKE_EVIL_ERROR", "claude")
    with pytest.raises(ProviderCallError) as e:
        asyncio.run(make().once("hi", "claude"))
    assert e.value.category == "provider_error"
    assert e.value.request_id is None
    assert str(e.value) == "provider_error"


def test_error_text_carries_canonical_request_uuid(env, monkeypatch):
    """A real opaque broker UUID survives normalization into the exception
    text (which is what the MCP SDK serializes) and the attributes."""
    make, log, _, _ = env
    monkeypatch.setenv("FAKE_CORRELATED", "claude")
    with pytest.raises(ProviderCallError) as e:
        asyncio.run(make().once("hi", "claude"))
    assert e.value.category == "quota_exceeded"
    canonical = "8b3d4c5e-1a2f-4c3d-9e2f-0a1b2c3d4e5f"
    assert e.value.request_id == canonical
    assert str(e.value) == f"quota_exceeded [request_id: {canonical}]"


def test_unreachable_socket_is_provider_unavailable(tmp_path):
    """Connection failure before the request write: provider_unavailable,
    no correlation id to expose."""
    gw = Gateway(load_settings({
        "REMOTE_RESEARCH_PRINCIPAL": "owner",
        "REMOTE_RESEARCH_STATE_ROOT": str(tmp_path / "state"),
        "REMOTE_RESEARCH_CATALOG": str(CATALOG),
        "REMOTE_RESEARCH_PROVIDER_SOCK": str(tmp_path / "absent.sock"),
    }))
    request = {"provider": "claude", "tool": "claude_status",
               "arguments": {}, "selection": None,
               "request_id": str(uuid.uuid4())}
    with pytest.raises(ProviderCallError) as e:
        asyncio.run(gw._rpc_once(request, timeout=1))
    assert e.value.category == "provider_unavailable"
    assert e.value.request_id is None


def test_result_unknown_after_write_not_replayed(tmp_path):
    """After a written request, EOF/timeout becomes provider_result_unknown
    with the explicit message plus the request id — and the gateway must
    not replay the request on the same or a new connection."""
    sock = tmp_path / "silent.sock"
    frames = []

    async def scenario():
        async def hold(reader, writer):
            data = b""
            while b"\n" not in data:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                data += chunk
            frames.append(data.split(b"\n", 1)[0])
            # Stay silent past the caller timeout, then close.
            try:
                await asyncio.sleep(1.0)
            finally:
                writer.close()
        server = await asyncio.start_unix_server(hold, path=str(sock))
        gw = Gateway(load_settings({
            "REMOTE_RESEARCH_PRINCIPAL": "owner",
            "REMOTE_RESEARCH_STATE_ROOT": str(tmp_path / "state"),
            "REMOTE_RESEARCH_CATALOG": str(CATALOG),
            "REMOTE_RESEARCH_PROVIDER_SOCK": str(sock),
        }))
        request = {"provider": "claude", "tool": "claude_once",
                   "arguments": {"prompt": "x"}, "selection": None,
                   "request_id": str(uuid.uuid4())}
        try:
            with pytest.raises(ProviderCallError) as e:
                await gw._rpc_once(request, timeout=0.2)
        finally:
            server.close()
            await server.wait_closed()
        return e, request

    e, request = asyncio.run(scenario())
    assert e.value.category == "provider_result_unknown"
    assert "Provider result unknown after request write; not replayed" \
        in str(e.value)
    assert request["request_id"] in str(e.value)
    # Exactly one envelope was ever written: nothing was replayed.
    assert len(frames) == 1


class _FakeWriter:
    """Records writes; optionally fails write or drain with OSError."""

    def __init__(self, fail_at: str | None = None):
        self.writes = 0
        self._fail_at = fail_at
        self.closed = False

    def write(self, data):
        self.writes += 1
        if self._fail_at == "write":
            raise OSError("fake write transport broke")

    async def drain(self):
        if self._fail_at == "drain":
            raise OSError("fake drain connection reset")

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


class _FakeReader:
    def __init__(self, exc: Exception | None = None):
        self._exc = exc

    async def readline(self):
        if self._exc is not None:
            raise self._exc
        return b""


def _unknown_regression_gateway(tmp_path):
    return Gateway(load_settings({
        "REMOTE_RESEARCH_PRINCIPAL": "owner",
        "REMOTE_RESEARCH_STATE_ROOT": str(tmp_path / "state"),
        "REMOTE_RESEARCH_CATALOG": str(CATALOG),
        "REMOTE_RESEARCH_PROVIDER_SOCK": str(tmp_path / "irrelevant.sock"),
    }))


def _run_unknown_case(monkeypatch, tmp_path, *, fail_at, reader_exc):
    """One fake connect, one write; transport failure after the write
    attempt is provider_result_unknown with the same request id and no
    raw error text echoed."""
    gw = _unknown_regression_gateway(tmp_path)
    writer = _FakeWriter(fail_at=fail_at)
    reader = _FakeReader(exc=reader_exc)
    calls = {"connects": 0}

    async def fake_open(path, limit=None):
        calls["connects"] += 1
        return reader, writer

    monkeypatch.setattr(asyncio, "open_unix_connection", fake_open)
    request_id = str(uuid.uuid4())
    request = {"provider": "claude", "tool": "claude_once",
               "arguments": {"prompt": "x"}, "selection": None,
               "request_id": request_id}
    with pytest.raises(ProviderCallError) as e:
        asyncio.run(gw._rpc_once(request, timeout=1))
    return e, writer, calls, request_id


def test_drain_failure_after_write_is_result_unknown(monkeypatch, tmp_path):
    """A partial write that then fails draining may already have executed:
    it is unknown, not a clean unavailable."""
    e, writer, calls, request_id = _run_unknown_case(
        monkeypatch, tmp_path, fail_at="drain", reader_exc=None)
    assert e.value.category == "provider_result_unknown"
    assert e.value.request_id == request_id
    assert request_id in str(e.value)
    assert writer.writes == 1 and calls["connects"] == 1
    assert "fake drain" not in str(e.value)


def test_readline_reset_after_write_is_result_unknown(monkeypatch, tmp_path):
    """Connection reset while reading the answer (after a written request)
    is unknown, with no raw error string echoed."""
    e, writer, calls, request_id = _run_unknown_case(
        monkeypatch, tmp_path, fail_at=None,
        reader_exc=ConnectionResetError("fake reset after request"))
    assert e.value.category == "provider_result_unknown"
    assert e.value.request_id == request_id
    assert request_id in str(e.value)
    assert writer.writes == 1 and calls["connects"] == 1
    assert "fake reset" not in str(e.value)


def test_malformed_and_oversize_sanitized(env, monkeypatch):
    make, log, _, _ = env
    gw = make()
    monkeypatch.setenv("FAKE_MALFORMED", "claude")
    with pytest.raises(ProviderCallError) as e:
        asyncio.run(gw.once("hi", "claude"))
    assert e.value.category == "malformed_response"
    monkeypatch.delenv("FAKE_MALFORMED")
    monkeypatch.setenv("FAKE_HUGE", "claude")
    with pytest.raises(ProviderCallError):
        asyncio.run(gw.once("hi", "claude"))
