#!/usr/bin/env python3
"""Supervise one stdio MCP worker; release idle workers without replaying calls."""
import asyncio
import contextlib
import json
import os
import signal
import sys
import time

IDLE_SECONDS = 1800.0
LIMIT = 32 * 1024 * 1024


async def run(command, idle_seconds=IDLE_SECONDS):
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=LIMIT)
    transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    out_transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(out_transport, protocol, None, loop)
    child = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=None, limit=LIMIT, start_new_session=True,
    )
    pending = set()
    last_activity = time.monotonic()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(sig, stop.set)

    def observe(raw, inbound):
        nonlocal last_activity
        try:
            message = json.loads(raw)
        except (ValueError, UnicodeError):
            return
        if not isinstance(message, dict):
            return
        method = message.get('method')
        # Ping and progress keepalives must not hold an unused worker forever.
        if inbound and method and method != 'ping':
            last_activity = time.monotonic()
            if 'id' in message:
                pending.add(json.dumps(message['id'], sort_keys=True))
        elif not inbound and 'id' in message and method is None:
            key = json.dumps(message['id'], sort_keys=True)
            if key in pending:
                pending.discard(key)
                last_activity = time.monotonic()

    async def upstream():
        while raw := await reader.readline():
            observe(raw, True)
            child.stdin.write(raw)
            await child.stdin.drain()

    async def downstream():
        while raw := await child.stdout.readline():
            observe(raw, False)
            writer.write(raw)
            await writer.drain()

    async def idle():
        while True:
            await asyncio.sleep(min(idle_seconds / 10, 1.0))
            if not pending and time.monotonic() - last_activity >= idle_seconds:
                print('mcp-idle-guard: idle timeout; releasing worker', file=sys.stderr, flush=True)
                return

    tasks = [asyncio.create_task(f()) for f in (upstream, downstream, idle, stop.wait)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # The group may still contain descendants after its leader exits.
        # Always signal the group, then kill any survivors after the grace.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(child.wait(), 3)
        except asyncio.TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        await child.wait()
        transport.close()
        writer.close()
    return 0


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('usage: mcp-idle-guard.py COMMAND [ARGS...]')
    raise SystemExit(asyncio.run(run(sys.argv[1:])))
