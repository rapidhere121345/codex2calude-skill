"""Read owner-maintained billing dates without accessing provider accounts."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo


MAX_SUBSCRIPTION_BYTES = 16_384
SUBSCRIPTION_TIMEZONE = "Asia/Shanghai"
PROVIDERS = ("claude", "gemini")


def _missing(reason: str) -> dict[str, object]:
    return {"configured": False, "reason": reason}


def _subscription(value: object, today: date) -> dict[str, object]:
    if value is None:
        return _missing("not_configured")
    if not isinstance(value, dict):
        return _missing("invalid")
    raw_date = value.get("date")
    kind = value.get("kind")
    if (
        not isinstance(raw_date, str)
        or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw_date) is None
        or kind not in ("expires", "renews")
    ):
        return _missing("invalid")
    try:
        billing_date = date.fromisoformat(raw_date)
    except ValueError:
        return _missing("invalid")
    remaining = (billing_date - today).days
    state = "past" if remaining < 0 else "today" if remaining == 0 else "soon" if remaining <= 7 else "scheduled"
    return {
        "configured": True,
        "date": billing_date.isoformat(),
        "kind": kind,
        "source": "manual",
        "days_remaining": remaining,
        "state": state,
    }


def read_subscriptions(
    path: Path, *, now: datetime | None = None
) -> dict[str, object]:
    """Only validated dates and fixed labels may leave this boundary.

    Missing/invalid dates never change account readiness. A renewal date in the
    past needs confirmation; it is not evidence of an expired subscription.
    """
    result: dict[str, object] = {"timezone": SUBSCRIPTION_TIMEZONE}
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_SUBSCRIPTION_BYTES + 1)
        if len(raw) > MAX_SUBSCRIPTION_BYTES:
            raise ValueError("oversized subscription metadata")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("invalid subscription metadata")
    except FileNotFoundError:
        return {**result, **{p: _missing("not_configured") for p in PROVIDERS}}
    except (OSError, ValueError, RecursionError):
        return {**result, **{p: _missing("unavailable") for p in PROVIDERS}}
    current = now or datetime.now(timezone.utc)
    today = current.astimezone(ZoneInfo(SUBSCRIPTION_TIMEZONE)).date()
    for provider in PROVIDERS:
        result[provider] = _subscription(payload.get(provider), today)
    return result
