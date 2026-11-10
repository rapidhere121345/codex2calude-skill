"""End-to-end transport integration test for the unified remote-research stack.

Exercises the REAL joined chain with a FAKE Claude CLI only:

    REAL Gateway.call -> REAL privileged_rpc.Broker (local test UID,
    drop_privileges=False) -> REAL provider_rpc.main helper child (fixed
    launcher wrapping ONLY provider_env) -> REAL claude_research_mcp
    config/server/executor -> FAKE claude executable.

This is an ordinary software integration test, not an LLM eval/benchmark.
Selection validation, Settings, dispatch, Gateway.call and the backend tool
functions are NOT mocked. The fake CLI never performs inference, never
touches the network or any account, and only emits lead-authored fixed
output; it records just argv, HOME and booleans (never environment values)
to a test-owned temp JSONL. The fixture rejects every non-Claude provider so
no live Gemini could ever run.

Run with:
    PYTHONPATH=<worktree>/src <venv>/bin/python -m pytest \
        tests/test_remote_research_e2e.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from remote_research_mcp.gateway import Gateway, load_settings
from remote_research_mcp.privileged_rpc import Broker

WORKTREE = Path(__file__).resolve().parents[1]
DEPLOY_CATALOG = WORKTREE / "deploy" / "remote-research-catalog.json"
VENV_PYTHON = Path(sys.executable)

PRINCIPAL = "owner"
PUBLIC_MODEL = "opus-5-5-high"
CLI_VALUE = "claude-opus-5-5"
PROMPT_MARKER = "E2E_TRANSPORT_ONLY"
FAKE_ANSWER = "E2E_TRANSPORT_OK"
FAKE_SESSION_ID = "11111111-1111-4111-8111-111111111111"
FAKE_VERSION = "2.1.280"
# API-key variables that must never leak into the provider CLI child.
FORBIDDEN_API_KEYS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY_HELPER",
    "ANTHROPIC_API_KEY_HELPER_TTL_MS", "CLAUDE_CODE_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

FAKE_CLAUDE_TEMPLATE = '''#!__VENV_PYTHON__
"""Hermetic fake Claude CLI (lead-authored E2E transport fixture).

Records ONLY: argv, HOME, boolean presence of forbidden API-key variables,
and one boolean for the exact lead-authored stdin marker. It never records
any other environment value, has no network or account access, and emits
fixed lead-authored output.
"""
import json
import os
import sys

LOG_PATH = "__LOG_PATH__"
STDIN_MARKER = "__STDIN_MARKER__"

def main() -> int:
    argv = sys.argv[1:]
    try:
        stdin_text = sys.stdin.read()
    except OSError:
        stdin_text = ""
    record = {
        "argv": argv,
        "home": os.environ.get("HOME"),
        "forbidden_api_keys": {
            key: (key in os.environ) for key in __FORBIDDEN_KEYS__},
        "stdin_is_exact_marker": stdin_text == STDIN_MARKER,
    }
    with open(LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\\n")
    if "--version" in argv:
        sys.stdout.write("__FAKE_VERSION__\\n")
        return 0
    if argv[:2] == ["auth", "status"]:
        sys.stdout.write(json.dumps({
            "loggedIn": False, "authMethod": "none",
            "apiProvider": "firstParty"}) + "\\n")
        return 1
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "__FAKE_ANSWER__",
        "session_id": "__FAKE_SESSION__",
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "modelUsage": {"__CLI_VALUE__": {"inputTokens": 0, "outputTokens": 0}},
    }
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\\n")
    return 0

raise SystemExit(main())
'''

HELPER_TEMPLATE = '''#!__VENV_PYTHON__
"""Fixed provider RPC helper launcher (test-owned E2E transport fixture).

Bridges the broker's fixed argv/env contract to the REAL
remote_research_mcp.provider_rpc child. It wraps ONLY provider_env, and only
to point HOME, CLAUDE_BIN, CLAUDE_MCP_WORK_ROOT and
CLAUDE_MCP_STATUSLINE_OBSERVATION at this test's temporary fixture; every
policy/role/telemetry variable is still built by the real provider_env from
this fixture's own catalog copy, and the real provider_rpc still installs
that environment before Settings/dispatch. The guard rejects every
non-Claude provider so this fixture can never launch a live Gemini CLI.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, "__SRC__")

