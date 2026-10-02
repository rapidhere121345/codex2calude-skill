#!/usr/bin/env python3
"""Call the already-installed Gemini Research MCP entry without a catalog reload."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import uuid
from typing import Any

if sys.version_info < (3, 11):
    print(
        "错误: call_tool.py 需要 Python 3.11 或更高版本。请改用 python3.11（或更新版本）运行本脚本。",
        file=sys.stderr,
    )
    raise SystemExit(2)

import tomllib

try:
    import setup_client as setup
except ModuleNotFoundError:  # Direct execution and import-by-path in tests.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import setup_client as setup


CONFIG_NAME = "config.toml"
SERVER_NAME = "gemini-research"
PROMPT_LIMIT = 20_000
TOOL_TIMEOUT_SEC = 720
OUTPUT_LIMIT = 1_048_576
ARGUMENTS_FILE_LIMIT = 1_048_576
PROMPT_TOOLS = frozenset({"gemini_once", "gemini_start", "gemini_continue"})
TEXT_ERROR_METADATA_RE = re.compile(
    r" \[request_id: ([0-9a-fA-F-]{36})\]"
    r"(?: \[raw_log_saved: (true|false)\])?$"
)


class CallError(RuntimeError):
    """A safe, user-facing call failure."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Invoke one of the seven installed gemini_research MCP tools over its "
            "pinned forced-command SSH entry."
        )
    )
    parser.add_argument("--tool", required=True, choices=setup.TOOLS)
    parser.add_argument("--prompt", help="UTF-8 prompt (at most 20,000 characters).")
    parser.add_argument(
        "--prompt-stdin",
        action="store_true",
        help="Read the UTF-8 prompt from stdin instead of --prompt.",
    )
    parser.add_argument(
        "--arguments-file",
        type=setup.absolute_path,
        help="JSON object passed to the server schema after local safety checks.",
    )
    parser.add_argument(
        "--codex-home",
        type=setup.absolute_path,
        default=setup.default_codex_home(),
        help="Codex home directory (default: CODEX_HOME or ~/.codex).",
    )
    return parser.parse_args(argv)


def _regular_config(path: Path) -> bytes:
    try:
        stat = path.lstat()
    except OSError as exc:
        raise CallError(f"无法检查 config.toml: {type(exc).__name__}") from exc
    if path.is_symlink() or not path.is_file():
        raise CallError("config.toml 必须是常规文件且不能是符号链接")
    if stat.st_size > OUTPUT_LIMIT:
        raise CallError("config.toml 超过安全大小上限")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CallError(f"无法读取 config.toml: {type(exc).__name__}") from exc


def installed_entry(codex_home: Path) -> tuple[Path, Path, list[str]]:
    """Return only paths and argv from an entry exactly matching setup policy."""

    raw = _regular_config(codex_home / CONFIG_NAME)
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise CallError("config.toml 无法安全解析") from exc
    servers = document.get("mcp_servers")
    if not isinstance(servers, dict) or "gemini_research" not in servers:
        raise CallError("config.toml 中未找到已安装的 gemini_research MCP 配置")
    entry = servers["gemini_research"]
    if not isinstance(entry, dict) or entry.get("command") != "ssh":
        raise CallError("gemini_research MCP 命令不受支持")
    argv = entry.get("args")
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise CallError("gemini_research MCP 参数无效")

    key_value: str | None = None
    known_hosts_value: str | None = None
    for index, item in enumerate(argv[:-1]):
        if item == "-i":
            if key_value is not None:
                raise CallError("gemini_research MCP 参数无效")
            key_value = argv[index + 1]
        if item == "-o" and argv[index + 1].startswith("UserKnownHostsFile="):
            value = argv[index + 1].split("=", 1)[1]
            if known_hosts_value is not None:
                raise CallError("gemini_research MCP 参数无效")
            known_hosts_value = value
    if key_value is None or known_hosts_value is None:
        raise CallError("gemini_research MCP 配置缺少专用密钥或主机 pin")

    key_path = setup.absolute_path(key_value)
    known_hosts_path = setup.absolute_path(known_hosts_value)
    expected = setup.expected_config(key_path, known_hosts_path)
    if entry != expected:
        raise CallError("gemini_research MCP 配置与安装器期望值不一致；已拒绝调用")
    return key_path, known_hosts_path, expected["args"]


def validate_local_files(
    key_path: Path, known_hosts_path: Path
) -> tuple[str, list[str]]:
    ssh, ssh_keygen = setup.require_tools()
    setup.require_regular_file(key_path)
    setup.validate_known_hosts(known_hosts_path, ssh_keygen)
    return ssh, list(setup.ssh_args(key_path, known_hosts_path))


