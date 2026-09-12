from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_forced_command_uses_root_owned_release_and_empty_environment() -> None:
    wrapper = (ROOT / "scripts" / "stdio-forced-command.sh").read_text()

    assert "exec /usr/bin/env -i" in wrapper
    assert "/opt/claude-research-mcp/current/.venv/bin/claude-research-mcp" in wrapper
    assert "/opt/claude-research-mcp/.venv/bin/claude-research-mcp" not in wrapper
    assert "HOME=/var/lib/claude-mcp" in wrapper
    assert "CLAUDE_BIN=/opt/claude-code/2.1.236/claude" in wrapper
    assert "SSH_ORIGINAL_COMMAND" not in wrapper


def test_public_sshd_is_forced_command_only() -> None:
    config = (ROOT / "deploy" / "sshd_config.claude-mcp").read_text()

    expected = (
        "AllowUsers claudemcp",
        "AuthenticationMethods publickey",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "DisableForwarding yes",
        "AllowTcpForwarding no",
        "AllowStreamLocalForwarding no",
        "PermitTTY no",
        "PermitUserEnvironment no",
        "PermitUserRC no",
    )
    for directive in expected:
        assert directive in config


def test_sshd_service_has_tested_filesystem_and_namespace_hardening() -> None:
    unit = (ROOT / "deploy" / "claude-mcp-sshd.service").read_text()

    expected = (
        "UMask=0077",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectSystem=strict",
        "ReadWritePaths=/var/lib/claude-mcp",
        "ProtectHome=yes",
        "ProtectProc=invisible",
        "NoNewPrivileges=yes",
        "RestrictNamespaces=yes",
    )
    for directive in expected:
        assert directive in unit


def test_authorized_key_example_contains_placeholders_only() -> None:
    example = (ROOT / "examples" / "authorized_keys").read_text()

    assert "OWNER_PUBLIC_KEY" in example
    assert "PARTNER_PUBLIC_KEY" in example
    assert "BEGIN OPENSSH PRIVATE KEY" not in example
    assert example.count('restrict,command="') == 2
