"""Cross-process, cancellation-safe global concurrency slots."""

from __future__ import annotations

from contextlib import asynccontextmanager
import fcntl
from pathlib import Path

import asyncio


class ConcurrencyLimitError(RuntimeError):
    """Raised when all global Gemini execution slots are busy."""


def _open_slot(lock_dir: Path, index: int):
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = (lock_dir / f"gemini-slot-{index:04d}.lock").open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


@asynccontextmanager
async def concurrency_slot(lock_dir: Path, slots: int):
    if slots < 1:
        raise ValueError("slots must be positive")
    handle = None
    try:
        while handle is None:
            for index in range(slots):
                handle = await asyncio.to_thread(_open_slot, lock_dir, index)
                if handle is not None:
                    break
            if handle is None:
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if handle is not None:
            handle.close()
