#!/usr/bin/env python3
"""Install the unified, pinned Remote Research MCP facade without touching keys."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import tomllib

from remote_research_state import (
    HOST, PORT, PRINCIPALS, atomic_private, private, secure_dir,
)

# Same pinned public host key and SSH options as the original setup scripts.
KNOWN_HOSTS_LINE = (
    "[44.255.229.140]:22022 ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIOposESz9sift7F1deDCPoJZucWQ69k1Ln4b1auELh5s"
)
TOOLS = (
    "research_status", "research_once", "research_start", "research_continue",
    "research_list_conversations", "research_export_conversation", "research_close",
)
CLIENT = Path(__file__).resolve().parent / "remote_research_client.py"
LEGACY = {
    "claude_research": ("claude", "claudemcp"),
    "gemini_research": ("gemini", "geminimcp"),
}


class SetupError(ValueError):
    pass


def args(argv=None):
    parser = argparse.ArgumentParser(description="Install unified Remote Research MCP")
    parser.add_argument("--client", choices=("codex", "devin"), default="codex")
    parser.add_argument("--principal", choices=PRINCIPALS, default="partner")
    parser.add_argument("--key-path", type=Path)
    parser.add_argument("--known-hosts-path", type=Path)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--devin-home", type=Path)
    parser.add_argument("--verify", action="store_true")
    return parser.parse_args(argv)


def home_path(raw: Path) -> Path:
    # CODEX_HOME is commonly a deliberate symlink; resolve it once and keep
    # both the registration and its private config beneath the real home.
    path = raw.expanduser().resolve()
    if not path.is_absolute() or not path.is_dir():
        raise SetupError("Client home must be a directory")
    return path


def config_home(options) -> Path:
    if options.client == "codex":
        return home_path(options.codex_home or Path(os.environ.get("CODEX_HOME", "~/.codex")))
    default = (Path(os.environ.get("APPDATA", "~")) / "devin" if os.name == "nt"
               else Path("~/.config/devin"))
    return home_path(options.devin_home or default)


def regular(path: Path, *, secret: bool = False) -> None:
    if path.is_symlink() or any(p.is_symlink() for p in path.parents) or not path.is_file():
        raise SetupError("Unexpected symlink or missing regular file")
    info = path.lstat()
    if os.name != "nt" and (info.st_uid != os.getuid() or (secret and info.st_mode & 0o077)):
        raise SetupError("File ownership or permissions invalid")


def safe_target(path: Path) -> None:
    if path.is_symlink():
        raise SetupError("Symlinked configuration file")
    if path.exists():
        regular(path)


def ssh_argv(key: Path, pin: Path, user: str = "researchmcp") -> list[str]:
    return [
        "ssh", "-F", "none", "-T", "-p", PORT, "-i", str(key),
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={pin}",
        "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=2", "-o", "ClearAllForwardings=yes",
        "-o", "RequestTTY=no", f"{user}@{HOST}",
    ]


def locate_identity(entry: dict, user: str) -> tuple[Path, Path] | None:
    if not isinstance(entry, dict) or entry.get("command") not in ("ssh", "python3"):
        return None
    argv = entry.get("args")
    if not isinstance(argv, list) or not all(isinstance(v, str) for v in argv):
        return None
    # Original setup scripts used either a direct SSH argv or their own
    # reconnect proxy, with no extra remote command.
    if entry["command"] == "python3":
        path = Path(__file__).resolve().parents[2] / (
            "claude-research" if user == "claudemcp" else "gemini-research"
        ) / "scripts" / "mcp_ssh_reconnect.py"
        if argv[:4] != [str(path), "ssh", "-F", "none"]:
            return None
        ssh_args = argv[4:]
    else:
        ssh_args = argv
    try:
        key = Path(ssh_args[ssh_args.index("-i") + 1])
        pin_arg = next(v for v in ssh_args if v.startswith("UserKnownHostsFile="))
        pin = Path(pin_arg.split("=", 1)[1])
    except (ValueError, IndexError, StopIteration):
        return None
    # Legacy did not use -F none / no-forwarding; match every other flag and
    # disallow any user-supplied command/options.
    # Existing generated entries use -T,-p,... and no -F none.
    expected = ["-T", "-p", PORT, "-i", str(key)]
    for opt in ("BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes",
                f"UserKnownHostsFile={pin}", "ConnectTimeout=10",
                "ServerAliveInterval=30", "ServerAliveCountMax=2"):
        expected += ["-o", opt]
    expected += [f"{user}@{HOST}"]
    if ssh_args != expected:
        return None
    provider = "claude" if user == "claudemcp" else "gemini"
    toolset = [f"{provider}_{name}" for name in (
        "status", "once", "start", "list_conversations",
        "export_conversation", "continue", "close",
    )]
    timeouts = (60, 900) if entry["command"] == "python3" else (20, 900)
    fields = {"command", "args", "startup_timeout_sec", "tool_timeout_sec",
              "enabled_tools"}
    if set(entry) - {"enabled", "disabled"} != fields or (
        entry["startup_timeout_sec"], entry["tool_timeout_sec"]
    ) != timeouts or entry["enabled_tools"] != toolset:
        return None
    if any(not isinstance(entry[k], bool) for k in ("enabled", "disabled") if k in entry):
        return None
    if not key.is_absolute() or not pin.is_absolute():
        return None
    return key, pin


def existing(doc: dict, client: str) -> tuple[dict, dict[str, tuple[Path, Path]]]:
    servers = doc.get("mcp_servers" if client == "codex" else "mcpServers", {})
    if not isinstance(servers, dict):
        raise SetupError("Invalid MCP server map")
    recognized = {}
    for name, (_, user) in LEGACY.items():
        if name in servers:
            pair = locate_identity(servers[name], user)
            if pair is None:
                raise SetupError("Custom legacy registration conflicts with migration")
            recognized[name] = pair
    return servers, recognized


def select_paths(options, found: dict[str, tuple[Path, Path]]) -> tuple[Path, Path]:
    discovered = found.get("claude_research") or found.get("gemini_research")
    key = options.key_path or (discovered[0] if discovered else None)
    pin = options.known_hosts_path or (discovered[1] if discovered else None)
    if key is None or pin is None:
        raise SetupError("Explicit installed key and pinned known_hosts are required")
    key, pin = key.expanduser().absolute(), pin.expanduser().absolute()
    regular(key, secret=True)  # Never open, copy, generate, or print private key data.
    regular(pin, secret=True)
    if pin.read_bytes() not in (KNOWN_HOSTS_LINE.encode(), (KNOWN_HOSTS_LINE + "\n").encode()):
        raise SetupError("Host key does not match the pinned public key")
    return key, pin


def registration(config: Path, client: str) -> dict:
    item = {"command": sys.executable, "args": [str(CLIENT), "--config", str(config)]}
    if client == "codex":
        item.update(startup_timeout_sec=60, tool_timeout_sec=3780, enabled_tools=list(TOOLS))
    return item


def verify(config: Path, principal: str) -> None:
    # Verify through the same facade, not a second direct SSH connection.
    deadline = time.monotonic() + 90
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, str(CLIENT), "--config", str(config)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        assert proc.stdin is not None and proc.stdout is not None
        lines: queue.Queue[bytes] = queue.Queue(maxsize=64)

        def read_output() -> None:
            while line := proc.stdout.readline(32 * 1024 * 1024 + 1):
                lines.put(line)
            lines.put(b"")

        thread = threading.Thread(
            target=read_output, daemon=True,
        )
        thread.start()

        def send(method: str, identifier: int | None, params: dict) -> None:
            if time.monotonic() >= deadline:
                raise SetupError("Transport verification timed out")
            request = {"jsonrpc": "2.0", "method": method, "params": params}
            if identifier is not None:
                request["id"] = identifier
            proc.stdin.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
            proc.stdin.flush()

        def expect(identifier: int) -> dict:
            while True:
                if time.monotonic() >= deadline:
                    raise SetupError("Transport verification timed out")
                line = lines.get(timeout=max(.001, deadline - time.monotonic()))
                if not line:
                    raise SetupError("Unified MCP transport closed")
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise SetupError("Malformed unified MCP response")
                if (isinstance(item.get("method"), str)
                        and item["method"].startswith("notifications/")
                        and "id" not in item):
                    continue
                if item.get("id") != identifier or (
                    ("result" in item) == ("error" in item)
                ):
                    raise SetupError("Unexpected or duplicate MCP response ID")
                if "error" in item:
                    raise SetupError("Unified MCP transport returned an error")
                return item["result"]

        send("initialize", 1, {"protocolVersion": "2024-11-05",
             "capabilities": {}, "clientInfo": {
                 "name": "remote-research-verify", "version": "1"}})
        handshake = expect(1)
        if not isinstance(handshake, dict) or "protocolVersion" not in handshake:
            raise SetupError("Unified MCP initialize failed")
        send("notifications/initialized", None, {})
        send("tools/list", 2, {})
        tools_result = expect(2)
        send("tools/call", 3, {"name": "research_status", "arguments": {}})
        status_result = expect(3)
        proc.stdin.close()
        proc.wait(timeout=max(.001, deadline - time.monotonic()))
        if time.monotonic() >= deadline:
            raise SetupError("Transport verification timed out")
        if proc.returncode:
            raise SetupError("Unified MCP verifier exited unexpectedly")
        thread.join(timeout=max(.001, deadline - time.monotonic()))
        if thread.is_alive():
            raise SetupError("Transport verification timed out")
        while not lines.empty():
            raw = lines.get_nowait()
            if raw:
                item = json.loads(raw)
                if not isinstance(item, dict) or not (
                    isinstance(item.get("method"), str)
                    and item["method"].startswith("notifications/")
                    and "id" not in item
                ):
                    raise SetupError("Unexpected extra MCP response")
    except (subprocess.TimeoutExpired, queue.Empty):
        proc.kill()
        proc.wait()
        raise SetupError("Transport verification timed out") from None
    except OSError as exc:
        raise SetupError(f"Cannot start verifier: {type(exc).__name__}") from None
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
    try:
        names = [t["name"] for t in tools_result["tools"]]
        if len(names) != len(TOOLS) or set(names) != set(TOOLS):
            raise SetupError("Unified MCP tool list mismatch")
        result = status_result
        if result.get("isError"):
            raise SetupError("Unified MCP status failed")
        status = result.get("structuredContent")
        if not isinstance(status, dict):
            content = result.get("content", [])
            status = next((json.loads(c["text"]) for c in content
                           if c.get("type") == "text"), {})
        if not isinstance(status, dict) or status.get("principal") != principal:
            raise SetupError("Unified MCP principal cannot be confirmed")
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, SetupError):
            raise
        raise SetupError("Malformed unified MCP verification response") from None


def toml_block(name: str, entry: dict) -> str:
    return "\n[mcp_servers." + name + "]\n" + "".join(
        f"{key} = {json.dumps(value)}\n" for key, value in entry.items()
    )


def disable_codex(text: str, names: dict) -> str:
    for name in names:
        header = f"[mcp_servers.{name}]"
        lines = text.splitlines(keepends=True)
        starts = [i for i, line in enumerate(lines) if line.strip() == header]
        if len(starts) != 1:
            raise SetupError("Cannot safely locate legacy TOML table")
        start = starts[0] + 1
        end = next((i for i in range(start, len(lines))
                    if lines[i].lstrip().startswith("[")), len(lines))
        fields = [i for i in range(start, end)
                  if re.match(r"^\s*enabled\s*=", lines[i])]
        if len(fields) > 1:
            raise SetupError("Ambiguous legacy enabled field")
        if fields:
            match = re.match(r"^(\s*)enabled\s*=\s*(?:true|false)(\s*(?:#.*)?(?:\r?\n)?)$",
                             lines[fields[0]])
            if not match:
                raise SetupError("Cannot safely update legacy enabled field")
            lines[fields[0]] = f"{match[1]}enabled = false{match[2]}"
        else:
            lines.insert(end, "enabled = false\n")
        text = "".join(lines)
    return text


def make_document(raw: bytes | None, client: str, entry: dict,
                  recognized: dict) -> bytes:
    if client == "codex":
        text = (raw or b"").decode("utf-8")
        if "remote_research" not in tomllib.loads(text).get("mcp_servers", {}):
            text += ("" if not text or text.endswith("\n") else "\n")
            text += toml_block("remote_research", entry)
        if recognized:
            text = disable_codex(text, recognized)
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise SetupError("Updated TOML is invalid") from exc
        for name in recognized:
            if parsed["mcp_servers"][name].get("enabled") is not False:
                raise SetupError("Legacy entry was not disabled")
        return text.encode("utf-8")
    doc = json.loads(raw) if raw else {}
    doc.setdefault("mcpServers", {})["remote_research"] = entry
    for name in recognized:
        doc["mcpServers"][name]["disabled"] = True
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode()


def main(argv=None) -> int:
    try:
        options = args(argv)
        home = config_home(options)
        target = home / ("config.toml" if options.client == "codex" else "mcp_config.json")
        safe_target(target)
        raw = target.read_bytes() if target.exists() else None
        doc = (tomllib.loads(raw.decode()) if options.client == "codex"
               else json.loads(raw)) if raw else {}
        if not isinstance(doc, dict):
            raise SetupError("Invalid client configuration")
        servers, recognized = existing(doc, options.client)
        key, pin = select_paths(options, recognized)
        private_dir = home / "remote-research"
        secure_dir(private_dir)
        client_config = private_dir / "client.json"
        safe_target(client_config)
        if client_config.exists():
            private(client_config)
        desired = {"version": 1, "principal": options.principal,
                   "ssh_command": ssh_argv(key, pin)}
        if client_config.exists() and json.loads(client_config.read_text()) != desired:
            raise SetupError("Existing private client config differs")
        entry = registration(client_config, options.client)
        if "remote_research" in servers and servers["remote_research"] != entry:
            raise SetupError("Existing remote_research registration differs")
        if not client_config.exists():
            atomic_private(client_config, json.dumps(desired).encode() + b"\n")
        if options.verify:
            verify(client_config, options.principal)
        # Without verification, legacy registrations stay enabled.
        updated = make_document(raw, options.client, entry,
                                recognized if options.verify else {})
        if raw != updated:
            safe_target(target)
            if raw is not None:
                backup = target.with_name(target.name + ".pre-remote-" +
                                          hashlib.sha256(raw).hexdigest()[:16] + ".bak")
                safe_target(backup)
                if backup.exists():
                    private(backup)
                    if backup.read_bytes() != raw:
                        raise SetupError("Backup path already has different contents")
                else:
                    atomic_private(backup, raw)
            if raw is not None and target.read_bytes() != raw:
                raise SetupError("Configuration changed during installation")
            atomic_private(target, updated)
        print("remote_research registered; transport " +
              ("verified" if options.verify else "not verified") +
              "; provider auth and model smoke not tested")
        return 0
    except (SetupError, OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"remote_research setup: {type(exc).__name__}: " +
              (str(exc) if isinstance(exc, SetupError) else "invalid configuration"),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
