"""Root-managed Unix-socket broker for provider RPC dispatch.

One bounded JSON envelope per connection from the researchmcp UID; provider,
principal, tool and selection are validated before any spawn. Fixed helper
argv dropped to the provider account via subprocess user/group. Bounded
admission (8) with a fixed worker pool; absolute deadlines per phase.
"""
from __future__ import annotations

import json
import os
import pwd
import selectors
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .catalog import ALLOWED_PRINCIPALS, PROVIDERS
from .provider_rpc import PROVIDER_TOOLS

SOCKET_PATH = Path("/run/remote-research/provider.sock")
MAX_FRAME = 32 * 1024 * 1024
HEADER_DEADLINE = 5.0
MAX_HANDLERS = 8
CLEANUP_GRACE = 15.0
PROVIDER_ACCOUNTS = {"claude": "claudemcp", "gemini": "geminimcp"}
OP_TIMEOUT = {"status": 60.0, "export": 120.0, "close": 60.0,
              "list": 60.0}


class BrokerError(ValueError):
    pass


def _peer_uid(conn: socket.socket) -> int:
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                            struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def _read_header(conn: socket.socket,
                 clock: Callable[[], float] = time.monotonic) -> bytes | None:
    """Read one bounded JSON header line with an absolute deadline.

    Returns the line bytes, or None on timeout/EOF/oversize. Extra bytes
    after the first newline inside one chunk are rejected: no legitimate
    caller pipelines a second frame, and silently dropping one would let
    the remainder be mistaken for a later request.
    """
    deadline = clock() + HEADER_DEADLINE
    data = b""
    while b"\n" not in data:
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        conn.settimeout(remaining)
        try:
            chunk = conn.recv(65536)
        except (socket.timeout, TimeoutError, OSError):
            return None
        if not chunk:
            return None
        data += chunk
        if len(data) > MAX_FRAME:
            return None
    line, extra = data.split(b"\n", 1)
    if extra:
        return None
    return line + b"\n"


def validate_envelope(raw: bytes) -> tuple[str, str, str, dict,
                                           dict | None, str, str]:
    """Return provider, principal, tool, arguments, selection, op and
    request_id for one validated envelope, or raise BrokerError."""
    if not raw or len(raw) > MAX_FRAME:
        raise BrokerError("bad frame")
    try:
        obj = json.loads(raw)
    except ValueError:
        raise BrokerError("bad frame") from None
    if not isinstance(obj, dict):
        raise BrokerError("bad frame")
    provider = obj.get("provider")
    principal = obj.get("principal")
    tool = obj.get("tool")
    arguments = obj.get("arguments", {})
    selection = obj.get("selection")
    request_id = obj.get("request_id")
    if provider not in PROVIDERS or principal not in ALLOWED_PRINCIPALS \
            or not isinstance(tool, str) \
            or tool not in PROVIDER_TOOLS.get(provider, {}).values() \
            or not isinstance(arguments, dict):
        raise BrokerError("rejected request")
    if selection is not None and not isinstance(selection, dict):
        raise BrokerError("rejected request")
    if request_id is not None and not isinstance(request_id, str):
        raise BrokerError("rejected request")
    if len(json.dumps(arguments)) > MAX_FRAME:
        raise BrokerError("bad frame")
    op = next(k for k, v in PROVIDER_TOOLS[provider].items() if v == tool)
    return provider, principal, tool, arguments, selection, op, request_id


def _request_budget(op: str, arguments: dict) -> float:
    if op in OP_TIMEOUT:
        return OP_TIMEOUT[op]
    try:
        budget = float(arguments.get("timeout_seconds", 900))
    except (TypeError, ValueError):
        budget = 900.0
    return min(max(budget, 1.0), 3600.0) + 120.0


