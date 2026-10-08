#!/usr/bin/env python3
"""Drop-tolerant stdio proxy for the claude-research SSH transport.

Usage: mcp_ssh_reconnect.py COMMAND [ARGS...]

Initialize is retried only before its first response, within a single startup
budget (CLAUDE_MCP_RECONNECT_STARTUP_TIMEOUT, default 45 seconds). Subsequent
connections replay the handshake privately. Business requests are never
replayed after a write attempt: their outcome may be unknown.
A quiet connection closes after CLAUDE_MCP_RECONNECT_IDLE_TIMEOUT seconds
(default 1800) and reconnects only when another client message arrives.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import signal
import sys
import threading
import time
from collections import deque

DEFAULT_DELAYS = "1,2,5,10,30"
DEFAULT_QUEUE_TIMEOUT = 30.0
DEFAULT_IDLE_TIMEOUT = 1800.0
DEFAULT_STARTUP_TIMEOUT = 45.0
STABLE_AFTER = 60.0
HANDSHAKE_TIMEOUT = 45.0
READ_LIMIT = 32 * 1024 * 1024
MAX_QUEUED_REQUESTS = 128
MAX_QUEUED_BYTES = 8 * 1024 * 1024


class ClientOutputClosed(RuntimeError):
    """The client closed the proxy's response stream."""


class InitializeRejected(RuntimeError):
    """A protocol response ended startup; transport retries cannot fix it."""


def _delays() -> tuple[float, ...]:
    raw = os.environ.get("CLAUDE_MCP_RECONNECT_DELAYS", DEFAULT_DELAYS)
    out = []
    for part in raw.split(","):
        try:
            value = float(part)
        except ValueError:
            continue
        if math.isfinite(value) and value >= 0:
            out.append(value)
    return tuple(out or [1.0])


