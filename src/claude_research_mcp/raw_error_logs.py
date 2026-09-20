"""Private, bounded retention of raw Claude invocation error output.

The functions in this module deliberately do not provide a read or export API.
Raw stdout/stderr can contain sensitive data, so it is persisted only in a
principal-owned private directory and is never logged by this module.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


_PRINCIPALS = frozenset(("owner", "partner"))
_UUIDv4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-"
    r"[0-9a-f]{12}$"
)
_RAW_LOG_RE = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-"
    r"[0-9a-f]{12})\.json$"
)
_LOCK_NAME = ".raw-error-logs.lock"
_STATE_NAME = "state"
_DIAGNOSTICS_NAME = "error-diagnostics"

RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_STREAM_BYTES = 65_536
MAX_FILE_BYTES = 1_048_576
MAX_FILES_PER_PRINCIPAL = 100
MAX_TOTAL_BYTES_PER_PRINCIPAL = 20 * 1_048_576
_LOCK_TIMEOUT_SECONDS = 5.0


def write_raw_error(
    work_root: Path,
    *,
    principal: str,
    request_id: str,
    stdout: str,
    stderr: str,
) -> bool:
    """Persist one failed invocation's raw output; return True on success."""

    try:
        if not _valid_principal(principal):
            return False
        if not _valid_uuidv4(request_id):
            return False
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            return False

        stdout_value, stdout_truncated = _truncate_for_json(stdout)
        stderr_value, stderr_truncated = _truncate_for_json(stderr)
        created_at = datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        document = {
            "request_id": request_id,
            "principal": principal,
            "created_at": created_at,
            "stdout": stdout_value,
            "stdout_truncated": stdout_truncated,
            "stderr": stderr_value,
            "stderr_truncated": stderr_truncated,
        }
        payload = json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        if len(payload) > MAX_FILE_BYTES:
            return False

        with _principal_lock(work_root, principal, create=True) as directory_fd:
            # A repeated identifier must not evict or replace its existing
            # evidence, even when the previous file is now past retention.
            try:
                os.stat(request_id + ".json", dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                return False
            if not _prune_locked(
                directory_fd,
                allowed_count=MAX_FILES_PER_PRINCIPAL - 1,
                allowed_bytes=MAX_TOTAL_BYTES_PER_PRINCIPAL - len(payload),
            ):
                return False
            _create_raw_file(directory_fd, request_id, payload)
            return True
    except Exception:
        # Failures may contain filesystem details or subprocess output. Do not
        # log, raise, or otherwise expose them through this API.
        return False


def prune_raw_errors(work_root: Path, *, principal: str) -> bool:
    """Apply retention and bounded-space policy for one principal."""

    try:
        if not _valid_principal(principal):
            return False
        with _principal_lock(work_root, principal, create=False) as directory_fd:
            if directory_fd is None:
                # Daily housekeeping must not provision or modify anything
                # above the already-deployed error-diagnostics parent. A
                # missing store simply has nothing to retain or evict.
                return True
            return _prune_locked(
                directory_fd,
                allowed_count=MAX_FILES_PER_PRINCIPAL,
                allowed_bytes=MAX_TOTAL_BYTES_PER_PRINCIPAL,
            )
    except Exception:
        return False


def _valid_principal(principal: object) -> bool:
    return principal in _PRINCIPALS


def _valid_uuidv4(request_id: object) -> bool:
    if not isinstance(request_id, str):
        return False
    try:
        parsed = uuid.UUID(request_id)
    except (ValueError, AttributeError, TypeError):
        return False
    return (
        parsed.version == 4
        and str(parsed) == request_id
        and _UUIDv4_RE.fullmatch(request_id) is not None
    )


def _truncate_for_json(value: str) -> tuple[str, bool]:
    """Bound text by UTF-8 bytes without replacing any retained character."""

    retained: list[str] = []
    used_bytes = 0
    truncated = False

    for character in value:
        try:
            character_bytes = len(
                character.encode("utf-8", errors="surrogatepass")
            )
        except UnicodeError:
            character_bytes = 1
            truncated = True
        if used_bytes + character_bytes > MAX_STREAM_BYTES:
            truncated = truncated or character != value[-1:]
            break
        retained.append(character)
        used_bytes += character_bytes

    return "".join(retained), truncated or _utf8_size(value) > MAX_STREAM_BYTES


def _utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8", errors="surrogatepass"))
    except UnicodeError:
        return MAX_STREAM_BYTES + 1


@contextmanager
def _principal_lock(
    work_root: Path, principal: str, *, create: bool
) -> Iterator[int | None]:
    if create:
        directory_fd = _open_principal_directory(work_root, principal)
    else:
        directory_fd = _open_existing_principal_directory(work_root, principal)
        if directory_fd is None:
            yield None
            return
    try:
        lock_fd = _open_lock_file(directory_fd)
        try:
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("raw-log lock timeout")
                    time.sleep(0.05)
            yield directory_fd
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
    finally:
        os.close(directory_fd)


def _open_principal_directory(work_root: Path, principal: str) -> int:
    work_root_fd = _open_existing_directory_path(work_root)
    try:
        state_fd = _ensure_private_directory(work_root_fd, _STATE_NAME)
        try:
            diagnostics_fd = _ensure_private_directory(
                state_fd, _DIAGNOSTICS_NAME
            )
            try:
                return _ensure_private_directory(diagnostics_fd, principal)
            finally:
                os.close(diagnostics_fd)
        finally:
            os.close(state_fd)
    finally:
        os.close(work_root_fd)


def _open_existing_principal_directory(
    work_root: Path, principal: str
) -> int | None:
    """Open a complete existing store, or return None when it is absent."""

    work_root_fd = _open_existing_directory_path(work_root)
    try:
        state_fd = _open_existing_private_directory(work_root_fd, _STATE_NAME)
        if state_fd is None:
            return None
        try:
            diagnostics_fd = _open_existing_private_directory(
                state_fd, _DIAGNOSTICS_NAME
            )
            if diagnostics_fd is None:
                return None
            try:
                return _open_existing_private_directory(
                    diagnostics_fd, principal
                )
            finally:
                os.close(diagnostics_fd)
        finally:
            os.close(state_fd)
    finally:
        os.close(work_root_fd)


def _open_existing_private_directory(
    parent_fd: int, name: str
) -> int | None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        directory_fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    try:
        _validate_private_directory(directory_fd)
        return os.dup(directory_fd)
    finally:
        os.close(directory_fd)


def _open_existing_directory_path(work_root: object) -> int:
    if isinstance(work_root, Path):
        raw_path = os.fspath(work_root)
    else:
        raw_path = work_root
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("work_root must be a non-empty path")
    if ".." in Path(raw_path).parts:
        raise ValueError("work_root must not contain parent traversal")

    absolute_path = os.path.abspath(raw_path)
    path = Path(absolute_path)
    components = [
        component
        for component in path.parts
        if component not in (os.sep, "", ".")
    ]
    if ".." in components:
        raise ValueError("work_root must not contain parent traversal")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current_fd = os.open(os.sep, flags)
    try:
        for component in components:
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        opened_fd = os.dup(current_fd)
        os.close(current_fd)
        return opened_fd
    except Exception:
        os.close(current_fd)
        raise


def _ensure_private_directory(parent_fd: int, name: str) -> int:
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        if created:
            os.fchmod(directory_fd, 0o700)
        _validate_private_directory(directory_fd)
        return os.dup(directory_fd)
    finally:
        os.close(directory_fd)


def _validate_private_directory(directory_fd: int) -> None:
    metadata = os.fstat(directory_fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError("managed path is not a directory")
    if metadata.st_uid != os.geteuid():
        raise OSError("managed directory has the wrong owner")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise OSError("managed directory is not mode 0700")


def _open_lock_file(directory_fd: int) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    created = False
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock_fd = os.open(_LOCK_NAME, flags, dir_fd=directory_fd)
            break
        except FileNotFoundError:
            try:
                lock_fd = os.open(
                    _LOCK_NAME,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_fd,
                )
                created = True
                break
            except FileExistsError:
                # Another writer won creation. Retry opening its lock file;
                # the retry loop also handles a pruner removing it again.
                if time.monotonic() >= deadline:
                    raise TimeoutError("raw-log lock creation timeout")
                continue

    try:
        if created:
            os.fchmod(lock_fd, 0o600)
        metadata = os.fstat(lock_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("raw-log lock is not a regular file")
        if metadata.st_uid != os.geteuid():
            raise OSError("raw-log lock has the wrong owner")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise OSError("raw-log lock is not mode 0600")
        if metadata.st_nlink != 1:
            raise OSError("raw-log lock has multiple links")
        return os.dup(lock_fd)
    finally:
        os.close(lock_fd)


def _prune_locked(
    directory_fd: int, *, allowed_count: int, allowed_bytes: int
) -> bool:
    candidates = _scan_candidates(directory_fd)
    if candidates is None:
        return False

    now_ns = time.time_ns()
    cutoff_ns = now_ns - RETENTION_SECONDS * 1_000_000_000
    newest_first = sorted(
        candidates,
        key=lambda item: (item[1].st_mtime_ns, item[0]),
        reverse=True,
    )
    evictions: list[str] = []
    retained = []
    for item in newest_first:
        if item[1].st_mtime_ns > cutoff_ns:
            retained.append(item)
        else:
            evictions.append(item[0])
    while len(retained) > allowed_count:
        evicted = retained.pop()
        evictions.append(evicted[0])

    retained_size = sum(item[1].st_size for item in retained)
    while retained_size > allowed_bytes:
        if not retained:
            return False
        evicted = retained.pop()
        retained_size -= evicted[1].st_size
        evictions.append(evicted[0])

    for name in evictions:
        if not _unlink_owned_candidate(directory_fd, name):
            return False
    if evictions:
        os.fsync(directory_fd)
    return True


def _scan_candidates(directory_fd: int) -> list[tuple[str, os.stat_result]] | None:
    candidates: list[tuple[str, os.stat_result]] = []
    for name in os.listdir(directory_fd):
        if _RAW_LOG_RE.fullmatch(name) is None:
            continue
        try:
            metadata = os.stat(
                name, dir_fd=directory_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode):
            continue
        if metadata.st_uid != os.geteuid():
            continue
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            continue
        candidates.append((name, metadata))
    return candidates


def _create_raw_file(
    directory_fd: int, request_id: str, payload: bytes
) -> None:
    filename = request_id + ".json"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | os.O_CLOEXEC
    )
    file_fd = os.open(filename, flags, 0o600, dir_fd=directory_fd)
    marker: os.stat_result | None = None
    try:
        os.fchmod(file_fd, 0o600)
        marker = os.fstat(file_fd)
        _write_all(file_fd, payload)
        created_ns = time.time_ns()
        os.utime(file_fd, ns=(created_ns, created_ns))
        os.fsync(file_fd)
        os.fsync(directory_fd)
    except BaseException:
        if marker is not None:
            _unlink_if_same_inode(directory_fd, filename, marker)
        raise
    finally:
        os.close(file_fd)


def _write_all(file_fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(file_fd, payload[offset:])
        if written <= 0:
            raise OSError("short write while persisting raw error")
        offset += written


def _unlink_owned_candidate(directory_fd: int, name: str) -> bool:
    try:
        metadata = os.stat(
            name, dir_fd=directory_fd, follow_symlinks=False
        )
        if not _is_owned_candidate(metadata):
            return False
        return _unlink_if_same_inode(directory_fd, name, metadata)
    except OSError:
        return False


def _is_owned_candidate(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o600
    )


def _unlink_if_same_inode(
    directory_fd: int, name: str, expected: os.stat_result
) -> bool:
    try:
        current = os.stat(
            name, dir_fd=directory_fd, follow_symlinks=False
        )
        if (current.st_dev, current.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            return False
        os.unlink(name, dir_fd=directory_fd)
        return True
    except OSError:
        return False
