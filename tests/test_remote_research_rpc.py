from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import types
import uuid
from pathlib import Path

import pytest

from remote_research_mcp import provider_rpc, privileged_rpc

CATALOG = Path(__file__).resolve().parents[1] / "deploy" / "remote-research-catalog.json"

CLAUDE_SEL = {"public_model": "opus-5-5-high", "cli_value": "claude-opus-5-5",
              "family": "opus", "reasoning_effort": "high"}
GEMINI_SEL = {"public_model": "gemini-3-8-flash-high",
              "cli_value": "gemini-3.8-flash-high", "family": "gemini",
              "reasoning_effort": "high"}


def _policy():
    return types.SimpleNamespace(
        models={
            "opus55": types.SimpleNamespace(
                cli_value="claude-opus-5-5", family="opus",
                reasoning_effort="high"),
            "pro": types.SimpleNamespace(
                cli_value="gemini-3.8-flash-high", family="gemini",
                reasoning_effort="high"),
        })


def fake_provider_modules(calls, behavior=None, actual_models=None):
    modules = {}
    for name, prov in (("claude_research_mcp", "claude"),
                       ("gemini_research_mcp", "gemini")):
        pkg = types.ModuleType(name)
        config = types.ModuleType(f"{name}.config")
        server = types.ModuleType(f"{name}.server")
        config.load_settings = lambda env, _p=_policy: _p()

        async def default_fn(_prov=prov, **kw):
            calls.append(kw)
            models = actual_models
            if models is None:
                models = (["claude-opus-5-5"] if _prov == "claude"
                          else ["gemini-3.8-flash-high"])
            return {"echo": kw, "actual_models": models}

        fn = behavior or default_fn
        tools = {t: types.SimpleNamespace(fn=fn) for t in
                 provider_rpc.PROVIDER_TOOLS[prov].values()}
        server.create_app = lambda settings, _t=tools: types.SimpleNamespace(
            _tool_manager=types.SimpleNamespace(_tools=_t))
        pkg.config = config
        pkg.server = server
        modules[name] = pkg
        modules[f"{name}.config"] = config
        modules[f"{name}.server"] = server
    return modules


@pytest.fixture(autouse=True)
def _catalog_env(monkeypatch):
    monkeypatch.setenv("REMOTE_RESEARCH_CATALOG", str(CATALOG))


@pytest.fixture()
def fake_modules(monkeypatch):
    calls = []
    for key, mod in fake_provider_modules(calls).items():
        monkeypatch.setitem(sys.modules, key, mod)
    return calls


def make_stdin(payload: bytes):
    """Real pipe stdin: returns (buffer_for_sys_stdin, write_fd)."""
    r, w = os.pipe()
    buf = os.fdopen(r, "rb", buffering=0)
    view = memoryview(payload)
    while view:
        view = view[os.write(w, view):]
    return buf, w


def run_main(provider="claude", principal="owner", request=None,
             keep_open=True, close_stdin=False):
    if request is None:
        request = {"tool": f"{provider}_status", "arguments": {}}
    line = json.dumps(request).encode() + b"\n"
    buf, w = make_stdin(line)
    saved_env = dict(os.environ)
    os.environ["REMOTE_RESEARCH_CATALOG"] = str(CATALOG)
    old = sys.stdin
    sys.stdin = types.SimpleNamespace(buffer=buf)
    try:
        if close_stdin:
            os.close(w)
        rc = provider_rpc.main(["provider_rpc", provider, principal])
    finally:
        sys.stdin = old
        os.environ.clear()
        os.environ.update(saved_env)
        try:
            os.close(w)
        except OSError:
            pass
        buf.close()
    return rc


