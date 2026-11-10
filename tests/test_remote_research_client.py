from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/remote-research/scripts"
sys.path.insert(0, str(SCRIPTS))
import remote_research_client as client
import remote_research_state as state


def fake_environment(tmp_path: Path, monkeypatch):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    bin_dir = home / "bin"
    bin_dir.mkdir(mode=0o700)
    ssh = bin_dir / "ssh"
    ssh.write_text(textwrap.dedent("""\
        #!{python}
        import json, os, sys, threading, time
        log = os.environ["FAKE_SSH_LOG"]
        def record(event):
            with open(log, "a") as f:
                f.write(json.dumps(event) + "\\n")
        record({{"event":"spawn","pid":os.getpid()}})
        lock = threading.Lock()
        def reply(item):
            with lock:
                print(json.dumps(item, separators=(",", ":")), flush=True)
        tools = {tools}
        for line in sys.stdin:
            req = json.loads(line)
            method = req.get("method")
            rid = req.get("id")
            if method == "initialize":
                record({{"event":"initialize"}})
                time.sleep(float(os.environ.get("FAKE_INIT_DELAY", "0")))
                reject = os.environ.get("FAKE_REJECT_ONCE")
                if reject and not os.path.exists(reject):
                    with open(reject, "w") as marker:
                        marker.write("rejected")
                    reply({{"jsonrpc":"2.0","id":rid,
                           "error":{{"code":-32000,"message":"rejected"}}}})
                    continue
                reply({{"jsonrpc":"2.0","id":rid,"result":{{"protocolVersion":"2024-11-05",
                      "capabilities":{{}}, "serverInfo":{{"name":"remote_research"}}}}}})
                initialized = False
            elif method == "notifications/initialized":
                initialized = True
                record({{"event":"initialized"}})
            elif method == "tools/list":
                if not initialized:
                    os._exit(12)
                reply({{"jsonrpc":"2.0","id":rid,"result":{{"tools":[{{"name":x}} for x in tools]}}}})
            elif method == "tools/call":
                if not initialized:
                    os._exit(12)
                args = req.get("params",{{}}).get("arguments",{{}})
                record({{"event":"call","tag":args.get("tag"),"id":rid}})
                token = req.get("params",{{}}).get("_meta",{{}}).get("progressToken")
                if token:
                    reply({{"jsonrpc":"2.0","method":"notifications/progress",
                           "params":{{"progressToken":token,"tag":args.get("tag")}}}})
                if args.get("unscoped"):
                    reply({{"jsonrpc":"2.0","method":"notifications/progress",
                           "params":{{"progressToken":"not-owned","tag":"SECRET"}}}})
                if args.get("crash"):
                    os._exit(7)
                if args.get("hold"):
                    def delayed(rid=rid, args=args):
                        time.sleep(.5)
                        reply({{"jsonrpc":"2.0","id":rid,"result":{{"tag":args.get("tag")}}}})
                    threading.Thread(target=delayed,daemon=True).start()
                else:
                    result = ({{"structuredContent":{{
                                   "principal":os.environ.get("FAKE_PRINCIPAL","partner"),
                                   "providers":{{}}}}}}
                              if req["params"]["name"] == "research_status" else
                              {{"tag":args.get("tag")}})
                    reply({{"jsonrpc":"2.0","id":rid,"result":result}})
            elif method == "notifications/cancelled":
                record({{"event":"cancel","id":req["params"]["requestId"]}})
    """.format(python=sys.executable, tools=list(__import__("setup_client").TOOLS))))
    ssh.chmod(0o700)
    key = home / "test-key"
    key.write_text("NOT A REAL KEY")
    key.chmod(0o600)
    (home / "test-key.pub").write_text("ssh-ed25519 example")
    pin = home / "pin"
    pin.write_text(__import__("setup_client").KNOWN_HOSTS_LINE + "\n")
    pin.chmod(0o600)
    conf = home / "client.json"
    conf.write_text(json.dumps({
        "version": 1, "principal": "partner",
        "ssh_command": __import__("setup_client").ssh_argv(key, pin),
    }))
    conf.chmod(0o600)
    log = home / "spawn.jsonl"
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
           "REMOTE_RESEARCH_STATE_DIR": str(home / "state"),
           "REMOTE_RESEARCH_TEST_LINGER": ".4", "FAKE_SSH_LOG": str(log)}
    monkeypatch.setenv("REMOTE_RESEARCH_STATE_DIR", env["REMOTE_RESEARCH_STATE_DIR"])
    monkeypatch.setenv("REMOTE_RESEARCH_TEST_LINGER", ".4")
    return conf, env, log


