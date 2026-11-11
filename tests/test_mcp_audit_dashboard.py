from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import threading

import pytest

from mcp_audit_dashboard.aggregation import (
    _reported_models,
    aggregate_dashboard,
    parse_filters,
    parse_event_filters,
)
from mcp_audit_dashboard.config import DashboardConfig, config_from_environment
from mcp_audit_dashboard.health import read_health_snapshot
from mcp_audit_dashboard.http_server import (
    DashboardRequestHandler,
    LoopbackDashboardServer,
    UnixDashboardServer,
)
from mcp_audit_dashboard.__main__ import main


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def make_database(path: Path) -> None:
    recent_inference = (NOW - timedelta(minutes=50)).isoformat()
    recent_error = (NOW - timedelta(minutes=40)).isoformat()
    recent_status = (NOW - timedelta(minutes=30)).isoformat()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            f"""
            CREATE TABLE audit_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              principal TEXT NOT NULL,
              tool TEXT NOT NULL,
              conversation_id TEXT,
              model TEXT,
              status TEXT NOT NULL,
              duration_ms INTEGER,
              created_at TEXT NOT NULL,
              error_category TEXT,
              http_status INTEGER,
              exit_code INTEGER,
              request_id TEXT,
              raw_log_saved INTEGER,
              input_tokens INTEGER,
              output_tokens INTEGER,
              thinking_tokens INTEGER,
              cache_creation_tokens INTEGER,
              cache_read_tokens INTEGER,
              total_tokens INTEGER,
              token_usage_source TEXT,
              reported_models_json TEXT
            );
            INSERT INTO audit_log (
              principal, tool, model, status, duration_ms, created_at,
              error_category, input_tokens, output_tokens, token_usage_source
            ) VALUES
              ('owner', 'claude_once', 'opus', 'success', 100,
               '{recent_inference}', NULL, 11, 5, 'claude_cli_result'),
              ('partner', 'gemini_once', 'pro', 'error', 300,
               '{recent_error}', 'runtime', NULL, NULL, NULL),
              ('owner', 'claude_once', 'opus', 'success', 200,
               '2026-08-01T00:00:00+00:00', NULL, 999, 999,
               'claude_cli_result'),
              ('owner', 'claude_status', 'opus', 'success', 20,
               '{recent_status}', NULL, NULL, NULL, NULL);
            """
        )


def test_dashboard_aggregation_filters_and_unknown_tokens(tmp_path):
    claude = tmp_path / "claude.sqlite3"
    gemini = tmp_path / "gemini.sqlite3"
    make_database(claude)
    make_database(gemini)

    result = aggregate_dashboard(
        {"claude": claude, "gemini": gemini},
        bridge_filter="total",
        principal_filter="total",
        period="24h",
        now=NOW,
    )
    assert result["database_availability"] == {
        "claude": "ok",
        "gemini": "ok",
    }
    total = result["metrics"]["total:total"]
    assert total["calls"] == 6
    assert total["inference_calls"] == 4
    assert total["admin_calls"] == 2
    assert total["successes"] == 4
    assert total["errors"] == 2
    assert total["token_known_calls"] == 2
    assert total["token_known_coverage"] == 0.5
    assert total["token_coverage_denominator"] == "inference_calls"
    assert total["tokens"]["input_tokens"] == {"total": 22, "known_calls": 2}
    assert result["metrics"]["total:partner"]["tokens"]["input_tokens"] == {
        "total": None,
        "known_calls": 0,
    }
    assert len(result["recent_events"]) == 6
    assert set(result["recent_events"][0]) == {
        "bridge",
        "principal",
        "tool",
        "configured_model",
        "reported_models",
        "status",
        "duration_ms",
        "created_at",
        "error_category",
        "http_status",
        "exit_code",
        "token_usage_source",
        "tokens",
    }
    assert "conversation_id" not in result["recent_events"][0]
    assert "request_id" not in result["recent_events"][0]