def read_prompt(args: argparse.Namespace) -> str | None:
    if args.prompt is not None and args.prompt_stdin:
        raise CallError("--prompt 与 --prompt-stdin 只能选择一个")
    if not args.prompt_stdin:
        return args.prompt
    try:
        raw = sys.stdin.buffer.read(PROMPT_LIMIT * 4 + 1)
    except OSError as exc:
        raise CallError(f"无法读取 stdin prompt: {type(exc).__name__}") from exc
    try:
        prompt = raw.decode("utf-8")
    except UnicodeError as exc:
        raise CallError("prompt 不是有效 UTF-8") from exc
    if len(prompt) > PROMPT_LIMIT:
        raise CallError("prompt 超过 20,000 字符上限")
    return prompt


def read_arguments(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        stat = path.lstat()
    except OSError as exc:
        raise CallError(f"无法检查 arguments 文件: {type(exc).__name__}") from exc
    if path.is_symlink() or not path.is_file():
        raise CallError("arguments 文件必须是常规文件且不能是符号链接")
    if stat.st_size > ARGUMENTS_FILE_LIMIT:
        raise CallError("arguments 文件超过安全大小上限")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CallError("arguments 文件无法解析为 JSON") from exc
    if not isinstance(value, dict):
        raise CallError("arguments 文件必须包含 JSON object")
    return value


def build_arguments(args: argparse.Namespace) -> dict[str, Any]:
    arguments = read_arguments(args.arguments_file)
    prompt = read_prompt(args)
    if prompt is not None:
        if "prompt" in arguments:
            raise CallError("prompt 同时来自 CLI/stdin 和 arguments 文件；请只保留一个")
        arguments["prompt"] = prompt
    prompt_value = arguments.get("prompt")
    if prompt_value is not None and (
        not isinstance(prompt_value, str)
        or not prompt_value
        or len(prompt_value) > PROMPT_LIMIT
    ):
        raise CallError("prompt 必须是非空字符串且不超过 20,000 字符")
    if args.tool in PROMPT_TOOLS and not isinstance(prompt_value, str):
        raise CallError(f"{args.tool} 需要 prompt")

    # Keep the old partner-facing defaults explicit. All other fields remain
    # untouched so the server's published schema remains the validator.
    if args.tool in {"gemini_once", "gemini_start"}:
        arguments.setdefault("model", "pro")
        arguments.setdefault("use_web", True)
    return arguments


def _server_name(response: Mapping[str, Any]) -> dict[str, Any]:
    if "error" in response:
        raise CallError("MCP initialize 返回错误")
    result = response.get("result")
    server_info = result.get("serverInfo") if isinstance(result, dict) else None
    if not isinstance(server_info, dict) or server_info.get("name") != SERVER_NAME:
        raise CallError("MCP initialize 返回了非预期服务器")
    return result  # type: ignore[return-value]


def _tool_names(response: Mapping[str, Any]) -> list[str]:
    if "error" in response:
        raise CallError("MCP tools/list 返回错误")
    result = response.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        raise CallError("MCP tools/list 返回格式无效")
    names: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise CallError("MCP tools/list 包含无效工具项")
        names.append(tool["name"])
    if len(names) != len(set(names)) or sorted(names) != sorted(set(setup.TOOLS)):
        raise CallError("MCP tools/list 未提供恰好七个 gemini 工具")
    return names


async def _read_tool_response(
    stream: asyncio.StreamReader, expected_id: int, timeout: int
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            raw = await asyncio.wait_for(
                stream.readline(), timeout=max(0, deadline - time.monotonic())
            )
        except asyncio.TimeoutError as exc:
            raise CallError(f"MCP 工具调用超过 {timeout} 秒；已终止连接") from exc
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise CallError("MCP 工具响应超过安全上限") from exc
        if not raw:
            raise CallError("SSH/MCP 连接在工具调用完成前关闭")
        if len(raw) > OUTPUT_LIMIT:
            raise CallError("MCP 工具响应超过安全上限")
        try:
            message = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CallError("MCP 工具服务器返回了无法解析的响应") from exc
        if not isinstance(message, dict):
            raise CallError("MCP 工具服务器返回了无效响应")
        if "id" not in message:
            continue
        if message.get("id") != expected_id:
            raise CallError("MCP 工具服务器返回了非预期响应 ID")
        return message


def _structured_result(response: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    if "error" in response:
        message = response["error"]
        text = message.get("message") if isinstance(message, dict) else None
        raise CallError(text if isinstance(text, str) and text else "MCP 工具返回错误")
    result = response.get("result")
    if not isinstance(result, dict):
        raise CallError("MCP 工具未返回有效 result")
    structured = result.get("structuredContent")
    is_error = result.get("isError") is True
    if not isinstance(structured, dict):
        if is_error:
            return _text_error_result(result), True
        raise CallError("MCP 工具未返回 structuredContent")
    return structured, is_error


def _text_error_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a FastMCP text error without republishing its free-form text."""

    error: dict[str, Any] = {"message": "MCP 工具失败"}
    content = result.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return error
    item = content[0]
    if not isinstance(item, dict) or item.get("type") != "text":
        return error
    text = item.get("text")
    if not isinstance(text, str):
        return error
    match = TEXT_ERROR_METADATA_RE.search(text)
    if match is None:
        return error
    request_id = match.group(1)
    try:
        parsed_request_id = uuid.UUID(request_id)
    except ValueError:
        return error
    if parsed_request_id.version != 4 or str(parsed_request_id) != request_id:
        return error
    error["request_id"] = request_id
    if match.group(2) is not None:
        error["raw_log_saved"] = match.group(2) == "true"
    return error


async def _exchange(command: list[str], tool: str, arguments: Mapping[str, Any]):
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=OUTPUT_LIMIT,
        )
    except OSError as exc:
        raise CallError(f"无法启动 SSH: {type(exc).__name__}") from exc

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stderr_task = asyncio.create_task(
        setup._read_bounded_stderr(process.stderr, limit=16_384)
    )
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": SERVER_NAME, "version": "1.0"},
        },
    }
    initialized = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }
    tools_list = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    tool_call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": tool, "arguments": dict(arguments)},
    }

    failure: CallError | None = None
    try:
        await setup._send_message(process.stdin, initialize)
        _server_name(await setup._read_response(process.stdout, 1))
        await setup._send_message(process.stdin, initialized)
        await setup._send_message(process.stdin, tools_list)
        _tool_names(await setup._read_response(process.stdout, 2))
        await setup._send_message(process.stdin, tool_call)
        return _structured_result(
            await _read_tool_response(process.stdout, 3, TOOL_TIMEOUT_SEC)
        )
    except CallError as exc:
        failure = exc
        raise
    except setup.VerifyError as exc:
        failure = CallError(str(exc))
        raise failure from exc
    except (BrokenPipeError, ConnectionResetError) as exc:
        failure = CallError("SSH/MCP 连接在调用完成前关闭")
        raise failure from exc
    finally:
        if not process.stdin.is_closing():
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            await setup._stop_process(process)
        if process.returncode is not None:
            process._transport.close()
        await asyncio.gather(stderr_task, return_exceptions=True)
        if failure is None and process.returncode not in (0, None):
            # stderr is intentionally consumed and discarded; it can contain SSH
            # host, account, or endpoint diagnostics that must not be republished.
            raise CallError(f"SSH/MCP 进程异常退出（退出码 {process.returncode}）")


def _write_success(structured: Mapping[str, Any]) -> None:
    payload = json.dumps(structured, ensure_ascii=False, separators=(",", ":")) + "\n"
    if len(payload.encode("utf-8")) > OUTPUT_LIMIT:
        raise CallError("MCP 工具结果超过 stdout 安全上限")
    sys.stdout.buffer.write(payload.encode("utf-8"))
    sys.stdout.buffer.flush()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        arguments = build_arguments(args)
        key_path, known_hosts_path, expected_argv = installed_entry(args.codex_home)
        ssh, strict_argv = validate_local_files(key_path, known_hosts_path)
        if strict_argv != expected_argv:
            raise CallError("SSH argv 与已安装 MCP 配置不一致；已拒绝调用")
        command = [ssh, *strict_argv]
        structured, is_error = asyncio.run(_exchange(command, args.tool, arguments))
        if is_error:
            wrapper = {"error": structured}
            payload = json.dumps(
                wrapper, ensure_ascii=False, separators=(",", ":")
            ) + "\n"
            if len(payload.encode("utf-8")) > OUTPUT_LIMIT:
                raise CallError("MCP 错误结果超过 stdout 安全上限")
            sys.stdout.buffer.write(payload.encode("utf-8"))
            sys.stdout.buffer.flush()
            return 1
        _write_success(structured)
        return 0
    except (CallError, setup.SetupError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
