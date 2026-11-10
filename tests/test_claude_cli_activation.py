"""Service upgrades must reach the executable used by provider requests."""
import json
from pathlib import Path

from remote_research_mcp.provider_rpc import provider_env


CATALOG = Path(__file__).parents[1] / "deploy" / "remote-research-catalog.json"


def test_service_uses_root_managed_native_version_pointer():
    env = provider_env("claude", "owner", CATALOG)
    assert env["CLAUDE_BIN"] == "/opt/claude-code/current/claude"
    assert env["PATH"].split(":")[0] == "/opt/claude-code/current"


def test_enabled_fable_requires_supported_cli_and_keeps_high(tmp_path):
    catalog = json.loads(CATALOG.read_text())
    fable = catalog["providers"]["claude"]["models"]["fable-5-2-high"]
    fable["enabled"] = True
    target = tmp_path / "catalog.json"
    target.write_text(json.dumps(catalog))
    env = provider_env("claude", "owner", target)
    assert env["CLAUDE_MCP_REQUIRED_CLI_VERSION"] == "2.1.290"
    policy = json.loads(env["CLAUDE_MCP_MODELS"])["fable52"]
    assert policy["cli_value"] == "claude-fable-5-2"
    assert policy["reasoning_effort"] == "high"
