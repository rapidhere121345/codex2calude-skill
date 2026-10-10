from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_raw_log_retention_runs_privately_without_network_or_shell() -> None:
    unit = (ROOT / "deploy/claude-mcp-error-retention.service").read_text()
    for directive in (
        "User=claudemcp",
        "Group=claudemcp",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "PrivateNetwork=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "ReadWritePaths=/var/lib/claude-mcp/research/state/error-diagnostics",
        "ExecStart=/opt/claude-research-mcp/current/.venv/bin/python -I",
    ):
        assert directive in unit
    assert "/bin/sh" not in unit
    timer = (ROOT / "deploy/claude-mcp-error-retention.timer").read_text()
    assert "OnCalendar=daily" in timer
    assert "Persistent=true" in timer
    assert "Unit=claude-mcp-error-retention.service" in timer


def test_pruner_only_calls_fixed_stores_and_never_prints_raw_content() -> None:
    source = (ROOT / "scripts/prune-error-logs.py").read_text()
    assert 'Path("/var/lib/claude-mcp/research")' in source
    assert '("owner", "partner", "partner2")' in source
    assert "prune_raw_errors" in source
    assert "read_text" not in source
    assert "read_bytes" not in source


def test_private_diagnostic_artifacts_are_gitignored() -> None:
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert "**/error-diagnostics/" in ignored
    assert "*.sqlite3" in ignored
    assert "*.sqlite3-*" in ignored
