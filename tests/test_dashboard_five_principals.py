from datetime import datetime, timezone
import sqlite3

import pytest

from mcp_audit_dashboard.aggregation import aggregate_dashboard, parse_filters


def test_all_five_roles_are_counted_once_and_filter_independently(tmp_path):
    path = tmp_path / "audit.sqlite3"
    now = datetime.now(timezone.utc)
    roles = ("owner", "partner", "partner2", "partner3", "partner4")
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY, principal TEXT, "
            "tool TEXT, model TEXT, status TEXT, duration_ms INTEGER, created_at TEXT, "
            "error_category TEXT, http_status INTEGER, exit_code INTEGER)"
        )
        db.executemany(
            "INSERT INTO audit_log (id,principal,tool,model,status,duration_ms,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [(index, role, "gemini_once", "pro", "success", 10, now.isoformat())
             for index, role in enumerate(roles, 1)],
        )
    result = aggregate_dashboard(
        {"gemini": path}, bridge_filter="total", principal_filter="total",
        period="24h", now=now,
    )
    assert result["metrics"]["total:total"]["calls"] == 5
    assert result["metrics"]["gemini:total"]["inference_calls"] == 5
    for role in roles:
        assert result["metrics"][f"gemini:{role}"]["calls"] == 1
        assert result["metrics"][f"total:{role}"]["calls"] == 1
        parse_filters({"principal": [role]})
        selected = aggregate_dashboard(
            {"gemini": path}, bridge_filter="gemini", principal_filter=role,
            period="24h", now=now,
        )
        assert selected["metrics"]["gemini:total"]["calls"] == 1
        assert [event["principal"] for event in selected["recent_events"]] == [role]
    with pytest.raises(ValueError):
        parse_filters({"principal": ["partner5"]})
