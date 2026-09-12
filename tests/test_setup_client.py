from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import textwrap
import typing as t

import pytest


SETUP_PATH = (
    Path(__file__).parents[1]
    / "skills"
    / "claude-research"
    / "scripts"
    / "setup_client.py"
)
SPEC = importlib.util.spec_from_file_location("setup_client", SETUP_PATH)
assert SPEC and SPEC.loader
setup = importlib.util.module_from_spec(SPEC)
t.cast(t.Any, SPEC.loader).exec_module(setup)


PRIVATE_MARKER = b"PRIVATE-MARKER-MUST-NOT-PRINT"
PUBLIC_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExamplePublicKeyMarker client@claude"
)


@pytest.fixture()
def workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    return tmp_path / "codex", tmp_path / "key", tmp_path / "known_hosts"


@pytest.fixture()
def tool_mocks(
    monkeypatch: pytest.MonkeyPatch, workspace: tuple[Path, Path, Path]
) -> dict[str, t.Any]:
    _, key_path, _ = workspace
    public_path = Path(f"{key_path}.pub")

    def fake_run_tool(command: list[str], *, timeout: int = 20):
        if command[:2] == ["fake-keygen", "-lf"]:
            target = command[2]
            if target.endswith(".pub"):
                return subprocess.CompletedProcess(
                    command,
                    0,
                    "256 SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA ED25519 key\n",
                    "",
                )
            return subprocess.CompletedProcess(
                command,
                0,
                f"25519 {setup.HOST_FINGERPRINT} known\n",
                "",
            )
        if command[:3] == ["fake-keygen", "-t", "ed25519"]:
            key_path.write_bytes(PRIVATE_MARKER)
            key_path.chmod(0o600)
            public_path.write_text(PUBLIC_KEY + "\n")
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(setup, "require_tools", lambda: ("fake-ssh", "fake-keygen"))
    monkeypatch.setattr(setup, "run_tool", fake_run_tool)
    return {}


def run(workspace: tuple[Path, Path, Path], *extra: str) -> int:
    codex_home, key_path, known_hosts_path = workspace
    return setup.main(
        [
            "--principal",
            "partner",
            "--codex-home",
            str(codex_home),
            "--key-path",
            str(key_path),
            "--known-hosts-path",
            str(known_hosts_path),
            *extra,
        ]
    )


def mcp_stdout(
    *,
    principal: str = "partner",
    logged_in: bool = True,
    missing_tool: bool = False,
) -> str:
    tools = [
        {"name": name}
        for name in setup.TOOLS
        if not (missing_tool and name == "claude_close")
    ]
    responses = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "claude-research", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {
                "isError": False,
                "structuredContent": {
                    "status": "ok",
                    "principal": principal,
                    "logged_in": logged_in,
                    "models": {"opus": {"cli_supported": True}},
                    "opus_quota": {
                        "available": False,
                        "source": "claude_code_oauth_usage",
                        "reason": "usage_endpoint_unavailable",
                    },
                },
            },
        },
    ]
    return "\n".join(json.dumps(item, separators=(",", ":")) for item in responses) + "\n"


