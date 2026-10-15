from pathlib import Path
import json


ROOT = Path(__file__).resolve().parents[1]


def _gemini_agent_frontmatter(path: Path) -> dict[str, object]:
    agent_text = path.read_text()
    assert agent_text.startswith("---\n")
    end = agent_text.index("\n---\n", 4)
    lines = agent_text[4:end].splitlines()
    result: dict[str, object] = {}
    index = 0
    while index < len(lines):
        key, value = lines[index].split(":", 1)
        if value:
            result[key] = value.strip()
            index += 1
            continue
        index += 1
        values = []
        while index < len(lines) and lines[index].startswith("  - "):
            values.append(lines[index].removeprefix("  - "))
            index += 1
        result[key] = values
    return result


def test_forced_command_uses_root_owned_release_and_empty_environment() -> None:
    wrapper = (ROOT / "scripts" / "stdio-forced-command.sh").read_text()

    assert "exec /usr/bin/env -i" in wrapper
    assert "/opt/claude-research-mcp/current/.venv/bin/claude-research-mcp" in wrapper
    assert "/opt/claude-research-mcp/.venv/bin/claude-research-mcp" not in wrapper
    assert "HOME=/var/lib/claude-mcp" in wrapper
    assert "CLAUDE_BIN=/opt/claude-code/2.1.236/claude" in wrapper
    assert "CLAUDE_MCP_CONTINUE_BURST_LIMIT=50" in wrapper
    assert "CLAUDE_MCP_CONTINUE_BURST_WINDOW_SECONDS=60" in wrapper
    assert "DISABLE_UPDATES=1" in wrapper
    assert "ENABLE_CLAUDEAI_MCP_SERVERS=false" in wrapper
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
        "ExecStartPre=/usr/bin/install -d -m 0755 -o root -g root /run/sshd",
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


def test_gemini_sshd_runtime_dropin_creates_privilege_separation_directory() -> None:
    dropin = (ROOT / "deploy" / "gemini-mcp-runtime-dir.conf").read_text()

    assert "ExecStartPre=/usr/bin/install -d -m 0755 -o root -g root /run/sshd" in dropin


def test_authorized_key_example_contains_placeholders_only() -> None:
    example = (ROOT / "examples" / "authorized_keys").read_text()

    assert "OWNER_PUBLIC_KEY" in example
    assert "PARTNER_PUBLIC_KEY" in example
    assert "BEGIN OPENSSH PRIVATE KEY" not in example
    assert example.count('restrict,command="') == 2


def test_gemini_permissions_proceed_and_allow_only_ordinary_tools() -> None:
    settings = json.loads(
        (ROOT / "deploy" / "gemini-agents" / "settings.json").read_text()
    )

    assert settings["useG1Credits"] is False
    assert settings["toolPermission"] == "always-proceed"
    assert settings["permissions"]["deny"] == []
    assert settings["permissions"]["ask"] == []
    allowed = settings["permissions"]["allow"]
    assert {"read_file(*)", "write_file(*)", "command(*)", "read_url(*)",
            "execute_url(*)", "unsandboxed(*)"} <= set(allowed)
    assert not {
        "invoke_subagent",
        "define_subagent",
        "manage_subagents",
        "browser_subagent",
        "schedule",
    }.intersection(allowed)


def test_gemini_research_agents_use_auto_policy_and_forbid_derivation() -> None:
    offline_path = ROOT / "deploy" / "gemini-agents" / "research-offline.md"
    web_path = ROOT / "deploy" / "gemini-agents" / "research-web.md"
    forbidden_tools = {
        "invoke_subagent",
        "define_subagent",
        "manage_subagents",
        "browser_subagent",
        "schedule",
    }
    required_constraints = (
        "Do not switch models",
        "Never invoke, define, manage, schedule, or otherwise derive another AI",
    )

    offline = _gemini_agent_frontmatter(offline_path)
    web = _gemini_agent_frontmatter(web_path)

    for agent in (offline, web):
        assert agent["mainAgent"] == "true"
        assert agent["model"] == "inherit"
        assert agent["commandExecutionPolicy"] == "auto"
        assert not forbidden_tools.intersection(agent["tools"])
    assert "run_command" in offline["tools"]
    assert "write_to_file" in offline["tools"]
    assert "search_web" not in offline["tools"]
    assert {"search_web", "read_url_content"} <= set(web["tools"])

    for body in (offline_path.read_text(), web_path.read_text()):
        for constraint in required_constraints:
            assert constraint in body
