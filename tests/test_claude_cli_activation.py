"""Service upgrades must reach the executable used by provider requests."""
import json
from pathlib import Path

from remote_research_mcp.provider_rpc import _evidence_error, provider_env


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


def test_configured_zero_output_bucket_does_not_verify_another_output_model():
    result = {
        "actual_models": ["claude-opus-5-5", "claude-opus-5"],
        "model_usage": [
            {"model": "claude-opus-5-5", "input_tokens": 2, "output_tokens": 0},
            {"model": "claude-opus-5", "input_tokens": 2, "output_tokens": 27},
        ],
    }
    assert _evidence_error(result, "claude", "claude-opus-5-5") == \
        "model_evidence_mismatch"


def test_selected_model_output_still_allows_existing_auxiliary_usage_contract():
    result = {
        "actual_models": ["claude-opus-5-5", "claude-haiku-4-5"],
        "model_usage": [
            {"model": "claude-opus-5-5", "output_tokens": 27},
            {"model": "claude-haiku-4-5", "output_tokens": 3},
        ],
    }
    assert _evidence_error(result, "claude", "claude-opus-5-5") is None


def test_verified_fable_output_matches_its_unchanged_execution_policy():
    result = {
        "actual_models": ["claude-fable-5-2"],
        "model_usage": [{"model": "claude-fable-5-2", "output_tokens": 27}],
    }
    assert _evidence_error(result, "claude", "claude-fable-5-2") is None
