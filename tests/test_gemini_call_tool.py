from __future__ import annotations

import importlib.util
import asyncio
import json
import os
from pathlib import Path
import stat
import subprocess
import textwrap
import typing as t

import pytest


SCRIPT_PATH = (
    Path(__file__).parents[1]
    / "skills"
    / "gemini-research"
    / "scripts"
    / "call_tool.py"
)
SPEC = importlib.util.spec_from_file_location("gemini_call_tool", SCRIPT_PATH)
assert SPEC and SPEC.loader
call_tool = importlib.util.module_from_spec(SPEC)
t.cast(t.Any, SPEC.loader).exec_module(call_tool)
setup = call_tool.setup


PRIVATE_MARKER = b"PRIVATE-GEMINI-MARKER-MUST-NOT-PRINT"
SECRET_STDERR = "SECRET-SSH-STDERR-MUST-NOT-PRINT"


@pytest.fixture()
def workspace(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    codex_home = tmp_path / "codex-home"
    key_path = tmp_path / "partner-key"
    known_hosts_path = tmp_path / "known-hosts"
    log_path = tmp_path / "ssh-log.jsonl"
    codex_home.mkdir(mode=0o700)
    key_path.write_bytes(PRIVATE_MARKER)
    key_path.chmod(0o600)
    known_hosts_path.write_text(setup.KNOWN_HOSTS_LINE + "\n", encoding="utf-8")
    known_hosts_path.chmod(0o600)
    config_path = codex_home / "config.toml"
    config_path.write_text(
        "[notice]\nhide = true\n\n" + setup.config_block(key_path, known_hosts_path),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    return codex_home, key_path, known_hosts_path, log_path


@pytest.fixture()
def fake_clis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, dict[str, str]]:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir(mode=0o700)
    fake_ssh = bin_directory / "ssh"
    fake_ssh_body = (
        """
        import json
        import os
        import sys
        import time

        log_path = os.environ["GEMINI_FAKE_SSH_LOG"]
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(json.dumps({"argv": sys.argv, "pid": os.getpid()}) + chr(10))
        print(__SECRET_STDERR__, file=sys.stderr, flush=True)

        names = os.environ.get(
            "GEMINI_FAKE_TOOLS",
            "gemini_status,gemini_once,gemini_start,gemini_list_conversations,"
            "gemini_export_conversation,gemini_continue,gemini_close",
        ).split(",")
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            request = json.loads(line)
            method = request.get("method")
            request_id = request.get("id")
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "gemini-research", "version": "1"},
                }
            elif method == "tools/list":
                result = {"tools": [{"name": name} for name in names]}
            elif method == "tools/call":
                if os.environ.get("GEMINI_FAKE_DELAY") == "1":
                    time.sleep(30)
                fake_result = os.environ.get("GEMINI_FAKE_TOOL_RESULT")
                if fake_result is not None:
                    result = json.loads(fake_result)
                    print(json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": result,
                        }
                    ), flush=True)
                    continue
                structured = {
                    "kind": request["params"]["name"],
                    "arguments": request["params"]["arguments"],
                }
                if os.environ.get("GEMINI_FAKE_OVERSIZE") == "1":
                    structured["padding"] = "x" * 1_200_000
                result = {"isError": False, "structuredContent": structured}
            else:
                continue
            print(json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": result,
                }
            ), flush=True)
        """
    )
    fake_ssh_script = (
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(fake_ssh_body).strip()
        + "\n"
    ).replace("__SECRET_STDERR__", json.dumps(SECRET_STDERR))
    fake_ssh.write_text(
        fake_ssh_script,
        encoding="utf-8",
    )
    fake_ssh.chmod(0o700)

    fake_keygen = bin_directory / "fake-keygen"
    fake_keygen.write_text(
        textwrap.dedent(
            """#!/usr/bin/env python3
            import os
            import sys
            from pathlib import Path

            if sys.argv[1:2] != ["-lf"]:
                raise SystemExit(2)
            pin = Path(sys.argv[3]).read_text(encoding="utf-8").strip()
            if pin == __PINNED_HOST_KEY__:
                print(f"25519 {os.environ['GEMINI_FAKE_HOST_FINGERPRINT']} pinned")
                raise SystemExit(0)
            raise SystemExit(1)
            """
        ).replace(
            "__PINNED_HOST_KEY__", json.dumps(setup.KNOWN_HOSTS_LINE)
        ),
        encoding="utf-8",
    )
    fake_keygen.chmod(0o700)

    environment = {
        "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
        "GEMINI_FAKE_SSH_LOG": str(tmp_path / "ssh-log.jsonl"),
        "GEMINI_FAKE_HOST_FINGERPRINT": setup.HOST_FINGERPRINT,
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    return fake_ssh, environment


def run_cli(
    workspace: tuple[Path, Path, Path, Path],
    fake_clis: tuple[Path, dict[str, str]],
    *arguments: str,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[str]:
    codex_home, _, _, _ = workspace
    _, environment = fake_clis
    merged = os.environ.copy()
    merged.update(environment)
    return subprocess.run(
        [
            "python3",
            str(SCRIPT_PATH),
            "--codex-home",
            str(codex_home),
            *arguments,
        ],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=merged,
        timeout=15,
        check=False,
    )


def log_records(path: Path) -> list[dict[str, t.Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def replace_config(
    workspace: tuple[Path, Path, Path, Path], transform: t.Callable[[dict[str, t.Any]], None]
) -> None:
    codex_home, key_path, known_hosts_path, _ = workspace
    import tomllib

    config_path = codex_home / "config.toml"
    document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    entry = document["mcp_servers"]["gemini_research"]
    transform(entry)

    def value(item: t.Any) -> str:
        return json.dumps(item, ensure_ascii=False)

    args = "\n".join(f"  {value(item)}," for item in entry["args"])
    tools = "\n".join(f"  {value(item)}," for item in entry["enabled_tools"])
    entry = document["mcp_servers"]["gemini_research"]
    config_path.write_text(
        f'''[mcp_servers.gemini_research]\ncommand = {value(entry["command"])}\nargs = [\n{args}\n]\nstartup_timeout_sec = {entry["startup_timeout_sec"]}\ntool_timeout_sec = {entry["tool_timeout_sec"]}\nenabled_tools = [\n{tools}\n]\n''',
        encoding="utf-8",
    )
    assert key_path.exists() and known_hosts_path.exists()


def test_installed_config_calls_status_once_and_continue_without_reload(
    workspace: tuple[Path, Path, Path, Path],
    fake_clis: tuple[Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    codex_home, key_path, known_hosts_path, log_path = workspace
    arguments_file = tmp_path / "continue.json"
    arguments_file.write_text(
        json.dumps({"conversation_id": "0123456789abcdef", "extra": ["保留"]}),
        encoding="utf-8",
    )

    status = run_cli(workspace, fake_clis, "--tool", "gemini_status")
    once = run_cli(
        workspace,
        fake_clis,
        "--tool",
        "gemini_once",
        "--prompt",
        "模型请求",
    )
    continued = run_cli(
        workspace,
        fake_clis,
        "--tool",
        "gemini_continue",
        "--prompt-stdin",
        "--arguments-file",
        str(arguments_file),
        stdin="续聊 prompt",
    )

    assert status.returncode == 0
    assert json.loads(status.stdout)["kind"] == "gemini_status"
    assert once.returncode == 0
    assert json.loads(once.stdout)["arguments"] == {
        "prompt": "模型请求",
        "model": "pro",
        "use_web": True,
    }
    assert continued.returncode == 0
    assert json.loads(continued.stdout)["arguments"] == {
        "conversation_id": "0123456789abcdef",
        "extra": ["保留"],
        "prompt": "续聊 prompt",
    }
    combined = status.stdout + status.stderr + once.stdout + once.stderr
    assert PRIVATE_MARKER.decode() not in combined
    assert SECRET_STDERR not in combined
    records = log_records(log_path)
    assert len(records) == 3
    expected = [str(fake_clis[0]), *setup.ssh_args(key_path, known_hosts_path)]
    assert all(record["argv"] == expected for record in records)
    assert codex_home.name


def test_explicit_model_and_web_choice_are_preserved(
    workspace: tuple[Path, Path, Path, Path],
    fake_clis: tuple[Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    arguments_file = tmp_path / "once.json"
    arguments_file.write_text(
        json.dumps({"model": "flash", "use_web": False, "timeout_seconds": 11}),
        encoding="utf-8",
    )

    result = run_cli(
        workspace,
        fake_clis,
        "--tool",
        "gemini_once",
        "--prompt",
        "explicit",
        "--arguments-file",
        str(arguments_file),
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["arguments"] == {
        "prompt": "explicit",
        "model": "flash",
        "use_web": False,
        "timeout_seconds": 11,
    }


@pytest.mark.parametrize("mode", ["missing", "extra"])
def test_exact_seven_tool_catalog_is_required(
    workspace: tuple[Path, Path, Path, Path],
    fake_clis: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    names = list(setup.TOOLS)
    if mode == "missing":
        names.remove("gemini_continue")
    else:
        names.append("gemini_evil_tool")
    monkeypatch.setenv("GEMINI_FAKE_TOOLS", ",".join(names))
    fake_clis[1]["GEMINI_FAKE_TOOLS"] = ",".join(names)

    result = run_cli(workspace, fake_clis, "--tool", "gemini_status")

    assert result.returncode == 2
    assert "恰好七个" in result.stderr


def test_evil_endpoint_remote_command_and_config_mismatch_are_rejected(
    workspace: tuple[Path, Path, Path, Path],
    fake_clis: tuple[Path, dict[str, str]],
) -> None:
    _, _, _, log_path = workspace

    def evil_endpoint(entry: dict[str, t.Any]) -> None:
        entry["args"][-1] = "ubuntu@evil.example"

    def remote_command(entry: dict[str, t.Any]) -> None:
        entry["args"].append("export SECRET=1")

    for transform in (evil_endpoint, remote_command):
        replace_config(workspace, transform)
        result = run_cli(workspace, fake_clis, "--tool", "gemini_status")
        assert result.returncode == 2
        assert "期望值不一致" in result.stderr
        assert "SECRET" not in result.stderr
    assert log_records(log_path) == []


def test_wrong_host_pin_and_key_symlink_are_rejected(
    workspace: tuple[Path, Path, Path, Path],
    fake_clis: tuple[Path, dict[str, str]],
) -> None:
    _, key_path, known_hosts_path, log_path = workspace
    known_hosts_path.write_text(
        "[44.255.229.140]:22022 ssh-ed25519 AAAAwrongpin\n",
        encoding="utf-8",
    )
    assert run_cli(workspace, fake_clis, "--tool", "gemini_status").returncode == 2
    assert log_records(log_path) == []

    known_hosts_path.write_text(setup.KNOWN_HOSTS_LINE + "\n", encoding="utf-8")
    symlink_key = key_path.with_name("key-symlink")
    target = key_path.with_name("key-target")
    key_path.replace(target)
    symlink_key.symlink_to(target)
    import tomllib

    config_path = workspace[0] / "config.toml"
    document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    entry = document["mcp_servers"]["gemini_research"]
    entry["args"][entry["args"].index(str(key_path)) + 1] = str(symlink_key)
    entry = setup.expected_config(symlink_key, known_hosts_path)
    config_path.write_text(
        "[mcp_servers.gemini_research]\n"
        + f'command = "ssh"\nargs = {json.dumps(entry["args"])}\n'
        + f'startup_timeout_sec = 20\ntool_timeout_sec = 720\n'
        + f'enabled_tools = {json.dumps(list(setup.TOOLS))}\n',
        encoding="utf-8",
    )
    assert run_cli(workspace, fake_clis, "--tool", "gemini_status").returncode == 2
    assert log_records(log_path) == []
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_timeout_closes_process_and_does_not_print_stderr(
    workspace: tuple[Path, Path, Path, Path],
    fake_clis: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, _, log_path = workspace
    monkeypatch.setenv("GEMINI_FAKE_DELAY", "1")
    fake_clis[1]["GEMINI_FAKE_DELAY"] = "1"
    monkeypatch.setattr(call_tool, "TOOL_TIMEOUT_SEC", 0.2)

    code = call_tool.main(
        [
            "--tool",
            "gemini_once",
            "--prompt",
            "delayed",
            "--codex-home",
            str(workspace[0]),
        ]
    )
    captured = capsys.readouterr()
    records = log_records(log_path)

    assert code == 2
    assert "超过 0.2 秒" in captured.err
    assert SECRET_STDERR not in captured.err
    assert records
    pid = records[-1]["pid"]
    status_path = Path(f"/proc/{pid}/status")
    if status_path.exists():
        with status_path.open(encoding="utf-8") as status:
            state = next(line for line in status if line.startswith("State:"))
        assert "Z (zombie)" in state


def test_stdout_is_bounded(
    workspace: tuple[Path, Path, Path, Path],
    fake_clis: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GEMINI_FAKE_OVERSIZE", "1")
    fake_clis[1]["GEMINI_FAKE_OVERSIZE"] = "1"
    monkeypatch.setattr(call_tool, "OUTPUT_LIMIT", 64_000)

    code = call_tool.main(
        [
            "--tool",
            "gemini_once",
            "--prompt",
            "large",
            "--codex-home",
            str(workspace[0]),
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert "安全上限" in captured.err
    assert len(captured.out) < 100


def test_notifications_cannot_extend_the_call_deadline() -> None:
    class Notifications:
        async def readline(self):
            await asyncio.sleep(0.01)
            return b'{"jsonrpc":"2.0","method":"notifications/progress"}\n'

    async def exercise():
        # The outer deadline also keeps a regression from hanging the suite.
        with pytest.raises(call_tool.CallError, match="超过"):
            await asyncio.wait_for(
                call_tool._read_tool_response(Notifications(), 3, 0.08), 0.5
            )

    asyncio.run(exercise())


@pytest.mark.parametrize("count", [20_000, 20_001])
def test_prompt_stdin_chinese_character_limit_is_exact(
    workspace: tuple[Path, Path, Path, Path],
    fake_clis: tuple[Path, dict[str, str]],
    count: int,
) -> None:
    result = run_cli(
        workspace,
        fake_clis,
        "--tool",
        "gemini_once",
        "--prompt-stdin",
        stdin="汉" * count,
    )

    if count == 20_000:
        assert result.returncode == 0
        assert json.loads(result.stdout)["arguments"]["prompt"] == "汉" * count
    else:
        assert result.returncode == 2
        assert "prompt 超过 20,000 字符上限" in result.stderr


def set_tool_result(
    monkeypatch: pytest.MonkeyPatch,
    fake_clis: tuple[Path, dict[str, str]],
    result: dict[str, t.Any],
) -> None:
    encoded = json.dumps(result, ensure_ascii=False)
    monkeypatch.setenv("GEMINI_FAKE_TOOL_RESULT", encoded)
    fake_clis[1]["GEMINI_FAKE_TOOL_RESULT"] = encoded


@pytest.mark.parametrize(
    ("error_text", "expected"),
    [
        (
            "provider diagnostic SECRET-CALL-TOOL-ERROR "
            "[request_id: 01234567-89ab-4def-8123-456789abcdef] "
            "[raw_log_saved: true]",
            {
                "message": "MCP 工具失败",
                "request_id": "01234567-89ab-4def-8123-456789abcdef",
                "raw_log_saved": True,
            },
        ),
        (
            "provider diagnostic SECRET-CALL-TOOL-ERROR "
            "[request_id: 01234567-89ab-4def-8123-456789abcdef] "
            "[raw_log_saved: false]",
            {
                "message": "MCP 工具失败",
                "request_id": "01234567-89ab-4def-8123-456789abcdef",
                "raw_log_saved": False,
            },
        ),
        ("provider diagnostic SECRET-CALL-TOOL-ERROR", {"message": "MCP 工具失败"}),
        (
            "provider diagnostic SECRET-CALL-TOOL-ERROR "
            "[request_id: 01234567-89ab-cdef-8123-456789abcdef] "
            "[raw_log_saved: true]",
            {"message": "MCP 工具失败"},
        ),
    ],
)
def test_text_only_tool_error_uses_fixed_message_and_safe_metadata(
    workspace: tuple[Path, Path, Path, Path],
    fake_clis: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    error_text: str,
    expected: dict[str, t.Any],
) -> None:
    set_tool_result(
        monkeypatch,
        fake_clis,
        {"isError": True, "content": [{"type": "text", "text": error_text}]},
    )

    result = run_cli(
        workspace,
        fake_clis,
        "--tool",
        "gemini_once",
        "--prompt",
        "prompt-must-not-leak",
    )

    assert result.returncode == 1
    assert json.loads(result.stdout) == {"error": expected}
    assert "SECRET-CALL-TOOL-ERROR" not in result.stdout + result.stderr
    assert "prompt-must-not-leak" not in result.stdout + result.stderr


def test_successful_missing_structured_content_remains_rejected(
    workspace: tuple[Path, Path, Path, Path],
    fake_clis: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_tool_result(monkeypatch, fake_clis, {"isError": False, "content": []})

    result = run_cli(workspace, fake_clis, "--tool", "gemini_status")

    assert result.returncode == 2
    assert result.stderr == "错误: MCP 工具未返回 structuredContent\n"