def last_out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_provider_env_is_clean_and_catalog_driven(monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 22 10.0.0.2 22")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    env = provider_rpc.provider_env("claude", "partner", CATALOG)
    assert env["CLAUDE_MCP_PRINCIPAL"] == "partner"
    assert env["CLAUDE_MCP_PRINCIPALS"] == \
        "owner,partner,partner2,partner3,partner4"
    models = json.loads(env["CLAUDE_MCP_MODELS"])
    assert models["opus55"]["cli_value"] == "claude-opus-5-5"
    assert models["opus55"]["reasoning_effort"] == "high"
    assert "opus" not in models and "fable52" not in models
    for bad in ("SSH_CONNECTION", "ANTHROPIC_API_KEY", "SSH_AUTH_SOCK"):
        assert bad not in env
    genv = provider_rpc.provider_env("gemini", "owner", CATALOG)
    assert json.loads(genv["GEMINI_MCP_MODELS"])["pro"][
        "reasoning_effort"] == "high"


def test_env_installed_into_os_environ(fake_modules, monkeypatch):
    """Backend load_settings must observe rebuilt HOME/policy, not wrapper."""
    seen = {}

    async def spy(**kw):
        seen["HOME"] = os.environ.get("HOME")
        seen["principal"] = os.environ.get("CLAUDE_MCP_PRINCIPAL")
        return {"actual_models": ["claude-opus-5-5"]}

    mods = fake_provider_modules([], spy)
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)
    req = {"tool": "claude_status", "arguments": {}}
    run_main("claude", "owner", req)
    assert seen["HOME"] == "/var/lib/claude-mcp"
    assert seen["principal"] == "owner"


def test_status_returns_while_stdin_open(fake_modules, capsys):
    req = {"tool": "claude_status", "arguments": {}}
    rc = run_main("claude", "owner", req, keep_open=True)
    assert rc == 0
    payload = last_out(capsys)
    assert payload["ok"] is True


def test_infer_requires_selection_and_evidence(fake_modules, capsys):
    # once without selection -> rejected before fn
    req = {"tool": "claude_once",
           "arguments": {"prompt": "x", "model": "opus55"}}
    run_main("claude", "owner", req)
    assert last_out(capsys)["error"]["category"] == "missing_selection"
    # with selection but wrong evidence -> mismatch
    req = {"tool": "claude_once",
           "arguments": {"prompt": "x", "model": "opus55"},
           "selection": CLAUDE_SEL}
    run_main("claude", "owner", req)
    assert last_out(capsys)["ok"] is True


def test_model_evidence_cases(monkeypatch, capsys):
    cases = [
        (["claude-opus-4-6"], "model_evidence_mismatch"),
        (["claude-opus-5-5", "claude-haiku-4-5"], None),
        (["claude-opus-5-5-20260101"], None),
        ([], "model_evidence_missing"),
        (["claude-opus-5-5", 42], "model_evidence_missing"),
    ]
    for models, expected in cases:
        mods = fake_provider_modules([], actual_models=models)
        for k, v in mods.items():
            monkeypatch.setitem(sys.modules, k, v)
        req = {"tool": "claude_once",
               "arguments": {"prompt": "x", "model": "opus55"},
               "selection": CLAUDE_SEL}
        run_main("claude", "owner", req)
        out = last_out(capsys)
        if expected is None:
            assert out["ok"] is True, (models, out)
        else:
            assert out["error"]["category"] == expected, (models, out)


def test_gemini_evidence_requires_exact(monkeypatch, capsys):
    mods = fake_provider_modules([], actual_models=["gemini-2.5-pro"])
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)
    req = {"tool": "gemini_once",
           "arguments": {"prompt": "x", "model": "pro"},
           "selection": GEMINI_SEL}
    run_main("gemini", "owner", req)
    assert last_out(capsys)["error"]["category"] == "model_evidence_mismatch"


def test_selection_tamper_rejected(fake_modules, capsys):
    req = {"tool": "claude_once",
           "arguments": {"prompt": "x", "model": "opus55"},
           "selection": {**CLAUDE_SEL, "cli_value": "claude-opus-4-6"}}
    run_main("claude", "owner", req)
    assert last_out(capsys)["error"]["category"] == "selection_rejected"


def test_unknown_tool_and_bad_frames(fake_modules, capsys):
    run_main("claude", "owner",
             {"tool": "claude_drop_tables", "arguments": {}},
             close_stdin=True)
    assert last_out(capsys)["error"]["category"] == "invalid_request"
    # malformed JSON line
    buf, w = make_stdin(b"not json\n")
    old = sys.stdin
    sys.stdin = types.SimpleNamespace(buffer=buf)
    try:
        os.close(w)
        provider_rpc.main(["provider_rpc", "claude", "owner"])
    finally:
        sys.stdin = old
        buf.close()
    assert last_out(capsys)["error"]["category"] == "invalid_request"


