#!/usr/bin/env python3
"""Safely prepare and optionally verify the Claude Research MCP client."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

if sys.version_info < (3, 11):
    print(
        "错误: setup_client.py 需要 Python 3.11 或更高版本。请改用 python3.11（或更新版本）运行本脚本。",
        file=sys.stderr,
    )
    raise SystemExit(2)

import tomllib


HOST = "44.255.229.140"
SSH_PORT = "22022"
SSH_USER = "claudemcp"
LEGACY_SSH_USER = "ubuntu"
KNOWN_HOSTS_LINE = (
    "[44.255.229.140]:22022 ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIOposESz9sift7F1deDCPoJZucWQ69k1Ln4b1auELh5s"
)
HOST_FINGERPRINT = "SHA256:YVHHRs5TyOc5v1vkGZ40vW6jaDyULwM8LQeywygLH/Q"
TOOLS = (
    "claude_status",
    "claude_once",
    "claude_start",
    "claude_list_conversations",
    "claude_export_conversation",
    "claude_continue",
    "claude_close",
)
LEGACY_TOOLS = (
    "claude_status",
    "claude_once",
    "claude_start",
    "claude_continue",
    "claude_close",
)
PRINCIPALS = ("owner", "partner")
FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/=]{43}$")


class SetupError(RuntimeError):
    """A safe, user-facing setup failure."""


class VerifyError(RuntimeError):
    """A safe, user-facing verification failure."""


def absolute_path(raw: str | Path) -> Path:
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return Path(os.path.normpath(str(expanded)))


def default_codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    return absolute_path(raw) if raw else absolute_path("~/.codex")


def default_ssh_directory() -> Path:
    return absolute_path("~/.ssh")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare an isolated Codex MCP SSH client configuration."
    )
    parser.add_argument("--principal", choices=PRINCIPALS, default="partner")
    parser.add_argument(
        "--codex-home",
        type=absolute_path,
        default=default_codex_home(),
        help="Codex home directory (default: CODEX_HOME or ~/.codex).",
    )
    parser.add_argument(
        "--key-path",
        type=absolute_path,
        help="Destination or already-installed private key (default: ~/.ssh/claude_mcp_PRINCIPAL_ed25519).",
    )
    parser.add_argument(
        "--import-key",
        type=absolute_path,
        help="Import a delivered, MCP-dedicated private key into --key-path.",
    )
    parser.add_argument(
        "--known-hosts-path",
        type=absolute_path,
        help="Pinned known_hosts path (default: ~/.ssh/claude_mcp_known_hosts).",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="After prepare, verify the MCP handshake and claude_status over SSH.",
    )
    return parser.parse_args(argv)


def run_tool(command: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise SetupError(f"无法运行 {Path(command[0]).name}: {type(exc).__name__}") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise SetupError(f"运行 {Path(command[0]).name} 超时") from None
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def require_tools() -> tuple[str, str]:
    ssh = shutil.which("ssh")
    ssh_keygen = shutil.which("ssh-keygen")
    if not ssh or not ssh_keygen:
        raise SetupError("未找到 ssh 或 ssh-keygen，请先安装 OpenSSH 客户端")
    return ssh, ssh_keygen


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise SetupError(f"目录不可用或不是真实目录: {path}")


def require_regular_file(path: Path, *, reject_symlink: bool = True) -> None:
    try:
        stat = path.lstat()
    except OSError as exc:
        raise SetupError(f"无法检查文件 {path}: {type(exc).__name__}") from exc
    if reject_symlink and path.is_symlink():
        raise SetupError(f"拒绝使用符号链接: {path}")
    if not path.is_file():
        raise SetupError(f"不是常规文件: {path}")
    if stat.st_mode & 0o077:
        raise SetupError(f"文件权限过宽（需要仅所有者可读写）: {path}")


def ssh_args(
    key_path: Path, known_hosts_path: Path, *, ssh_user: str = SSH_USER
) -> list[str]:
    return [
        "-T",
        "-p",
        SSH_PORT,
        "-i",
        str(key_path),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_path}",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=2",
        f"{ssh_user}@{HOST}",
    ]


def expected_config(
    key_path: Path,
    known_hosts_path: Path,
    tools: tuple[str, ...] = TOOLS,
    *,
    ssh_user: str = SSH_USER,
) -> dict[str, Any]:
    return {
        "command": "ssh",
        "args": ssh_args(key_path, known_hosts_path, ssh_user=ssh_user),
        "startup_timeout_sec": 20,
        "tool_timeout_sec": 720,
        "enabled_tools": list(tools),
    }


def validate_known_hosts(path: Path, ssh_keygen: str) -> None:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SetupError(f"无法读取 known_hosts: {type(exc).__name__}") from exc
    if raw not in (KNOWN_HOSTS_LINE.encode(), (KNOWN_HOSTS_LINE + "\n").encode()):
        raise SetupError("known_hosts 内容与固定服务器主机密钥不一致；已拒绝继续")

    result = run_tool([ssh_keygen, "-lf", str(path)])
    if result.returncode != 0:
        raise SetupError("无法校验 known_hosts 主机密钥指纹")
    fingerprints = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            fingerprints.append(fields[1])
    if fingerprints != [HOST_FINGERPRINT]:
        raise SetupError("known_hosts 指纹与固定服务器指纹不一致；已拒绝继续")


def validate_public_key(public_key_path: Path, ssh_keygen: str) -> str:
    try:
        raw = public_key_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SetupError("无法读取公钥") from exc
    public_key = raw.strip()
    fields = public_key.split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise SetupError("公钥不是有效的 ED25519 公钥")

    result = run_tool([ssh_keygen, "-lf", str(public_key_path)])
    if result.returncode != 0 or "ED25519" not in result.stdout:
        raise SetupError("无法校验客户端公钥指纹")
    fields = result.stdout.split()
    if len(fields) < 2 or not FINGERPRINT_RE.fullmatch(fields[1]):
        raise SetupError("无法解析客户端公钥指纹")
    return fields[1]


def prepare_key(key_path: Path, ssh_keygen: str) -> str:
    public_key_path = Path(f"{key_path}.pub")
    if key_path.exists() or key_path.is_symlink():
        require_regular_file(key_path)
        if not public_key_path.is_file() or public_key_path.is_symlink():
            raise SetupError("私钥存在但对应 .pub 公钥缺失或无效")
        return validate_public_key(public_key_path, ssh_keygen)

    if public_key_path.exists() or public_key_path.is_symlink():
        raise SetupError("检测到孤立的 .pub 公钥；请先处理该路径后重试")

    result = run_tool(
        [
            ssh_keygen,
            "-t",
            "ed25519",
            "-a",
            "100",
            "-f",
            str(key_path),
            "-N",
            "",
        ],
        timeout=30,
    )
    if result.returncode != 0:
        raise SetupError("生成 ED25519 私钥失败；未输出密钥内容")
    require_regular_file(key_path)
    if not public_key_path.is_file() or public_key_path.is_symlink():
        raise SetupError("ssh-keygen 未生成可用公钥")
    return validate_public_key(public_key_path, ssh_keygen)


def import_delivered_key(source: Path, destination: Path, ssh_keygen: str) -> str:
    """Copy a delivered MCP-dedicated key without printing or retaining paths."""

    source = absolute_path(str(source))
    destination = absolute_path(str(destination))
    if source == destination:
        raise SetupError("导入源不能等于目标路径")
    if destination.exists() or destination.is_symlink():
        raise SetupError("目标专用私钥已存在；为避免覆盖已停止")
    derived_public = Path(f"{destination}.pub")
    if derived_public.exists() or derived_public.is_symlink():
        raise SetupError("目标公钥已存在；为避免覆盖已停止")
    require_regular_file(source)
    ensure_private_directory(destination.parent)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise SetupError("无法读取交付私钥") from exc
    if not raw or len(raw) > 1_048_576:
        raise SetupError("交付私钥格式或大小无效")
    fingerprint = run_tool([ssh_keygen, "-lf", str(source)])
    if fingerprint.returncode != 0 or "ED25519" not in fingerprint.stdout:
        raise SetupError("交付私钥不是有效的无口令 ED25519 私钥")
    fields = fingerprint.stdout.split()
    if len(fields) < 2 or not FINGERPRINT_RE.fullmatch(fields[1]):
        raise SetupError("无法解析交付私钥指纹")
    probe = run_tool([ssh_keygen, "-y", "-P", "", "-f", str(source)])
    derived_fields = probe.stdout.strip().split()
    if (
        probe.returncode != 0
        or len(derived_fields) < 2
        or derived_fields[0] != "ssh-ed25519"
    ):
        raise SetupError("交付私钥需要口令或无法派生公钥")
    derived_key = " ".join(derived_fields[:2]) + "\n"
    try:
        atomic_create(destination, raw, mode=0o600)
        atomic_create(derived_public, derived_key.encode("ascii"), mode=0o600)
        return validate_public_key(derived_public, ssh_keygen)
    except Exception:
        # Both targets were absent on entry and the parent is private. Roll back
        # a partial import so a retry cannot be mistaken for a verified key.
        for created in (derived_public, destination):
            try:
                created.unlink()
            except FileNotFoundError:
                pass
        raise


def atomic_create(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    ensure_private_directory(path.parent)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise SetupError(f"目标文件在创建过程中已存在: {path}") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_replace(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    ensure_private_directory(path.parent)
    before = path.stat() if path.exists() else None
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        current = path.stat() if path.exists() else None
        if current != before:
            raise SetupError("配置文件在写入过程中发生变化；已停止")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def validate_config(
    config_path: Path, key_path: Path, known_hosts_path: Path
) -> tuple[str, bytes | None]:
    expected = expected_config(key_path, known_hosts_path)
    if not config_path.exists():
        return "missing", None
    if config_path.is_symlink() or not config_path.is_file():
        raise SetupError("config.toml 必须是常规文件且不能是符号链接")
    try:
        raw = config_path.read_bytes()
        document = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise SetupError("config.toml 不存在或无法安全解析") from exc

    servers = document.get("mcp_servers")
    if "mcp_servers" in document and not isinstance(servers, dict):
        raise SetupError("config.toml 中 mcp_servers 配置无效")
    if "claude_research" not in (servers or {}):
        return "missing", raw
    section = servers["claude_research"]
    if section == expected:
        return "current", raw
    known_generated_configs = (
        expected_config(key_path, known_hosts_path, LEGACY_TOOLS),
        expected_config(
            key_path,
            known_hosts_path,
            TOOLS,
            ssh_user=LEGACY_SSH_USER,
        ),
        expected_config(
            key_path,
            known_hosts_path,
            LEGACY_TOOLS,
            ssh_user=LEGACY_SSH_USER,
        ),
    )
    if section in known_generated_configs:
        return "legacy", raw
    raise SetupError(
        "已有 [mcp_servers.claude_research] 与期望配置不完全一致；为避免覆盖已停止"
    )


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def enabled_tools_assignment(tools: tuple[str, ...]) -> str:
    values = "\n".join(f"  {toml_string(item)}," for item in tools)
    return f"enabled_tools = [\n{values}\n]"


def config_block(key_path: Path, known_hosts_path: Path) -> str:
    expected = expected_config(key_path, known_hosts_path)
    args = "\n".join(f"  {toml_string(item)}," for item in expected["args"])
    return f'''
[mcp_servers.claude_research]
command = "ssh"
args = [
{args}
]
startup_timeout_sec = {expected["startup_timeout_sec"]}
tool_timeout_sec = {expected["tool_timeout_sec"]}
{enabled_tools_assignment(TOOLS)}
'''


def migrate_legacy_config(
    raw: bytes, key_path: Path, known_hosts_path: Path
) -> bytes:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise SetupError("config.toml 无法安全解析") from exc
    header = "[mcp_servers.claude_research]"
    starts = [match.start() for match in re.finditer(rf"(?m)^{re.escape(header)}[ \t]*$", text)]
    if len(starts) != 1:
        raise SetupError("旧版 claude_research 配置无法安全定位；已拒绝自动迁移")
    start = starts[0]
    next_header = re.search(r"(?m)^\[[^\r\n]+\][ \t]*$", text[start + len(header) :])
    end = (
        start + len(header) + next_header.start()
        if next_header is not None
        else len(text)
    )
    replacement = config_block(key_path, known_hosts_path).lstrip("\n")
    updated = text[:start] + replacement + text[end:]
    try:
        parsed = tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError("升级后的 config.toml 无法安全解析；已停止") from exc
    if parsed.get("mcp_servers", {}).get("claude_research") != expected_config(
        key_path, known_hosts_path
    ):
        raise SetupError("旧版 claude_research 配置迁移校验失败；已停止")
    return updated.encode("utf-8")


def prepare_config(
    config_path: Path, key_path: Path, known_hosts_path: Path
) -> None:
    state, raw = validate_config(config_path, key_path, known_hosts_path)
    if state == "current":
        os.chmod(config_path, 0o600)
        return
    if state == "legacy":
        assert raw is not None
        atomic_replace(
            config_path,
            migrate_legacy_config(raw, key_path, known_hosts_path),
        )
        return
    block = config_block(key_path, known_hosts_path).encode("utf-8")
    if raw is None:
        atomic_create(config_path, block)
        return
    existing = raw.decode("utf-8")
    if existing and not existing.endswith("\n"):
        existing += "\n"
    atomic_replace(config_path, (existing + block).encode("utf-8"))


def prepare(
    *,
    principal: str,
    codex_home: Path,
    key_path: Path,
    known_hosts_path: Path,
    import_key: Path | None = None,
) -> tuple[str, str, str, str, str]:
    ssh, ssh_keygen = require_tools()
    ensure_private_directory(codex_home)

    config_path = codex_home / "config.toml"
    key_path = absolute_path(str(key_path))
    known_hosts_path = absolute_path(str(known_hosts_path))
    ensure_private_directory(key_path.parent)
    validate_config(config_path, key_path, known_hosts_path)
    if known_hosts_path.exists() or known_hosts_path.is_symlink():
        require_regular_file(known_hosts_path)
        validate_known_hosts(known_hosts_path, ssh_keygen)

    imported = import_key is not None
    mode = "imported" if imported else ("pre_authorized" if key_path.exists() else "generated")
    fingerprint = (
        import_delivered_key(import_key, key_path, ssh_keygen)
        if imported
        else prepare_key(key_path, ssh_keygen)
    )
    if not known_hosts_path.exists():
        atomic_create(
            known_hosts_path, (KNOWN_HOSTS_LINE + "\n").encode("utf-8")
        )
    validate_known_hosts(known_hosts_path, ssh_keygen)
    prepare_config(config_path, key_path, known_hosts_path)

    try:
        public_key = Path(f"{key_path}.pub").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise SetupError("无法读取公钥") from exc
    return ssh, ssh_keygen, public_key, fingerprint, mode


def rpc_messages() -> tuple[dict[str, Any], ...]:
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "claude-research-setup", "version": "1.0"},
        },
    }
    initialized = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }
    tools_list = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }
    status_call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "claude_status", "arguments": {}},
    }
    return initialize, initialized, tools_list, status_call


def jsonrpc_messages() -> str:
    """Return the transcript shape for tests and diagnostics, not transport."""

    return "\n".join(
        json.dumps(message, separators=(",", ":"), sort_keys=True)
        for message in rpc_messages()
    ) + "\n"


def parse_responses(stdout: str) -> dict[Any, dict[str, Any]]:
    responses: dict[Any, dict[str, Any]] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise VerifyError("MCP 服务器返回了无法解析的响应") from exc
        if not isinstance(message, dict):
            raise VerifyError("MCP 服务器返回了无效响应")
        if "id" not in message:
            continue
        request_id = message["id"]
        if request_id in responses:
            raise VerifyError("MCP 服务器返回了重复响应 ID")
        responses[request_id] = message
    return responses


def rpc_result(response: Mapping[str, Any], label: str) -> dict[str, Any]:
    if "error" in response:
        raise VerifyError(f"{label} 返回 MCP 错误")
    result = response.get("result")
    if not isinstance(result, dict):
        raise VerifyError(f"{label} 未返回有效 result")
    return result


def status_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if not isinstance(content, list):
        raise VerifyError("claude_status 返回格式无效")
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        try:
            payload = json.loads(item.get("text", ""))
        except (TypeError, json.JSONDecodeError) as exc:
            raise VerifyError("claude_status 返回格式无效") from exc
        if isinstance(payload, dict):
            return payload
    raise VerifyError("claude_status 未返回状态 JSON")


def check_responses(stdout: str, principal: str) -> None:
    responses = parse_responses(stdout)
    for request_id, label in ((1, "initialize"), (2, "tools/list"), (3, "claude_status")):
        if request_id not in responses:
            raise VerifyError(f"MCP 服务器未返回 {label} 响应")

    initialized = rpc_result(responses[1], "initialize")
    server_info = initialized.get("serverInfo")
    if not isinstance(server_info, dict) or server_info.get("name") != "claude-research":
        raise VerifyError("MCP initialize 返回了非预期服务器")

    tools_result = rpc_result(responses[2], "tools/list")
    raw_tools = tools_result.get("tools")
    if not isinstance(raw_tools, list):
        raise VerifyError("tools/list 返回格式无效")
    names: list[str] = []
    for tool in raw_tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise VerifyError("tools/list 包含无效工具项")
        names.append(tool["name"])
    if sorted(names) != sorted(TOOLS):
        raise VerifyError("tools/list 未提供全部七个 claude 工具")

    call_result = rpc_result(responses[3], "claude_status")
    if call_result.get("isError") is True:
        raise VerifyError("claude_status 调用失败")
    status = status_payload(call_result)
    if status.get("principal") != principal:
        raise VerifyError("claude_status 返回的 principal 与请求不一致")
    if status.get("logged_in") is not True:
        raise VerifyError("Claude CLI 未登录")
    if status.get("status") != "ok":
        raise VerifyError("claude_status 返回状态异常")
    models = status.get("models")
    if (
        not isinstance(models, dict)
        or not isinstance(models.get("opus"), dict)
        or models["opus"].get("cli_supported") is not True
    ):
        raise VerifyError("claude_status 未确认 opus 可用")
    quota = status.get("opus_quota")
    if (
        not isinstance(quota, dict)
        or quota.get("source")
        not in {"claude_code_statusline", "claude_code_usage_command"}
        or not isinstance(quota.get("available"), bool)
    ):
        raise VerifyError("claude_status 未提供有效的 Opus 额度状态")


async def _read_bounded_stderr(
    stream: asyncio.StreamReader, limit: int = 16_384
) -> str:
    kept = bytearray()
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        if len(kept) < limit:
            kept.extend(chunk[: limit - len(kept)])
    return kept.decode("utf-8", errors="replace")


async def _send_message(
    stream: asyncio.StreamWriter, message: Mapping[str, Any]
) -> None:
    payload = json.dumps(message, separators=(",", ":"), sort_keys=True) + "\n"
    stream.write(payload.encode("utf-8"))
    await stream.drain()


async def _read_response(
    stream: asyncio.StreamReader, expected_id: int
) -> dict[str, Any]:
    while True:
        try:
            raw = await asyncio.wait_for(stream.readline(), timeout=20)
        except asyncio.TimeoutError as exc:
            raise VerifyError("MCP 验证超时；请检查服务器状态") from exc
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise VerifyError("MCP 服务器响应超过安全上限") from exc
        if not raw:
            raise VerifyError("SSH/MCP 连接在验证完成前关闭")
        try:
            message = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise VerifyError("MCP 服务器返回了无法解析的响应") from exc
        if not isinstance(message, dict):
            raise VerifyError("MCP 服务器返回了无效响应")
        if "id" not in message:
            continue
        if message.get("id") != expected_id:
            raise VerifyError("MCP 服务器返回了非预期响应 ID")
        return message


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=1)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


def _connection_error(returncode: int, stderr: str) -> VerifyError:
    diagnostic = stderr.casefold()
    if returncode == 255 and (
        "permission denied" in diagnostic
        or "no supported authentication methods" in diagnostic
    ):
        return VerifyError(
            "服务器尚未授权这把公钥。请管理员将上面显示的公钥加入 "
            "/etc/ssh/authorized_keys/claude-mcp，并绑定对应 principal 的 forced command。"
        )
    return VerifyError(f"SSH/MCP 验证失败（退出码 {returncode}）")


async def _exchange_mcp(command: list[str]) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=262_144,
        )
    except OSError as exc:
        raise VerifyError(f"无法启动 SSH: {type(exc).__name__}") from exc

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stderr_task = asyncio.create_task(_read_bounded_stderr(process.stderr))
    responses: list[dict[str, Any]] = []
    pending_error: VerifyError | None = None
    messages = rpc_messages()
    try:
        await _send_message(process.stdin, messages[0])
        responses.append(await _read_response(process.stdout, 1))
        await _send_message(process.stdin, messages[1])
        await _send_message(process.stdin, messages[2])
        responses.append(await _read_response(process.stdout, 2))
        await _send_message(process.stdin, messages[3])
        responses.append(await _read_response(process.stdout, 3))
    except (BrokenPipeError, ConnectionResetError) as exc:
        pending_error = VerifyError("SSH/MCP 连接在验证完成前关闭")
        pending_error.__cause__ = exc
    except VerifyError as exc:
        pending_error = exc
    finally:
        if not process.stdin.is_closing():
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        await _stop_process(process)
        stderr_task.cancel()
        await asyncio.gather(stderr_task, return_exceptions=True)
        raise VerifyError("SSH/MCP 验证进程未正常结束") from None

    stderr = await stderr_task
    if process.returncode != 0:
        raise _connection_error(process.returncode or -1, stderr)
    if pending_error is not None:
        raise pending_error
    return "\n".join(
        json.dumps(response, separators=(",", ":"), sort_keys=True)
        for response in responses
    ) + "\n"


def exchange_mcp(command: list[str]) -> str:
    return asyncio.run(_exchange_mcp(command))


def verify(
    *,
    principal: str,
    ssh: str,
    key_path: Path,
    known_hosts_path: Path,
) -> None:
    command = [ssh, *ssh_args(key_path, known_hosts_path)]
    check_responses(exchange_mcp(command), principal)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    codex_home = args.codex_home
    ssh_directory = default_ssh_directory()
    key_path = (
        args.key_path
        if args.key_path
        else ssh_directory / f"claude_mcp_{args.principal}_ed25519"
    )
    known_hosts_path = (
        args.known_hosts_path
        if args.known_hosts_path
        else ssh_directory / "claude_mcp_known_hosts"
    )

    try:
        ssh, _ssh_keygen, public_key, fingerprint, mode = prepare(
            principal=args.principal,
            codex_home=codex_home,
            key_path=key_path,
            known_hosts_path=known_hosts_path,
            import_key=args.import_key,
        )
        labels = {
            "imported": "pre-authorized import",
            "pre_authorized": "pre-authorized",
            "generated": "generated fallback",
        }
        print(f"密钥模式: {labels[mode]}")
        if mode == "imported":
            print("已导入 Owner 预授权的 MCP 专用密钥（未输出私钥）。")
        elif mode == "pre_authorized":
            print("检测到已就位的 MCP 专用密钥；假定 Owner 已预授权其公钥。")
        else:
            print("已生成新的 MCP 专用密钥 fallback。")
        if mode == "generated":
            print("公钥:")
            print(public_key)
        print(f"公钥指纹: {fingerprint}")
        if args.verify:
            verify(
                principal=args.principal,
                ssh=ssh,
                key_path=key_path,
                known_hosts_path=known_hosts_path,
            )
            print("verify: 通过")
            print("服务器已授权该公钥，MCP 状态正常。")
            if args.import_key:
                print(f"导入源文件保留在: {args.import_key}")
                print("确认连接可用后，请通过系统安全删除机制删除该交付私钥。")
        else:
            print("请管理员授权上述公钥。")
        return 0
    except (SetupError, VerifyError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
