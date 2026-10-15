from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import textwrap
import typing as t

import pytest


SETUP_PATH = (
    Path(__file__).parents[1]
    / "skills"
    / "gemini-research"
    / "scripts"
    / "setup_client.py"
)
SPEC = importlib.util.spec_from_file_location("gemini_setup_client", SETUP_PATH)
assert SPEC and SPEC.loader
setup = importlib.util.module_from_spec(SPEC)
t.cast(t.Any, SPEC.loader).exec_module(setup)


PRIVATE_KEY = b"FAKE-GEMINI-PRIVATE-KEY-MATERIAL\n"
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleGeminiPublicKey partner"
CLIENT_FINGERPRINT = "SHA256:" + "A" * 43


@pytest.fixture()
def workspace(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    codex_home = tmp_path / "codex-home"
    key_path = tmp_path / "partner-key"
    known_hosts_path = tmp_path / "known-hosts"
    delivered_key = tmp_path / "delivered-key"
    return codex_home, key_path, known_hosts_path, delivered_key


@pytest.fixture()
def fake_clis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path]:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir(mode=0o700)

    fake_ssh = bin_directory / "fake-ssh"
    fake_ssh.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json
            import os
            import sys

            log_path = os.environ["GEMINI_FAKE_SSH_LOG"]
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(json.dumps({"argv": sys.argv}) + "\\n")

            tools = [
                "gemini_status",
                "gemini_once",
                "gemini_start",
                "gemini_list_conversations",
                "gemini_export_conversation",
                "gemini_continue",
                "gemini_close",
            ]
            missing_tool = os.environ.get("GEMINI_FAKE_MISSING_TOOL")
            if missing_tool:
                tools.remove(missing_tool)

            for line in sys.stdin:
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
                    result = {"tools": [{"name": name} for name in tools]}
                elif method == "tools/call":
                    if os.environ.get("GEMINI_FAKE_STATUS_ERROR") == "1":
                        print(
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": request_id,
                                    "error": {"code": -32000, "message": "status failed"},
                                }
                            ),
                            flush=True,
                        )
                        continue
                    status = {
                        "isError": False,
                        "status": "ok",
                        "principal": os.environ.get(
                            "GEMINI_FAKE_PRINCIPAL", "partner"
                        ),
                        "cli_name": "antigravity",
                        "cli_version": "1.2.14",
                        "reasoning_effort": "max",
                        "auth": {
                            "configured": os.environ.get(
                                "GEMINI_FAKE_AUTH_CONFIGURED", "1"
                            )
                            == "1",
                            "logged_in": os.environ.get(
                                "GEMINI_FAKE_LOGGED_IN", "1"
                            )
                            == "1",
                        },
                        "availability": {
                            "status": os.environ.get(
                                "GEMINI_FAKE_AVAILABILITY", "eligible"
                            ),
                            "eligible": os.environ.get(
                                "GEMINI_FAKE_ELIGIBLE", "1"
                            )
                            == "1",
                        },
                        "models": json.loads(
                            os.environ.get(
                                "GEMINI_FAKE_MODELS",
                                '{"auto": {"cli_supported": true}}',
                            )
                        ),
                    }
                    default_model = os.environ.get("GEMINI_FAKE_DEFAULT_MODEL")
                    if default_model is not None:
                        status["default_model"] = default_model
                        status["model_selection_ready"] = (
                            os.environ.get("GEMINI_FAKE_MODEL_READY", "1") == "1"
                        )
                    result = {"isError": False, "structuredContent": status}
                else:
                    continue
                print(
                    json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}),
                    flush=True,
                )
            """
        ).lstrip("\n"),
        encoding="utf-8",
    )
    fake_ssh.chmod(0o700)

    monkeypatch.setenv("GEMINI_FAKE_SSH_LOG", str(tmp_path / "ssh-log.jsonl"))
    monkeypatch.setenv("PATH", f"{bin_directory}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(setup, "require_tools", lambda: ("fake-ssh", "fake-keygen"))
    monkeypatch.setattr(setup, "run_tool", fake_run_tool)
    return Path("fake-keygen"), fake_ssh


def fake_run_tool(command: list[str], *, timeout: int = 20):
    if command[:2] == ["fake-keygen", "-lf"]:
        target = Path(command[2])
        if target.name == "delivered-key" or target.suffix == ".pub":
            stdout = f"256 {CLIENT_FINGERPRINT} ED25519 key\n"
        else:
            stdout = f"25519 {setup.HOST_FINGERPRINT} known-host\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")
    if command[:2] == ["fake-keygen", "-y"]:
        return subprocess.CompletedProcess(command, 0, PUBLIC_KEY + "\n", "")
    raise AssertionError(f"unexpected command: {command}")


def existing_config() -> str:
    args = "\n".join(
        f"  {json.dumps(item)},"
        for item in (
            "-T",
            "-p",
            "22022",
            "-i",
            "/existing/claude-key",
            f"claudemcp@{setup.HOST}",
        )
    )
    return f'''# existing user configuration
[mcp_servers.claude_research]
command = "ssh"
args = [
{args}
]
startup_timeout_sec = 20
tool_timeout_sec = 900

[notice]
hide = true
'''


def install_workspace(
    workspace: tuple[Path, Path, Path, Path],
    *,
    existing_known_hosts: str | None = None,
) -> bytes:
    codex_home, _, known_hosts_path, delivered_key = workspace
    codex_home.mkdir(mode=0o700)
    config_path = codex_home / "config.toml"
    initial = existing_config().encode("utf-8")
    config_path.write_bytes(initial)
    config_path.chmod(0o600)
    delivered_key.write_bytes(PRIVATE_KEY)
    delivered_key.chmod(0o600)
    if existing_known_hosts is not None:
        known_hosts_path.write_text(existing_known_hosts)
        known_hosts_path.chmod(0o600)
    return initial


def preinstall_gemini_config(
    workspace: tuple[Path, Path, Path, Path],
) -> None:
    codex_home, key_path, known_hosts_path, _ = workspace
    config_path = codex_home / "config.toml"
    config_path.write_text(
        existing_config() + setup.config_block(key_path, known_hosts_path),
        encoding="utf-8",
    )
    config_path.chmod(0o600)


def preinstall_direct_gemini_config(
    workspace: tuple[Path, Path, Path, Path],
) -> None:
    codex_home, key_path, known_hosts_path, _ = workspace
    args = "\n".join(
        f"  {json.dumps(item)},"
        for item in setup.ssh_args(key_path, known_hosts_path)
    )
    tools = ",\n".join(f"  {json.dumps(item)}" for item in setup.TOOLS)
    lines = [
        "[mcp_servers.gemini_research]",
        'command = "ssh"',
        "args = [",
        args,
        "]",
        "startup_timeout_sec = 20",
        "tool_timeout_sec = 900",
        "enabled_tools = [",
        tools,
        "]",
    ]
    (codex_home / "config.toml").write_text(
        existing_config() + "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def test_staged_partner_import_verifies_mcp_without_replacing_claude(
    fake_clis: tuple[Path, Path],
    workspace: tuple[Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_workspace(workspace)
    preinstall_gemini_config(workspace)
    codex_home, key_path, known_hosts_path, delivered_key = workspace
    monkeypatch.delenv("GEMINI_FAKE_MISSING_TOOL", raising=False)

    code = run_import(workspace, "--verify")
    output = capsys.readouterr()

    assert code == 0
    assert "verify: 通过" in output.out
    assert PRIVATE_KEY not in (output.out + output.err).encode()
    assert key_path.read_bytes() == PRIVATE_KEY
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert delivered_key.read_bytes() == PRIVATE_KEY
    assert known_hosts_path.read_text() == setup.KNOWN_HOSTS_LINE + "\n"

    import tomllib

    parsed = tomllib.loads((codex_home / "config.toml").read_text("utf-8"))
    assert parsed["mcp_servers"]["claude_research"]["args"][-1] == (
        f"claudemcp@{setup.HOST}"
    )
    assert parsed["mcp_servers"]["gemini_research"] == setup.expected_config(
        key_path, known_hosts_path
    )

    log_path = Path(os.environ["GEMINI_FAKE_SSH_LOG"])
    invocation = json.loads(log_path.read_text("utf-8").splitlines()[0])["argv"]
    assert invocation[invocation.index("-p") + 1] == "22022"
    assert invocation[invocation.index("-i") + 1] == str(key_path)
    assert invocation[-1] == f"{setup.SSH_USER}@{setup.HOST}"


def run_import(workspace: tuple[Path, Path, Path, Path], *extra: str) -> int:
    codex_home, key_path, known_hosts_path, delivered_key = workspace
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
            "--import-key",
            str(delivered_key),
            *extra,
        ]
    )


def test_partner_import_adds_gemini_without_replacing_claude(
    fake_clis: tuple[Path, Path],
    workspace: tuple[Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_workspace(workspace)
    codex_home, key_path, known_hosts_path, delivered_key = workspace
    monkeypatch.delenv("GEMINI_FAKE_MISSING_TOOL", raising=False)

    code = run_import(workspace, "--verify")
    output = capsys.readouterr()

    assert code == 0
    assert "verify: 通过" in output.out
    assert PRIVATE_KEY not in (output.out + output.err).encode()
    assert key_path.read_bytes() == PRIVATE_KEY
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert Path(f"{key_path}.pub").read_text() == " ".join(PUBLIC_KEY.split()[:2]) + "\n"
    assert delivered_key.read_bytes() == PRIVATE_KEY
    assert known_hosts_path.read_text() == setup.KNOWN_HOSTS_LINE + "\n"
    assert stat.S_IMODE(known_hosts_path.stat().st_mode) == 0o600

    import tomllib

    config_path = codex_home / "config.toml"
    parsed = tomllib.loads(config_path.read_text("utf-8"))
    assert parsed["notice"] == {"hide": True}
    assert parsed["mcp_servers"]["claude_research"] == {
        "command": "ssh",
        "args": [
            "-T",
            "-p",
            "22022",
            "-i",
            "/existing/claude-key",
            f"claudemcp@{setup.HOST}",
        ],
        "startup_timeout_sec": 20,
        "tool_timeout_sec": 900,
    }
    assert parsed["mcp_servers"]["gemini_research"] == setup.expected_config(
        key_path, known_hosts_path
    )
    gemini_args = parsed["mcp_servers"]["gemini_research"]["args"]
    assert gemini_args[gemini_args.index("-p") + 1] == "22022"
    assert gemini_args[-1] == f"{setup.SSH_USER}@{setup.HOST}"
    assert setup.LEGACY_SSH_PORT not in gemini_args
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600

    log_path = Path(os.environ["GEMINI_FAKE_SSH_LOG"])
    invocation = json.loads(log_path.read_text("utf-8").splitlines()[0])["argv"]
    assert Path(invocation[0]).name == "fake-ssh"
    assert invocation[invocation.index("-p") + 1] == "22022"
    assert invocation[invocation.index("-i") + 1] == str(key_path)
    assert f"UserKnownHostsFile={known_hosts_path}" in invocation
    assert invocation[-1] == f"{setup.SSH_USER}@{setup.HOST}"


def test_existing_exact_direct_gemini_config_is_backed_up_and_migrated(
    fake_clis: tuple[Path, Path],
    workspace: tuple[Path, Path, Path, Path],
) -> None:
    initial = install_workspace(workspace)
    preinstall_direct_gemini_config(workspace)
    codex_home, key_path, known_hosts_path, _ = workspace
    direct_registration = (codex_home / "config.toml").read_bytes()

    assert run_import(workspace) == 0

    import tomllib

    parsed = tomllib.loads((codex_home / "config.toml").read_text("utf-8"))
    assert parsed["mcp_servers"]["claude_research"]["args"][-1] == (
        f"claudemcp@{setup.HOST}"
    )
    assert parsed["mcp_servers"]["gemini_research"] == setup.expected_config(
        key_path, known_hosts_path
    )
    proxy_args = parsed["mcp_servers"]["gemini_research"]["args"]
    assert proxy_args[:4] == [
        str(setup.RECONNECT_PROXY),
        "ssh",
        "-F",
        "none",
    ]
    digest = hashlib.sha256(direct_registration).hexdigest()[:16]
    assert (
        codex_home / f"config.toml.pre-gemini-{digest}.bak"
    ).read_bytes() == direct_registration


def test_existing_different_backup_does_not_block_safe_migration(
    fake_clis: tuple[Path, Path],
    workspace: tuple[Path, Path, Path, Path],
) -> None:
    install_workspace(workspace)
    preinstall_direct_gemini_config(workspace)
    codex_home, key_path, known_hosts_path, _ = workspace
    config_path = codex_home / "config.toml"
    before = config_path.read_bytes()
    fixed_backup = codex_home / "config.toml.pre-gemini.bak"
    fixed_backup.write_bytes(b"older unrelated pre-gemini backup\n")
    fixed_backup.chmod(0o600)

    assert run_import(workspace) == 0

    import tomllib

    parsed = tomllib.loads(config_path.read_text("utf-8"))
    assert parsed["mcp_servers"]["claude_research"] is not None
    assert parsed["mcp_servers"]["gemini_research"] == setup.expected_config(
        key_path, known_hosts_path
    )
    assert fixed_backup.read_bytes() == b"older unrelated pre-gemini backup\n"
    digest = hashlib.sha256(before).hexdigest()[:16]
    private_backup = codex_home / f"config.toml.pre-gemini-{digest}.bak"
    assert private_backup.read_bytes() == before
    assert stat.S_IMODE(private_backup.stat().st_mode) == 0o600


def test_config_backup_retry_is_idempotent_for_same_content(
    fake_clis: tuple[Path, Path],
    workspace: tuple[Path, Path, Path, Path],
) -> None:
    install_workspace(workspace)
    codex_home, _, _, _ = workspace
    config_path = codex_home / "config.toml"
    before = config_path.read_bytes()
    digest = hashlib.sha256(before).hexdigest()[:16]
    backup_path = codex_home / f"config.toml.pre-gemini-{digest}.bak"
    backup_path.write_bytes(before)
    backup_path.chmod(0o600)

    assert run_import(workspace) == 0

    assert backup_path.read_bytes() == before


def test_changed_direct_gemini_transport_conflict_is_rejected(
    fake_clis: tuple[Path, Path],
    workspace: tuple[Path, Path, Path, Path],
) -> None:
    install_workspace(workspace)
    preinstall_direct_gemini_config(workspace)
    codex_home, _, _, _ = workspace
    config_path = codex_home / "config.toml"
    before = config_path.read_bytes()
    config_path.write_text(
        config_path.read_text("utf-8").replace(
            f"{setup.SSH_USER}@{setup.HOST}", "other-user@example.invalid"
        ),
        encoding="utf-8",
    )

    assert run_import(workspace) == 1
    assert config_path.read_bytes() == before.replace(
        f"{setup.SSH_USER}@{setup.HOST}".encode("utf-8"),
        b"other-user@example.invalid",
    )


def test_import_rerun_fails_closed_and_preserves_installed_key(
    fake_clis: tuple[Path, Path],
    workspace: tuple[Path, Path, Path, Path],
) -> None:
    install_workspace(workspace)
    preinstall_gemini_config(workspace)
    _, key_path, _, delivered_key = workspace

    assert run_import(workspace) == 0
    installed = key_path.read_bytes()
    config_before = (workspace[0] / "config.toml").read_bytes()
    delivered_key.write_bytes(b"REPLACEMENT-PRIVATE-KEY\n")
    delivered_key.chmod(0o600)

    assert run_import(workspace) == 1
    assert key_path.read_bytes() == installed
    assert (workspace[0] / "config.toml").read_bytes() == config_before


def test_wrong_host_pin_fails_closed_before_key_or_config_change(
    fake_clis: tuple[Path, Path],
    workspace: tuple[Path, Path, Path, Path],
) -> None:
    initial = install_workspace(workspace, existing_known_hosts="attacker host key\n")
    codex_home, key_path, known_hosts_path, delivered_key = workspace

    assert run_import(workspace) == 1
    assert known_hosts_path.read_text() == "attacker host key\n"
    assert not key_path.exists()
    assert not Path(f"{key_path}.pub").exists()
    assert (codex_home / "config.toml").read_bytes() == initial
    assert delivered_key.read_bytes() == PRIVATE_KEY


def test_client_inventory_is_exactly_seven_gemini_tools() -> None:
    assert setup.TOOLS == (
        "gemini_status",
        "gemini_once",
        "gemini_start",
        "gemini_list_conversations",
        "gemini_export_conversation",
        "gemini_continue",
        "gemini_close",
    )


@pytest.mark.parametrize(
    "environment",
    [
        {"GEMINI_FAKE_PRINCIPAL": "owner"},
        {"GEMINI_FAKE_MISSING_TOOL": "gemini_export_conversation"},
        {"GEMINI_FAKE_STATUS_ERROR": "1"},
        {"GEMINI_FAKE_AUTH_CONFIGURED": "0"},
        {"GEMINI_FAKE_LOGGED_IN": "0"},
        {
            "GEMINI_FAKE_AVAILABILITY": "unsupported_location",
            "GEMINI_FAKE_ELIGIBLE": "0",
        },
        {
            "GEMINI_FAKE_AVAILABILITY": "account_ineligible",
            "GEMINI_FAKE_ELIGIBLE": "0",
        },
        {
            "GEMINI_FAKE_AVAILABILITY": "authentication_required",
            "GEMINI_FAKE_ELIGIBLE": "0",
        },
        {"GEMINI_FAKE_AVAILABILITY": "unknown", "GEMINI_FAKE_ELIGIBLE": "0"},
        {"GEMINI_FAKE_AVAILABILITY": "eligible", "GEMINI_FAKE_ELIGIBLE": "0"},
    ],
)
def test_verify_fails_closed_for_invalid_mcp_state(
    fake_clis: tuple[Path, Path],
    workspace: tuple[Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environment: dict[str, str],
) -> None:
    install_workspace(workspace)
    preinstall_gemini_config(workspace)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert run_import(workspace, "--verify") == 1
    output = capsys.readouterr()
    assert PRIVATE_KEY not in (output.out + output.err).encode()


def test_verify_reports_official_location_eligibility_before_reauth(
    fake_clis: tuple[Path, Path, Path, Path],
    workspace: tuple[Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_workspace(workspace)
    preinstall_gemini_config(workspace)
    monkeypatch.setenv("GEMINI_FAKE_AVAILABILITY", "unsupported_location")
    monkeypatch.setenv("GEMINI_FAKE_ELIGIBLE", "0")

    assert run_import(workspace, "--verify") == 1

    output = capsys.readouterr()
    assert "unsupported_location" in output.err
    assert "官方 FAQ" in output.err
    assert "重新 OAuth" in output.err
    assert "远端 Antigravity CLI 认证未配置" not in output.err
    assert "远端 Antigravity CLI 未登录" not in output.err


def configure_explicit_pro_status(
    monkeypatch: pytest.MonkeyPatch,
    *,
    default_model: str,
    model_ready: bool,
    selection_ready: bool | None,
) -> None:
    model = {"cli_supported": True}
    if selection_ready is not None:
        model["selection_ready"] = selection_ready
    monkeypatch.setenv("GEMINI_FAKE_DEFAULT_MODEL", default_model)
    monkeypatch.setenv("GEMINI_FAKE_MODEL_READY", "1" if model_ready else "0")
    monkeypatch.setenv("GEMINI_FAKE_MODELS", json.dumps({"pro": model}))


@pytest.mark.parametrize("selection_ready", [None, "true", 1, 0])
def test_verify_rejects_nonboolean_selection_readiness(selection_ready: object) -> None:
    with pytest.raises(setup.VerifyError):
        setup.check_model_status({
            "default_model": "pro",
            "model_selection_ready": True,
            "models": {"pro": {
                "cli_supported": True,
                "selection_ready": selection_ready,
            }},
        })


def test_verify_accepts_explicit_ready_pro_and_prints_sanitized_summary(
    fake_clis: tuple[Path, Path],
    workspace: tuple[Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_workspace(workspace)
    preinstall_gemini_config(workspace)
    configure_explicit_pro_status(
        monkeypatch,
        default_model="pro",
        model_ready=True,
        selection_ready=True,
    )

    assert run_import(workspace, "--verify") == 0
    output = capsys.readouterr()
    assert "verify: 通过" in output.out
    assert "principal: partner" in output.out
    assert "cli: antigravity 1.2.14" in output.out
    assert "default logical model: pro" in output.out
    assert "effort: max" in output.out
    assert "account" not in output.out.casefold()
    assert "modelNativeSession" not in output.out
    assert "credentials" not in output.out.casefold()


@pytest.mark.parametrize(
    ("model_ready", "selection_ready"),
    [(False, True), (True, False)],
)
def test_verify_rejects_pro_until_explicit_model_is_bound(
    fake_clis: tuple[Path, Path],
    workspace: tuple[Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    model_ready: bool,
    selection_ready: bool | None,
) -> None:
    install_workspace(workspace)
    preinstall_gemini_config(workspace)
    configure_explicit_pro_status(
        monkeypatch,
        default_model="pro",
        model_ready=model_ready,
        selection_ready=selection_ready,
    )

    assert run_import(workspace, "--verify") == 1
    output = capsys.readouterr()
    assert "远端 Pro 模型尚未绑定；请 Owner 完成 OAuth 后核实模型配置" in output.err
    assert "verify: 通过" not in output.out
    log_path = Path(os.environ["GEMINI_FAKE_SSH_LOG"])
    assert len(log_path.read_text("utf-8").splitlines()) == 1


def test_verify_accepts_pro_when_optional_model_selection_ready_is_omitted(
    fake_clis: tuple[Path, Path],
    workspace: tuple[Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_workspace(workspace)
    preinstall_gemini_config(workspace)
    configure_explicit_pro_status(
        monkeypatch,
        default_model="pro",
        model_ready=True,
        selection_ready=None,
    )

    assert run_import(workspace, "--verify") == 0


def test_verify_rejects_unknown_default_model_not_present_in_catalog(
    fake_clis: tuple[Path, Path],
    workspace: tuple[Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_workspace(workspace)
    preinstall_gemini_config(workspace)
    configure_explicit_pro_status(
        monkeypatch,
        default_model="unknown",
        model_ready=True,
        selection_ready=True,
    )

    assert run_import(workspace, "--verify") == 1
    output = capsys.readouterr()
    assert "gemini_status 未提供 unknown 模型配置" in output.err
    assert "verify: 通过" not in output.out


def test_verify_rejects_new_status_auto_placeholder_even_if_flags_claim_ready(
    fake_clis: tuple[Path, Path],
    workspace: tuple[Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_workspace(workspace)
    preinstall_gemini_config(workspace)
    monkeypatch.setenv("GEMINI_FAKE_DEFAULT_MODEL", "auto")
    monkeypatch.setenv("GEMINI_FAKE_MODEL_READY", "1")
    monkeypatch.setenv(
        "GEMINI_FAKE_MODELS",
        json.dumps(
            {
                "auto": {
                    "cli_supported": True,
                    "selection_ready": True,
                }
            }
        ),
    )

    assert run_import(workspace, "--verify") == 1
    output = capsys.readouterr()
    assert "远端 Pro 模型尚未绑定；请 Owner 完成 OAuth 后核实模型配置" in output.err
    assert "verify: 通过" not in output.out


def test_gemini_forced_command_limits_streams_to_four_mib() -> None:
    wrapper = (SETUP_PATH.parents[1] / "scripts" / "stdio-forced-command.sh").read_text()

    assert "GEMINI_MCP_OUTPUT_LIMIT=4194304" in wrapper
    assert "GEMINI_MCP_RESULT_LIMIT=4194304" in wrapper