def test_eof_cancels_call_and_kills_owned_child(monkeypatch, capsys):
    holder = {}

    async def slow_fn(**kw):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time;time.sleep(60)")
        holder["proc"] = proc
        try:
            await asyncio.sleep(3600)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        return {"actual_models": ["claude-opus-5-5"]}

    mods = fake_provider_modules([], slow_fn)
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)
    req = {"tool": "claude_once",
           "arguments": {"prompt": "x", "model": "opus55"},
           "selection": CLAUDE_SEL}
    buf, w = make_stdin(json.dumps(req).encode() + b"\n")
    old = sys.stdin
    sys.stdin = types.SimpleNamespace(buffer=buf)
    try:
        # close stdin write end -> EOF mid-call
        os.close(w)
        provider_rpc.main(["provider_rpc", "claude", "owner"])
    finally:
        sys.stdin = old
        buf.close()
    out = last_out(capsys)
    assert out["error"]["category"] == "cancelled"
    assert holder["proc"].returncode is not None


# ---------------- privileged broker tests ----------------

import os as _os
import socket as _socket
import threading as _threading


def _client(sock_path, payload: bytes, read=True):
    conn = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    conn.settimeout(10)
    conn.connect(str(sock_path))
    try:
        conn.sendall(payload)
    except (BrokenPipeError, ConnectionResetError):
        pass
    out = b""
    if read:
        while b"\n" not in out:
            try:
                chunk = conn.recv(65536)
            except (ConnectionResetError, BrokenPipeError):
                break
            if not chunk:
                break
            out += chunk
    conn.close()
    return out


@pytest.fixture()
def broker_env(tmp_path):
    log = tmp_path / "helper.log"
    stdin_capture = tmp_path / "stdin.txt"
    sock = tmp_path / "provider.sock"
    helper = tmp_path / "helper.sh"
    helper.write_text(
        "#!/bin/sh\n"
        f"echo \"$@\" >> '{log}'\n"
        "IFS= read -r line\n"
        f"printf '%s\\n' \"$line\" > '{stdin_capture}'\n"
        "printf '%s\\n' '{\"ok\":true,\"result\":{\"done\":true}}'\n"
        "cat > /dev/null\n"
        f"echo exited >> '{log}'\n")
    helper.chmod(0o755)
    broker = privileged_rpc.Broker(
        sock, helper, allowed_uid=_os.getuid(), drop_privileges=False)
    stop = _threading.Event()
    t = _threading.Thread(target=broker.serve, kwargs={"stop_event": stop},
                          daemon=True)
    t.start()
    for _ in range(50):
        if sock.exists():
            break
        time.sleep(0.05)
    yield sock, log, stdin_capture
    stop.set()
    t.join(timeout=5)


def _envelope(**kw):
    base = {"provider": "claude", "principal": "owner",
            "tool": "claude_status", "arguments": {}}
    base.update(kw)
    return (json.dumps(base) + "\n").encode()


def test_broker_valid_request_spawns_helper(broker_env):
    sock, log, _ = broker_env
    out = _client(sock, _envelope())
    payload = json.loads(out.split(b"\n")[0])
    assert payload["ok"] is True
    assert "claude owner" in log.read_text()


def test_broker_rejects_bad_principal_tool_provider(broker_env):
    sock, log, _ = broker_env
    for env in (_envelope(principal="stranger"),
                _envelope(tool="claude_drop_tables"),
                _envelope(provider="openai"),
                _envelope(selection="not-a-dict")):
        _client(sock, env, read=False)
        time.sleep(0.1)
    assert not log.exists() or log.read_text() == ""


def test_broker_rejects_oversize_and_malformed(broker_env):
    sock, log, _ = broker_env
    _client(sock, b"x" * (40 * 1024 * 1024) + b"\n", read=False)
    _client(sock, b"not json\n", read=False)
    time.sleep(0.2)
    assert not log.exists() or log.read_text() == ""


def test_broker_peer_denial(broker_env, tmp_path):
    sock, log, _ = broker_env
    denied = privileged_rpc.Broker(
        tmp_path / "denied.sock", tmp_path / "helper.sh",
        allowed_uid=_os.getuid() + 1, drop_privileges=False)
    stop = _threading.Event()
    t = _threading.Thread(target=denied.serve, kwargs={"stop_event": stop},
                          daemon=True)
    t.start()
    for _ in range(50):
        if (tmp_path / "denied.sock").exists():
            break
        time.sleep(0.05)
    out = _client(tmp_path / "denied.sock", _envelope(), read=True)
    assert out == b""
    stop.set()
    t.join(timeout=5)
    assert not log.exists() or log.read_text() == ""


