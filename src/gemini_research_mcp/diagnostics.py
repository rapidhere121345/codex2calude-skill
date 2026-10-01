"""Private failure diagnostics and safe audit metadata helpers."""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from .raw_error_logs import write_raw_error


@dataclass(frozen=True)
class RawErrorData:
    """Bounded process output kept only on a private exception attribute."""

    stdout: str
    stderr: str

    def __repr__(self) -> str:
        return "RawErrorData(<redacted>)"


def new_request_id() -> str:
    """Generate the caller-correlatable, per-invocation UUID."""

    return str(uuid.uuid4())


def attach_raw_error(
    exc: BaseException, *, stdout: str = "", stderr: str = ""
) -> None:
    if not hasattr(exc, "_raw_error_data"):
        exc._raw_error_data = RawErrorData(stdout, stderr)  # noqa: SLF001


def raw_error_data(exc: BaseException) -> RawErrorData | None:
    value = getattr(exc, "_raw_error_data", None)
    if not isinstance(value, RawErrorData):
        return None
    if not isinstance(value.stdout, str) or not isinstance(value.stderr, str):
        return None
    return value