def test_parse_args_resolves_default_codex_home(monkeypatch, tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    args = setup.parse_args([])

    assert args.codex_home == codex_home.resolve()


def mock_verify(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: str | None = None,
    stderr: str = "",
) -> dict[str, t.Any]:
    captured: dict[str, t.Any] = {}

    def fake_exchange(command):
        captured["command"] = command
        if returncode != 0:
            raise setup._connection_error(returncode, stderr)
        return stdout or ""

    monkeypatch.setattr(setup, "exchange_mcp", fake_exchange)
    return captured


def test_prepare_creates_and_is_idempotent(
    tool_mocks: dict[str, t.Any],
    workspace: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = run(workspace)
    codex_home, key_path, known_hosts_path = workspace
    output = capsys.readouterr().out

    assert code == 0
    assert PRIVATE_MARKER not in output.encode()
    assert PUBLIC_KEY in output
    assert "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" in output
    assert "请管理员授权" in output
    assert key_path.read_bytes() == PRIVATE_MARKER
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert known_hosts_path.read_text() == setup.KNOWN_HOSTS_LINE + "\n"
    assert stat.S_IMODE(known_hosts_path.stat().st_mode) == 0o600

    config_path = codex_home / "config.toml"
    raw = config_path.read_text()
    import tomllib

    parsed = tomllib.loads(raw)
    assert parsed["mcp_servers"]["claude_research"] == setup.expected_config(
        key_path, known_hosts_path
    )
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600

    code = run(workspace)
    assert code == 0
    assert config_path.read_text() == raw


def test_conflicting_server_section_is_rejected(
    tool_mocks: dict[str, t.Any],
    workspace: tuple[Path, Path, Path],
) -> None:
    run(workspace)
    codex_home, _, _ = workspace
    config_path = codex_home / "config.toml"
    raw = config_path.read_text().replace('"ssh"', '"/usr/bin/ssh"')
    config_path.write_text(raw)

    code = run(workspace)
    assert code == 1
    assert config_path.read_text() == raw


def test_legacy_five_tool_config_is_safely_upgraded(
    tool_mocks: dict[str, t.Any],
    workspace: tuple[Path, Path, Path],
) -> None:
    assert run(workspace) == 0
    codex_home, key_path, known_hosts_path = workspace
    config_path = codex_home / "config.toml"
    current = config_path.read_text()
    legacy = current.replace(
        setup.enabled_tools_assignment(setup.TOOLS),
        setup.enabled_tools_assignment(setup.LEGACY_TOOLS),
        1,
    )
    config_path.write_text("# unrelated user setting\n" + legacy + "\n[notice]\nhide = true\n")

    assert run(workspace) == 0
    upgraded = config_path.read_text()
    import tomllib

    parsed = tomllib.loads(upgraded)
    assert parsed["mcp_servers"]["claude_research"] == setup.expected_config(
        key_path, known_hosts_path
    )
    assert upgraded.startswith("# unrelated user setting\n")
    assert upgraded.endswith("[notice]\nhide = true\n")
    assert setup.enabled_tools_assignment(setup.LEGACY_TOOLS) not in upgraded
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_known_hosts_mismatch_fails_closed(
    tool_mocks: dict[str, t.Any],
    workspace: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home, key_path, known_hosts_path = workspace
    known_hosts_path.write_text("attacker host key\n")
    monkeypatch.setattr(
        setup, "KNOWN_HOSTS_LINE", "[44.255.229.140]:22022 ssh-ed25519 expected"
    )

    code = run(workspace)
    assert code == 1
    assert known_hosts_path.read_text() == "attacker host key\n"
    assert not key_path.exists()
    assert not (codex_home / "config.toml").exists()


def test_verify_uses_pinned_ssh_and_parses_responses(
    tool_mocks: dict[str, t.Any],
    workspace: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured = mock_verify(monkeypatch, stdout=mcp_stdout())
    code = run(workspace, "--verify")
    _, key_path, known_hosts_path = workspace
    output = capsys.readouterr().out

    assert code == 0
    assert "verify: 通过" in output
    assert PRIVATE_MARKER not in output.encode()
    assert captured["command"] == [
        "fake-ssh",
        *setup.ssh_args(key_path, known_hosts_path),
    ]
    messages = list(setup.rpc_messages())
    assert [message.get("id") for message in messages] == [1, None, 2, 3]
    assert messages[1]["method"] == "notifications/initialized"
    assert messages[3]["params"]["name"] == "claude_status"


def test_client_inventory_is_exactly_seven_tools() -> None:
    assert setup.TOOLS == (
        "claude_status",
        "claude_once",
        "claude_start",
        "claude_list_conversations",
        "claude_export_conversation",
        "claude_continue",
        "claude_close",
    )


def test_verify_rejects_missing_opus_quota_contract(
    tool_mocks: dict[str, t.Any],
    workspace: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [json.loads(line) for line in mcp_stdout().splitlines()]
    del responses[2]["result"]["structuredContent"]["opus_quota"]
    stdout = "\n".join(json.dumps(item) for item in responses) + "\n"
    mock_verify(monkeypatch, stdout=stdout)

    assert run(workspace, "--verify") == 1


def test_unauthorized_key_reports_admin_action(
    tool_mocks: dict[str, t.Any],
    workspace: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_verify(
        monkeypatch,
        returncode=255,
        stderr="Permission denied (publickey).\n",
    )
    code = run(workspace, "--verify")
    output = capsys.readouterr()
    assert code == 1
    assert PUBLIC_KEY in output.out
    assert PRIVATE_MARKER not in (output.out + output.err).encode()
    assert "服务器尚未授权" in output.err


@pytest.mark.parametrize(
    ("principal", "logged_in", "missing_tool"),
    [("owner", True, False), ("partner", False, False), ("partner", True, True)],
)
def test_verify_rejects_invalid_server_state(
    tool_mocks: dict[str, t.Any],
    workspace: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    principal: str,
    logged_in: bool,
    missing_tool: bool,
) -> None:
    mock_verify(
        monkeypatch,
        stdout=mcp_stdout(
            principal=principal, logged_in=logged_in, missing_tool=missing_tool
        ),
    )
    code = run(workspace, "--verify")
    assert code == 1


def test_real_exchange_sequences_requests_without_closing_stdin(tmp_path: Path) -> None:
    fake_server = tmp_path / "fake_mcp.py"
    fake_server.write_text(
        textwrap.dedent(
            """
            import json
            import sys

            tools = [
                "claude_status", "claude_once", "claude_start",
                "claude_list_conversations", "claude_export_conversation",
                "claude_continue", "claude_close",
            ]
            for line in sys.stdin:
                message = json.loads(line)
                method = message.get("method")
                if method == "initialize":
                    result = {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "serverInfo": {"name": "claude-research", "version": "1"},
                    }
                elif method == "tools/list":
                    result = {"tools": [{"name": name} for name in tools]}
                elif method == "tools/call":
                    result = {
                        "isError": False,
                        "structuredContent": {
                            "status": "ok",
                            "principal": "partner",
                            "logged_in": True,
                            "models": {"opus": {"cli_supported": True}},
                            "opus_quota": {
                                "available": False,
                                "source": "claude_code_oauth_usage",
                                "reason": "usage_endpoint_unavailable",
                            },
                        },
                    }
                else:
                    continue
                print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
            """
        )
    )
    stdout = setup.exchange_mcp([sys.executable, str(fake_server)])
    setup.check_responses(stdout, "partner")


def test_real_exchange_classifies_unauthorized_process() -> None:
    command = [
        sys.executable,
        "-c",
        "import sys; print('Permission denied (publickey).', file=sys.stderr); sys.exit(255)",
    ]
    with pytest.raises(setup.VerifyError, match="服务器尚未授权"):
        setup.exchange_mcp(command)
