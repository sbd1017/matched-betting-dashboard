"""Promotional commission rates with a known end date (accounts.commission_expires_at).

Generic: any exchange can carry one. The stored rate is never changed automatically; once the date passes the
app says the rate may be stale (issue, dashboard, calculators) until the user records the rate that applies now.

Levels, by whole UK days left:
  later    more than SOON_DAYS: shown on the account and the calendar only
  soon     SOON_DAYS or fewer: an Up next row on the dashboard
  urgent   URGENT_DAYS or fewer: moves into Needs you
  expired  on or after the date: an issue, a Needs you row, and a warning wherever the rate is used
"""
from __future__ import annotations

from datetime import date, datetime

from . import times
from .money import commission_text

SOON_DAYS = 30
URGENT_DAYS = 7


def expiry_from_date(value: str | None) -> str | None:
    """'2027-09-28' (the day the rate ends) -> UTC ISO of 00:00 UK time that day. Empty -> None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise ValueError("Enter the promotion's end date as a date") from None
    return times.iso(datetime(d.year, d.month, d.day, tzinfo=times.UK))


def date_input(expires_at: str | None) -> str:
    """The stored value as a date input's value."""
    dt = times.parse(expires_at)
    return dt.astimezone(times.UK).date().isoformat() if dt else ""


def promo(acc) -> dict | None:
    """The state of an account's promotional rate, or None if it has no end date."""
    expires_at = acc["commission_expires_at"] if "commission_expires_at" in acc.keys() else None
    if not expires_at or acc["kind"] != "exchange":
        return None
    end = times.parse(expires_at).astimezone(times.UK).date()
    days = (end - times.now().astimezone(times.UK).date()).days
    level = ("expired" if days <= 0 else "urgent" if days <= URGENT_DAYS else
             "soon" if days <= SOON_DAYS else "later")
    rate = commission_text(acc["commission_bp"])
    when = "tomorrow" if days == 1 else f"in {days} days"
    out = {"account_id": acc["id"], "name": acc["name"], "rate": rate, "expires_at": expires_at,
           "date": end.strftime("%-d %b %Y"), "days": days, "level": level, "expired": level == "expired"}
    if out["expired"]:
        out["title"] = f"Verify {acc['name']} commission rate"
        out["text"] = f"Your recorded {rate} promotional rate expired {out['date']}."
        out["warning"] = (f"The {rate} promotional commission recorded for {acc['name']} expired {out['date']}: "
                          "check the rate that applies now")
    else:
        out["title"] = f"{acc['name']} {rate} commission expires {when}"
        out["text"] = f"{out['date']} · Verify the rate that will apply afterwards"
        out["warning"] = ""
    return out


def promos(conn) -> list[dict]:
    """Active, open exchanges with a promotional rate, soonest end first."""
    rows = conn.execute("SELECT * FROM accounts WHERE kind = 'exchange' AND active = 1 AND standing <> 'closed' "
                        "AND commission_expires_at IS NOT NULL ORDER BY commission_expires_at")
    return [p for p in (promo(r) for r in rows) if p]


def stale_warning(acc, commission_bp: int | None) -> str:
    """A warning when a calculation uses an exchange's stored rate after its promotion ended."""
    p = promo(acc) if acc else None
    if p and p["expired"] and (commission_bp is None or commission_bp == acc["commission_bp"]):
        return p["warning"]
    return ""