def _timeout(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def _queue_timeout() -> float:
    return _timeout("CLAUDE_MCP_RECONNECT_QUEUE_TIMEOUT", DEFAULT_QUEUE_TIMEOUT)


def _idle_timeout() -> float:
    return _timeout("CLAUDE_MCP_RECONNECT_IDLE_TIMEOUT", DEFAULT_IDLE_TIMEOUT)


def _log(message: str) -> None:
    print(f"mcp-reconnect: {message}", file=sys.stderr, flush=True)


def _debug(message: str) -> None:
    if os.environ.get("CLAUDE_MCP_RECONNECT_DEBUG"):
        _log(message)


def _parse(raw: bytes) -> dict | None:
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _id_key(value: object) -> str:
    return json.dumps(value, sort_keys=True)


class Proxy:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.delays = _delays()
        self.queue_timeout = _queue_timeout()
        self.idle_timeout = _idle_timeout()
        self.startup_timeout = _timeout(
            "CLAUDE_MCP_RECONNECT_STARTUP_TIMEOUT", DEFAULT_STARTUP_TIMEOUT
        )
        self.startup_deadline: float | None = None
        self.backoff = -1
        self.ready = False
        self.stop_event = asyncio.Event()
        self.init_available = asyncio.Event()
        self.send_available = asyncio.Event()
        self.demand_available = asyncio.Event()
        self.proc: asyncio.subprocess.Process | None = None
        self.pending: dict[str, object] = {}
        self.queue: deque[tuple[float, bytes]] = deque()
        self.queue_bytes = 0
        self.init_raw: bytes | None = None
        self.init_id: object = None
        self.init_complete = False
        self.init_answered = False
        self.initialized_raw: bytes | None = None
        self.initialized_sent = False
        self.quiet_since: float | None = None
        self.client_in: asyncio.StreamReader | None = None
        self.client_out: asyncio.StreamWriter | None = None
        self.client_read_transport: asyncio.ReadTransport | None = None

    def _mark_busy(self) -> None:
        self.quiet_since = None

    def _mark_quiet_if_idle(self) -> None:
        # Queue housekeeping is not client activity. Preserve the start of an
        # existing quiet period across the supervisor's periodic checks.
        if not self.pending and not self.queue and self.quiet_since is None:
            self.quiet_since = time.monotonic()

    # ---------------- client-facing output ----------------

    def write_client(self, raw: bytes) -> None:
        try:
            if self.client_out is None:
                sys.stdout.buffer.write(raw)
                sys.stdout.buffer.flush()
            else:
                self.client_out.write(raw)
        except (BrokenPipeError, ValueError, OSError):
            raise ClientOutputClosed() from None

    async def open_client_stdio(self) -> None:
        loop = asyncio.get_running_loop()
        self.client_in = asyncio.StreamReader(limit=READ_LIMIT)
        try:
            reader_protocol = asyncio.StreamReaderProtocol(self.client_in)
            self.client_read_transport, _ = await loop.connect_read_pipe(
                lambda: reader_protocol, sys.stdin.buffer
            )
        except ValueError:
            # Regular-file stdin (test harnesses); Codex always uses a pipe.
            reader = self.client_in

            def feed() -> None:
                try:
                    for line in sys.stdin.buffer:
                        loop.call_soon_threadsafe(reader.feed_data, line)
                finally:
                    with contextlib.suppress(RuntimeError):
                        loop.call_soon_threadsafe(reader.feed_eof)

            threading.Thread(target=feed, daemon=True).start()
        try:
            writer_protocol = asyncio.streams.FlowControlMixin()
            writer_transport, _ = await loop.connect_write_pipe(
                lambda: writer_protocol, sys.stdout.buffer
            )
            self.client_out = asyncio.StreamWriter(
                writer_transport, writer_protocol, None, loop
            )
        except ValueError:
            self.client_out = None

    async def drain_client(self) -> None:
        if self.client_out is not None:
            try:
                await self.client_out.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                raise ClientOutputClosed() from None

    def write_error(self, request_id: object, message: str) -> None:
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32000, "message": message},
        }
        self.write_client((json.dumps(response) + "\n").encode("utf-8"))

    def reject_unsent(self, raw: bytes, reason: str) -> None:
        obj = _parse(raw)
        if obj is not None and "method" in obj and "id" in obj:
            self.write_error(
                obj["id"], f"claude-research {reason}; request was not sent"
            )

    # ---------------- persistent client reader / bounded queue ----------------

    async def handle_client_line(self, raw: bytes) -> None:
        _debug(f"client message received ({len(raw)} bytes)")
        obj = _parse(raw)
        if obj is not None and obj.get("method") == "initialize" and "id" in obj:
            if self.init_raw is not None:
                self.reject_unsent(raw, "duplicate initialize")
                return
            self.init_raw = raw
            self.init_id = obj["id"]
            # One budget across all attempts; queue expiry never consumes initialize.
            self.startup_deadline = time.monotonic() + self.startup_timeout
            self.init_available.set()
            return
        if obj is not None and obj.get("method") == "notifications/initialized" and "id" not in obj:
            self.initialized_raw = raw
            self.send_available.set()
            return
        if obj is not None and obj.get("method") == "ping" and "id" in obj and self.init_complete:
            response = {"jsonrpc": "2.0", "id": obj["id"], "result": {}}
            self.write_client((json.dumps(response) + "\n").encode("utf-8"))
            return
        self._mark_busy()
        self.fail_expired_queued()
        if len(self.queue) >= MAX_QUEUED_REQUESTS or self.queue_bytes + len(raw) > MAX_QUEUED_BYTES:
            self.reject_unsent(raw, "transport queue full")
            return
        self.queue.append((time.monotonic(), raw))
        self.queue_bytes += len(raw)
        self.demand_available.set()
        self.send_available.set()

    async def client_stdin_loop(self) -> None:
        assert self.client_in is not None
        while True:
            raw = await self.client_in.readline()
            if not raw:
                self.request_stop()
                return
            await self.handle_client_line(raw)
            await self.drain_client()

    def fail_expired_queued(self) -> None:
        now = time.monotonic()
        while self.queue and now - self.queue[0][0] > self.queue_timeout:
            _, raw = self.queue.popleft()
            self.queue_bytes -= len(raw)
            self.reject_unsent(raw, "transport remained offline")
        self._mark_quiet_if_idle()

    async def supervise_queued_requests(self) -> None:
        interval = min(max(self.queue_timeout / 10.0, 0.05), 1.0)
        while True:
            await asyncio.sleep(interval)
            self.fail_expired_queued()
            await self.drain_client()

    # ---------------- child plumbing ----------------

    async def write_child(self, raw: bytes) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        _debug(f"writing child message ({len(raw)} bytes)")
        self.proc.stdin.write(raw)
        await self.proc.stdin.drain()

    async def handshake(self) -> None:
        assert self.init_raw is not None
        assert self.proc is not None and self.proc.stdout is not None
        replay = self.init_complete
        await self.write_child(self.init_raw)
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                raise ConnectionError("child closed during initialize")
            obj = _parse(raw)
            if obj is not None and "method" not in obj and "id" in obj and _id_key(obj["id"]) == _id_key(self.init_id):
                if "error" in obj or not isinstance(obj.get("result"), dict):
                    if replay:
                        raise ConnectionError("initialize replay rejected")
                    self.init_answered = True
                    if "error" in obj:
                        # A real MCP rejection is terminal, and preserved verbatim.
                        self.write_client(raw)
                    else:
                        self.write_error(self.init_id, "claude-research invalid initialize response")
                    _log("initialize rejected; stopping startup retries")
                    raise InitializeRejected()
                if not replay:
                    self.init_answered = True
                    self.write_client(raw)
                    # Once emitted, retries must be private even if output drain
                    # stalls or is cancelled at the startup deadline.
                    self.init_complete = True
                    await self.drain_client()
                    _log("initial initialize response delivered")
                else:
                    _log("initialize handshake replayed")
                return
            self.write_client(raw)
            await self.drain_client()

    async def establish(self) -> None:
        self.ready = False
        self.initialized_sent = False
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
            limit=READ_LIMIT,
        )
        _log(f"child pid {self.proc.pid} started")
        await self.handshake()
        self.quiet_since = time.monotonic()
        self.ready = True

    async def send_loop(self) -> None:
        while True:
            self.send_available.clear()
            if self.initialized_raw is not None and not self.initialized_sent:
                await self.write_child(self.initialized_raw)
                self.initialized_sent = True
                self._mark_quiet_if_idle()
            self.fail_expired_queued()
            while self.queue:
                _, raw = self.queue.popleft()
                self.queue_bytes -= len(raw)
                obj = _parse(raw)
                if obj is not None and "method" in obj and "id" in obj:
                    if len(self.pending) >= MAX_QUEUED_REQUESTS:
                        self.reject_unsent(raw, "too many requests in flight")
                        continue
                    self.pending[_id_key(obj["id"])] = obj["id"]
                # From this point even a failed drain may have reached the server.
                # Never put this message back in the queue on any write failure.
                await self.write_child(raw)
                self._mark_quiet_if_idle()
            await self.send_available.wait()

    async def idle_timeout_loop(self) -> None:
        while True:
            if self.pending or self.queue or self.quiet_since is None:
                await asyncio.sleep(min(max(self.idle_timeout / 10.0, 0.05), 1.0))
                continue
            remaining = self.idle_timeout - (time.monotonic() - self.quiet_since)
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            if self.pending or self.queue or self.quiet_since is None:
                continue
            proc = self.proc
            if proc is None or proc.returncode is not None:
                continue
            _log("idle timeout reached; closing child until the next request")
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.terminate()
            return

    async def child_stdout_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                raise ConnectionError("child stdout closed")
            _debug(f"child message received ({len(raw)} bytes)")
            obj = _parse(raw)
            if obj is not None and "method" not in obj and "id" in obj:
                if _id_key(obj["id"]) == _id_key(self.init_id):
                    # Duplicate/replayed initialize responses must never escape.
                    continue
                self.pending.pop(_id_key(obj["id"]), None)
                if not self.pending and not self.queue:
                    self.quiet_since = time.monotonic()
            self.write_client(raw)
            await self.drain_client()

    async def pump(self) -> None:
        tasks = (
            asyncio.create_task(self.child_stdout_loop()),
            asyncio.create_task(self.send_loop()),
            asyncio.create_task(self.idle_timeout_loop()),
        )
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            self.ready = False
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_child(self) -> None:
        self.ready = False
        proc = self.proc
        if proc is None:
            return
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        else:
            await proc.wait()
        # Keep the handle during awaits so cancellation cannot orphan the child.
        if self.proc is proc:
            self.proc = None

    def fail_pending(self) -> None:
        pending, self.pending = self.pending, {}
        for request_id in pending.values():
            self.write_error(
                request_id,
                "claude-research transport failed before the response was received; "
                "result unknown and tools/call was not replayed",
            )

    async def fail_startup(self) -> None:
        if not self.init_answered:
            self.init_answered = True
            self.write_error(
                self.init_id,
                f"claude-research initialize failed: startup recovery budget "
                f"exhausted after {self.startup_timeout:g}s (SSH unavailable or "
                "no initialize response)",
            )
            await self.drain_client()
        _log("startup recovery budget exhausted; stopping")

    # ---------------- main loop ----------------

    async def run_connections(self) -> None:
        # No subprocess or startup countdown until the client supplies initialize.
        await self.init_available.wait()
        assert self.startup_deadline is not None
        while not self.stop_event.is_set():
            self.fail_expired_queued()
            await self.drain_client()
            if self.init_complete and not self.queue:
                while not self.stop_event.is_set():
                    self.demand_available.clear()
                    self.fail_expired_queued()
                    await self.drain_client()
                    if self.queue:
                        break
                    await self.demand_available.wait()
                if self.stop_event.is_set():
                    return
            started = time.monotonic()
            if not self.init_complete and started >= self.startup_deadline:
                await self.fail_startup()
                return
            budget = HANDSHAKE_TIMEOUT if self.init_complete else self.startup_deadline - started
            try:
                # Covers spawn, initialize write/drain, and response. It does not
                # limit a successfully initialized connection's lifetime.
                await asyncio.wait_for(self.establish(), timeout=budget)
                await self.pump()
            except InitializeRejected:
                await self.drain_client()
                return
            except ClientOutputClosed:
                raise
            except asyncio.TimeoutError:
                if not self.init_complete:
                    # Emit the deadline error before the child's shutdown grace.
                    await self.fail_startup()
                    return
                _log("connection cycle failed (TimeoutError)")
            except Exception as exc:
                # Exception text may contain payloads or command arguments.
                _log(f"connection cycle failed ({type(exc).__name__})")
            finally:
                await self.stop_child()
            if self.stop_event.is_set():
                return
            self.fail_pending()
            self.fail_expired_queued()
            await self.drain_client()
            if time.monotonic() - started >= STABLE_AFTER:
                self.backoff = -1
            else:
                self.backoff = min(self.backoff + 1, len(self.delays) - 1)
            delay = self.delays[max(self.backoff, 0)]
            if not self.init_complete:
                remaining = self.startup_deadline - time.monotonic()
                if remaining <= 0:
                    await self.fail_startup()
                    return
                delay = min(delay, remaining)
            _log(f"reconnecting in {delay:g}s")
            await asyncio.sleep(delay)

    async def run(self) -> None:
        tasks = (
            asyncio.create_task(self.client_stdin_loop()),
            asyncio.create_task(self.supervise_queued_requests()),
            asyncio.create_task(self.run_connections()),
            asyncio.create_task(self.stop_event.wait()),
        )
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def request_stop(self) -> None:
        self.stop_event.set()
        proc = self.proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    async def cleanup(self) -> None:
        await self.stop_child()
        if self.client_read_transport is not None:
            self.client_read_transport.close()
        if self.client_out is not None:
            self.client_out.close()


async def amain(argv: list[str]) -> None:
    proxy = Proxy(argv)
    await proxy.open_client_stdio()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, proxy.request_stop)
        except NotImplementedError:
            pass
    try:
        await proxy.run()
    finally:
        await proxy.cleanup()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        asyncio.run(amain(sys.argv[1:]))
    except (KeyboardInterrupt, SystemExit):
        pass
    except ClientOutputClosed:
        _log("client stdout closed; stopping")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
