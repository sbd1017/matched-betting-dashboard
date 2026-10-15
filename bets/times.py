"""Times are stored as UTC ISO-8601 ('2026-10-08T14:30:00Z') and shown in UK time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(now())


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


SKEW = timedelta(minutes=10)      # a "when it happened" time this little ahead of the clock is just clock difference


def from_local_input(value: str | None, *, happened: bool = False) -> str | None:
    """UK local date/time (HTML datetime-local / date input, or what the assistant sends) -> UTC ISO. Empty -> None.
    BST/GMT is applied for that date. A value with Z or an offset is taken as given.

    A date alone means the end of that day (right for expiries and deadlines). For something that already happened
    (happened=True: placed, settled, transferred, received...) a date alone means "that day": today -> now, other
    days -> 23:59 that day; and a time a few minutes ahead of the clock is taken as now."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        if len(value) == 10:                       # date only
            dt = datetime.fromisoformat(value + "T23:59")
        else:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Enter a valid date/time") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UK)
    if happened:
        current = now()
        today = current.astimezone(UK).date().isoformat()
        if (len(value) == 10 and value == today and dt > current) or current < dt <= current + SKEW:
            dt = current
    return iso(dt)


def in_future(value: str | None) -> bool:
    """True when a stored time is genuinely ahead of now (beyond clock difference)."""
    dt = parse(value)
    return bool(dt and dt > now() + SKEW)


def to_local_input(value: str | None) -> str:
    dt = parse(value)
    return dt.astimezone(UK).strftime("%Y-%m-%dT%H:%M") if dt else ""


def local(value: str | None, fmt: str = "%a %-d %b %H:%M") -> str:
    dt = parse(value)
    return dt.astimezone(UK).strftime(fmt) if dt else "—"


def relative(value: str | None) -> str:
    """'in 3 h', '2 d ago' for deadlines."""
    dt = parse(value)
    if not dt:
        return ""
    delta = dt - now()
    secs = abs(delta.total_seconds())
    if secs < 3600:
        text = f"{max(1, int(secs // 60))} min"
    elif secs < 172800:
        text = f"{int(secs // 3600)} h"
    else:
        text = f"{int(secs // 86400)} d"
    return f"in {text}" if delta.total_seconds() >= 0 else f"{text} ago"


def month_start(ref: datetime | None = None) -> str:
    ref = (ref or now()).astimezone(UK)
    return iso(ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


def week_start(ref: datetime | None = None) -> str:
    ref = (ref or now()).astimezone(UK)
    start = ref - timedelta(days=ref.weekday())
    return iso(start.replace(hour=0, minute=0, second=0, microsecond=0))
