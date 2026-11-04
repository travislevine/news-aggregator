"""Small date/time helpers. The DB stores UTC ISO strings; the vault shows local time."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def parse_iso(value: str | datetime | None) -> datetime | None:
    """Parse an ISO 8601 string (``Z`` allowed). Naive values are treated as UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def to_utc_iso(dt: datetime) -> str:
    """UTC ISO string with seconds precision (sortable as text)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def to_local_iso(dt: datetime | None, tz: ZoneInfo) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz).isoformat(timespec="seconds")


def local(dt: datetime | str | None, tz: ZoneInfo) -> datetime | None:
    d = parse_iso(dt) if not isinstance(dt, datetime) else dt
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(tz)


def human_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"
