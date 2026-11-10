"""Native CLI session binding, explicitly not backend execution-model proof."""
from __future__ import annotations

import uuid

EXECUTION_MODEL = "execution_model"
CLI_SESSION_BINDING = "cli_session_binding"
BINDING_SOURCE = "antigravity_cli_stream"
EFFORT_SOURCE = "pinned_cli_argument"
BINDING_KEYS = frozenset({
    "level", "source", "reported_model", "conversation_bound",
    "result_success", "usage_reported", "backend_model_verified",
    "reasoning_effort", "reasoning_effort_source",
})


def _conversation_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def extract_session_binding(
    payloads: list[dict[str, object]],
    *,
    expected_effort: str,
) -> dict[str, object] | None:
    """Require one correlated init/result pair, success and positive usage.

    Native identifiers are compared in memory and never returned. The model is
    the CLI's session declaration, not an asserted inference-engine identity.
    """
    if len(payloads) < 2:
        return None
    if payloads[0].get("event") != "init" or payloads[-1].get("event") != "result":
        return None
    if any(item.get("event") != "step_update" for item in payloads[1:-1]):
        return None
    init = payloads[0].get("init")
    result = payloads[-1].get("result")
    if not isinstance(init, dict) or not isinstance(result, dict):
        return None
    model = init.get("model")
    if not isinstance(model, str) or not model or len(model) > 160:
        return None
    if init.get("effort", expected_effort) != expected_effort:
        return None
    initial_id = _conversation_id(payloads[0].get("conversation_id"))
    final_id = _conversation_id(result.get("conversation_id"))
    if initial_id is None or initial_id != final_id:
        return None
    model_keys = ("model", "model_id", "model_name", "modelId", "modelName",
                  "modelVersion", "actual_model", "actualModel")
    for key in model_keys:
        if key in result and result[key] != model:
            return None
    for item in payloads[1:-1]:
        step = item.get("step_update")
        if not isinstance(step, dict):
            return None
        if step.get("conversation_id") is not None:
            if _conversation_id(step["conversation_id"]) != initial_id:
                return None
        for key in model_keys:
            if key in step and step[key] != model:
                return None
    if (result.get("status") != "SUCCESS" or result.get("error") is not None
            or result.get("failure") is not None or result.get("denied_actions")):
        return None
    response = result.get("response")
    usage = result.get("usage")
    if not isinstance(response, str) or not response.strip() or not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if type(total) is not int or not 0 < total <= 10**12:
        return None
    if any(type(value) is not int or not 0 <= value <= 10**12
           for key, value in usage.items() if key.endswith("_tokens")):
        return None
    return {
        "level": CLI_SESSION_BINDING,
        "source": BINDING_SOURCE,
        "reported_model": model,
        "conversation_bound": True,
        "result_success": True,
        "usage_reported": True,
        "backend_model_verified": False,
    }


def binding_matches(
    evidence: object, expected_model: str | None, expected_effort: str | None
) -> bool:
    """Validate the exact sanitized binding contract; never upgrade its level."""
    if not isinstance(evidence, dict) or set(evidence) != BINDING_KEYS:
        return False
    return (
        isinstance(expected_model, str) and bool(expected_model)
        and isinstance(expected_effort, str) and bool(expected_effort)
        and evidence.get("level") == CLI_SESSION_BINDING
        and evidence.get("source") == BINDING_SOURCE
        and evidence.get("reported_model") == expected_model
        and evidence.get("reasoning_effort") == expected_effort
        and evidence.get("reasoning_effort_source") == EFFORT_SOURCE
        and evidence.get("conversation_bound") is True
        and evidence.get("result_success") is True
        and evidence.get("usage_reported") is True
        and evidence.get("backend_model_verified") is False
    )
