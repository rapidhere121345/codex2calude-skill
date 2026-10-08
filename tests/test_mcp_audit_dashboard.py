from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import threading

import pytest

from mcp_audit_dashboard.aggregation import (
    _reported_models,
    aggregate_dashboard,
    parse_filters,
)
from mcp_audit_dashboard.config import DashboardConfig, config_from_environment
from mcp_audit_dashboard.health import read_health_snapshot
from mcp_audit_dashboard.http_server import (
    DashboardRequestHandler,
    LoopbackDashboardServer,
    UnixDashboardServer,
)
from mcp_audit_dashboard.__main__ import main


NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def make_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
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
               '2026-10-09T00:10:00+00:00', NULL, 11, 5, 'claude_cli_result'),
              ('partner', 'gemini_once', 'pro', 'error', 300,
               '2026-10-09T00:20:00+00:00', 'runtime', NULL, NULL, NULL),
              ('owner', 'claude_once', 'opus', 'success', 200,
               '2026-08-01T00:00:00+00:00', NULL, 999, 999,
               'claude_cli_result'),
              ('owner', 'claude_status', 'opus', 'success', 20,
               '2026-10-09T00:30:00+00:00', NULL, NULL, NULL, NULL);
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
    config = DashboardConfig(
        database_paths={
            "claude": tmp_path / "claude.sqlite3",
            "gemini": tmp_path / "gemini.sqlite3",
        },
        health_snapshot=tmp_path / "health.json",
        static_dir=Path(__file__).parents[1] / "src/mcp_audit_dashboard/static",
        owner_login="owner@example.invalid",
        allowed_hosts=("127.0.0.1",),
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
    assert request(http_server, path="/healthz", owner=None)[0] == 401
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
    assert "http://" not in source


def test_aggregation_does_not_silently_truncate_at_twenty_thousand(tmp_path):
    path=tmp_path/'many.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE audit_log (id INTEGER,principal TEXT,tool TEXT,model TEXT,status TEXT,duration_ms INTEGER,created_at TEXT,error_category TEXT,http_status INTEGER,exit_code INTEGER)')
        db.executemany('INSERT INTO audit_log VALUES (?,"owner","claude_once","opus","success",1,"2026-10-09T00:00:00+00:00",NULL,NULL,NULL)', ((n,) for n in range(20005)))
    result=aggregate_dashboard({'claude':path},bridge_filter='total',principal_filter='total',period='all')
    assert result['metrics']['total:total']['calls']==20005
    assert len(result['recent_events'])==100


def test_filters_have_valid_default_period():
    assert parse_filters({})==('total','total','7d')


def test_health_matches_fifteen_minute_cadence_and_preserves_readiness(tmp_path):
    from datetime import timedelta
    path=tmp_path/'health.json'
    path.write_text(json.dumps({'observed_at':(NOW-timedelta(minutes=16)).isoformat(),'bridges':{'gemini':{'probe_ok':True,'account_ready':False}}}))
    result=read_health_snapshot(path,now=NOW)
    assert result['staleness']=='fresh'
    assert result['bridges']['gemini']['account_ready'] is False