class Broker:
    def __init__(self, socket_path: Path, helper_path: Path,
                 allowed_uid: int, drop_privileges: bool = True):
        self.socket_path = Path(socket_path)
        self.helper_path = Path(helper_path)
        self.allowed_uid = allowed_uid
        self.drop_privileges = drop_privileges
        self._slots = threading.BoundedSemaphore(MAX_HANDLERS)
        self._pool = ThreadPoolExecutor(
            max_workers=MAX_HANDLERS, thread_name_prefix="rrpc")
        self._accounts: dict[str, tuple[int, int]] = {}
        # Admitted connections (bounded by MAX_HANDLERS) so stop can shut
        # them down and let queued workers finish cleanup promptly.
        self._active_lock = threading.Lock()
        self._active_conns: set[socket.socket] = set()
        if drop_privileges:
            for provider, account in PROVIDER_ACCOUNTS.items():
                try:
                    pw = pwd.getpwnam(account)
                except KeyError as exc:
                    raise BrokerError(f"missing account {account}") from exc
                self._accounts[provider] = (pw.pw_uid, pw.pw_gid)

    def _spawn(self, provider: str, principal: str) -> subprocess.Popen:
        argv = [str(self.helper_path), provider, principal]
        kwargs: dict = {
            "stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL, "bufsize": 0, "close_fds": True,
            "env": {"PATH": "/usr/local/bin:/usr/bin:/bin",
                    "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        }
        if self.drop_privileges:
            uid, gid = self._accounts[provider]
            kwargs.update(user=uid, group=gid, extra_groups=[])
        return subprocess.Popen(argv, **kwargs)

    def _finish_helper(self, proc: subprocess.Popen) -> None:
        """Supervise one helper to completion and close its pipe handles.

        Closing stdin first is the cancellation signal for the helper;
        the cleanup grace is supervision-only and never shortens an
        accepted execution budget."""
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=CLEANUP_GRACE)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        finally:
            # Both pipe handles are closed exactly once here, after the
            # helper is reaped; stdout was previously never closed.
            for stream in (proc.stdout, proc.stdin):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass

    # ---- request write phase: nonblocking stdin, bounded by deadline ----
    def _write_request(self, proc: subprocess.Popen, request: bytes,
                       conn: socket.socket, deadline: float) -> bool:
        fd = proc.stdin.fileno()
        os.set_blocking(fd, False)
        sel = selectors.DefaultSelector()
        sel.register(fd, selectors.EVENT_WRITE, "stdin")
        sel.register(conn, selectors.EVENT_READ, "client")
        view = memoryview(request)
        try:
            while view:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                events = sel.select(min(1.0, remaining))
                if not events:
                    continue
                for key, _ in events:
                    if key.data == "client":
                        # ANY client readiness after the single header —
                        # extra bytes or EOF — cancels immediately, the
                        # same rule as the response phase. Waiting for
                        # more stdin writability while a non-reading peer
                        # holds readiness would busy-spin.
                        return False
                    try:
                        n = os.write(fd, view[:65536])
                    except BlockingIOError:
                        continue
                    except OSError:
                        return False
                    view = view[n:]
            return True
        finally:
            sel.close()
            try:
                os.set_blocking(fd, True)
            except OSError:
                pass

    # ---- response phase: helper stdout vs client, absolute deadline ----
    def _read_response(self, proc: subprocess.Popen, conn: socket.socket,
                       deadline: float) -> bytes | None:
        """Returns response line bytes, b"" on client cancel/EOF, None on
        helper EOF/timeout/oversize."""
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ, "helper")
        sel.register(conn, selectors.EVENT_READ, "client")
        response = b""
        helper_eof = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                events = sel.select(min(1.0, remaining))
                if not events:
                    continue
                for key, _ in events:
                    if key.data == "client":
                        try:
                            probe = conn.recv(1, socket.MSG_PEEK)
                        except OSError:
                            probe = b""
                        if not probe:
                            return b""  # caller gone -> cancel
                        # extra client bytes after the single request: cancel
                        return b""
                    try:
                        chunk = os.read(proc.stdout.fileno(), 65536)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        sel.unregister(proc.stdout)
                        helper_eof = True
                        continue
                    response += chunk
                    if len(response) > MAX_FRAME:
                        return None
                    if b"\n" in response:
                        return response.split(b"\n", 1)[0]
                if helper_eof:
                    return None  # helper exited without a full line
        finally:
            sel.close()

    def serve(self, stop_event=None) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o660)
        srv.listen(MAX_HANDLERS * 2)
        srv.settimeout(1.0)
        try:
            while stop_event is None or not stop_event.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                # Peer check before admission: unauthorized UIDs never
                # consume a handler slot or reach the pool.
                try:
                    uid = _peer_uid(conn)
                except OSError:
                    conn.close()
                    continue
                if uid != self.allowed_uid \
                        or not self._slots.acquire(blocking=False):
                    conn.close()
                    continue
                # Register before submit so stop can reach this conn even
                # if it is still queued; discard+release on submit failure.
                with self._active_lock:
                    self._active_conns.add(conn)
                try:
                    self._pool.submit(self._run_admitted, conn)
                except Exception:
                    with self._active_lock:
                        self._active_conns.discard(conn)
                    self._slots.release()
                    conn.close()
        finally:
            srv.close()
            # Signal every still-admitted worker via client-side EOF: the
            # response phase treats the shutdown as caller cancellation and
            # its finally path closes/reaps/releases. Queued workers still
            # run their finally cleanup; nothing is dropped silently.
            with self._active_lock:
                active = tuple(self._active_conns)
            for conn in active:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self._pool.shutdown(wait=False, cancel_futures=False)
            try:
                self.socket_path.unlink()
            except OSError:
                pass

    def _run_admitted(self, conn: socket.socket) -> None:
        # Owns exactly one close/discard/release for the admitted conn;
        # the handler itself must not release the admission slot.
        try:
            self._handle_admitted(conn)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self._active_lock:
                self._active_conns.discard(conn)
            try:
                self._slots.release()
            except ValueError:
                pass

    def _handle_admitted(self, conn: socket.socket) -> None:
        proc = None
        try:
            raw = _read_header(conn)
            if raw is None:
                return
            (provider, principal, tool, arguments,
             selection, op, request_id) = validate_envelope(raw)
            deadline = time.monotonic() + _request_budget(op, arguments)
            proc = self._spawn(provider, principal)
            inner = {"tool": tool, "arguments": arguments,
                     "request_id": request_id}
            if selection is not None:
                inner["selection"] = selection
            request = json.dumps(inner, separators=(",", ":")).encode() + b"\n"
            if len(request) > MAX_FRAME:
                return
            if not self._write_request(proc, request, conn, deadline):
                return
            line = self._read_response(proc, conn, deadline)
            if line is None or line == b"":
                return
            if len(line) <= MAX_FRAME:
                conn.settimeout(max(0.001, deadline - time.monotonic()))
                try:
                    conn.sendall(line + b"\n")
                except (socket.timeout, TimeoutError, OSError):
                    pass
        except (OSError, BrokerError, ValueError):
            pass
        finally:
            if proc is not None:
                self._finish_helper(proc)


def main() -> int:
    sock = Path(os.environ.get(
        "REMOTE_RESEARCH_PROVIDER_SOCK", str(SOCKET_PATH)))
    helper = Path(os.environ.get(
        "REMOTE_RESEARCH_PROVIDER_HELPER",
        "/opt/remote-research-mcp/current/scripts/"
        "run-remote-provider-rpc.sh"))
    try:
        uid = int(os.environ.get("REMOTE_RESEARCH_BROKER_UID", "-1"))
    except ValueError:
        uid = -1
    if uid < 0:
        try:
            uid = pwd.getpwnam("researchmcp").pw_uid
        except KeyError:
            print("researchmcp account missing", file=sys.stderr)
            return 1
    Broker(sock, helper, uid).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
