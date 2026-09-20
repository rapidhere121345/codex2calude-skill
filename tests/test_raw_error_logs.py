import json
import os
import stat
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from claude_research_mcp import raw_error_logs


def principal_dir(work_root: Path, principal: str = "owner") -> Path:
    return (
        work_root
        / "state"
        / "error-diagnostics"
        / principal
    )


def valid_uuid() -> str:
    return str(uuid.uuid4())


def write(work_root: Path, request_id=None, principal="owner"):
    return raw_error_logs.write_raw_error(
        work_root,
        principal=principal,
        request_id=request_id or valid_uuid(),
        stdout="out",
        stderr="err",
    )


def read_log(work_root: Path, request_id: str, principal="owner"):
    return json.loads(
        (principal_dir(work_root, principal) / f"{request_id}.json").read_text()
    )


def test_successful_write_schema_permissions_and_directory_modes(tmp_path):
    request_id = valid_uuid()

    assert write(tmp_path, request_id)

    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700
    diagnostics = tmp_path / "state" / "error-diagnostics"
    assert stat.S_IMODE(diagnostics.stat().st_mode) == 0o700
    directory = principal_dir(tmp_path)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    lock = directory / ".raw-error-logs.lock"
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    log_path = directory / f"{request_id}.json"
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert set(read_log(tmp_path, request_id)) == {
        "request_id",
        "principal",
        "created_at",
        "stdout",
        "stdout_truncated",
        "stderr",
        "stderr_truncated",
    }
    assert read_log(tmp_path, request_id)["request_id"] == request_id


def test_truncation_on_utf8_boundary_and_robust_surrogates(tmp_path):
    request_id = valid_uuid()
    stdout_prefix = "a" * (raw_error_logs.MAX_STREAM_BYTES - 2)
    stdout = stdout_prefix + "é" + "ignored"
    stderr = "\ud800" * 30_000

    assert raw_error_logs.write_raw_error(
        tmp_path,
        principal="owner",
        request_id=request_id,
        stdout=stdout,
        stderr=stderr,
    )

    document = read_log(tmp_path, request_id)
    assert document["stdout"] == stdout_prefix + "é"
    assert document["stdout_truncated"] is True
    assert len(
        document["stderr"].encode("utf-8", errors="surrogatepass")
    ) <= raw_error_logs.MAX_STREAM_BYTES
    assert document["stderr_truncated"] is True
    assert (
        principal_dir(tmp_path) / f"{request_id}.json"
    ).stat().st_size <= raw_error_logs.MAX_FILE_BYTES


