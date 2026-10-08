from __future__ import annotations

import argparse
from pathlib import Path

from .config import config_from_environment
from .http_server import (
    DashboardRequestHandler,
    LoopbackDashboardServer,
    UnixDashboardServer,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Owner-only read-only MCP audit dashboard"
    )
    transport = parser.add_mutually_exclusive_group(required=True)
    transport.add_argument(
        "--unix-socket",
        type=Path,
        help="production AF_UNIX socket (root peer required)",
    )
    transport.add_argument(
        "--listen",
        choices=("127.0.0.1", "::1"),
        help="explicit loopback development/test listener",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--claude-db",
        type=Path,
        help="sanitized Claude audit_log SQLite snapshot",
    )
    parser.add_argument(
        "--gemini-db",
        type=Path,
        help="sanitized Gemini audit_log SQLite snapshot",
    )
    parser.add_argument(
        "--health-snapshot",
        type=Path,
        help="cached sanitized health JSON snapshot",
    )
    parser.add_argument(
        "--local-test-no-auth",
        action="store_true",
        help="explicit no-auth mode; loopback TCP only, never production",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.port < 0 or args.port > 65535:
        raise SystemExit("port must be between 0 and 65535")
    static_dir = Path(__file__).with_name("static")
    config = config_from_environment(
        static_dir=static_dir,
        local_test_no_auth=args.local_test_no_auth,
        claude_database=args.claude_db,
        gemini_database=args.gemini_db,
        health_snapshot=args.health_snapshot,
    )
    if args.unix_socket is not None and config.local_test_no_auth:
        raise SystemExit("no-auth test mode cannot use the production UNIX socket")
    DashboardRequestHandler.dashboard_config = config
    if args.unix_socket is not None:
        server = UnixDashboardServer(args.unix_socket, DashboardRequestHandler)
    else:
        server = LoopbackDashboardServer(
            (args.listen, args.port), DashboardRequestHandler
        )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
