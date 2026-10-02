#!/usr/bin/env python3
"""Drop-tolerant stdio proxy for the gemini_research SSH transport.

Codex launches stdio MCP servers as child processes and never re-establishes
them: when the SSH transport dies after a network flap, every later tool call
in that session fails with "Transport closed" until the session is restarted.

This proxy keeps the client-facing stdio transport alive while transparently
respawning the SSH child:

* requests in flight when the child dies get an explicit unknown-result error;
  the proxy never replays a tools/call that the server may already have run;
* the child is respawned with capped backoff (GEMINI_MCP_RECONNECT_DELAYS,
  default "1,2,5,10,30" seconds);
* the captured initialize request and notifications/initialized notification
  are replayed to the fresh server (the replayed response is swallowed);
* requests arriving while offline are queued and flushed after the handshake,
  or failed after GEMINI_MCP_RECONNECT_QUEUE_TIMEOUT seconds (default 30).

Usage: mcp_ssh_reconnect.py COMMAND [ARGS...]
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal
import sys
import threading
import time

DEFAULT_DELAYS = "1,2,5,10,30"
DEFAULT_QUEUE_TIMEOUT = 30.0
STABLE_AFTER = 60.0
HANDSHAKE_TIMEOUT = 45.0
READ_LIMIT = 32 * 1024 * 1024


class ClientOutputClosed(RuntimeError):
    """The client closed the proxy's response stream."""


def _delays() -> tuple[float, ...]:
    raw = os.environ.get("GEMINI_MCP_RECONNECT_DELAYS", DEFAULT_DELAYS)
    out = []
    for part in raw.split(","):
        try:
            value = float(part)
        except ValueError:
            continue
        if value >= 0:
            out.append(value)
    return tuple(out or [1.0])


def _queue_timeout() -> float:
    try:
        return float(
            os.environ.get("GEMINI_MCP_RECONNECT_QUEUE_TIMEOUT", DEFAULT_QUEUE_TIMEOUT)
        )
    except ValueError:
        return DEFAULT_QUEUE_TIMEOUT


def _log(message: str) -> None:
    print(f"gemini-mcp-reconnect: {message}", file=sys.stderr, flush=True)


def _debug(message: str) -> None:
    if os.environ.get("GEMINI_MCP_RECONNECT_DEBUG"):
        _log(message)


def _parse(raw: bytes) -> dict | None:
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _id_key(value: object) -> str:
    return json.dumps(value, sort_keys=True)


