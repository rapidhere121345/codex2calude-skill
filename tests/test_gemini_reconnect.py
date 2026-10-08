from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import textwrap
from typing import Any

import pytest


ROOT = Path(__file__).parents[1]
PROXY = ROOT / "skills/gemini-research/scripts/mcp_ssh_reconnect.py"


def message(method: str, request_id: object | None, params: dict[str, Any] | None = None) -> bytes:
    value: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        value["id"] = request_id
    if params is not None:
        value["params"] = params
    return (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")


async def response(reader: asyncio.StreamReader, expected_id: object) -> dict[str, Any]:
    while True:
        raw = await asyncio.wait_for(reader.readline(), timeout=15)
        assert raw, "proxy closed stdout before responding"
        value = json.loads(raw)
        if value.get("id") == expected_id and "method" not in value:
            return value
        if value.get("id") == 1 and expected_id != 1:
            raise AssertionError(f"duplicate initialize response reached the client: {value}")


@pytest.fixture()
def mock_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    server = tmp_path / "mock_gemini_ssh.py"
    request_log = tmp_path / "requests.jsonl"
    server.write_text(
        textwrap.dedent(
            """
            import json, os, sys, threading, time
            log_path = os.environ["GEMINI_MOCK_REQUEST_LOG"]
            gate_path = os.environ.get("GEMINI_MOCK_GATE")
            generation = sum(
                1 for line in open(log_path, encoding="utf-8")
                if json.loads(line).get("method") == "initialize"
            ) if os.path.exists(log_path) else 0
            initialized = False
            with open(log_path, "a", encoding="utf-8", buffering=1) as log:
                for line in sys.stdin:
                    request = json.loads(line)
                    method = request.get("method")
                    request_id = request.get("id")
                    gate_down = gate_path and os.path.exists(os.path.join(gate_path, "down"))
                    if generation and gate_down and method == "notifications/cancelled":
                        os._exit(75)
                    log.write(line)
                    if method == "initialize":
                        generation += 1
                        if gate_path and os.path.exists(os.path.join(gate_path, "reject")):
                            print(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                              "error": {"code": -32600, "message": "initialize rejected"}}), flush=True)
                            continue
                        result = {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "serverInfo": {"name": "mock-gemini", "version": "1"},
                        }
                    elif method == "notifications/initialized":
                        initialized = True
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
                        if name == "output4m":
                            result = {"content": [{"type": "text", "text": "x" * 4194304}]}
                            print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
                            continue
                        arguments = request["params"].get("arguments", {})
                        result = {
                            "isError": False,
                            "structuredContent": {
                                "generation": generation,
                                "payload_bytes": len(arguments.get("payload", "").encode("utf-8")),
                            },
                        }
                    else:
                        continue
                    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result},
                                     separators=(",", ":")), flush=True)
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GEMINI_MOCK_REQUEST_LOG", str(request_log))
    return server, request_log


async def start_proxy(
    mock_server: Path,
    *,
    queue_timeout: str = "2",
    reconnect_delays: str = "0.05,0.05",
    idle_timeout: str = "1800",
) -> asyncio.subprocess.Process:
    env = dict(os.environ)
    env.update(
        {
            "GEMINI_MCP_RECONNECT_DELAYS": reconnect_delays,
            "GEMINI_MCP_RECONNECT_QUEUE_TIMEOUT": queue_timeout,
            "GEMINI_MCP_RECONNECT_IDLE_TIMEOUT": idle_timeout,
            "GEMINI_MOCK_GATE": str(mock_server.parent / "reconnect-gate"),
        }
    )
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(PROXY),
        sys.executable,
        str(mock_server),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=32 * 1024 * 1024,
        env=env,
    )


async def handshake(proxy: asyncio.subprocess.Process) -> None:
    assert proxy.stdin is not None
    proxy.stdin.write(
        message(
            "initialize",
            1,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
    )
    await proxy.stdin.drain()
    value = await response(proxy.stdout, 1)
    assert "result" in value
    proxy.stdin.write(message("notifications/initialized", None, {}))
    await proxy.stdin.drain()


def logged_methods(log_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log_path.read_text("utf-8").splitlines()]


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


def test_reconnect_replays_handshake_but_not_in_flight_call(
    mock_server: tuple[Path, Path],
) -> None:
    asyncio.run(_reconnect_case(mock_server))


def test_idle_closes_and_next_request_replays_private_handshake(
    mock_server: tuple[Path, Path],
) -> None:
    asyncio.run(_idle_lifecycle_case(mock_server))


async def _idle_lifecycle_case(mock_server: tuple[Path, Path]) -> None:
    server, request_log = mock_server
    proxy = await start_proxy(server, idle_timeout="0.6", reconnect_delays="0.05")
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
        assert (await response(proxy.stdout, 3))["result"]["structuredContent"]["generation"] == 2
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
    proxy = await start_proxy(server, idle_timeout="0.06")
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
        recovered = await response(proxy.stdout, 3)
        assert recovered["result"]["structuredContent"]["generation"] == 2
        requests = logged_methods(request_log)
        assert [item["id"] for item in requests if item.get("method") == "initialize"] == [1, 1]
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
        recovered = await response(proxy.stdout, 3)
        assert recovered["result"]["structuredContent"]["generation"] == 2
        requests = logged_methods(request_log)
        assert [item["id"] for item in requests if item.get("method") == "initialize"] == [1, 1]
        assert [item["id"] for item in requests if item.get("method") == "tools/call"] == [2, 3]
    finally:
        assert proxy.stdin is not None
        proxy.stdin.close()
        await asyncio.wait_for(proxy.wait(), timeout=5)
        await collector


def test_disconnect_waits_for_demand_and_never_replays_call(
    mock_server: tuple[Path, Path],
) -> None:
    asyncio.run(_disconnect_demand_case(mock_server))


async def _disconnect_demand_case(mock_server: tuple[Path, Path]) -> None:
    server, request_log = mock_server
    proxy = await start_proxy(server, idle_timeout="0.06", reconnect_delays="0.05")
    stderr_lines: list[str] = []
    collector = asyncio.create_task(collect_stderr(proxy, stderr_lines))
    try:
        await handshake(proxy)
        assert proxy.stdin is not None
        proxy.stdin.write(message("tools/call", 2, {"name": "die", "arguments": {}}))
        await proxy.stdin.drain()
        failed = await response(proxy.stdout, 2)
        assert "not replayed" in failed["error"]["message"]
        await asyncio.sleep(0.12)
        assert len(child_pids(stderr_lines)) == 1

        proxy.stdin.write(message("tools/call", 3, {"name": "echo", "arguments": {}}))
        await proxy.stdin.drain()
        recovered = await response(proxy.stdout, 3)
        assert recovered["result"]["structuredContent"]["generation"] == 2
        requests = logged_methods(request_log)
        died = [item for item in requests if item.get("method") == "tools/call" and item.get("id") == 2]
        assert len(died) == 1
    finally:
        assert proxy.stdin is not None
        proxy.stdin.close()
        await asyncio.wait_for(proxy.wait(), timeout=5)
        await collector


async def _reconnect_case(mock_server: tuple[Path, Path]) -> None:
    server, log_path = mock_server
    proxy = await start_proxy(server)
    await handshake(proxy)
    assert proxy.stdin is not None and proxy.stdout is not None

    proxy.stdin.write(message("tools/call", 2, {"name": "echo", "arguments": {}}))
    await proxy.stdin.drain()
    first = await response(proxy.stdout, 2)
    assert first["result"]["structuredContent"]["generation"] == 1

    proxy.stdin.write(message("tools/call", 3, {"name": "die", "arguments": {}}))
    await proxy.stdin.drain()
    failed = await response(proxy.stdout, 3)
    assert "result unknown" in failed["error"]["message"]
    assert "was not replayed" in failed["error"]["message"]

    proxy.stdin.write(message("tools/call", 4, {"name": "echo", "arguments": {}}))
    await proxy.stdin.drain()
    recovered = await response(proxy.stdout, 4)
    assert recovered["result"]["structuredContent"]["generation"] == 2

    requests = logged_methods(log_path)
    assert [r["id"] for r in requests if r.get("method") == "initialize"] == [1, 1]
    assert [r["id"] for r in requests if r.get("method") == "tools/call" and r["id"] == 3] == [3]

    proxy.stdin.close()
    await asyncio.wait_for(proxy.wait(), timeout=10)


def test_transport_accepts_four_mib_message(
    mock_server: tuple[Path, Path],
) -> None:
    asyncio.run(_four_mib_case(mock_server))


async def _four_mib_case(mock_server: tuple[Path, Path]) -> None:
    server, _ = mock_server
    proxy = await start_proxy(server)
    await handshake(proxy)
    assert proxy.stdin is not None and proxy.stdout is not None

    payload = "x" * (4 * 1024 * 1024)
    proxy.stdin.write(message("tools/call", 2, {"name": "echo", "arguments": {"payload": payload}}))
    await proxy.stdin.drain()
    value = await response(proxy.stdout, 2)
    assert value["result"]["structuredContent"]["payload_bytes"] == 4 * 1024 * 1024

    proxy.stdin.close()
    await asyncio.wait_for(proxy.wait(), timeout=10)


def test_client_eof_stops_child(mock_server: tuple[Path, Path]) -> None:
    asyncio.run(_client_eof_case(mock_server))


async def _client_eof_case(mock_server: tuple[Path, Path]) -> None:
    server, _ = mock_server
    proxy = await start_proxy(server)
    await handshake(proxy)
    assert proxy.stdin is not None and proxy.stderr is not None

    proxy.stdin.close()
    assert await asyncio.wait_for(proxy.wait(), timeout=10) == 0
    stderr = await proxy.stderr.read()
    matches = [line for line in stderr.decode("utf-8", "replace").splitlines() if "pid" in line]
    assert matches, stderr.decode("utf-8", "replace")
    child_pid = int(matches[-1].split("pid", 1)[1].split()[0])
    for _ in range(100):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"mock SSH child {child_pid} survived proxy exit")


def test_offline_request_times_out_and_is_not_sent_after_recovery(
    mock_server: tuple[Path, Path],
) -> None:
    asyncio.run(_offline_timeout_case(mock_server))


async def _offline_timeout_case(mock_server: tuple[Path, Path]) -> None:
    server, log_path = mock_server
    down = server.parent / "reconnect-gate" / "down"
    down.parent.mkdir()
    down.write_text("", encoding="utf-8")
    proxy = await start_proxy(server, queue_timeout="0.3", reconnect_delays="1")
    await handshake(proxy)
    assert proxy.stdin is not None
    proxy.stdin.write(message("notifications/cancelled", None, {}))
    await proxy.stdin.drain()
    await asyncio.sleep(0.1)

    proxy.stdin.write(message("tools/call", 2, {"name": "echo", "arguments": {}}))
    await proxy.stdin.drain()
    expired = await response(proxy.stdout, 2)
    assert "remained offline" in expired["error"]["message"]
    assert "was not sent" in expired["error"]["message"]

    down.unlink()
    # Cross the full reconnect backoff: expired demand must not spawn SSH.
    await asyncio.sleep(1.2)
    requests = logged_methods(log_path)
    assert len([r for r in requests if r.get("method") == "initialize"]) == 1
    assert [r["id"] for r in requests if r.get("method") == "tools/call"] == []

    proxy.stdin.close()
    await asyncio.wait_for(proxy.wait(), timeout=10)


def test_offline_client_eof_stops_child_and_proxy(
    mock_server: tuple[Path, Path],
) -> None:
    asyncio.run(_offline_eof_case(mock_server))


async def _offline_eof_case(mock_server: tuple[Path, Path]) -> None:
    server, _ = mock_server
    down = server.parent / "reconnect-gate" / "down"
    down.parent.mkdir()
    down.write_text("", encoding="utf-8")
    proxy = await start_proxy(server, reconnect_delays="30")
    await handshake(proxy)
    child_pids = []

    async def collect_children() -> None:
        while True:
            raw = await proxy.stderr.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace")
            if "pid" in line:
                child_pids.append(int(line.split("pid", 1)[1].split()[0]))

    collector = asyncio.create_task(collect_children())
    proxy.stdin.write(message("tools/call", 2, {"name": "die", "arguments": {}}))
    await proxy.stdin.drain()
    assert "error" in await response(proxy.stdout, 2)
    await asyncio.sleep(0.05)
    assert child_pids
    proxy.stdin.close()
    assert await asyncio.wait_for(proxy.wait(), timeout=2) == 0
    await collector
    for pid in child_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_sigterm_stops_child_and_proxy(
    mock_server: tuple[Path, Path],
) -> None:
    asyncio.run(_signal_case(mock_server))


async def _signal_case(mock_server: tuple[Path, Path]) -> None:
    server, _ = mock_server
    proxy = await start_proxy(server)
    await handshake(proxy)
    assert proxy.stdin is not None and proxy.stderr is not None
    raw = await asyncio.wait_for(proxy.stderr.readline(), timeout=2)
    child_pid = int(raw.decode().split("pid", 1)[1].split()[0])
    proxy.send_signal(signal.SIGTERM)
    assert await asyncio.wait_for(proxy.wait(), timeout=10) == 0
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_four_mib_child_result_passes_through_proxy(
    mock_server: tuple[Path, Path],
) -> None:
    asyncio.run(_four_mib_output_case(mock_server))


def test_rejected_reconnect_handshake_does_not_send_queued_calls(
    mock_server: tuple[Path, Path],
) -> None:
    asyncio.run(_rejected_handshake_case(mock_server))


async def _rejected_handshake_case(mock_server: tuple[Path, Path]) -> None:
    server, log_path = mock_server
    proxy = await start_proxy(server, queue_timeout="0.25")
    try:
        await handshake(proxy)
        reject = server.parent / "reconnect-gate" / "reject"
        reject.parent.mkdir()
        reject.touch()
        proxy.stdin.write(message("tools/call", 2, {"name": "die", "arguments": {}}))
        await proxy.stdin.drain()
        assert "error" in await response(proxy.stdout, 2)
        proxy.stdin.write(message("tools/call", 3, {"name": "echo", "arguments": {}}))
        await proxy.stdin.drain()
        expired = await response(proxy.stdout, 3)
        assert "was not sent" in expired["error"]["message"]
        assert all(r.get("id") != 3 for r in logged_methods(log_path))
        reject.unlink()
        proxy.stdin.write(message("tools/call", 4, {"name": "echo", "arguments": {}}))
        await proxy.stdin.drain()
        assert "result" in await response(proxy.stdout, 4)
    finally:
        proxy.stdin.close()
        await asyncio.wait_for(proxy.wait(), timeout=2)


async def _four_mib_output_case(mock_server: tuple[Path, Path]) -> None:
    server, _ = mock_server
    proxy = await start_proxy(server)
    await handshake(proxy)
    proxy.stdin.write(message("tools/call", 2, {"name": "output4m", "arguments": {}}))
    await proxy.stdin.drain()
    raw = await asyncio.wait_for(proxy.stdout.readline(), timeout=20)
    assert len(raw) > 4 * 1024 * 1024
    value = json.loads(raw)
    assert len(value["result"]["content"][0]["text"]) == 4 * 1024 * 1024

    proxy.stdin.close()
    await asyncio.wait_for(proxy.wait(), timeout=10)