class Facade:
    def __init__(self, conf, env):
        self.proc = subprocess.Popen([sys.executable, str(SCRIPTS / "remote_research_client.py"),
                                      "--config", str(conf)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.messages = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.messages.put(json.loads(line))

    def send(self, method, rid, params=None):
        assert self.proc.stdin
        request = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if rid is not None:
            request["id"] = rid
        self.proc.stdin.write(client.encode(request))
        self.proc.stdin.flush()

    def get(self, rid):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            message = self.messages.get(timeout=max(.01, deadline - time.monotonic()))
            if message.get("id") == rid:
                return message
        raise AssertionError("missing response")

    def close(self):
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        self.proc.wait(timeout=5)


def log_events(log):
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def wait_until(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.03)
    assert predicate()


def test_three_facades_share_single_ssh_and_linger(tmp_path, monkeypatch):
    conf, env, log = fake_environment(tmp_path, monkeypatch)
    facades = [Facade(conf, env) for _ in range(3)]
    try:
        for f in facades:
            f.send("initialize", 1)
        for f in facades:
            assert f.get(1)["result"]["serverInfo"]["name"] == "remote_research"
        for i, f in enumerate(facades):
            f.send("tools/call", 1, {"name": "research_once",
                                     "arguments": {"tag": str(i), "hold": True}})
        for i, f in enumerate(facades):
            assert f.get(1)["result"]["tag"] == str(i)
        assert len([x for x in log_events(log) if x["event"] == "spawn"]) == 1
        assert len([x for x in log_events(log) if x["event"] == "initialize"]) == 1
        facades[0].close()
        facades[1].close()
        time.sleep(.7)  # Longer than the test linger; one facade still attached.
        facades[2].send("tools/call", 2, {"name": "research_once",
                                         "arguments": {"tag": "still-attached"}})
        assert facades[2].get(2)["result"]["tag"] == "still-attached"
        assert len([x for x in log_events(log) if x["event"] == "spawn"]) == 1
        facades[2].close()
        metadata, _, _ = state.state_paths(state.read_config(conf))
        wait_until(lambda: not metadata.exists())
        newcomer = Facade(conf, env)
        try:
            newcomer.send("initialize", 1)
            assert "result" in newcomer.get(1)
            newcomer.send("tools/call", 2, {"name": "research_once",
                                             "arguments": {"tag": "cold"}})
            assert newcomer.get(2)["result"]["tag"] == "cold"
            assert len([x for x in log_events(log) if x["event"] == "spawn"]) == 2
        finally:
            newcomer.close()
    finally:
        for f in facades:
            if f.proc.poll() is None:
                f.close()


def test_crash_unknown_not_replayed_and_pings_offline(tmp_path, monkeypatch):
    conf, env, log = fake_environment(tmp_path, monkeypatch)
    f = Facade(conf, env)
    try:
        f.send("initialize", 1)
        f.get(1)
        f.send("tools/call", 2, {"name": "research_once",
                                 "arguments": {"tag": "crash", "crash": True}})
        assert "result unknown" in f.get(2)["error"]["message"]
        for i in range(3, 6):
            f.send("ping", i)
            assert f.get(i)["result"] == {}
        assert len([x for x in log_events(log) if x["event"] == "spawn"]) == 1
        f.send("tools/call", 6, {"name": "research_once",
                                 "arguments": {"tag": "next"}})
        assert f.get(6)["result"]["tag"] == "next"
        events = log_events(log)
        assert [x["tag"] for x in events if x["event"] == "call"] == ["crash", "next"]
        assert len([x for x in events if x["event"] == "initialize"]) == 2
    finally:
        f.close()


def test_new_facade_initializes_offline_then_first_call_reconnects(tmp_path, monkeypatch):
    conf, env, log = fake_environment(tmp_path, monkeypatch)
    first = Facade(conf, env)
    second = None
    try:
        first.send("initialize", 1)
        assert "result" in first.get(1)
        first.send("tools/call", 2, {"name": "research_once",
                                     "arguments": {"tag": "uncertain", "crash": True}})
        assert "result unknown" in first.get(2)["error"]["message"]
        second = Facade(conf, env)
        second.send("initialize", 1)
        assert second.get(1)["result"]["serverInfo"]["name"] == "remote_research"
        assert len([x for x in log_events(log) if x["event"] == "spawn"]) == 1
        second.send("ping", 2)
        assert second.get(2)["result"] == {}
        assert len([x for x in log_events(log) if x["event"] == "spawn"]) == 1
        second.send("tools/call", 3, {"name": "research_once",
                                      "arguments": {"tag": "new"}})
        assert second.get(3)["result"]["tag"] == "new"
        assert [x["tag"] for x in log_events(log) if x["event"] == "call"] == [
            "uncertain", "new"
        ]
        assert len([x for x in log_events(log) if x["event"] == "spawn"]) == 2
    finally:
        first.close()
        if second:
            second.close()


def test_rejected_initialize_retires_child_and_retries_on_new_demand(
    tmp_path, monkeypatch,
):
    conf, env, log = fake_environment(tmp_path, monkeypatch)
    env["FAKE_REJECT_ONCE"] = str(tmp_path / "rejected-once")
    f = Facade(conf, env)
    try:
        f.send("initialize", 1)
        assert "error" in f.get(1)
        events = log_events(log)
        assert len([x for x in events if x["event"] == "spawn"]) == 1
        original_pid = next(x["pid"] for x in events if x["event"] == "spawn")
        wait_until(lambda: not Path(f"/proc/{original_pid}").exists())
        f.send("ping", 2)
        assert f.get(2)["result"] == {}
        assert len([x for x in log_events(log) if x["event"] == "spawn"]) == 1
        f.send("initialize", 3)
        assert f.get(3)["result"]["serverInfo"]["name"] == "remote_research"
        assert len([x for x in log_events(log) if x["event"] == "initialize"]) == 2
    finally:
        f.close()
        for event in log_events(log):
            if event["event"] == "spawn":
                try:
                    os.kill(event["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass


def test_stalled_handshake_retires_child_and_next_demand_retries(
    tmp_path, monkeypatch,
):
    import asyncio
    conf, env, log = fake_environment(tmp_path, monkeypatch)
    for name in ("PATH", "FAKE_SSH_LOG"):
        monkeypatch.setenv(name, env[name])
    monkeypatch.setenv("FAKE_INIT_DELAY", "5")
    monkeypatch.setattr(client, "HANDSHAKE_TIMEOUT", .15)

    async def run():
        broker = client.Broker(state.read_config(conf), tmp_path / "meta", .4)
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(broker.start_remote(work=True), 3)
            assert broker.ssh is None
            monkeypatch.setenv("FAKE_INIT_DELAY", "0")
            result = await asyncio.wait_for(broker.start_remote(work=True), 4)
            assert result["serverInfo"]["name"] == "remote_research"
            assert len([x for x in log_events(log) if x["event"] == "initialize"]) == 2
        finally:
            if broker.ssh and broker.ssh.returncode is None:
                broker.ssh.terminate()
                await broker.ssh.wait()
    asyncio.run(run())


def test_cancel_is_scoped_and_invalid_ipc_rejected(tmp_path, monkeypatch):
    import asyncio
    conf, env, log = fake_environment(tmp_path, monkeypatch)
    a, b = Facade(conf, env), Facade(conf, env)
    try:
        a.send("initialize", 1)
        b.send("initialize", 1)
        a.get(1)
        b.get(1)
        a.send("tools/call", 8, {"name": "research_once",
                                 "arguments": {"hold": True, "tag": "a"}})
        b.send("tools/call", 8, {"name": "research_once",
                                 "arguments": {"hold": True, "tag": "b"}})
        wait_until(lambda: len([x for x in log_events(log) if x["event"] == "call"]) == 2)
        a.close()
        assert b.get(8)["result"]["tag"] == "b"
        wait_until(lambda: len([x for x in log_events(log) if x["event"] == "cancel"]) >= 1)
        metadata, _, _ = state.state_paths(state.read_config(conf))
        item = json.loads(metadata.read_text())

        async def invalid():
            r, w = await asyncio.open_connection("127.0.0.1", item["port"])
            w.write(client.encode({"version": 1, "token": "wrong"}))
            await w.drain()
            assert await r.read() == b""
            w.close()
            r, w = await asyncio.open_connection("127.0.0.1", item["port"])
            w.write(b"x" * 8192 + b"\n")
            with __import__("contextlib").suppress(ConnectionResetError, BrokenPipeError):
                await w.drain()
                assert await r.read() == b""
            w.close()
            r, w = await asyncio.open_connection("127.0.0.1", item["port"])
            w.write(client.encode({"version": 1, "token": item["token"]}))
            await w.drain()
            assert json.loads(await r.readline()) == {"ok": True}
            w.write(b"x" * (state.MAX_LINE + 2) + b"\n")
            with __import__("contextlib").suppress(ConnectionResetError, BrokenPipeError):
                await w.drain()
                assert await r.read() == b""
            w.close()
        asyncio.run(invalid())
    finally:
        if a.proc.poll() is None:
            a.close()
        b.close()


def test_progress_never_crosses_facades(tmp_path, monkeypatch):
    conf, env, _ = fake_environment(tmp_path, monkeypatch)
    a, b = Facade(conf, env), Facade(conf, env)
    try:
        for f in (a, b):
            f.send("initialize", 1)
            f.get(1)
        for f, tag in ((a, "alice"), (b, "bob")):
            f.send("tools/call", 2, {
                "name": "research_once", "arguments": {"tag": tag, "unscoped": True},
                "_meta": {"progressToken": "same-local-token"},
            })
        for f, tag in ((a, "alice"), (b, "bob")):
            messages = [f.messages.get(timeout=4), f.messages.get(timeout=4)]
            assert {m.get("method") for m in messages} == {None, "notifications/progress"}
            assert all(m.get("params", {}).get("tag", tag) == tag for m in messages)
            notification = next(m for m in messages if "method" in m)
            assert notification["params"]["progressToken"] == "same-local-token"
            assert f.messages.empty()
    finally:
        a.close()
        b.close()


def test_windows_acl_fail_closed_with_mocked_icacls(tmp_path, monkeypatch):
    monkeypatch.setenv("USERNAME", "researcher")
    monkeypatch.setattr(state, "windows_owned_by_current_user", lambda path: True)
    def command(argv, **kwargs):
        if len(argv) > 2:
            return subprocess.CompletedProcess(argv, 0)
        return subprocess.CompletedProcess(
            argv, 0, f"{tmp_path} MACHINE\\researcher:(F)\n", ""
        )
    monkeypatch.setattr(state.subprocess, "run", command)
    state.secure_windows_acl(tmp_path)

    def foreign(argv, **kwargs):
        if len(argv) > 2:
            return subprocess.CompletedProcess(argv, 0)
        return subprocess.CompletedProcess(
            argv, 0, f"{tmp_path} MACHINE\\researcher:(F)\n BUILTIN\\Other:(F)\n", ""
        )
    monkeypatch.setattr(state.subprocess, "run", foreign)
    with pytest.raises(ValueError, match="ACL"):
        state.secure_windows_acl(tmp_path)
    monkeypatch.setattr(state, "windows_owned_by_current_user", lambda path: False)
    with pytest.raises(ValueError, match="owner"):
        state.secure_windows_acl(tmp_path)


def test_windows_pid_check_never_uses_os_kill(monkeypatch):
    import ctypes
    from ctypes import wintypes

    calls = []

    class Function:
        def __init__(self, fn):
            self.fn = fn

        def __call__(self, *args):
            return self.fn(*args)

    def exit_code(handle, value):
        ctypes.cast(value, ctypes.POINTER(wintypes.DWORD))[0] = 259
        return True

    kernel = type("Kernel", (), {})()
    kernel.OpenProcess = Function(lambda rights, inherit, pid: (
        calls.append(("open", rights, pid)) or 42
    ))
    kernel.GetExitCodeProcess = Function(exit_code)
    kernel.CloseHandle = Function(lambda handle: calls.append(("close", handle)))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setattr(client.os, "kill", lambda *args: pytest.fail("os.kill on Windows"))
    with monkeypatch.context() as windows:
        windows.setattr(client.os, "name", "nt")
        assert client.pid_alive(123) is True
    assert calls == [("open", 0x1000, 123), ("close", 42)]

    def exited(handle, value):
        ctypes.cast(value, ctypes.POINTER(wintypes.DWORD))[0] = 0
        return True

    kernel.GetExitCodeProcess = Function(exited)
    with monkeypatch.context() as windows:
        windows.setattr(client.os, "name", "nt")
        assert client.pid_alive(123) is False


def test_cancel_before_handshake_never_writes_business_call(tmp_path, monkeypatch):
    conf, env, log = fake_environment(tmp_path, monkeypatch)
    env["FAKE_INIT_DELAY"] = ".5"
    a, b = Facade(conf, env), Facade(conf, env)
    try:
        a.send("initialize", 1)
        wait_until(lambda: any(x["event"] == "initialize" for x in log_events(log)))
        a.send("tools/call", 2, {"name": "research_once",
                                 "arguments": {"tag": "must-not-run"}})
        a.send("notifications/cancelled", None, {"requestId": 2})
        b.send("initialize", 1)
        assert "result" in b.get(1)
        b.send("tools/call", 2, {"name": "research_once",
                                 "arguments": {"tag": "other"}})
        assert b.get(2)["result"]["tag"] == "other"
        assert "result" in a.get(1)
        assert [x["tag"] for x in log_events(log) if x["event"] == "call"] == ["other"]
    finally:
        a.close()
        b.close()


def test_broker_death_wakes_facade_with_stdin_open(tmp_path, monkeypatch):
    conf, env, log = fake_environment(tmp_path, monkeypatch)
    f = Facade(conf, env)
    metadata = None
    try:
        f.send("initialize", 1)
        f.get(1)
        metadata, _, _ = state.state_paths(state.read_config(conf))
        f.send("tools/call", 2, {"name": "research_once",
                                 "arguments": {"hold": True, "tag": "lost"}})
        wait_until(lambda: any(x.get("tag") == "lost" for x in log_events(log)))
        pid = json.loads(metadata.read_text())["pid"]
        os.kill(pid, signal.SIGKILL)  # Test-created broker only.
        assert ("result unknown" in f.get(2)["error"]["message"]
                or f.proc.poll() is not None)
        f.proc.wait(timeout=3)  # stdin is still open; no blocked executor shutdown.
    finally:
        if f.proc.poll() is None:
            f.close()
        # Broker SIGKILL cannot reap its own test-created fake SSH child.
        for event in log_events(log):
            if event["event"] == "spawn":
                try:
                    os.kill(event["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass


def test_unauthenticated_connections_are_bounded(tmp_path, monkeypatch):
    import asyncio
    conf, env, _ = fake_environment(tmp_path, monkeypatch)
    f = Facade(conf, env)
    try:
        f.send("initialize", 1)
        f.get(1)
        metadata, _, _ = state.state_paths(state.read_config(conf))
        port = json.loads(metadata.read_text())["port"]

        async def flood():
            sockets = [await asyncio.open_connection("127.0.0.1", port)
                       for _ in range(30)]
            try:
                await asyncio.sleep(.1)
                closed = 0
                for reader, writer in sockets:
                    try:
                        if await asyncio.wait_for(reader.read(1), .05) == b"":
                            closed += 1
                    except asyncio.TimeoutError:
                        pass
                assert closed >= 14  # 16 pre-auth slots; 14 immediately rejected.
            finally:
                for _, writer in sockets:
                    writer.close()
        asyncio.run(flood())
        f.send("tools/call", 2, {"name": "research_once", "arguments": {"tag": "alive"}})
        assert f.get(2)["result"]["tag"] == "alive"
    finally:
        f.close()


def test_stalled_progress_recipient_does_not_close_shared_ssh(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setattr(client, "SEND_TIMEOUT", .02)

    class Writer:
        def __init__(self, stalled=False):
            self.stalled = stalled
            self.data = []
            self.closed = False

        def write(self, raw):
            self.data.append(json.loads(raw))

        async def drain(self):
            if self.stalled:
                await asyncio.Event().wait()

        def close(self):
            self.closed = True

    class Remote:
        def __init__(self, reader):
            self.stdout = reader
            self.returncode = 0

        async def wait(self):
            return 0

    async def run():
        broker = client.Broker({}, tmp_path / "metadata", .4)
        slow_writer, fast_writer = Writer(True), Writer()
        slow, fast = client.Facade(slow_writer, broker), client.Facade(fast_writer, broker)
        slow.pending.add("rr:1")
        fast.pending.add("rr:2")
        broker.pending["rr:1"] = (slow, 1, "slow-token")
        broker.pending["rr:2"] = (fast, 1, None)
        reader = asyncio.StreamReader(limit=state.MAX_LINE + 1)
        process = Remote(reader)
        broker.ssh = process
        broker.cached = {"protocolVersion": "2024-11-05"}
        reader.feed_data(client.encode({
            "jsonrpc": "2.0", "method": "notifications/progress",
            "params": {"progressToken": "rr:p:rr:1", "private": "slow-only"},
        }))
        reader.feed_data(client.encode({"jsonrpc": "2.0", "id": "rr:2",
                                        "result": {"private": "fast-only"}}))
        task = asyncio.create_task(broker.read_remote(process))
        await asyncio.sleep(.05)
        assert slow_writer.closed
        assert fast_writer.data == [{"jsonrpc": "2.0", "id": 1,
                                     "result": {"private": "fast-only"}}]
        assert broker.ssh is process
        assert all("slow-only" not in json.dumps(m) for m in fast_writer.data)
        assert broker.outgoing_bytes == 0
        reader.feed_eof()
        await asyncio.wait_for(task, 1)
    asyncio.run(run())


def test_outgoing_large_reply_and_budgets(tmp_path, monkeypatch):
    import asyncio

    class Writer:
        def __init__(self, delay=0):
            self.delay = delay
            self.data = []
            self.closed = False

        def write(self, value):
            self.data.append(value)

        async def drain(self):
            await asyncio.sleep(self.delay)

        def close(self):
            self.closed = True

    async def run():
        broker = client.Broker({}, tmp_path / "metadata", .4)
        writer = Writer(.2)
        facade = client.Facade(writer, broker)
        await facade.send({"result": "a" * (4 * 1024 * 1024)})
        assert facade.outgoing_bytes >= 4 * 1024 * 1024
        await asyncio.sleep(.3)
        assert not writer.closed and len(writer.data) == 1
        assert broker.outgoing_bytes == facade.outgoing_bytes == 0
        assert facade.outgoing_frames == 0

        stalled = Writer(5)
        overflowing = client.Facade(stalled, broker)
        for i in range(client.MAX_FACADE_OUTGOING_FRAMES):
            await overflowing.send({"id": i, "result": "bounded"})
        assert overflowing.outgoing_frames == 128
        with pytest.raises(ConnectionError):
            await overflowing.send({"id": 129, "result": "over limit"})
        await asyncio.sleep(0)
        assert stalled.closed and broker.outgoing_bytes == 0
        assert overflowing.outgoing_bytes == overflowing.outgoing_frames == 0

        monkeypatch.setattr(client, "MAX_OUTGOING_BYTES", 30)
        limited = client.Facade(Writer(5), broker)
        with pytest.raises(ConnectionError):
            await limited.send({"result": "too big for shared cap"})
        assert limited.closed and broker.outgoing_bytes == 0

        monkeypatch.setattr(client, "MAX_OUTGOING_BYTES", 128 * 1024 * 1024)
        monkeypatch.setattr(client, "MAX_FACADE_OUTGOING_BYTES", 30)
        per_client = client.Facade(Writer(5), broker)
        with pytest.raises(ConnectionError):
            await per_client.send({"result": "too big for own cap"})
        assert per_client.closed and broker.outgoing_bytes == 0

        monkeypatch.setattr(client, "MAX_FACADE_OUTGOING_BYTES", 32 * 1024 * 1024)
        in_flight = client.Facade(Writer(5), broker)
        await in_flight.send({"result": "close while draining"})
        await asyncio.sleep(.01)
        assert broker.outgoing_bytes > 0
        in_flight.close()
        await asyncio.sleep(.01)
        assert in_flight.outgoing_frames == in_flight.outgoing_bytes == 0
        assert broker.outgoing_bytes == 0
    asyncio.run(run())


def test_successive_crashes_backoff_only_on_real_work(tmp_path, monkeypatch):
    conf, env, log = fake_environment(tmp_path, monkeypatch)
    f = Facade(conf, env)
    try:
        f.send("initialize", 1)
        f.get(1)
        f.send("tools/call", 2, {"name": "research_once",
                                 "arguments": {"tag": "2", "crash": True}})
        assert "result unknown" in f.get(2)["error"]["message"]
        before = time.monotonic()
        f.send("ping", 12)
        assert f.get(12)["result"] == {}
        assert time.monotonic() - before < .5
        assert len([x for x in log_events(log) if x["event"] == "spawn"]) == 1
        f.send("tools/call", 3, {"name": "research_once",
                                 "arguments": {"tag": "3", "crash": True}})
        assert "result unknown" in f.get(3)["error"]["message"]
        started = time.monotonic()
        f.send("tools/call", 4, {"name": "research_once",
                                 "arguments": {"tag": "recovered"}})
        assert f.get(4)["result"]["tag"] == "recovered"
        assert time.monotonic() - started >= 1.8
        assert [x["tag"] for x in log_events(log) if x["event"] == "call"] == [
            "2", "3", "recovered"
        ]
    finally:
        f.close()


def test_backoff_schedule_caps_at_thirty_seconds(tmp_path):
    import asyncio

    class DeadProcess:
        returncode = 1

        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_eof()

        async def wait(self):
            return self.returncode

    async def run():
        broker = client.Broker({}, tmp_path / "metadata", .4)
        for expected in (1, 2, 5, 10, 30, 30):
            process = DeadProcess()
            broker.ssh = process
            broker.handshake = asyncio.get_running_loop().create_future()
            broker.handshake.set_result({})
            await broker.read_remote(process)
            assert expected - .1 <= broker.retry_at - time.monotonic() <= expected
            assert broker.ssh is None
    asyncio.run(run())