class Proxy:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.delays = _delays()
        self.queue_timeout = _queue_timeout()
        self.backoff = -1
        self.stdin_open = True
        self.ready = False
        self.stdin_task: asyncio.Task[None] | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.pending: dict[str, object] = {}
        self.queue: list[tuple[float, bytes]] = []
        self.init_raw: bytes | None = None
        self.init_id: object = None
        self.init_complete = False
        self.initialized_raw: bytes | None = None
        self.client_in: asyncio.StreamReader | None = None
        self.client_out: asyncio.StreamWriter | None = None

    # ---------------- client-facing output ----------------

    def write_client(self, raw: bytes) -> None:
        try:
            writer = self.client_out
            if writer is None:
                sys.stdout.buffer.write(raw)
                sys.stdout.buffer.flush()
            else:
                writer.write(raw)
        except (BrokenPipeError, ValueError, OSError):
            _log("client stdout closed; exiting")
            raise ClientOutputClosed()

    async def open_client_stdio(self) -> None:
        loop = asyncio.get_running_loop()
        self.client_in = asyncio.StreamReader(limit=READ_LIMIT)
        try:
            reader_protocol = asyncio.StreamReaderProtocol(self.client_in)
            await loop.connect_read_pipe(lambda: reader_protocol, sys.stdin.buffer)
        except ValueError:
            # Regular-file stdin (test harnesses); Codex always uses a pipe.
            def feed() -> None:
                try:
                    for line in sys.stdin.buffer:
                        loop.call_soon_threadsafe(self.client_in.feed_data, line)
                finally:
                    loop.call_soon_threadsafe(self.client_in.feed_eof)

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

    def write_error(self, request_id: object, message: str) -> None:
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32000, "message": message},
        }
        self.write_client((json.dumps(response) + "\n").encode("utf-8"))

    # ---------------- child plumbing ----------------

    async def send_to_child(self, raw: bytes, enqueued_at: float | None = None) -> None:
        proc = self.proc
        if not self.ready or proc is None or proc.returncode is not None or proc.stdin is None:
            self.queue.append((enqueued_at if enqueued_at is not None else time.monotonic(), raw))
            return
        obj = _parse(raw)
        if obj is not None and "method" in obj and "id" in obj:
            self.pending[_id_key(obj["id"])] = obj["id"]
        try:
            _debug(f"write child: {raw[:100]!r}")
            proc.stdin.write(raw)
            await proc.stdin.drain()
            _debug(f"drained child: {raw[:60]!r}")
        except (BrokenPipeError, ConnectionResetError, OSError):
            if obj is not None and "method" in obj and "id" in obj:
                self.pending.pop(_id_key(obj["id"]), None)
                self.write_error(
                    obj["id"],
                    "gemini_research transport failed while sending the request; "
                    "result unknown and tools/call was not replayed",
                )
                return
            self.queue.append(
                (enqueued_at if enqueued_at is not None else time.monotonic(), raw)
            )

    async def flush_queue(self) -> None:
        queued = self.queue
        self.queue = []
        for enqueued_at, raw in queued:
            if time.monotonic() - enqueued_at > self.queue_timeout:
                obj = _parse(raw)
                if obj is not None and "method" in obj and "id" in obj:
                    self.write_error(
                        obj["id"],
                        "gemini_research transport remained offline; request was "
                        "not sent and was not replayed",
                    )
                continue
            await self.send_to_child(raw, enqueued_at)

    async def replay_handshake(self) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        swallow_key = _id_key(self.init_id)
        assert self.init_raw is not None
        self.proc.stdin.write(self.init_raw)
        await self.proc.stdin.drain()
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT
        while swallow_key is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("initialize replay timed out")
            raw = await asyncio.wait_for(self.proc.stdout.readline(), timeout=remaining)
            if not raw:
                raise ConnectionError("child closed during initialize replay")
            obj = _parse(raw)
            if (
                obj is not None
                and "method" not in obj
                and "id" in obj
                and _id_key(obj["id"]) == swallow_key
            ):
                if "error" in obj or not isinstance(obj.get("result"), dict):
                    raise ConnectionError("initialize replay rejected")
                swallow_key = None
            else:
                self.write_client(raw)
                await self.drain_client()
        if self.initialized_raw is not None:
            self.proc.stdin.write(self.initialized_raw)
            await self.proc.stdin.drain()
        _log("initialize handshake replayed")

    def fail_expired_queued(self) -> None:
        if not self.queue:
            return
        now = time.monotonic()
        keep: list[tuple[float, bytes]] = []
        for enqueued_at, raw in self.queue:
            if now - enqueued_at <= self.queue_timeout:
                keep.append((enqueued_at, raw))
                continue
            obj = _parse(raw)
            if obj is not None and "method" in obj and "id" in obj:
                self.write_error(
                    obj["id"],
                    "gemini_research transport remained offline; request was not "
                    "sent and was not replayed",
                )
        self.queue = keep

    async def handle_client_line(self, raw: bytes) -> None:
        _debug(f"client line: {raw[:100]!r}")
        obj = _parse(raw)
        if obj is not None and "method" in obj:
            if obj.get("method") == "initialize" and "id" in obj:
                self.init_raw = raw
                self.init_id = obj["id"]
            elif obj.get("method") == "notifications/initialized" and "id" not in obj:
                self.initialized_raw = raw
                if not self.ready:
                    return
        await self.send_to_child(raw)

    async def client_stdin_loop(self) -> None:
        reader = self.client_in
        assert reader is not None
        while True:
            raw = await reader.readline()
            if not raw:
                self.stdin_open = False
                if self.proc is not None and self.proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        self.proc.kill()
                return
            await self.handle_client_line(raw)

    async def supervise_queued_requests(self) -> None:
        interval = max(self.queue_timeout / 10.0, 0.05)
        while True:
            await asyncio.sleep(interval)
            self.fail_expired_queued()

    async def child_stdout_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                return
            _debug(f"child line: {raw[:100]!r}")
            obj = _parse(raw)
            if obj is not None and "method" not in obj and "id" in obj:
                self.pending.pop(_id_key(obj["id"]), None)
                if obj["id"] == self.init_id and "result" in obj and "error" not in obj:
                    self.init_complete = True
            self.write_client(raw)
            await self.drain_client()

    async def pump(self) -> None:
        stdout_task = asyncio.create_task(self.child_stdout_loop())
        try:
            await stdout_task
        finally:
            stdout_task.cancel()
            await asyncio.gather(stdout_task, return_exceptions=True)

    async def connect_once(self) -> None:
        self.ready = False
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
            limit=READ_LIMIT,
        )
        _log(f"child pid {self.proc.pid} started")
        if self.init_complete and self.init_raw is not None:
            await self.replay_handshake()
        self.ready = True
        await self.flush_queue()
        await self.pump()

    async def drain_client(self) -> None:
        if self.client_out is not None:
            try:
                await self.client_out.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                raise ClientOutputClosed()

    async def stop_child(self) -> None:
        self.ready = False
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        if proc.returncode is not None:
            await proc.wait()
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    async def cleanup(self) -> None:
        task = self.stdin_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.stdin_task = None
        await self.stop_child()

    # ---------------- failure bookkeeping ----------------

    def fail_pending(self, reason: str) -> None:
        for request_id in self.pending.values():
            self.write_error(
                request_id,
                "gemini_research transport failed before the response was received: "
                f"{reason}; result unknown and tools/call was not replayed",
            )
        self.pending = {}

    def fail_stale_queued(self) -> None:
        self.fail_expired_queued()

    # ---------------- main loop ----------------

    async def run(self) -> None:
        self.stdin_task = asyncio.create_task(self.client_stdin_loop())
        queue_task = asyncio.create_task(self.supervise_queued_requests())
        connection_task = asyncio.create_task(self.run_connections())
        tasks = (self.stdin_task, queue_task, connection_task)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled():
                    await task
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run_connections(self) -> None:
        while self.stdin_open:
            started = time.monotonic()
            try:
                await self.connect_once()
            except ClientOutputClosed:
                _log("client stdout closed; stopping")
                raise
            except Exception as exc:
                _log(f"connection cycle failed: {exc!r}")
            finally:
                await self.stop_child()
            if time.monotonic() - started >= STABLE_AFTER:
                self.backoff = -1
            else:
                self.backoff = min(self.backoff + 1, len(self.delays) - 1)
            self.fail_pending("SSH connection lost")
            self.fail_stale_queued()
            if not self.stdin_open:
                break
            delay = self.delays[max(self.backoff, 0)]
            _log(f"reconnecting in {delay:g}s")
            await asyncio.sleep(delay)

    def request_stop(self) -> None:
        self.stdin_open = False
        if self.stdin_task is not None:
            self.stdin_task.cancel()
        proc = self.proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()


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
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
