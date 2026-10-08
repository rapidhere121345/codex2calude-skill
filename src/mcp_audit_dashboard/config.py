from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


DEFAULT_CLAUDE_DATABASE = Path("/var/lib/mcp-audit/claude.sqlite3")
DEFAULT_GEMINI_DATABASE = Path("/var/lib/mcp-audit/gemini.sqlite3")
DEFAULT_HEALTH_SNAPSHOT = Path("/var/lib/mcp-audit/health.json")
DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "::1")


@dataclass(frozen=True)
class DashboardConfig:
    database_paths: dict[str, Path]
    health_snapshot: Path
    static_dir: Path
    owner_login: str | None
    local_test_no_auth: bool = False
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS


def _nonempty_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value if value else None


def _hosts(value: str) -> tuple[str, ...]:
    hosts = tuple(
        host.strip().lower()
        for host in value.split(",")
        if host.strip()
    )
    return hosts or DEFAULT_ALLOWED_HOSTS


def config_from_environment(
    *,
    static_dir: Path,
    local_test_no_auth: bool = False,
    claude_database: Path | None = None,
    gemini_database: Path | None = None,
    health_snapshot: Path | None = None,
) -> DashboardConfig:
    owner_login = _nonempty_env("MCP_AUDIT_OWNER_LOGIN")
    test_flag = _nonempty_env("MCP_AUDIT_LOCAL_TEST_NO_AUTH")
    explicit_test = local_test_no_auth or test_flag == "1"
    if test_flag is not None and test_flag != "1":
        raise ValueError("MCP_AUDIT_LOCAL_TEST_NO_AUTH must be 1 when set")
    if owner_login is not None and explicit_test:
        raise ValueError("local test mode cannot be combined with owner login")
    if not owner_login and not explicit_test:
        raise ValueError("MCP_AUDIT_OWNER_LOGIN is required outside local test mode")
    if owner_login is not None and (len(owner_login) > 320 or "\x00" in owner_login):
        raise ValueError("invalid owner login")

    return DashboardConfig(
        database_paths={
            "claude": Path(
                _nonempty_env("MCP_AUDIT_CLAUDE_DB")
                or claude_database
                or DEFAULT_CLAUDE_DATABASE
            ),
            "gemini": Path(
                _nonempty_env("MCP_AUDIT_GEMINI_DB")
                or gemini_database
                or DEFAULT_GEMINI_DATABASE
            ),
        },
        health_snapshot=Path(
            _nonempty_env("MCP_AUDIT_HEALTH_SNAPSHOT")
            or health_snapshot
            or DEFAULT_HEALTH_SNAPSHOT
        ),
        static_dir=static_dir,
        owner_login=owner_login,
        local_test_no_auth=explicit_test,
        allowed_hosts=_hosts(
            _nonempty_env("MCP_AUDIT_ALLOWED_HOSTS")
            or ",".join(DEFAULT_ALLOWED_HOSTS)
        ),
    )
