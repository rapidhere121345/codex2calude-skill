"""Integration plumbing for CLI session-binding model evidence."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

from gemini_research_mcp.model_evidence import (
    BINDING_KEYS, CLI_SESSION_BINDING, EFFORT_SOURCE, EXECUTION_MODEL,
    extract_session_binding,
)
from gemini_research_mcp.server import _result, parse_gemini_result
from mcp_audit_dashboard import health

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rpc = _load("rpc_case_tests", ROOT / "tests" / "test_remote_research_rpc.py")
gw = _load("gw_case_tests", ROOT / "tests" / "test_remote_research_gateway.py")
gem = _load("gem_case_tests", ROOT / "tests" / "test_gemini_research.py")
collector = _load("collector_case", ROOT / "scripts" / "collect-audit-health.py")

CID = "a1fb011c-4af2-45cc-9348-e158f5538f54"
MODEL = "gemini-3.8-flash-high"
PUBLIC_MODEL = "gemini-3-8-flash-high"


def _frames() -> list[dict]:
    return [
        {"event": "init", "conversation_id": CID,
         "init": {"model": MODEL, "tools": []}},
        {"event": "step_update", "step_update": {
            "conversation_id": CID, "state": "DONE"}},
        {"event": "result", "result": {
            "conversation_id": CID, "status": "SUCCESS",
            "response": "REMOTE_RESEARCH_ACCEPTANCE_OK\n",
            "usage": {"input_tokens": 10, "output_tokens": 1,
                      "total_tokens": 11}}},
    ]


def _stdout(frames: list[dict]) -> str:
    return "\n".join(json.dumps(frame) for frame in frames)


def _binding_evidence() -> dict:
    evidence = extract_session_binding(_frames(), expected_effort="high")
    assert evidence is not None
    return {**evidence, "reasoning_effort": "high",
            "reasoning_effort_source": EFFORT_SOURCE}


def test_parse_attaches_binding_and_result_reports_effort(tmp_path):
    parsed = parse_gemini_result(_stdout(_frames()))
    assert parsed["actual_models"] == []
    binding = parsed["model_evidence"]
    assert binding["level"] == CLI_SESSION_BINDING
    assert "reasoning_effort" not in binding
    settings = gem._settings(tmp_path / "agy")
    out = _result(parsed, settings=settings, model="pro",
                  actual_models=[], use_web=False)
    assert out["reasoning_effort"] == "high"
    evidence = out["model_evidence"]
    assert set(evidence) == BINDING_KEYS
    assert evidence["reasoning_effort"] == "high"
    assert evidence["reasoning_effort_source"] == EFFORT_SOURCE
    assert evidence["backend_model_verified"] is False
    assert evidence["reported_model"] == MODEL
    assert CID not in json.dumps(evidence)


def test_legacy_parse_and_execution_evidence_unchanged(tmp_path):
    parsed = parse_gemini_result(json.dumps({
        "session_id": "session-1", "response": "answer",
        "stats": {"models": {"gemini-2.5-pro": {
            "tokens": {"input": 1, "output": 2, "total": 3}}}},
    }))
    assert "model_evidence" not in parsed
    assert parsed["actual_models"] == ["gemini-2.5-pro"]
    settings = gem._settings(tmp_path / "agy")
    out = _result(parsed, settings=settings, model="pro",
                  actual_models=parsed["actual_models"], use_web=False)
    assert out["reasoning_effort"] == "high"
    assert out["model_evidence"] == {
        "level": EXECUTION_MODEL,
        "source": "native_result_model_usage",
        "backend_model_verified": True,
    }


def _run_main_catalog(provider, principal, request, catalog_path, capsys):
    line = json.dumps(request).encode() + b"\n"
    buf, w = rpc.make_stdin(line)
    saved_env = dict(os.environ)
    os.environ["REMOTE_RESEARCH_CATALOG"] = str(catalog_path)
    old_stdin = sys.stdin
    sys.stdin = types.SimpleNamespace(buffer=buf)
    try:
        rpc.provider_rpc.main(["provider_rpc", provider, principal])
    finally:
        sys.stdin = old_stdin
        os.environ.clear()
        os.environ.update(saved_env)
        try:
            os.close(w)
        except OSError:
            pass
        buf.close()
    return rpc.last_out(capsys)


def _gemini_once_request() -> dict:
    return {"tool": "gemini_once",
            "arguments": {"prompt": "Reply with REMOTE_RESEARCH_ACCEPTANCE_OK only.",
                          "model": "pro"},
            "selection": rpc.GEMINI_SEL}


def _install_fake_modules(monkeypatch, behavior):
    modules = rpc.fake_provider_modules([], behavior)
    for key, module in modules.items():
        monkeypatch.setitem(sys.modules, key, module)


def test_rpc_binding_succeeds_only_under_binding_catalog(
        monkeypatch, capsys, tmp_path):
    async def fn(**kw):
        return {"actual_models": [],
                "model_evidence": _binding_evidence()}

    _install_fake_modules(monkeypatch, fn)
    out = _run_main_catalog("gemini", "owner", _gemini_once_request(),
                            rpc.CATALOG, capsys)
    assert out["ok"] is True, out

    raw = json.loads((ROOT / "deploy" / "remote-research-catalog.json")
                     .read_text())
    raw["providers"]["gemini"]["models"]["gemini-3-8-flash-high"].pop(
        "evidence_policy", None)
    strict = tmp_path / "catalog-strict.json"
    strict.write_text(json.dumps(raw))
    out = _run_main_catalog("gemini", "owner", _gemini_once_request(),
                            strict, capsys)
    assert out["error"]["category"] == "model_evidence_missing", out


def test_rpc_status_policy_is_overwritten_by_trusted_catalog(
        monkeypatch, capsys):
    async def claude_status(**kw):
        return {"status": "ok",
                "model_evidence_policy": "cli_session_binding"}

    _install_fake_modules(monkeypatch, claude_status)
    rpc.run_main("claude", "owner",
                 {"tool": "claude_status", "arguments": {}})
    out = rpc.last_out(capsys)
    assert out["ok"] is True
    assert out["result"]["model_evidence_policy"] == "execution_model"

    async def gemini_status(**kw):
        return {"status": "ok",
                "model_evidence_policy": "execution_model"}

    _install_fake_modules(monkeypatch, gemini_status)
    rpc.run_main("gemini", "owner",
                 {"tool": "gemini_status", "arguments": {}})
    out = rpc.last_out(capsys)
    assert out["ok"] is True
    assert out["result"]["model_evidence_policy"] == CLI_SESSION_BINDING


def test_gateway_status_reports_binding_policy_and_unverified(tmp_path):
    sock = tmp_path / "provider.sock"
    log = tmp_path / "rpc.jsonl"
    broker = gw.FakeBroker(sock, log)
    try:
        gateway = gw.Gateway(gw.load_settings({
            "REMOTE_RESEARCH_PRINCIPAL": "owner",
            "REMOTE_RESEARCH_STATE_ROOT": str(tmp_path / "state"),
            "REMOTE_RESEARCH_CATALOG": str(gw.CATALOG),
            "REMOTE_RESEARCH_PROVIDER_SOCK": str(sock),
        }))
        out = asyncio.run(gateway.status())
    finally:
        broker.close()
    assert out["principal"] == "owner"
    gemini_meta = out["providers"]["gemini"]["models"][PUBLIC_MODEL]
    assert gemini_meta["evidence_policy"] == CLI_SESSION_BINDING
    assert gemini_meta["verified"] is False
    claude_meta = out["providers"]["claude"]["models"]["opus-5-5-high"]
    assert claude_meta["evidence_policy"] == EXECUTION_MODEL


def test_collector_and_health_reader_keep_only_known_evidence_modes():
    account = collector.sanitize_account({
        "status": "ok", "logged_in": True,
        "model_evidence_policy": CLI_SESSION_BINDING}, "gemini")
    assert account["model_evidence_policy"] == CLI_SESSION_BINDING
    bad = collector.sanitize_account({
        "status": "ok", "model_evidence_policy": "bogus_mode"}, "gemini")
    assert "model_evidence_policy" not in bad

    bridge = health._bridge({"probe_ok": True, "account": {
        "logged_in": True,
        "model_evidence_policy": CLI_SESSION_BINDING,
        "models": {
            PUBLIC_MODEL: {"enabled": True, "verified": False,
                           "evidence_policy": CLI_SESSION_BINDING},
            "bogus-model": {"evidence_policy": CLI_SESSION_BINDING},
        }}}, "gemini")
    clean = bridge["account"]
    assert clean["model_evidence_policy"] == CLI_SESSION_BINDING
    assert clean["models"][PUBLIC_MODEL]["evidence_policy"] == \
        CLI_SESSION_BINDING
    assert "bogus-model" not in clean["models"]

    bad_bridge = health._bridge(
        {"account": {"model_evidence_policy": "bogus_mode"}}, "gemini")
    assert "model_evidence_policy" not in bad_bridge["account"]
    model_bridge = health._bridge({"account": {"models": {
        PUBLIC_MODEL: {"evidence_policy": "bogus_mode"}}}}, "gemini")
    assert "evidence_policy" not in \
        model_bridge["account"]["models"][PUBLIC_MODEL]