from remote_research_mcp import provider_rpc  # REAL provider helper module

_TMP = Path("__TMP__")
_CATALOG = _TMP / "catalog.json"

# The broker gives the helper a minimal fixed environment, so the temp
# catalog location is baked into this launcher for the real main() to find.
provider_rpc.CATALOG_PATH = _CATALOG
os.environ["REMOTE_RESEARCH_CATALOG"] = str(_CATALOG)

_REAL_PROVIDER_ENV = provider_rpc.provider_env


def _wrapped_provider_env(provider, principal, catalog_path=None):
    env = _REAL_PROVIDER_ENV(
        provider, principal,
        _CATALOG if catalog_path is None else catalog_path)
    if provider == "claude":
        env["HOME"] = str(_TMP / "home")
        env["CLAUDE_BIN"] = str(_TMP / "fake-claude")
        env["CLAUDE_MCP_WORK_ROOT"] = str(_TMP / "work")
        env["CLAUDE_MCP_STATUSLINE_OBSERVATION"] = str(
            _TMP / "state" / "statusline-observation-absent.json")
    return env


provider_rpc.provider_env = _wrapped_provider_env


def main() -> int:
    # Fixture guard: fake-only transport test, no non-Claude provider may run.
    if len(sys.argv) != 3 or sys.argv[1] != "claude":
        sys.stdout.write(
            '{"ok":false,"error":{"category":"invalid_request"}}\\n')
        sys.stdout.flush()
        return 64
    return provider_rpc.main(["provider_rpc", sys.argv[1], sys.argv[2]])


