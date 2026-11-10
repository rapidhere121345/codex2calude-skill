from copy import deepcopy
import json
from pathlib import Path

import pytest

from gemini_research_mcp.model_evidence import (
    BINDING_KEYS, CLI_SESSION_BINDING, EFFORT_SOURCE,
    binding_matches, extract_session_binding,
)
from remote_research_mcp.catalog import CatalogError, load_catalog
from remote_research_mcp.provider_rpc import _evidence_error

MODEL = "gemini-3.8-flash-high"
NATIVE_ID = "a1fb011c-4af2-45cc-9348-e158f5538f54"


def frames():
    return [
        {"event": "init", "conversation_id": NATIVE_ID,
         "init": {"model": MODEL, "tools": []}},
        {"event": "step_update", "step_update": {
            "conversation_id": NATIVE_ID, "state": "DONE",
        }},
        {"event": "result", "result": {
            "conversation_id": NATIVE_ID, "status": "SUCCESS",
            "response": "REMOTE_RESEARCH_ACCEPTANCE_OK\n",
            "usage": {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11},
        }},
    ]


def full_binding():
    evidence = extract_session_binding(frames(), expected_effort="high")
    assert evidence is not None
    return {**evidence, "reasoning_effort": "high",
            "reasoning_effort_source": EFFORT_SOURCE}


def test_correlated_binding_is_not_execution_proof_and_hides_native_ids():
    evidence = full_binding()
    assert set(evidence) == BINDING_KEYS
    assert binding_matches(evidence, MODEL, "high")
    assert evidence["backend_model_verified"] is False
    assert NATIVE_ID not in json.dumps(evidence)
    assert "REMOTE_RESEARCH_ACCEPTANCE_OK" not in json.dumps(evidence)
    assert _evidence_error({"actual_models": [], "model_evidence": evidence},
                           "gemini", MODEL) == "model_evidence_missing"
    assert _evidence_error({"actual_models": [], "model_evidence": evidence},
                           "gemini", MODEL, evidence_policy=CLI_SESSION_BINDING,
                           reasoning_effort="high") is None


@pytest.mark.parametrize("mutation", [
    "missing_init", "duplicate_init", "duplicate_result", "trailing_event",
    "missing_model", "wrong_effort", "different_conversation", "step_conversation",
    "step_model_change", "result_model_change", "unknown_event", "failed_result", "denied_action",
    "blank_response", "missing_usage", "zero_usage", "boolean_usage", "negative_usage",
])
def test_incomplete_ambiguous_or_failed_streams_never_produce_binding(mutation):
    data = frames()
    init, step, result = data[0]["init"], data[1]["step_update"], data[-1]["result"]
    if mutation == "missing_init":
        data.pop(0)
    elif mutation == "duplicate_init":
        data.insert(1, deepcopy(data[0]))
    elif mutation == "duplicate_result":
        data.append(deepcopy(data[-1]))
    elif mutation == "trailing_event":
        data.append({"event": "step_update"})
    elif mutation == "missing_model":
        init.pop("model")
    elif mutation == "wrong_effort":
        init["effort"] = "low"
    elif mutation == "different_conversation":
        result["conversation_id"] = "e3fcb978-7662-437e-969b-18d4cfb6523d"
    elif mutation == "step_conversation":
        step["conversation_id"] = "e3fcb978-7662-437e-969b-18d4cfb6523d"
    elif mutation == "step_model_change":
        step["model"] = "gemini-4-argon-high"
    elif mutation == "result_model_change":
        result["modelName"] = "gemini-4-argon-high"
    elif mutation == "unknown_event":
        data[1]["event"] = "model_change"
    elif mutation == "failed_result":
        result["status"] = "ERROR"
    elif mutation == "denied_action":
        result["denied_actions"] = ["run_command"]
    elif mutation == "blank_response":
        result["response"] = " \n"
    elif mutation == "missing_usage":
        result.pop("usage")
    elif mutation == "zero_usage":
        result["usage"]["total_tokens"] = 0
    elif mutation == "boolean_usage":
        result["usage"]["total_tokens"] = True
    elif mutation == "negative_usage":
        result["usage"]["input_tokens"] = -1
    assert extract_session_binding(data, expected_effort="high") is None


def test_binding_cannot_override_strict_models_or_claude():
    evidence = full_binding()
    assert _evidence_error({"actual_models": ["gemini-4-argon-high"],
                           "model_evidence": evidence}, "gemini", MODEL,
                           evidence_policy=CLI_SESSION_BINDING,
                           reasoning_effort="high") == "model_evidence_mismatch"
    assert _evidence_error({"actual_models": [], "model_evidence": evidence},
                           "claude", MODEL, evidence_policy=CLI_SESSION_BINDING,
                           reasoning_effort="high") == "model_evidence_missing"
    assert _evidence_error({"actual_models": ["claude-opus-5-5"]},
                           "claude", "claude-opus-5-5") is None
    assert _evidence_error({"actual_models": ["claude-opus-5-5-20261101"]},
                           "claude", "claude-opus-5-5") is None


@pytest.mark.parametrize("key,value", [
    ("backend_model_verified", True), ("conversation_bound", 1),
    ("result_success", False), ("usage_reported", False),
    ("source", "caller_supplied"), ("level", "execution_model"),
    ("reasoning_effort_source", "assumed"), ("native_session_id", NATIVE_ID),
])
def test_forged_or_leaky_evidence_shape_is_rejected(key, value):
    evidence = full_binding()
    evidence[key] = value
    assert not binding_matches(evidence, MODEL, "high")


@pytest.mark.parametrize("key,value", [
    ("reported_model", "gemini-4-argon-high"), ("reasoning_effort", "low"),
])
def test_binding_mismatch_is_never_silently_accepted(key, value):
    evidence = full_binding()
    evidence[key] = value
    assert _evidence_error({"actual_models": [], "model_evidence": evidence},
                           "gemini", MODEL, evidence_policy=CLI_SESSION_BINDING,
                           reasoning_effort="high") == "model_evidence_mismatch"


def test_catalog_policy_defaults_strict_and_binding_cannot_claim_verified(tmp_path):
    source = Path(__file__).parents[1] / "deploy/remote-research-catalog.json"
    raw = json.loads(source.read_text())
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(raw))
    catalog = load_catalog(path)
    assert catalog.resolve("claude", None).evidence_policy == "execution_model"
    spec = catalog.resolve("gemini", None)
    assert spec.evidence_policy == CLI_SESSION_BINDING
    assert spec.verified is False
    raw["providers"]["gemini"]["models"]["gemini-3-8-flash-high"]["verified"] = True
    path.write_text(json.dumps(raw))
    with pytest.raises(CatalogError):
        load_catalog(path)
    raw["providers"]["gemini"]["models"]["gemini-3-8-flash-high"]["verified"] = False
    raw["providers"]["claude"]["models"]["opus-5-5-high"]["evidence_policy"] = CLI_SESSION_BINDING
    path.write_text(json.dumps(raw))
    with pytest.raises(CatalogError):
        load_catalog(path)