def test_exact_stream_boundary_is_not_flagged(tmp_path):
    request_id = valid_uuid()
    exact = "é" * (raw_error_logs.MAX_STREAM_BYTES // 2)

    assert raw_error_logs.write_raw_error(
        tmp_path,
        principal="owner",
        request_id=request_id,
        stdout=exact,
        stderr="",
    )
    document = read_log(tmp_path, request_id)
    assert document["stdout"] == exact
    assert document["stdout_truncated"] is False
    assert document["stderr_truncated"] is False


def test_age_pruning_and_future_daily_housekeeping(tmp_path):
    old_id = valid_uuid()
    new_id = valid_uuid()
    assert write(tmp_path, old_id)
    assert write(tmp_path, new_id)
    old_path = principal_dir(tmp_path) / f"{old_id}.json"
    old_ns = time.time_ns() - raw_error_logs.RETENTION_SECONDS * 1_000_000_000
    os.utime(old_path, ns=(old_ns, old_ns))

    assert raw_error_logs.prune_raw_errors(tmp_path, principal="owner")
    assert not old_path.exists()
    assert (principal_dir(tmp_path) / f"{new_id}.json").exists()


def test_prune_missing_store_is_true_and_creates_no_directories(tmp_path):
    assert raw_error_logs.prune_raw_errors(tmp_path, principal="owner")
    assert not (tmp_path / "state").exists()


def test_collision_does_not_prune_or_replace_expired_evidence(tmp_path):
    request_id = valid_uuid()
    assert write(tmp_path, request_id)
    path = principal_dir(tmp_path) / f"{request_id}.json"
    original = path.read_bytes()
    old = time.time() - raw_error_logs.RETENTION_SECONDS - 10
    os.utime(path, (old, old))
    assert not write(tmp_path, request_id)
    assert path.read_bytes() == original


def test_collision_at_capacity_does_not_evict_other_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(raw_error_logs, "MAX_FILES_PER_PRINCIPAL", 2)
    first, second = valid_uuid(), valid_uuid()
    assert write(tmp_path, first)
    assert write(tmp_path, second)
    assert not write(tmp_path, second)
    assert (principal_dir(tmp_path) / f"{first}.json").exists()
    assert (principal_dir(tmp_path) / f"{second}.json").exists()


def test_count_cap_and_concurrent_writers(tmp_path):
    request_ids = [valid_uuid() for _ in range(130)]

    def persist(request_id):
        return raw_error_logs.write_raw_error(
            tmp_path,
            principal="owner",
            request_id=request_id,
            stdout="o",
            stderr="e",
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        assert all(executor.map(persist, request_ids))

    files = list(principal_dir(tmp_path).glob("*.json"))
    assert len(files) == raw_error_logs.MAX_FILES_PER_PRINCIPAL
    assert len({path.stem for path in files}) == len(files)


def test_size_cap_prunes_oldest_owned_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(
        raw_error_logs, "MAX_TOTAL_BYTES_PER_PRINCIPAL", 40_000
    )
    ids = [valid_uuid() for _ in range(5)]
    for index, request_id in enumerate(ids):
        assert raw_error_logs.write_raw_error(
            tmp_path,
            principal="owner",
            request_id=request_id,
            stdout=str(index) * 10_000,
            stderr="e",
        )

    files = list(principal_dir(tmp_path).glob("*.json"))
    total = sum(path.stat().st_size for path in files)
    assert total <= 40_000
    assert not (principal_dir(tmp_path) / f"{ids[0]}.json").exists()
    assert (principal_dir(tmp_path) / f"{ids[-1]}.json").exists()


def test_prune_never_deletes_other_names_or_unowned_logs(tmp_path):
    (tmp_path / "state").mkdir(mode=0o700)
    (tmp_path / "state" / "error-diagnostics").mkdir(mode=0o700)
    directory = principal_dir(tmp_path)
    directory.mkdir(mode=0o700)
    other = directory / "not-managed.txt"
    other.write_text("preserve")
    wrong_mode_id = valid_uuid()
    wrong_mode = directory / f"{wrong_mode_id}.json"
    wrong_mode.write_text("preserve")
    wrong_mode.chmod(0o644)

    assert raw_error_logs.prune_raw_errors(tmp_path, principal="owner")

    assert other.read_text() == "preserve"
    assert wrong_mode.read_text() == "preserve"
    assert stat.S_IMODE(wrong_mode.stat().st_mode) == 0o644


def test_rejects_hostile_symlinked_directory_lock_and_file(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()

    state = tmp_path / "state"
    state.symlink_to(outside, target_is_directory=True)
    assert not write(tmp_path)
    state.unlink()

    diagnostics = tmp_path / "state" / "error-diagnostics"
    diagnostics.parent.mkdir(mode=0o700)
    diagnostics.symlink_to(outside, target_is_directory=True)
    assert not write(tmp_path)
    diagnostics.unlink()

    directory = principal_dir(tmp_path)
    directory.mkdir(parents=True, mode=0o700)
    (directory / ".raw-error-logs.lock").symlink_to(outside / "lock")
    assert not write(tmp_path)

    lock_target = outside / "lock"
    lock_target.write_text("do not change")
    (directory / ".raw-error-logs.lock").unlink()
    (directory / ".raw-error-logs.lock").symlink_to(lock_target)
    assert not write(tmp_path)
    assert lock_target.read_text() == "do not change"

    request_id = valid_uuid()
    target = outside / f"{request_id}.json"
    target.write_text("existing sensitive data")
    (directory / f"{request_id}.json").symlink_to(target)
    assert not write(tmp_path, request_id)
    assert target.read_text() == "existing sensitive data"


def test_rejects_traversal_in_work_root_components(tmp_path):
    real_root = tmp_path / "real"
    real_root.mkdir()
    link_root = tmp_path / "link-root"
    link_root.symlink_to(real_root, target_is_directory=True)

    assert not write(link_root)


def test_rejects_explicit_parent_traversal_in_work_root(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)

    assert not write(tmp_path / ".." / target.name)


def test_collision_preserves_existing_regular_file(tmp_path):
    request_id = valid_uuid()
    path = principal_dir(tmp_path) / f"{request_id}.json"
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text("existing sensitive data")
    os.chmod(path, 0o600)

    assert not write(tmp_path, request_id)
    assert path.read_text() == "existing sensitive data"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "request_id",
    [
        "not-a-uuid",
        str(uuid.uuid3(uuid.NAMESPACE_DNS, "example.com")),
        str(uuid.uuid4()).upper(),
        f"../{uuid.uuid4()}",
        f"/tmp/{uuid.uuid4()}",
    ],
)
def test_rejects_invalid_uuid(tmp_path, request_id):
    assert not write(tmp_path, request_id)


@pytest.mark.parametrize("principal", ["", "admin", "../owner", "owner\0x"])
def test_rejects_invalid_principal(tmp_path, principal):
    assert not write(tmp_path, principal=principal)


def test_rejects_insecure_preexisting_directory(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    state.chmod(0o707)
    try:
        assert not write(tmp_path)
    finally:
        state.chmod(0o700)


def test_io_failure_returns_false_without_raising_or_output(
    tmp_path, monkeypatch, capsys
):
    request_id = valid_uuid()

    def failed_write(*args, **kwargs):
        raise OSError("secret filesystem detail")

    monkeypatch.setattr(os, "write", failed_write)
    assert not write(tmp_path, request_id)
    assert not (principal_dir(tmp_path) / f"{request_id}.json").exists()
    captured = capsys.readouterr()
    assert not captured.out
    assert not captured.err


def test_early_file_failure_cleans_up_new_file(
    tmp_path, monkeypatch
):
    request_id = valid_uuid()

    def failed_chmod(*args, **kwargs):
        raise OSError("secret chmod detail")

    monkeypatch.setattr(os, "fchmod", failed_chmod)
    assert not write(tmp_path, request_id)
    assert not (principal_dir(tmp_path) / f"{request_id}.json").exists()
