import asyncio
import re
import sqlite3
import sys

import pytest

from claude_research_mcp import diagnostics, server
from claude_research_mcp.config import load_settings
from claude_research_mcp.executor import ClaudeResultError, ExecutionError


def test_status_failure_returns_the_same_request_id_as_private_audit(tmp_path, monkeypatch):
    settings = load_settings({
        "CLAUDE_MCP_WORK_ROOT": str(tmp_path),
        "CLAUDE_MCP_PRINCIPAL": "owner",
        "CLAUDE_BIN": sys.executable,
    })
    (tmp_path / "state").mkdir(mode=0o700)

    async def failing_run(*args, **kwargs):
        exc = ClaudeResultError("auth", 401, 1)
        diagnostics.attach_raw_error(exc, stdout="private-status-error", stderr="private-stderr")
        raise exc

    monkeypatch.setattr(server, "_run", failing_run)
    app = server.create_app(settings)
    with pytest.raises(ExecutionError) as caught:
        asyncio.run(app._tool_manager._tools["claude_status"].fn())
    message = str(caught.value)
    request_id = re.search(r"request_id: ([0-9a-f-]{36})", message).group(1)
    assert "raw_log_saved: true" in message
    assert "private-status-error" not in message
    assert "private-stderr" not in message
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute(
            "SELECT request_id,error_category,http_status,exit_code,raw_log_saved FROM audit_log"
        ).fetchone() == (request_id, "auth", 401, 1, 1)
