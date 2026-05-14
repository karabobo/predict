from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_et(value: str | datetime | None, fmt: str = "%Y-%m-%d %I:%M:%S %p ET") -> str:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parse_timestamp(value)
    if parsed is None:
        return "n/a"
    return parsed.astimezone(ET).strftime(fmt)


def format_et_short(value: str | datetime | None) -> str:
    return format_et(value, "%m-%d %I:%M %p ET")


def now_et_label() -> str:
    return datetime.now(timezone.utc).astimezone(ET).strftime("%Y-%m-%d %I:%M:%S %p ET")
