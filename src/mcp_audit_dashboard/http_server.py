from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import ipaddress
import json
import os
from pathlib import Path
import socket
import stat
import struct
from urllib.parse import parse_qs, urlsplit

from .aggregation import aggregate_dashboard, parse_filters
from .config import DashboardConfig
from .health import read_health_snapshot


OWNER_LOGIN_HEADER = "Tailscale-User-Login"
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self'; font-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
STATIC_FILES = {
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
MAX_STATIC_BYTES = 1_048_576


class DashboardRequestHandler(BaseHTTPRequestHandler):
    dashboard_config: DashboardConfig
    server_version = "MCPAuditDashboard"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 15

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self._handle_request(send_body=True)

    def do_HEAD(self) -> None:
        self._handle_request(send_body=False)

    def do_POST(self) -> None:
        self._reject_method()

    def do_PUT(self) -> None:
        self._reject_method()

    def do_PATCH(self) -> None:
        self._reject_method()

    def do_DELETE(self) -> None:
        self._reject_method()

    def do_OPTIONS(self) -> None:
        self._reject_method()

    def do_TRACE(self) -> None:
        self._reject_method()

    def _reject_method(self) -> None:
        if not self._transport_authorized():
            self._error(HTTPStatus.FORBIDDEN, "禁止访问", send_body=False)
            return
        if not self._host_authorized():
            self._error(HTTPStatus.FORBIDDEN, "禁止访问", send_body=False)
            return
        if not self._identity_authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "未授权", send_body=False)
            return
        self._response(
            HTTPStatus.METHOD_NOT_ALLOWED,
            b"",
            "text/plain; charset=utf-8",
            send_body=False,
        )

    def _handle_request(self, *, send_body: bool) -> None:
        if not self._transport_authorized():
            self._error(HTTPStatus.FORBIDDEN, "禁止访问", send_body=send_body)
            return
        if not self._host_authorized():
            self._error(HTTPStatus.FORBIDDEN, "禁止访问", send_body=send_body)
            return
        if not self._identity_authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "未授权", send_body=send_body)
            return

        split = urlsplit(self.path)
        path = split.path
        if path == "/":
            self._serve_file("index.html", "text/html; charset=utf-8", send_body)
            return
        if path in STATIC_FILES:
            filename, content_type = STATIC_FILES[path]
            self._serve_file(filename, content_type, send_body)
            return
        if path == "/api/dashboard":
            self._dashboard_api(send_body=send_body)
            return
        if path == "/healthz":
            payload = {
                "status": "ok",
                "read_only": True,
                "identity_verified": True,
            }
            self._json(HTTPStatus.OK, payload, send_body=send_body)
            return
        self._error(HTTPStatus.NOT_FOUND, "未找到", send_body=send_body)

    def _transport_authorized(self) -> bool:
        if isinstance(self.server, UnixDashboardServer):
            return self._unix_peer_is_root()
        client = self.client_address[0] if self.client_address else ""
        try:
            return ipaddress.ip_address(client).is_loopback
        except ValueError:
            return False

    def _unix_peer_is_root(self) -> bool:
        if not hasattr(socket, "SO_PEERCRED"):
            return False
        try:
            raw = self.connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, 12
            )
            _, uid, _ = struct.unpack("=iII", raw[:12])
            return uid == 0
        except (OSError, struct.error):
            return False

    def _host_authorized(self) -> bool:
        values = self.headers.get_all("Host", [])
        if len(values) != 1:
            return False
        host = _normalize_host(values[0])
        return host in self.dashboard_config.allowed_hosts

    def _identity_authorized(self) -> bool:
        config = self.dashboard_config
        if config.local_test_no_auth:
            return True
        values = self.headers.get_all(OWNER_LOGIN_HEADER, [])
        if len(values) != 1 or config.owner_login is None:
            return False
        return hmac.compare_digest(
            values[0].encode("utf-8"), config.owner_login.encode("utf-8")
        )

    def _dashboard_api(self, *, send_body: bool) -> None:
        try:
            query = parse_qs(
                split.query if (split := urlsplit(self.path)) else "",
                keep_blank_values=True,
                max_num_fields=16,
            )
            bridge, principal, period = parse_filters(query)
        except (ValueError, TypeError):
            self._error(
                HTTPStatus.BAD_REQUEST, "查询参数无效", send_body=send_body
            )
            return
        config = self.dashboard_config
        payload = aggregate_dashboard(
            config.database_paths,
            bridge_filter=bridge,
            principal_filter=principal,
            period=period,
        )
        payload["health"] = read_health_snapshot(config.health_snapshot)
        payload["generated_at"] = _utc_now()
        payload["read_only"] = True
        self._json(HTTPStatus.OK, payload, send_body=send_body)

    def _serve_file(
        self, filename: str, content_type: str, send_body: bool
    ) -> None:
        path = self.dashboard_config.static_dir / filename
        try:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size > MAX_STATIC_BYTES
            ):
                raise OSError
            body = path.read_bytes()
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "未找到", send_body=send_body)
            return
        self._response(
            HTTPStatus.OK, body, content_type, send_body=send_body
        )

    def _json(
        self, status: HTTPStatus, payload: object, *, send_body: bool
    ) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self._response(
            status, body, "application/json; charset=utf-8", send_body=send_body
        )

    def _error(
        self, status: HTTPStatus, message: str, *, send_body: bool
    ) -> None:
        self._json(
            status,
            {"error": message},
            send_body=send_body,
        )

    def _response(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        *,
        send_body: bool,
    ) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Vary", "Host, Tailscale-User-Login")
        self.send_header("X-Read-Only", "1")
        self.end_headers()
        if send_body:
            self.wfile.write(body)


def _normalize_host(value: str) -> str:
    host = value.strip().lower()
    if host.startswith("["):
        return host.partition("]")[0][1:]
    if host.count(":") == 1:
        name, port = host.rsplit(":", 1)
        if port in ("80", "443"):
            return name
    return host


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class UnixDashboardServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, socket_path: Path, handler: type[DashboardRequestHandler]):
        self.socket_path = socket_path
        parent = socket_path.parent
        parent_stat = parent.stat()
        if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_IMODE(parent_stat.st_mode) & 0o077:
            raise PermissionError("UNIX socket parent directory must be mode 0700")
        if socket_path.exists() or socket_path.is_symlink():
            existing = socket_path.lstat()
            if not stat.S_ISSOCK(existing.st_mode):
                raise FileExistsError("refusing to replace non-socket path")
            socket_path.unlink()
        super().__init__(str(socket_path), handler)
        os.chmod(socket_path, 0o600)

    def server_bind(self) -> None:
        self.socket.bind(str(self.socket_path))

    def server_close(self) -> None:
        super().server_close()
        try:
            if self.socket_path.is_socket():
                self.socket_path.unlink()
        except OSError:
            pass


class LoopbackDashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[DashboardRequestHandler],
    ):
        if address[0] not in ("127.0.0.1", "::1"):
            raise ValueError("TCP mode accepts loopback addresses only")
        super().__init__(address, handler)
