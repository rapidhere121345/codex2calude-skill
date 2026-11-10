#!/usr/bin/env python3
"""Stdio MCP facade and per-identity shared, token-authenticated SSH broker."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time

from remote_research_state import (
    MAX_LINE, PROTOCOL, atomic_private, private, read_config, startup_lock,
    state_paths,
)

MAX_FACADES = 64
MAX_PENDING = 128
MAX_QUEUE_BYTES = 8 * 1024 * 1024
MAX_OUTGOING_BYTES = 128 * 1024 * 1024
MAX_FACADE_OUTGOING_BYTES = 32 * 1024 * 1024
MAX_FACADE_OUTGOING_FRAMES = 128
SEND_TIMEOUT = 2
HANDSHAKE_TIMEOUT = 25
UNKNOWN = "Remote transport lost after request write; result unknown; not replayed"


def encode(message: dict) -> bytes:
    line = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
    if len(line) > MAX_LINE:
        raise ValueError("Frame too large")
    return line


async def read_line(reader: asyncio.StreamReader, maximum: int = MAX_LINE) -> dict | None:
    try:
        line = await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as exc:
        if exc.partial:
            raise ValueError("Incomplete frame") from exc
        return None
    except asyncio.LimitOverrunError as exc:
        raise ValueError("Frame too large") from exc
    if len(line) > maximum:
        raise ValueError("Frame too large")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value


def response(identifier, *, result=None, error=None) -> dict:
    item = {"jsonrpc": "2.0", "id": identifier}
    item["error" if error is not None else "result"] = error if error is not None else result
    return item


def failure(identifier, message: str) -> dict:
    return response(identifier, error={"code": -32000, "message": message})


class Facade:
    def __init__(self, writer: asyncio.StreamWriter, broker: Broker | None = None):
        self.writer = writer
        self.broker = broker
        self.pending: set[str] = set()
        self.tasks: set[asyncio.Task] = set()
        self.queued_tasks: dict[str, asyncio.Task] = {}
        self.outgoing: asyncio.Queue[bytes] = asyncio.Queue()
        self.outgoing_bytes = 0
        self.outgoing_frames = 0
        self.pump_task: asyncio.Task | None = None
        self.closed = False

    def release(self, size: int) -> None:
        self.outgoing_frames -= 1
        self.outgoing_bytes -= size
        if self.broker:
            self.broker.outgoing_bytes -= size

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.writer.close()
        while not self.outgoing.empty():
            self.release(len(self.outgoing.get_nowait()))
        if self.pump_task and self.pump_task is not asyncio.current_task():
            self.pump_task.cancel()

    async def pump(self) -> None:
        try:
            while not self.closed:
                raw = await self.outgoing.get()
                try:
                    self.writer.write(raw)
                    await asyncio.wait_for(self.writer.drain(), SEND_TIMEOUT)
                except (asyncio.TimeoutError, OSError, ConnectionError,
                        ValueError, RuntimeError):
                    self.close()
                    return
                finally:
                    self.release(len(raw))
        except asyncio.CancelledError:
            return

    async def send(self, message: dict) -> None:
        if self.closed:
            return
        try:
            raw = encode(message)
        except ValueError:
            self.close()
            raise ConnectionError("Local facade response exceeds frame limit") from None
        if (self.outgoing_frames >= MAX_FACADE_OUTGOING_FRAMES
                or self.outgoing_bytes + len(raw) > MAX_FACADE_OUTGOING_BYTES
                or (self.broker and self.broker.outgoing_bytes + len(raw) > MAX_OUTGOING_BYTES)):
            self.close()
            raise ConnectionError("Local facade outbound capacity exceeded")
        self.outgoing_frames += 1
        self.outgoing_bytes += len(raw)
        if self.broker:
            self.broker.outgoing_bytes += len(raw)
        self.outgoing.put_nowait(raw)
        if self.pump_task is None or self.pump_task.done():
            self.pump_task = asyncio.create_task(self.pump())


class Broker:
    def __init__(self, config: dict, metadata: Path, linger: float):
        self.config = config
        self.metadata = metadata
        self.linger = linger
        self.clients: set[Facade] = set()
        self.outgoing_bytes = 0
        self.connections = 0
        self.unauthenticated = 0
        self.pending: dict[str, tuple[Facade, object, object]] = {}
        self.queued = 0
        self.queued_count = 0
        self.next_id = 0
        self.ssh: asyncio.subprocess.Process | None = None
        self.remote_task: asyncio.Task | None = None
        self.handshake: asyncio.Future | None = None
        self.cached: dict | None = None
        self.last_initialized: dict | None = None
        self.starting = asyncio.Lock()
        self.failures = 0
        self.retry_at = 0.0
        self.stop = asyncio.Event()
        self.linger_task: asyncio.Task | None = None

    async def remote_send(self, message: dict) -> None:
        if self.ssh is None or self.ssh.stdin is None:
            raise ConnectionError("SSH unavailable")
        self.ssh.stdin.write(encode(message))
        await self.ssh.stdin.drain()

    async def start_remote(self, *, work: bool = False) -> dict:
        async with self.starting:
            if self.cached is not None:
                return self.cached
            if self.ssh is not None and self.remote_task is not None and self.handshake.done():
                await self.remote_task
            if self.ssh is None:
                if self.failures and not work:
                    raise ConnectionError("Reconnect requires queued work")
                delay = self.retry_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                self.ssh = await asyncio.create_subprocess_exec(
                    *self.config["ssh_command"], stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    limit=MAX_LINE + 1,
                )
                self.handshake = asyncio.get_running_loop().create_future()
                self.remote_task = asyncio.create_task(self.read_remote(self.ssh))
                await self.remote_send({
                    "jsonrpc": "2.0", "id": "rr:init", "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05",
                               "capabilities": {}, "clientInfo": {
                                   "name": "remote-research-broker", "version": "1"}},
                })
            assert self.handshake is not None
            try:
                # Leave time inside the 30-second unsent-work budget to retire
                # a stalled SSH handshake before the caller is cancelled.
                await asyncio.wait_for(asyncio.shield(self.handshake), HANDSHAKE_TIMEOUT)
            except asyncio.TimeoutError:
                if self.ssh and self.ssh.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        self.ssh.terminate()
                if self.remote_task:
                    await self.remote_task
                raise
            assert self.cached is not None
            return self.cached

    async def read_remote(self, process: asyncio.subprocess.Process) -> None:
        try:
            assert process.stdout is not None
            while (message := await read_line(process.stdout)) is not None:
                rid = message.get("id")
                if rid == "rr:init" and ("result" in message or "error" in message):
                    if "result" in message and isinstance(message["result"], dict):
                        self.cached = message["result"]
                        assert self.handshake is not None
                        await self.remote_send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                        self.last_initialized = self.cached
                        if not self.handshake.done():
                            self.handshake.set_result(self.cached)
                    else:
                        if self.handshake and not self.handshake.done():
                            self.handshake.set_exception(ConnectionError("Remote initialize rejected"))
                        break  # A rejected handshake is not a usable shared SSH session.
                    continue
                if "method" in message:
                    if rid is not None:
                        await self.remote_send(response(rid, error={
                            "code": -32601, "message": "Method not supported"}))
                    elif message["method"] == "notifications/progress":
                        params = message.get("params")
                        token = params.get("progressToken") if isinstance(params, dict) else None
                        if isinstance(token, str) and token.startswith("rr:p:"):
                            entry = self.pending.get(token[5:])
                            if entry:
                                owner, _, original_token = entry
                                if original_token is not None:
                                    with contextlib.suppress(ConnectionError, OSError, ValueError):
                                        await owner.send({
                                            "jsonrpc": "2.0", "method": "notifications/progress",
                                            "params": {**params, "progressToken": original_token},
                                        })
                    continue
                entry = self.pending.pop(str(rid), None)
                if entry:
                    self.failures = 0
                    self.retry_at = 0
                    owner, original_id, _ = entry
                    owner.pending.discard(str(rid))
                    with contextlib.suppress(ConnectionError, OSError, ValueError):
                        await owner.send({**message, "id": original_id})
        except (ConnectionError, OSError, ValueError, json.JSONDecodeError):
            pass
        finally:
            if self.ssh is process:
                self.cached = None
                if self.handshake and not self.handshake.done():
                    self.handshake.set_exception(ConnectionError("Remote SSH closed"))
                old = list(self.pending.items())
                for rid, (owner, _, _) in old:
                    self.pending.pop(rid, None)
                    owner.pending.discard(rid)
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), 2)
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
                if not self.stop.is_set():
                    self.failures = min(self.failures + 1, 5)
                    self.retry_at = time.monotonic() + (1, 2, 5, 10, 30)[self.failures - 1]
                self.ssh = None
                for _, (owner, original_id, _) in old:
                    with contextlib.suppress(ConnectionError, OSError, ValueError):
                        await owner.send(failure(original_id, UNKNOWN))

    async def request(self, client: Facade, message: dict) -> None:
        method = message.get("method")
        original_id = message.get("id")
        if method == "notifications/cancelled":
            params = message.get("params")
            requested = params.get("requestId") if isinstance(params, dict) else None
            task = client.queued_tasks.pop(json.dumps(requested), None)
            if task:
                task.cancel()
            for rid in list(client.pending):
                entry = self.pending.get(rid)
                if entry and entry[1] == requested:
                    self.pending.pop(rid, None)
                    client.pending.discard(rid)
                    with contextlib.suppress(ConnectionError, OSError):
                        await self.remote_send({"jsonrpc": "2.0", "method": method,
                                                "params": {"requestId": rid}})
            return
        if method == "ping":
            if "id" in message:
                await client.send(response(original_id, result={}))
            return
        if "id" not in message:
            # Client notifications are not broadcast; initialized is broker-owned.
            return
        if method == "initialize":
            try:
                result = self.last_initialized
                if result is None:
                    result = await asyncio.wait_for(self.start_remote(work=True), 30)
                await client.send(response(original_id, result=result))
            except (ConnectionError, OSError, asyncio.TimeoutError):
                await client.send(failure(original_id, "Remote initialize unavailable"))
            return
        if not isinstance(method, str) or method not in ("tools/list", "tools/call",
                                                         "resources/list", "prompts/list"):
            await client.send(response(original_id, error={
                "code": -32601, "message": "Method not supported"}))
            return
        if isinstance(original_id, bool) or not isinstance(original_id, (int, str, type(None))):
            await client.send(failure(original_id, "Invalid request ID"))
            return
        key = json.dumps(original_id)
        if key in client.queued_tasks or any(
            owner is client and identifier == original_id
            for owner, identifier, _ in self.pending.values()
        ):
            await client.send(failure(original_id, "Duplicate request ID"))
            return
        raw = encode(message)
        if (len(self.pending) + self.queued_count >= MAX_PENDING
                or self.queued + len(raw) > MAX_QUEUE_BYTES):
            await client.send(failure(original_id, "Broker queue capacity exceeded"))
            return
        self.queued += len(raw)
        self.queued_count += 1
        client.queued_tasks[key] = asyncio.current_task()
        rid = None
        try:
            await asyncio.wait_for(self.start_remote(work=True), 30)
            if client.closed:
                return
            client.queued_tasks.pop(key, None)
            self.next_id += 1
            rid = f"rr:{self.next_id}"
            params = message.get("params")
            token = None
            if isinstance(params, dict) and isinstance(params.get("_meta"), dict):
                token = params["_meta"].get("progressToken")
                params = {**params, "_meta": {**params["_meta"], "progressToken": f"rr:p:{rid}"}}
            self.pending[rid] = (client, original_id, token)
            client.pending.add(rid)
            await self.remote_send({**message, "id": rid, "params": params} if params is not None
                                   else {**message, "id": rid})
        except (ConnectionError, OSError, asyncio.TimeoutError):
            if not client.closed:
                if rid is None:
                    await client.send(failure(original_id, "Remote unavailable; request not sent"))
                elif self.pending.pop(rid, None):
                    client.pending.discard(rid)
                    await client.send(failure(original_id, UNKNOWN))
        finally:
            if client.queued_tasks.get(key) is asyncio.current_task():
                client.queued_tasks.pop(key, None)
            self.queued -= len(raw)
            self.queued_count -= 1

    async def attach(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                     token: str) -> None:
        client = None
        admitted = self.connections < MAX_FACADES and self.unauthenticated < 16
        if admitted:
            self.connections += 1
            self.unauthenticated += 1
        unauthenticated = admitted
        try:
            if not admitted:
                return
            auth = await asyncio.wait_for(read_line(reader, 4096), 3)
            if not auth or not isinstance(auth.get("token"), str) or not secrets.compare_digest(
                auth["token"], token
            ) or auth.get("version") != PROTOCOL or len(self.clients) >= MAX_FACADES:
                return
            self.unauthenticated -= 1
            unauthenticated = False
            # start_server has a 4 KiB pre-auth high-water mark.
            reader._limit = MAX_LINE + 1
            client = Facade(writer, self)
            self.clients.add(client)
            if self.linger_task:
                self.linger_task.cancel()
                self.linger_task = None
            await client.send({"ok": True})
            while (message := await read_line(reader)) is not None:
                if len(client.tasks) >= MAX_PENDING:
                    break
                task = asyncio.create_task(self.request(client, message))
                client.tasks.add(task)
                task.add_done_callback(client.tasks.discard)
        except (ConnectionError, OSError, ValueError, json.JSONDecodeError,
                asyncio.TimeoutError):
            pass
        finally:
            if admitted:
                self.connections -= 1
            if unauthenticated:
                self.unauthenticated -= 1
            if client:
                client.close()
                self.clients.discard(client)
                for task in client.tasks:
                    task.cancel()
                for rid in list(client.pending):
                    self.pending.pop(rid, None)
                    with contextlib.suppress(ConnectionError, OSError):
                        await self.remote_send({
                            "jsonrpc": "2.0", "method": "notifications/cancelled",
                            "params": {"requestId": rid},
                        })
                if not self.clients:
                    self.linger_task = asyncio.create_task(self.linger_then_stop())
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

    async def linger_then_stop(self) -> None:
        await asyncio.sleep(self.linger)
        if not self.clients:
            self.stop.set()


async def run_broker(config: dict, metadata: Path) -> None:
    linger = float(os.environ.get("REMOTE_RESEARCH_TEST_LINGER", "60"))
    broker = Broker(config, metadata, linger)
    token = secrets.token_hex(32)
    server = await asyncio.start_server(
        lambda r, w: broker.attach(r, w, token),
        host="127.0.0.1", port=0, limit=4096,
    )
    port = server.sockets[0].getsockname()[1]
    atomic_private(metadata, encode({
        "port": port, "token": token, "version": PROTOCOL, "pid": os.getpid(),
    }))
    broker.linger_task = asyncio.create_task(broker.linger_then_stop())
    try:
        await broker.stop.wait()
    finally:
        server.close()
        await server.wait_closed()
        try:
            if broker.ssh and broker.ssh.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    broker.ssh.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(broker.ssh.wait(), 2)
                if broker.ssh.returncode is None:
                    broker.ssh.kill()
                    await broker.ssh.wait()
        finally:
            # Do not unlink metadata belonging to a newer broker.
            with contextlib.suppress(OSError, ValueError):
                private(metadata)
                if json.loads(metadata.read_text())["pid"] == os.getpid():
                    metadata.unlink()


async def open_broker(metadata: Path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    private(metadata)
    item = json.loads(metadata.read_text())
    if item["version"] != PROTOCOL or not 0 < item["port"] < 65536:
        raise ValueError("Invalid broker metadata")
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", item["port"], limit=MAX_LINE + 1), 2,
    )
    try:
        writer.write(encode({"version": PROTOCOL, "token": item["token"]}))
        await writer.drain()
        ack = await asyncio.wait_for(read_line(reader), 3)
        if ack != {"ok": True}:
            raise ConnectionError("Broker authentication rejected")
        return reader, writer
    except BaseException:
        writer.close()
        raise


def pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return True
    if os.name == "nt":
        return windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def windows_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)  # Query only, never terminate.
    if not handle:
        return ctypes.get_last_error() != 87  # Invalid PID; access denied fails closed.
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


async def connect(config_path: Path, config: dict):
    metadata, lock, _ = state_paths(config)
    # The lock protects checking, starting, and authenticating, not just
    # writing the PID: simultaneous cold starts cannot fork multiple brokers.
    with startup_lock(lock):
        if metadata.exists() or metadata.is_symlink():
            private(metadata)
            try:
                return await open_broker(metadata)
            except (ConnectionError, OSError, asyncio.TimeoutError):
                item = json.loads(metadata.read_text())
                if pid_alive(item["pid"]):
                    raise ConnectionError("Existing broker unavailable")
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--broker",
             "--config", str(config_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=(os.name != "nt"),
            close_fds=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if metadata.exists():
                try:
                    return await open_broker(metadata)
                except (ConnectionError, OSError, asyncio.TimeoutError):
                    pass
        raise ConnectionError("Broker startup failed")


async def run_facade(config_path: Path, config: dict) -> None:
    reader = writer = None
    output_lock = asyncio.Lock()
    incoming: asyncio.Queue[bytes] = asyncio.Queue(maxsize=128)
    outstanding: dict[str, object] = {}
    loop = asyncio.get_running_loop()
    slots = threading.Semaphore(128)

    def stdin_reader() -> None:
        while True:
            slots.acquire()
            line = sys.stdin.buffer.readline(MAX_LINE + 1)
            try:
                loop.call_soon_threadsafe(incoming.put_nowait, line)
            except RuntimeError:
                return
            if not line:
                return

    threading.Thread(target=stdin_reader, daemon=True).start()

    async def output(message: dict) -> None:
        async with output_lock:
            sys.stdout.buffer.write(encode(message))
            sys.stdout.buffer.flush()

    async def receive() -> None:
        assert reader is not None
        while (message := await read_line(reader)) is not None:
            if "id" in message:
                outstanding.pop(json.dumps(message["id"]), None)
            await output(message)

    receiver = None
    try:
        while True:
            input_task = asyncio.create_task(incoming.get())
            active = {input_task}
            if receiver:
                active.add(receiver)
            done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                input_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await input_task
                with contextlib.suppress(OSError, ValueError, ConnectionError):
                    receiver.result()
                for identifier in outstanding.values():
                    await output(failure(identifier, UNKNOWN))
                break
            line = input_task.result()
            slots.release()
            if not line:
                break
            if len(line) > MAX_LINE or not line.endswith(b"\n"):
                break
            try:
                message = {}
                message = json.loads(line)
                if not isinstance(message, dict):
                    continue
                if message.get("method") == "ping":
                    if "id" in message:
                        await output(response(message["id"], result={}))
                    continue
                if writer is None:
                    reader, writer = await connect(config_path, config)
                    receiver = asyncio.create_task(receive())
                if "id" in message:
                    outstanding[json.dumps(message["id"])] = message["id"]
                writer.write(encode(message))
                await writer.drain()
            except (ValueError, ConnectionError, OSError, json.JSONDecodeError):
                if "id" in message and isinstance(message, dict):
                    outstanding.pop(json.dumps(message["id"]), None)
                    await output(failure(message["id"], "Local broker unavailable"))
                break
    finally:
        if writer:
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()
        if receiver:
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError):
                await receiver


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--broker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        config = read_config(args.config)
        if args.broker:
            metadata, _, _ = state_paths(config)
            asyncio.run(run_broker(config, metadata))
        else:
            asyncio.run(run_facade(args.config, config))
    except (ValueError, OSError, ConnectionError) as exc:
        print(f"remote-research client: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