def test_dashboard_filters_are_strict_and_database_errors_are_generic(tmp_path):
    with pytest.raises(ValueError):
        parse_filters({"bridge": ["https://example.invalid"]})
    result = aggregate_dashboard(
        {"claude": tmp_path / "absent.sqlite3", "gemini": tmp_path / "bad.sqlite3"},
        bridge_filter="claude",
        principal_filter="owner",
        period="7d",
        now=NOW,
    )
    assert result["database_availability"] == {
        "claude": "unavailable",
        "gemini": "unavailable",
    }
    assert result["metrics"]["claude:owner"]["calls"] == 0


def test_health_snapshot_is_sanitized_and_reports_staleness(tmp_path):
    path = tmp_path / "health.json"
    payload = {
        "observed_at": NOW.isoformat(),
        "oauth_access_token": "secret",
        "bridges": {
            "claude": {
                "probe_ok": True,
                "checked_at": NOW.isoformat(),
                "latency_ms": 30,
                "account": {
                    "logged_in": True,
                    "status": "ok",
                    "default_model": "opus",
                    "oauth_token": "secret",
                    "quota": {"available": True, "oauth_token": "secret"},
                },
                "transport": {
                    "probe_kind": "local_forced_command",
                    "initialize_ok": True,
                    "tools_count": 7,
                    "server_version": "1.0",
                    "ssh_banner_ok": True,
                },
            }
        },
        "system": {
            "memory_total_bytes": 1,
            "memory_available_bytes": 2,
            "workers": {"claude": 3, "gemini": 4},
            "private_key": "secret",
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = read_health_snapshot(path, now=NOW)
    serialized = json.dumps(result)
    assert "secret" not in serialized
    assert "oauth" not in serialized
    assert result["staleness"] == "fresh"
    bridge = result["bridges"]["claude"]
    assert bridge["transport"]["probe_kind"] == "local_forced_command"
    assert bridge["transport"]["ssh_banner_ok"] is True
    assert result["system"]["workers"] == {"claude": 3, "gemini": 4}


def test_unified_health_reader_preserves_public_models_and_hides_logged_out_cache(tmp_path):
    path = tmp_path / "health.json"
    timestamp = NOW.isoformat()
    payload = {
        "observed_at": timestamp,
        "bridges": {
            "claude": {
                "probe_ok": True, "account_ready": False,
                "account": {
                    "logged_in": False, "status": "ok",
                    "default_model": "opus-5-5-high",
                    "service_available": False,
                    "service_unavailable_reason": "requires_claude_ai_login",
                    "quota": {
                        "available": True, "source": "claude_code_statusline",
                        "five_hour": {"used_percent": 42, "remaining_percent": 58},
                        "token": "SECRET",
                    },
                    "models": {
                        "opus-5-5-high": {
                            "enabled": True, "verified": False,
                            "cli_supported": True, "selection_ready": False,
                            "reasoning_effort": "high", "token": "SECRET",
                        },
                        "fable-5-2-high": {"enabled": False, "verified": False},
                        "pro": {"enabled": True, "email": "SECRET"},
                    },
                },
                "transport": {"probe_kind": "local_unified_forced_command"},
            },
            "gemini": {
                "probe_ok": True, "account_ready": True,
                "provider_error_category": "busy",
                "account": {
                    "logged_in": True, "default_model": "gemini-3-8-flash-high",
                    "models": {"gemini-3-8-flash-high": {
                        "enabled": True, "verified": True, "reasoning_effort": "high",
                    }},
                    "quota": {
                        "available": True, "source": "antigravity_cli_usage",
                        "observed_at": timestamp, "stale": True,
                        "five_hour": {"used_percent": 0, "remaining_percent": 100,
                                      "resets_at": timestamp, "raw": "SECRET"},
                    },
                },
                "transport": {"probe_kind": "local_unified_forced_command"},
            },
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    bridges = read_health_snapshot(path, now=NOW)["bridges"]
    claude = bridges["claude"]["account"]
    assert claude["quota"] == {
        "available": False, "reason": "requires_claude_ai_login",
        "manual_check_url": "https://claude.ai/usage",
    }
    assert claude["default_model"] == "opus-5-5-high"
    assert claude["models"] == {
        "opus-5-5-high": {
            "enabled": True, "verified": False, "cli_supported": True,
            "selection_ready": False, "reasoning_effort": "high",
        },
        "fable-5-2-high": {"enabled": False, "verified": False},
    }
    gemini = bridges["gemini"]["account"]
    assert gemini["default_model"] == "gemini-3-8-flash-high"
    assert gemini["models"]["gemini-3-8-flash-high"] == {
        "enabled": True, "verified": True, "reasoning_effort": "high",
    }
    assert gemini["quota"]["five_hour"] == {
        "used_percent": 0, "remaining_percent": 100, "resets_at": timestamp,
    }
    assert gemini["quota"]["observed_at"] == timestamp
    assert gemini["quota"]["stale"] is True
    assert bridges["gemini"]["provider_error_category"] == "busy"
    assert "SECRET" not in json.dumps(bridges)


def test_health_snapshot_preserves_probe_quota_and_rejects_unknown_values(tmp_path):
    from claude_research_mcp import quota_probe
    path = tmp_path / "health.json"
    timestamp = NOW.isoformat()
    reasons = sorted(quota_probe.REASONS)
    payload = {
        "observed_at": timestamp,
        "bridges": {
            "claude": {
                "probe_ok": True,
                "account": {
                    "logged_in": True,
                    "quota": {
                        "available": False,
                        "source": "anthropic_rate_limit_headers",
                        "reason": reasons[0],
                        "observed_at": timestamp,
                        "age_seconds": 42,
                        "stale": True,
                        "manual_check_url": "https://claude.ai/usage",
                        "access_token": "SECRET",
                    },
                },
            },
            "gemini": {
                "probe_ok": True,
                "account": {
                    "logged_in": True,
                    "quota": {
                        "available": True,
                        "source": "SECRET_SOURCE",
                        "reason": "SECRET_REASON",
                        "five_hour": {
                            "used_percent": 20, "remaining_percent": 80,
                            "resets_at": timestamp, "raw": "SECRET",
                        },
                    },
                },
            },
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    bridges = read_health_snapshot(path, now=NOW)["bridges"]
    claude_quota = bridges["claude"]["account"]["quota"]
    assert claude_quota["source"] == "anthropic_rate_limit_headers"
    assert claude_quota["reason"] == reasons[0]
    assert claude_quota["observed_at"] == timestamp
    assert claude_quota["age_seconds"] == 42
    assert claude_quota["stale"] is True
    gemini_quota = bridges["gemini"]["account"]["quota"]
    assert "source" not in gemini_quota
    assert "reason" not in gemini_quota
    assert gemini_quota["five_hour"] == {
        "used_percent": 20, "remaining_percent": 80, "resets_at": timestamp,
    }
    assert "SECRET" not in json.dumps(bridges)

    for reason in reasons[1:]:
        payload["bridges"]["claude"]["account"]["quota"]["reason"] = reason
        path.write_text(json.dumps(payload), encoding="utf-8")
        quota = read_health_snapshot(path, now=NOW)["bridges"]["claude"]["account"]["quota"]
        assert quota["reason"] == reason
        assert quota["source"] == "anthropic_rate_limit_headers"


def test_health_snapshot_age_is_unknown_without_timestamp(tmp_path):
    path = tmp_path / "health.json"
    path.write_text("{}", encoding="utf-8")
    result = read_health_snapshot(path, now=NOW)
    assert result["staleness"] == "unknown"
    assert result["source"] == "cached_file_only"


def test_sanitized_snapshot_paths_and_cli_overrides_prefer_explicit_paths(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MCP_AUDIT_OWNER_LOGIN", "owner@example.invalid")
    for name in (
        "MCP_AUDIT_CLAUDE_DB",
        "MCP_AUDIT_GEMINI_DB",
        "MCP_AUDIT_HEALTH_SNAPSHOT",
    ):
        monkeypatch.delenv(name, raising=False)
    config = config_from_environment(static_dir=tmp_path)
    assert config.database_paths["claude"] == Path(
        "/var/lib/mcp-audit/claude.sqlite3"
    )
    assert config.database_paths["gemini"] == Path(
        "/var/lib/mcp-audit/gemini.sqlite3"
    )
    claude = tmp_path / "claude-snapshot.sqlite3"
    gemini = tmp_path / "gemini-snapshot.sqlite3"
    health = tmp_path / "health.json"
    config = config_from_environment(
        static_dir=tmp_path,
        claude_database=claude,
        gemini_database=gemini,
        health_snapshot=health,
    )
    assert config.database_paths == {"claude": claude, "gemini": gemini}
    assert config.health_snapshot == health


def test_reported_models_are_bounded_and_unknown_stays_null():
    assert _reported_models('["opus", 3, ""]') == ["opus"]
    assert _reported_models('["x" * 201]') is None
    assert _reported_models("{not-json") is None
    assert _reported_models(None) is None


def test_environment_no_auth_flag_also_cannot_select_unix_socket(
    monkeypatch, capsys
):
    monkeypatch.setenv("MCP_AUDIT_LOCAL_TEST_NO_AUTH", "1")
    monkeypatch.delenv("MCP_AUDIT_OWNER_LOGIN", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        [
            "dashboard",
            "--unix-socket",
            "/tmp/should-not-bind.sock",
        ],
    )
    with pytest.raises(SystemExit, match="production UNIX socket"):
        main()
    assert not Path("/tmp/should-not-bind.sock").exists()


@pytest.fixture()
def http_server(tmp_path):
    make_database(tmp_path / "claude.sqlite3")
    subscriptions = tmp_path / "subscriptions.json"
    subscriptions.write_text(json.dumps({
        "claude": {"date": "2026-10-12", "kind": "renews", "secret": "OMIT"},
    }))
    config = DashboardConfig(
        database_paths={
            "claude": tmp_path / "claude.sqlite3",
            "gemini": tmp_path / "gemini.sqlite3",
        },
        health_snapshot=tmp_path / "health.json",
        static_dir=Path(__file__).parents[1] / "src/mcp_audit_dashboard/static",
        owner_login="owner@example.invalid",
        allowed_hosts=("127.0.0.1",),
        subscriptions_file=subscriptions,
    )
    DashboardRequestHandler.dashboard_config = config
    server = LoopbackDashboardServer(
        ("127.0.0.1", 0), DashboardRequestHandler
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def request(
    port,
    method="GET",
    path="/",
    *,
    owner="owner@example.invalid",
    host="127.0.0.1",
):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    connection.putrequest(method, path, skip_host=True)
    connection.putheader("Host", host)
    if owner is not None:
        connection.putheader("Tailscale-User-Login", owner)
    connection.endheaders()
    response = connection.getresponse()
    body = response.read()
    headers = dict(response.getheaders())
    connection.close()
    return response.status, headers, body


def test_every_route_requires_exact_owner_identity_and_strict_headers(http_server):
    assert request(http_server, owner=None)[0] == 401
    assert request(http_server, owner="partner@example.invalid")[0] == 401
    assert request(http_server, path="/not-found", owner=None)[0] == 401
    assert request(http_server, path="/static/app.js", owner=None)[0] == 401
    assert request(http_server, path="/static/favicon.svg", owner=None)[0] == 401
    assert request(http_server, path="/static/favicon.svg")[0] == 200
    assert request(http_server, path="/healthz", owner=None)[0] == 401
    assert request(http_server, path="/api/dashboard", owner=None)[0] == 401
    assert request(http_server, path="/api/dashboard", owner="partner@example.invalid")[0] == 401
    assert request(http_server, host="evil.invalid")[0] == 403

    status, headers, body = request(http_server)
    assert status == 200
    assert b"MCP" in body
    assert headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert headers["X-Read-Only"] == "1"
    assert "Set-Cookie" not in headers
    assert headers["Cache-Control"] == "no-store"

    status, _, body = request(http_server, path="/api/dashboard?bridge=claude&principal=owner&period=24h")
    payload = json.loads(body)
    assert status == 200
    assert payload["read_only"] is True
    assert payload["metrics"]["claude:owner"]["calls"] == 2
    assert payload["metrics"]["claude:owner"]["inference_calls"] == 1
    assert payload["metrics"]["claude:owner"]["admin_calls"] == 1
    assert payload["health"]["source"] == "cached_file_only"
    assert payload["subscriptions"]["claude"]["date"] == "2026-10-12"
    assert payload["subscriptions"]["claude"]["kind"] == "renews"
    assert "OMIT" not in body.decode()

    status, _, body = request(http_server, path="/api/dashboard?bridge=evil")
    payload = json.loads(body)
    assert status == 400
    assert set(payload) == {"error"}


def test_api_is_read_only_and_rejects_mutating_methods(http_server):
    status, headers, _ = request(http_server, method="POST", path="/api/dashboard")
    assert status == 405
    assert headers["X-Read-Only"] == "1"
    assert "Set-Cookie" not in headers


def test_unix_socket_is_restricted_and_root_peer_is_enforced(tmp_path):
    runtime = tmp_path / "mcp-audit"
    runtime.mkdir(mode=0o700)
    config = DashboardConfig(
        database_paths={"claude": tmp_path / "c", "gemini": tmp_path / "g"},
        health_snapshot=tmp_path / "health.json",
        static_dir=tmp_path,
        owner_login="owner@example.invalid",
    )
    DashboardRequestHandler.dashboard_config = config
    socket_path = runtime / "http.sock"
    server = UnixDashboardServer(socket_path, DashboardRequestHandler)
    try:
        assert stat_mode(runtime) == 0o700
        assert stat_mode(socket_path) == 0o600
    finally:
        server.server_close()
    assert not socket_path.exists()

    regular = runtime / "blocked"
    regular.write_text("not a socket", encoding="utf-8")
    with pytest.raises(FileExistsError):
        UnixDashboardServer(regular, DashboardRequestHandler)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_so_peercred_checker_accepts_only_root(monkeypatch):
    handler = object.__new__(DashboardRequestHandler)
    class FakeSocket:
        def __init__(self, uid):
            self.uid = uid

        def getsockopt(self, *args):
            return struct.pack("=iII", os.getpid(), self.uid, self.uid)

    handler.connection = FakeSocket(0)
    assert handler._unix_peer_is_root() is True
    handler.connection = FakeSocket(1000)
    assert handler._unix_peer_is_root() is False


def test_explicit_test_mode_cannot_bypass_production_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_AUDIT_OWNER_LOGIN", "owner@example.invalid")
    monkeypatch.setenv("MCP_AUDIT_LOCAL_TEST_NO_AUTH", "1")
    with pytest.raises(ValueError):
        config_from_environment(static_dir=tmp_path)

    monkeypatch.delenv("MCP_AUDIT_LOCAL_TEST_NO_AUTH")
    monkeypatch.delenv("MCP_AUDIT_OWNER_LOGIN")
    with pytest.raises(ValueError):
        config_from_environment(static_dir=tmp_path)


def test_static_assets_are_local_and_dom_safe():
    static = Path(__file__).parents[1] / "src/mcp_audit_dashboard/static"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in static.iterdir()
    )
    assert "innerHTML" not in source
    assert "insertAdjacentHTML" not in source
    assert "document.write" not in source
    assert "https://" not in source
    # SVG's XML namespace is an identifier, not an outgoing asset request.
    assert "http://" not in source.replace("http://www.w3.org/2000/svg", "")


def test_quota_card_dom_hides_unknown_and_logged_out_cache_but_shows_real_zero():
    script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Element {
  constructor(tag) { this.tag = tag; this.textContent = ""; this.children = []; }
  get firstChild() { return this.children[0] || null; }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  setAttribute(name, value) { this[name] = value; }
  addEventListener() {}
}
const ids = new Map();
const document = {
  getElementById(id) {
    if (!ids.has(id)) ids.set(id, new Element(id));
    return ids.get(id);
  },
  createElement(tag) { return new Element(tag); },
};
const source = fs.readFileSync(process.argv[1], "utf8").replace(
  /await loadDashboard\(\);\s*$/, ""
);
const context = { document };
vm.runInNewContext(
  source + "\nglobalThis.healthTest = { healthCard, renderHealth };",
  context
);
const { healthCard, renderHealth } = context.healthTest;
const health = document.getElementById("health-grid");
function texts(element) {
  return [element.textContent, ...element.children.flatMap(texts)].filter(Boolean);
}
healthCard("claude", {
  probe_ok: true,
  account_ready: false,
  transport: { probe_kind: "local_unified_forced_command" },
  account: { logged_in: false, quota: {
    available: true, five_hour: { used_percent: 63 }, stale: true
  } },
}, null);
let output = texts(health.children[0]);
assert.ok(output.includes("统一接口正常"));
assert.ok(output.includes("远端尚未登录，额度当前未知。请在服务器完成登录后刷新。"));
assert.ok(!output.some(value => value.includes("63%")));
assert.ok(!output.some(value => value.includes("额度数据较旧")));

healthCard("claude", {
  probe_ok: false,
  transport: { probe_kind: "local_unified_forced_command" },
  account: { logged_in: null, quota: { available: false } },
}, null);
output = texts(health.children[1]);
assert.ok(output.includes("统一接口失败"));
assert.ok(output.includes("暂未取得 Claude 额度，请等待远端登录或下一次监测更新。"));

healthCard("claude", {
  probe_ok: true,
  transport: { probe_kind: "local_unified_forced_command" },
  account: { logged_in: true, quota: {
    available: true, stale: true, five_hour: { used_percent: 0 }
  } },
}, null);
output = texts(health.children[2]);
assert.ok(output.includes("0%"));
assert.ok(output.includes("额度数据较旧，请以官方账户页面为准。"));
assert.ok(!output.some(value => value.includes("暂未取得 Claude 额度")));

renderHealth({ health: {
  bridges: {}, system: { workers: { claude: 2, gemini: 3 } }
}, subscriptions: {} });
const runtime = document.getElementById("system-health");
assert.equal(
  texts(runtime).filter(value => value.includes("旧入口 worker（不含统一网关）")).length,
  2
);
"""
    static = Path(__file__).parents[1] / "src/mcp_audit_dashboard/static/app.js"
    subprocess.run(["node", "-e", script, str(static)], check=True, capture_output=True, text=True)


def test_quota_card_shows_probe_source_reason_and_observation_without_secrets():
    script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Element {
  constructor(tag) { this.tag = tag; this.textContent = ""; this.children = []; }
  get firstChild() { return this.children[0] || null; }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  setAttribute(name, value) { this[name] = value; }
  addEventListener() {}
}
const ids = new Map();
const document = {
  getElementById(id) {
    if (!ids.has(id)) ids.set(id, new Element(id));
    return ids.get(id);
  },
  createElement(tag) { return new Element(tag); },
};
const source = fs.readFileSync(process.argv[1], "utf8").replace(
  /await loadDashboard\(\);\s*$/, ""
);
const context = { document };
vm.runInNewContext(
  source + "\nglobalThis.healthTest = { healthCard };",
  context
);
const { healthCard } = context.healthTest;
const health = document.getElementById("health-grid");
function texts(element) {
  return [element.textContent, ...element.children.flatMap(texts)].filter(Boolean);
}
const observed = new Date(Date.now() - 400000).toISOString();
healthCard("claude", {
  probe_ok: true,
  account_ready: true,
  transport: { probe_kind: "local_unified_forced_command" },
  account: { logged_in: true, quota: {
    available: true, stale: true, source: "anthropic_rate_limit_headers",
    observed_at: observed, age_seconds: 400, reason: "probe_rate_limited",
    access_token: "SECRET", raw_headers: "SECRET",
    five_hour: { used_percent: 20, remaining_percent: 80,
                 resets_at: new Date(Date.now() + 7200000).toISOString() },
  } },
}, null);
let output = texts(health.children[0]);
assert.ok(output.includes("5 小时窗口"));
assert.ok(output.some(value => value.includes("20%")));
assert.ok(output.includes("额度数据较旧，请以官方账户页面为准。"));
assert.ok(output.some(value => value.includes("Haiku 探测响应头（最多输出 1 个 token）")));
assert.ok(output.some(value => value.includes("观测时间：")));
assert.ok(output.includes("额度状态：额度探测被限流"));
assert.ok(!output.some(value => value.includes("SECRET")));
assert.ok(!output.some(value => value.includes("access_token")));
assert.ok(!output.some(value => value.includes("raw_headers")));

healthCard("claude", {
  probe_ok: true,
  account_ready: true,
  transport: { probe_kind: "local_unified_forced_command" },
  account: { logged_in: true, quota: {
    available: false, source: "anthropic_rate_limit_headers",
    reason: "oauth_expired", observed_at: observed, age_seconds: 400,
  } },
}, null);
output = texts(health.children[1]);
assert.ok(output.includes("额度状态：远端凭据已过期"));
assert.ok(output.some(value => value.includes("Haiku 探测响应头")));
assert.ok(!output.some(value => value.includes("暂未取得 Claude 额度")));
assert.ok(!output.some(value => value.includes("SECRET")));

healthCard("claude", {
  probe_ok: true,
  account_ready: true,
  transport: { probe_kind: "local_unified_forced_command" },
  account: { logged_in: true, quota: {
    available: false, source: "unlisted_source", reason: "SECRET_REASON",
  } },
}, null);
output = texts(health.children[2]);
assert.ok(output.includes("暂未取得 Claude 额度，请等待远端登录或下一次监测更新。"));
assert.ok(!output.some(value => value.includes("SECRET_REASON")));
assert.ok(!output.some(value => value.includes("unlisted_source")));
"""
    static = Path(__file__).parents[1] / "src/mcp_audit_dashboard/static/app.js"
    subprocess.run(["node", "-e", script, str(static)], check=True, capture_output=True, text=True)


def test_aggregation_does_not_silently_truncate_at_twenty_thousand(tmp_path):
    path=tmp_path/'many.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE audit_log (id INTEGER,principal TEXT,tool TEXT,model TEXT,status TEXT,duration_ms INTEGER,created_at TEXT,error_category TEXT,http_status INTEGER,exit_code INTEGER)')
        db.executemany('INSERT INTO audit_log VALUES (?,"owner","claude_once","opus","success",1,"2026-10-09T00:00:00+00:00",NULL,NULL,NULL)', ((n,) for n in range(20005)))
    result=aggregate_dashboard({'claude':path},bridge_filter='total',principal_filter='total',period='all')
    assert result['metrics']['total:total']['calls']==20005
    assert len(result['recent_events'])==20
    assert result['pagination']['total_rows']==20005
    assert result['pagination']['total_pages']==1001


def test_filters_have_valid_default_period():
    assert parse_filters({})==('total','total','7d')


def test_health_matches_fifteen_minute_cadence_and_preserves_readiness(tmp_path):
    from datetime import timedelta
    path=tmp_path/'health.json'
    path.write_text(json.dumps({'observed_at':(NOW-timedelta(minutes=16)).isoformat(),'bridges':{'gemini':{'probe_ok':True,'account_ready':False}}}))
    result=read_health_snapshot(path,now=NOW)
    assert result['staleness']=='fresh'
    assert result['bridges']['gemini']['account_ready'] is False


def test_pagination_covers_both_providers_with_stable_ties(tmp_path):
    paths={name:tmp_path/(name+'.sqlite3') for name in ('claude','gemini')}
    for bridge,path in paths.items():
        make_database(path)
        with sqlite3.connect(path) as db:
            db.execute('DELETE FROM audit_log')
            db.executemany(
                'INSERT INTO audit_log (principal,tool,status,duration_ms,created_at) VALUES (?,?,?,?,?)',
                [('owner',bridge+'_once','success',i,'2026-10-09T00:10:00+00:00') for i in range(61)],
            )
    seen=[]
    for page in range(1,8):
        result=aggregate_dashboard(paths,bridge_filter='total',principal_filter='total',period='all',page=page)
        assert result['pagination']=={'page':page,'page_size':20,'total_rows':122,'total_pages':7,'has_prev':page>1,'has_next':page<7}
        assert result['metrics']['total:total']['inference_calls']==122
        assert result['metrics']['claude:owner']['inference_calls']==61
        seen.extend((r['bridge'],r['duration_ms']) for r in result['recent_events'])
    assert len(seen)==len(set(seen))==122
    assert seen==sorted(seen,reverse=True)
    last=aggregate_dashboard(paths,bridge_filter='total',principal_filter='total',period='all',page=999)
    assert last['pagination']['page']==7
    assert len(last['recent_events'])==2


def test_event_filters_do_not_change_usage_totals(tmp_path):
    path=tmp_path/'claude.sqlite3';make_database(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO audit_log (principal,tool,status,created_at) VALUES ('owner','claude_once','running',?)", ((NOW - timedelta(minutes=20)).isoformat(),))
        db.execute("INSERT INTO audit_log (principal,tool,status,created_at) VALUES ('owner','claude_once','timeout',?)", ((NOW - timedelta(minutes=10)).isoformat(),))
    args={'database_paths':{'claude':path},'bridge_filter':'total','principal_filter':'total','period':'24h','now':NOW}
    base=aggregate_dashboard(**args)
    failed=aggregate_dashboard(**args,event_status='failed',event_kind='inference')
    assert failed['metrics']==base['metrics']
    assert {r['status'] for r in failed['recent_events']}=={'error','timeout'}
    assert failed['pagination']['total_rows']==2
    owner=base['metrics']['claude:owner']
    assert owner['inference_calls']==3
    assert owner['inference_successes']==1
    assert owner['inference_failures']==1
    assert owner['inference_success_rate']==.5
    assert owner['inference_duration_ms_average']==100
    admin=aggregate_dashboard(**args,event_kind='admin')
    assert admin['pagination']['total_rows']==1
    assert admin['recent_events'][0]['tool']=='claude_status'
    running=aggregate_dashboard(**args,event_status='running')
    assert len(running['recent_events'])==1
    empty=aggregate_dashboard(**args,event_status='running',event_kind='admin',page=3)
    assert empty['pagination']=={'page':1,'page_size':20,'total_rows':0,'total_pages':0,'has_prev':False,'has_next':False}


@pytest.mark.parametrize('query',[
    {'page':['0']},{'page':['-1']},{'page':['1.0']},{'page':['1000001']},
    {'page':['１']},{'page':['1','2']},{'page_size':['1000']},
    {'event_status':['error']},{'event_kind':['shell']},{'event_kind':['all','admin']},
])
def test_event_filter_validation(query):
    with pytest.raises(ValueError):parse_event_filters(query)


def test_http_pagination_and_event_filters(http_server):
    status,_,raw=request(http_server,path='/api/dashboard?period=all&page=100&page_size=10&event_kind=inference&event_status=success')
    payload=json.loads(raw)
    assert status==200
    assert payload['pagination']=={'page':1,'page_size':10,'total_rows':2,'total_pages':1,'has_prev':False,'has_next':False}
    assert payload['metrics']['total:total']['calls']==4
    assert payload['recent_events'][0]['tokens']['input_tokens']==11
    assert request(http_server,path='/api/dashboard?page_size=100000')[0]==400
    assert request(http_server,path='/api/dashboard?page=1&page=2')[0]==400
    assert request(http_server,path='/api/dashboard?event_status=failed',owner=None)[0]==401


def test_health_admin_rows_cannot_inflate_inference_token_coverage(tmp_path):
    path=tmp_path/'audit.sqlite3';make_database(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE audit_log SET input_tokens=8000, token_usage_source='claude_cli_result' WHERE tool='claude_status'")
    result=aggregate_dashboard({'claude':path},bridge_filter='total',principal_filter='owner',period='24h',now=NOW)
    metric=result['metrics']['claude:owner']
    assert metric['token_known_coverage']==1
    assert metric['token_known_calls']==1
    assert metric['tokens']['input_tokens']['total']==11