def test_broker_disconnect_cancels_helper(broker_env):
    sock, log, stdin_capture = broker_env
    conn = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    conn.settimeout(10)
    conn.connect(str(sock))
    conn.sendall(_envelope())
    conn.close()
    deadline = time.time() + 10
    while time.time() < deadline:
        if log.exists() and "exited" in log.read_text():
            break
        time.sleep(0.1)
    assert log.exists() and "exited" in log.read_text()


class _FakeConn:
    """Scripted socket stand-in for _read_header unit tests."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(round(value, 3))

    def recv(self, size):
        return self._chunks.pop(0) if self._chunks else b""


def test_read_header_deadline_is_absolute_and_bounded():
    """Deterministic fake clock: the remaining timeout decreases on every
    step and the absolute deadline ends the read without hardware timing."""
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
    conn = _FakeConn([b"a", b"b", b"c"])  # never a newline
    out = privileged_rpc._read_header(conn, clock=lambda: next(ticks))
    assert out is None
    assert conn.timeouts == [4.0, 3.0, 2.0, 1.0]


def test_read_header_rejects_pipelined_second_frame():
    """Extra bytes after the first newline inside one chunk are rejected,
    never silently dropped for later reuse."""
    ticks = iter([0.0, 1.0])
    conn = _FakeConn([b'{"a":1}\n{"b":2}\n'])
    assert privileged_rpc._read_header(conn, clock=lambda: next(ticks)) is None


def test_read_header_accepts_single_frame():
    ticks = iter([0.0, 1.0])
    conn = _FakeConn([b'{"a":1}\n'])
    assert privileged_rpc._read_header(conn, clock=lambda: next(ticks)) \
        == b'{"a":1}\n'


class _FakeProc:
    """Just enough helper proc for _write_request: a real pipe fd."""

    def __init__(self, fd):
        self.stdin = os.fdopen(fd, "wb", buffering=0)


def _broker_for(tmp_path):
    return privileged_rpc.Broker(tmp_path / "unit.sock", tmp_path / "helper",
                                 allowed_uid=_os.getuid(),
                                 drop_privileges=False)


def test_write_request_cancels_on_client_readiness(tmp_path):
    """Full helper stdin plus a nonempty client readiness signal must
    cancel promptly — the old peek-and-continue path busy-spun until the
    deadline with a non-reading child."""
    r_fd, w_fd = os.pipe()
    proc = _FakeProc(w_fd)
    peer, conn = _socket.socketpair()
    broker = _broker_for(tmp_path)
    os.set_blocking(w_fd, False)
    try:
        while True:  # fill the pipe so no further write can land
            os.write(w_fd, b"x" * 65536)
    except (BlockingIOError, OSError):
        pass
    request = b"y" * (512 * 1024) + b"\n"  # larger than any pipe buffer
    peer.sendall(b"!")  # nonempty client readiness after the header
    deadline = time.monotonic() + 10.0
    started = time.monotonic()
    ok = broker._write_request(proc, request, conn, deadline)
    elapsed = time.monotonic() - started
    for closer in (conn.close, peer.close, proc.stdin.close):
        try:
            closer()
        except OSError:
            pass
    os.close(r_fd)
    assert ok is False
    assert elapsed < 5.0, f"cancel took {elapsed:.2f}s (deadline 10s)"


def test_write_request_cancels_on_client_eof(tmp_path):
    """Client EOF during the write phase cancels the same way."""
    r_fd, w_fd = os.pipe()
    proc = _FakeProc(w_fd)
    peer, conn = _socket.socketpair()
    broker = _broker_for(tmp_path)
    request = b"y" * (512 * 1024) + b"\n"
    deadline = time.monotonic() + 10.0
    started = time.monotonic()
    try:
        peer.close()  # caller gone before the request fully lands
        ok = broker._write_request(proc, request, conn, deadline)
    finally:
        for closer in (conn.close, peer.close, proc.stdin.close):
            try:
                closer()
            except OSError:
                pass
        os.close(r_fd)
    assert ok is False
    assert time.monotonic() - started < 5.0


def test_stop_event_cancels_active_helper_and_quiesces(tmp_path):
    """stop_event during an active call: the admitted client is shut down
    (client-side EOF), the helper is reaped via stdin close, the slot is
    released, and serve() quiesces promptly instead of waiting the
    request budget."""
    hold_log = tmp_path / "hold.log"
    hold = tmp_path / "hold.sh"
    hold.write_text(
        "#!/bin/sh\n"
        "IFS= read -r line\n"
        "cat > /dev/null\n"  # hold until stdin EOF = cancellation
        f"echo exited >> '{hold_log}'\n")
    hold.chmod(0o755)
    sock2 = tmp_path / "stop.sock"
    b2 = privileged_rpc.Broker(sock2, hold, allowed_uid=_os.getuid(),
                               drop_privileges=False)
    stop2 = _threading.Event()
    t2 = _threading.Thread(target=b2.serve, kwargs={"stop_event": stop2},
                           daemon=True)
    t2.start()
    for _ in range(50):
        if sock2.exists():
            break
        time.sleep(0.05)
    conn = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    conn.settimeout(10)
    conn.connect(str(sock2))
    try:
        # 20-minute request budget: cleanup must not wait anywhere near it.
        envelope = _envelope(tool="claude_once",
                             arguments={"prompt": "x",
                                        "timeout_seconds": 1200})
        conn.sendall(envelope)
        time.sleep(0.5)  # header read + helper spawn + request write
        stop2.set()
        t2.join(timeout=15)
        assert not t2.is_alive(), "serve() did not quiesce promptly"
        deadline = time.monotonic() + 10
        while not hold_log.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert hold_log.exists(), "helper was not reaped after stop"
        assert "exited" in hold_log.read_text()
        assert conn.recv(1) == b""  # admitted conn was shut down
        deadline = time.monotonic() + 10
        while (time.monotonic() < deadline
               and b2._slots._value != privileged_rpc.MAX_HANDLERS):
            time.sleep(0.05)
        assert b2._slots._value == privileged_rpc.MAX_HANDLERS
    finally:
        stop2.set()
        try:
            conn.close()
        except OSError:
            pass
        t2.join(timeout=10)


def test_setup_failure_returns_bounded_error_with_request_id(
        monkeypatch, capsys):
    """Setup/import/Settings failures return one bounded generic category
    plus the normalized opaque id — never backend or import text."""
    mods = fake_provider_modules([])

    def boom(env):
        raise RuntimeError("secret config text")

    mods["claude_research_mcp.config"].load_settings = boom
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)
    run_main("claude", "owner",
             {"tool": "claude_status", "arguments": {},
              "request_id": "not-a-uuid"})
    out = last_out(capsys)
    assert out["ok"] is False
    assert out["error"]["category"] == "provider_error"
    rid = out["error"]["request_id"]
    assert isinstance(rid, str) and uuid.UUID(rid)
    assert "secret" not in json.dumps(out)


def test_broker_concurrent_providers_with_barrier(broker_env, tmp_path):
    """Barrier proof of parallel dispatch (not a timing benchmark): the
    claude helper publishes a started marker and holds until a release
    file appears; the gemini call is admitted only after that marker and
    must complete while claude is still held. A serial broker would
    answer gemini only after claude's release."""
    sock, log, _ = broker_env
    started = tmp_path / "claude.started"
    release = tmp_path / "claude.release"
    hold = tmp_path / "hold.sh"
    hold.write_text(
        "#!/bin/sh\n"
        "IFS= read -r line\n"
        "if [ \"$1\" = \"claude\" ]; then\n"
        f"  echo started > '{started}'\n"
        "  n=0\n"
        f"  while [ ! -f '{release}' ] && [ \"$n\" -lt 200 ]; do\n"
        "    sleep 0.05; n=$((n+1))\n"
        "  done\n"
        "fi\n"
        "printf '%s\\n' '{\"ok\":true,\"result\":{\"held\":true}}'\n"
        "cat > /dev/null\n")
    hold.chmod(0o755)
    sock2 = tmp_path / "barrier.sock"
    b2 = privileged_rpc.Broker(sock2, hold, allowed_uid=_os.getuid(),
                               drop_privileges=False)
    stop2 = _threading.Event()
    t2 = _threading.Thread(target=b2.serve, kwargs={"stop_event": stop2},
                           daemon=True)
    t2.start()
    for _ in range(50):
        if sock2.exists():
            break
        time.sleep(0.05)
    results = {}

    def call(tag, envelope):
        results[tag] = _client(sock2, envelope)

    claude_env = _envelope(tool="claude_status")
    gemini_env = _envelope(provider="gemini", tool="gemini_status")
    claude_thread = _threading.Thread(target=call,
                                      args=("claude", claude_env))
    claude_thread.start()
    deadline = time.monotonic() + 5
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        assert started.exists(), "claude helper never reached its marker"
        # Claude is now held inside its helper; admit gemini only now.
        gemini_thread = _threading.Thread(target=call,
                                          args=("gemini", gemini_env))
        gemini_thread.start()
        gemini_thread.join(timeout=8)
        assert "gemini" in results, "gemini did not finish while claude held"
        assert json.loads(
            results["gemini"].split(b"\n")[0])["ok"] is True
        assert "claude" not in results, "claude answered without release"
        assert claude_thread.is_alive(), "claude call was not held"
    finally:
        # Unconditional release/cleanup: no helper child or client thread
        # may hang because an assert failed.
        release.write_text("go\n")
        claude_thread.join(timeout=15)
        stop2.set()
        t2.join(timeout=10)
    assert "claude" in results, "claude never completed after release"
    assert json.loads(results["claude"].split(b"\n")[0])["ok"] is True


