from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import textwrap
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "skills/claude-research/scripts/mcp_ssh_reconnect.py"


def message(method: str, request_id: object | None, params: dict[str, Any] | None = None) -> bytes:
    value: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        value["id"] = request_id
    if params is not None:
        value["params"] = params
    return (json.dumps(value) + "\n").encode()


async def response(reader: asyncio.StreamReader, expected_id: object) -> dict[str, Any]:
    while True:
        raw = await asyncio.wait_for(reader.readline(), timeout=5)
        assert raw
        value = json.loads(raw)
        if value.get("id") == 1 and expected_id != 1:
            raise AssertionError(f"replayed initialize escaped to the client: {value}")
        if value.get("id") == expected_id and "method" not in value:
            return value


def logged_methods(log_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log_path.read_text("utf-8").splitlines()]


@pytest.fixture()
def mock_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    server = tmp_path / "mock_claude_ssh.py"
    request_log = tmp_path / "requests.jsonl"
    monkeypatch.setenv("CLAUDE_MOCK_REQUEST_LOG", str(request_log))
    server.write_text(
        textwrap.dedent(
            """
            import json, os, sys, threading, time
            log_path = os.environ["CLAUDE_MOCK_REQUEST_LOG"]
            generation = sum(
                1 for line in open(log_path, encoding="utf-8")
                if json.loads(line).get("method") == "initialize"
            ) if os.path.exists(log_path) else 0
            initialized = False
            with open(log_path, "a", encoding="utf-8", buffering=1) as log:
                for line in sys.stdin:
                    request = json.loads(line)
                    log.write(line)
                    method = request.get("method")
                    request_id = request.get("id")
                    if method == "initialize":
                        generation += 1
                        initialized = True
                        result = {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "serverInfo": {"name": "mock", "version": "1"}}
                    elif method == "notifications/initialized":
                        continue
                    elif not initialized:
                        print(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                          "error": {"code": -32600, "message": "not initialized"}}), flush=True)
                        continue
                    elif method == "tools/call":
                        name = request["params"]["name"]
                        if name == "arm_server_idle":
                            threading.Timer(0.03, os._exit, [0]).start()
                        if name == "die":
                            os._exit(70)
                        if name == "sleep":
                            time.sleep(0.18)
                        result = {"content": [{"type": "text", "text": f"gen={generation}"}]}
                    else:
                        continue
                    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    return server, request_log


async def start_proxy(
    server: Path, *, idle_timeout: str = "0.06", reconnect_delays: str = "0.05"
) -> asyncio.subprocess.Process:
    env = dict(os.environ)
    env.update(
        {
            "CLAUDE_MCP_RECONNECT_DELAYS": reconnect_delays,
            "CLAUDE_MCP_RECONNECT_IDLE_TIMEOUT": idle_timeout,
            "CLAUDE_MCP_RECONNECT_QUEUE_TIMEOUT": "2",
        }
    )
    return await asyncio.create_subprocess_exec(
        sys.executable, str(PROXY), sys.executable, str(server),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env,
    )


async def handshake(proxy: asyncio.subprocess.Process) -> None:
    assert proxy.stdin is not None
    proxy.stdin.write(message("initialize", 1, {"protocolVersion": "2024-11-05", "capabilities": {}}))
    await proxy.stdin.drain()
    assert "result" in await response(proxy.stdout, 1)
    proxy.stdin.write(message("notifications/initialized", None))
    await proxy.stdin.drain()


async def collect_stderr(proxy: asyncio.subprocess.Process, lines: list[str]) -> None:
    assert proxy.stderr is not None
    while True:
        raw = await proxy.stderr.readline()
        if not raw:
            return
        lines.append(raw.decode("utf-8", "replace"))


def child_pids(lines: list[str]) -> list[int]:
    return [int(line.split("pid", 1)[1].split()[0]) for line in lines if "pid" in line]


async def wait_for_process_exit(pid: int) -> None:
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"child {pid} did not exit")


def test_idle_closes_and_next_request_replays_private_handshake(
    mock_server: tuple[Path, Path],
) -> None:
    asyncio.run(_idle_lifecycle_case(mock_server))


async def _idle_lifecycle_case(
    mock_server: tuple[Path, Path],
) -> None:
    server, request_log = mock_server
    proxy = await start_proxy(server, idle_timeout="0.6")
    stderr_lines: list[str] = []
    collector = asyncio.create_task(collect_stderr(proxy, stderr_lines))
    try:
        await handshake(proxy)
        assert proxy.stdin is not None
        proxy.stdin.write(message("tools/call", 2, {"name": "echo", "arguments": {}}))
        await proxy.stdin.drain()
        assert "result" in await response(proxy.stdout, 2)
        await wait_for_process_exit(child_pids(stderr_lines)[0])
        assert any("idle timeout" in line for line in stderr_lines)

        await asyncio.sleep(0.12)
        assert len(child_pids(stderr_lines)) == 1
        assert len(logged_methods(request_log)) == 3

        proxy.stdin.write(message("tools/call", 3, {"name": "echo", "arguments": {}}))
        await proxy.stdin.drain()
        assert (await response(proxy.stdout, 3))["result"]["content"][0]["text"] == "gen=2"
        requests = logged_methods(request_log)
        assert [item["id"] for item in requests if item.get("method") == "initialize"] == [1, 1]
        assert [item.get("id") for item in requests if item.get("method") == "notifications/initialized"] == [None, None]
        assert [item["id"] for item in requests if item.get("method") == "tools/call"] == [2, 3]
    finally:
        assert proxy.stdin is not None
        proxy.stdin.close()
        await asyncio.wait_for(proxy.wait(), timeout=5)
        await collector


def test_active_request_is_not_idle_closed(mock_server: tuple[Path, Path]) -> None:
    asyncio.run(_active_request_case(mock_server))


async def _active_request_case(mock_server: tuple[Path, Path]) -> None:
    server, _ = mock_server
    proxy = await start_proxy(server)
    stderr_lines: list[str] = []
    collector = asyncio.create_task(collect_stderr(proxy, stderr_lines))
    try:
        await handshake(proxy)
        assert proxy.stdin is not None
        proxy.stdin.write(message("tools/call", 2, {"name": "sleep", "arguments": {}}))
        await proxy.stdin.drain()
        await asyncio.sleep(0.12)
        assert not any("idle timeout" in line for line in stderr_lines)
        assert "result" in await response(proxy.stdout, 2)
    finally:
        assert proxy.stdin is not None
        proxy.stdin.close()
        await asyncio.wait_for(proxy.wait(), timeout=5)
        await collector


def test_server_idle_close_waits_for_next_request(
    mock_server: tuple[Path, Path],
) -> None:
    asyncio.run(_server_idle_close_case(mock_server))


async def _server_idle_close_case(mock_server: tuple[Path, Path]) -> None:
    server, request_log = mock_server
    proxy = await start_proxy(server, idle_timeout="5")
    stderr_lines: list[str] = []
    collector = asyncio.create_task(collect_stderr(proxy, stderr_lines))
    try:
        await handshake(proxy)
        assert proxy.stdin is not None
        proxy.stdin.write(message("tools/call", 2, {"name": "arm_server_idle", "arguments": {}}))
        await proxy.stdin.drain()
        assert "result" in await response(proxy.stdout, 2)
        await wait_for_process_exit(child_pids(stderr_lines)[0])
        await asyncio.sleep(0.12)
        assert len(child_pids(stderr_lines)) == 1

        proxy.stdin.write(message("tools/call", 3, {"name": "echo", "arguments": {}}))
        await proxy.stdin.drain()
        assert (await response(proxy.stdout, 3))["result"]["content"][0]["text"] == "gen=2"
        assert [item["id"] for item in logged_methods(request_log) if item.get("method") == "initialize"] == [1, 1]
    finally:
        assert proxy.stdin is not None
        proxy.stdin.close()
        await asyncio.wait_for(proxy.wait(), timeout=5)
        await collector


def test_protocol_pings_do_not_affect_idle_lifecycle(
    mock_server: tuple[Path, Path],
) -> None:
    asyncio.run(_protocol_ping_case(mock_server))


async def _protocol_ping_case(mock_server: tuple[Path, Path]) -> None:
    server, request_log = mock_server
    proxy = await start_proxy(server, idle_timeout="0.12")
    stderr_lines: list[str] = []
    collector = asyncio.create_task(collect_stderr(proxy, stderr_lines))
    try:
        await handshake(proxy)
        assert proxy.stdin is not None
        proxy.stdin.write(message("tools/call", 2, {"name": "echo", "arguments": {}}))
        await proxy.stdin.drain()
        assert "result" in await response(proxy.stdout, 2)

        for request_id in (10, 11, 12, 13):
            proxy.stdin.write(message("ping", request_id))
            await proxy.stdin.drain()
            assert (await response(proxy.stdout, request_id))["result"] == {}
            await asyncio.sleep(0.03)
        await wait_for_process_exit(child_pids(stderr_lines)[0])
        assert any("idle timeout" in line for line in stderr_lines)

        proxy.stdin.write(message("ping", 14))
        await proxy.stdin.drain()
        assert (await response(proxy.stdout, 14))["result"] == {}
        await asyncio.sleep(0.12)
        assert len(child_pids(stderr_lines)) == 1
        assert len(logged_methods(request_log)) == 3

        proxy.stdin.write(message("tools/call", 3, {"name": "echo", "arguments": {}}))
        await proxy.stdin.drain()
        assert (await response(proxy.stdout, 3))["result"]["content"][0]["text"] == "gen=2"
        requests = logged_methods(request_log)
        assert [item["id"] for item in requests if item.get("method") == "initialize"] == [1, 1]
        assert [item["id"] for item in requests if item.get("method") == "tools/call"] == [2, 3]
    finally:
        assert proxy.stdin is not None
        proxy.stdin.close()
        await asyncio.wait_for(proxy.wait(), timeout=5)
        await collector


def test_unexpected_disconnect_waits_for_demand_and_never_replays_call(
    mock_server: tuple[Path, Path],
) -> None:
    asyncio.run(_disconnect_demand_case(mock_server))


async def _disconnect_demand_case(
    mock_server: tuple[Path, Path],
) -> None:
    server, request_log = mock_server
    proxy = await start_proxy(server)
    stderr_lines: list[str] = []
    collector = asyncio.create_task(collect_stderr(proxy, stderr_lines))
    try:
        await handshake(proxy)
        assert proxy.stdin is not None
        proxy.stdin.write(message("tools/call", 2, {"name": "die", "arguments": {}}))
        await proxy.stdin.drain()
        assert "not replayed" in (await response(proxy.stdout, 2))["error"]["message"]
        await asyncio.sleep(0.12)
        assert len(child_pids(stderr_lines)) == 1

        proxy.stdin.write(message("tools/call", 3, {"name": "echo", "arguments": {}}))
        await proxy.stdin.drain()
        assert (await response(proxy.stdout, 3))["result"]["content"][0]["text"] == "gen=2"
        requests = logged_methods(request_log)
        died = [item for item in requests if item.get("method") == "tools/call" and item.get("id") == 2]
        assert len(died) == 1
    finally:
        assert proxy.stdin is not None
        proxy.stdin.close()
        await asyncio.wait_for(proxy.wait(), timeout=5)
        await collector