raise SystemExit(main())
'''


def _render(template: str, **tokens: str) -> str:
    text = template
    for name, value in tokens.items():
        text = text.replace(f"__{name}__", value)
    return text


def _write_fixture_catalog(path: Path) -> None:
    """Copy the deployment catalog; Opus 5.5 High stays enabled, Fable stays
    disabled, and every Gemini model is force-disabled so no live Gemini
    could run inside this fake-only transport test."""
    raw = json.loads(DEPLOY_CATALOG.read_text(encoding="utf-8"))
    claude = raw["providers"]["claude"]["models"]
    opus = claude["opus-5-5-high"]
    assert opus["enabled"] is True, "fixture requires original Opus enabled"
    assert opus["cli_value"] == CLI_VALUE, "fixture expects the lead CLI value"
    assert opus["family"] == "opus"
    assert opus["reasoning_effort"] == "high"
    assert claude["fable-5-2-high"]["enabled"] is False, \
        "fixture requires original Fable disabled"
    for spec in raw["providers"]["gemini"]["models"].values():
        spec["enabled"] = False  # fixture guard: no live Gemini could run
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def _flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def _read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture()
def transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Real broker + real gateway over temp fixture state with a fake CLI.

    Decoy API keys are planted in the gateway/broker process environment to
    prove nothing inherits into the provider CLI child; the fake CLI records
    only boolean presence, never values."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-e2e-decoy-must-not-propagate")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "e2e-decoy-oauth-token")

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    work = tmp_path / "work"
    (work / "state").mkdir(parents=True)
    records = tmp_path / "fake-claude-calls.jsonl"

    catalog = tmp_path / "catalog.json"
    _write_fixture_catalog(catalog)

    fake_bin = tmp_path / "fake-claude"
    fake_bin.write_text(_render(
        FAKE_CLAUDE_TEMPLATE,
        VENV_PYTHON=str(VENV_PYTHON),
        LOG_PATH=str(records),
        STDIN_MARKER=PROMPT_MARKER,
        FORBIDDEN_KEYS=repr(list(FORBIDDEN_API_KEYS)),
        FAKE_VERSION=FAKE_VERSION,
        FAKE_ANSWER=FAKE_ANSWER,
        FAKE_SESSION=FAKE_SESSION_ID,
        CLI_VALUE=CLI_VALUE,
    ), encoding="utf-8")
    fake_bin.chmod(0o755)

    helper = tmp_path / "provider-rpc-helper.py"
    helper.write_text(_render(
        HELPER_TEMPLATE,
        VENV_PYTHON=str(VENV_PYTHON),
        SRC=str(WORKTREE / "src"),
        TMP=str(tmp_path),
    ), encoding="utf-8")
    helper.chmod(0o755)

    sock = tmp_path / "provider.sock"
    broker = Broker(sock, helper, allowed_uid=os.getuid(),
                    drop_privileges=False)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=broker.serve, kwargs={"stop_event": stop_event},
        name="e2e-provider-broker", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not sock.exists():
        if time.monotonic() > deadline:
            stop_event.set()
            thread.join(timeout=10)
            raise RuntimeError("broker socket did not appear in time")
        time.sleep(0.05)

    gateway = Gateway(load_settings({
        "REMOTE_RESEARCH_PRINCIPAL": PRINCIPAL,
        "REMOTE_RESEARCH_STATE_ROOT": str(tmp_path / "gateway-state"),
        "REMOTE_RESEARCH_CATALOG": str(catalog),
        "REMOTE_RESEARCH_PROVIDER_SOCK": str(sock),
    }))

    try:
        yield SimpleNamespace(
            gateway=gateway, tmp=tmp_path, records=records, home=home,
            work=work, fake_bin=fake_bin, helper=helper, catalog=catalog)
    finally:
        stop_event.set()
        thread.join(timeout=15)


def test_fixture_rejects_non_claude_provider(transport) -> None:
    """Fixture guard: the temp catalog keeps Opus enabled / Fable disabled
    with Gemini force-disabled, and the fixed helper refuses to launch any
    non-Claude provider, so no live Gemini CLI could ever run here."""
    ns = transport
    fixture = json.loads(ns.catalog.read_text(encoding="utf-8"))
    assert fixture["providers"]["claude"]["default_model"] == PUBLIC_MODEL
    assert fixture["providers"]["claude"]["models"][
        PUBLIC_MODEL]["enabled"] is True
    assert fixture["providers"]["claude"]["models"][
        "fable-5-2-high"]["enabled"] is False
    assert all(spec["enabled"] is False for spec in
               fixture["providers"]["gemini"]["models"].values())

    result = subprocess.run(
        [str(VENV_PYTHON), str(ns.helper), "gemini", PRINCIPAL],
        input=b"{}\n", capture_output=True, timeout=30,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 64
    payload = json.loads(result.stdout.decode().strip())
    assert payload["ok"] is False
    assert payload["error"]["category"] == "invalid_request"
    # Nothing ran: no inference CLI invocation was recorded.
    assert not ns.records.exists()


def test_gateway_broker_provider_rpc_full_transport(transport) -> None:
    """once -> start -> continue through the REAL Gateway, REAL Broker, REAL
    provider_rpc child and REAL claude backend, against the FAKE Claude CLI;
    then REAL Gateway.status. The fake CLI argv/HOME/env evidence and the
    unified-vs-native id mapping are verified from recorded artifacts."""
    ns = transport
    gateway = ns.gateway

    async def flow():
        # The whole flow shares one event loop: the gateway holds per-provider
        # dispatch semaphores, so once/start/continue/status must run inside
        # a single loop as they do in production.
        once_out = await gateway.once(
            PROMPT_MARKER, "claude", PUBLIC_MODEL,
            use_web=False, max_turns=1, timeout_seconds=5)
        start_out = await gateway.start(
            PROMPT_MARKER, "claude", PUBLIC_MODEL, title="E2E transport",
            use_web=False, max_turns=1, timeout_seconds=5)
        continue_out = await gateway.continue_conversation(
            start_out["conversation_id"], PROMPT_MARKER,
            max_turns=1, timeout_seconds=5)
        status_out = await gateway.status("claude")
        return once_out, start_out, continue_out, status_out

    once_out, start_out, continue_out, status_out = asyncio.run(flow())

    # ---- once: fake inference answer with unforged model evidence ----
    assert once_out["status"] == "ok"
    assert once_out["result"] == FAKE_ANSWER
    assert once_out["actual_models"] == [CLI_VALUE]
    assert once_out["provider"] == "claude"
    assert once_out["model"] == PUBLIC_MODEL
    assert once_out["reasoning_effort"] == "high"
    assert once_out["use_web"] is False
    assert once_out["configured_model"] == CLI_VALUE

    # ---- start: new unified route id, never the native backend UUID ----
    assert start_out["result"] == FAKE_ANSWER
    assert start_out["actual_models"] == [CLI_VALUE]
    assert start_out["provider"] == "claude"
    assert start_out["model"] == PUBLIC_MODEL
    assert start_out["reasoning_effort"] == "high"
    assert start_out["title"] == "E2E transport"
    route_id = start_out["conversation_id"]
    assert UUID4_RE.fullmatch(route_id), route_id
    assert route_id != FAKE_SESSION_ID
    route = gateway.store.get(route_id, PRINCIPAL)
    assert route is not None
    backend_id = route["backend_id"]
    assert UUID4_RE.fullmatch(backend_id), backend_id
    assert backend_id != route_id, "gateway must mint its own route UUID"
    start_dump = json.dumps(start_out)
    assert backend_id not in start_dump, "native backend UUID leaked"
    assert FAKE_SESSION_ID not in start_dump, "native session id leaked"

    # ---- continue: same route, stored selection, no model argument ----
    assert continue_out["conversation_id"] == route_id
    assert continue_out["result"] == FAKE_ANSWER
    assert continue_out["actual_models"] == [CLI_VALUE]
    assert continue_out["provider"] == "claude"
    assert continue_out["model"] == PUBLIC_MODEL
    assert continue_out["reasoning_effort"] == "high"
    cont_dump = json.dumps(continue_out)
    assert backend_id not in cont_dump, "native backend UUID leaked"
    assert FAKE_SESSION_ID not in cont_dump, "native session id leaked"

    # ---- status: principal, public catalog ids, logged out, quota unknown ----
    assert status_out["principal"] == PRINCIPAL
    entry = status_out["providers"]["claude"]
    assert entry["status"] == "ok"
    assert entry["default_model"] == PUBLIC_MODEL
    assert set(entry["models"]) == {PUBLIC_MODEL, "fable-5-2-high"}
    assert entry["models"][PUBLIC_MODEL]["enabled"] is True
    assert entry["models"]["fable-5-2-high"]["enabled"] is False
    assert entry["logged_in"] is False
    assert entry["service_available"] is False
    assert entry["service_unavailable_reason"] == "requires_claude_ai_login"
    quota = entry["quota"]
    assert quota["available"] is False
    # Quota is unknown (login-gated): no zero-filled usage numbers, only
    # labels and the availability boolean.
    quota_numbers = [value for value in quota.values()
                     if isinstance(value, (int, float))
                     and not isinstance(value, bool)]
    assert quota_numbers == [], quota

    # ---- fake CLI evidence: argv, HOME, env, stdin, resume ----
    records = _read_records(ns.records)
    inference = [r for r in records if "-p" in r["argv"]]
    assert len(inference) == 3, records
    assert len(records) == 5, records  # 3 inference + version + auth status
    for record in records:
        assert record["home"] == str(ns.home), record
        assert record["forbidden_api_keys"] == {
            key: False for key in FORBIDDEN_API_KEYS}, record
    for record in inference:
        assert record["stdin_is_exact_marker"] is True, record

    once_argv = inference[0]["argv"]
    assert _flag(once_argv, "--model") == CLI_VALUE
    assert _flag(once_argv, "--effort") == "high"
    assert "--resume" not in once_argv

    start_argv = inference[1]["argv"]
    assert _flag(start_argv, "--model") == CLI_VALUE
    assert _flag(start_argv, "--effort") == "high"
    assert "--resume" not in start_argv

    cont_argv = inference[2]["argv"]
    assert _flag(cont_argv, "--model") == CLI_VALUE
    assert _flag(cont_argv, "--effort") == "high"
    assert _flag(cont_argv, "--resume") == FAKE_SESSION_ID

    version_records = [r for r in records if r["argv"] == ["--version"]]
    auth_records = [r for r in records
                    if r["argv"][:2] == ["auth", "status"]]
    assert len(version_records) == 1
    assert len(auth_records) == 1
    # Native fake auth status stays separate from fake inference by design:
    # this is a transport test, not a live availability test.