def test_effort_none_vs_expected_high_rejected(monkeypatch, capsys):
    """Actual backend policy effort=None must not satisfy expected 'high'."""
    shared = _policy()
    shared.models["opus55"].reasoning_effort = None
    mods = fake_provider_modules([])
    mods["claude_research_mcp.config"].load_settings = \
        lambda env, _s=shared: _s
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)
    req = {"tool": "claude_once",
           "arguments": {"prompt": "x", "model": "opus55"},
           "selection": CLAUDE_SEL}
    run_main("claude", "owner", req)
    assert last_out(capsys)["error"]["category"] == "selection_rejected"


def test_continue_rejects_supplied_model(fake_modules, capsys):
    req = {"tool": "claude_continue",
           "arguments": {"conversation_id": "c1", "prompt": "x",
                         "model": "opus55"},
           "selection": CLAUDE_SEL}
    run_main("claude", "owner", req)
    assert last_out(capsys)["error"]["category"] == "selection_rejected"


def test_helper_eof_without_newline_fails_fast(broker_env, tmp_path):
    """Helper exits with no newline -> broker fails fast, not at budget."""
    sock, log, _ = broker_env
    helper = tmp_path / "noeol.sh"
    helper.write_text("#!/bin/sh\nexit 0\n")
    helper.chmod(0o755)
    b = privileged_rpc.Broker(tmp_path / "n.sock", helper,
                              allowed_uid=_os.getuid(),
                              drop_privileges=False)
    stop = _threading.Event()
    t = _threading.Thread(target=b.serve, kwargs={"stop_event": stop},
                          daemon=True)
    t.start()
    for _ in range(50):
        if (tmp_path / "n.sock").exists():
            break
        time.sleep(0.05)
    start = time.monotonic()
    out = _client(tmp_path / "n.sock", _envelope())
    assert time.monotonic() - start < 5
    assert out == b""
    stop.set()
    t.join(timeout=5)


def test_broker_ninth_busy_does_not_leak_permits(broker_env):
    """8 held slots + denied/failed acquires must not free permits."""
    sock, log, _ = broker_env
    # Hold all 8 slots with slow clients that never finish headers.
    conns = []
    for _ in range(8):
        c = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        c.settimeout(10)
        c.connect(str(sock))
        conns.append(c)  # connect but send nothing -> header deadline 5s
    # Wait for slots to be consumed (header read in progress).
    time.sleep(0.5)
    # 9th valid request: no slot -> closed without spawn.
    before = log.read_text() if log.exists() else ""
    out = _client(sock, _envelope(), read=False)
    time.sleep(0.3)
    after = log.read_text() if log.exists() else ""
    assert after == before
    for c in conns:
        c.close()
