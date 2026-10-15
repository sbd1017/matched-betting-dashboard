"""Exact money and odds handling.

Money is always an ``int`` number of pence. Parsing and arithmetic that needs
fractions (odds, commission) use ``Decimal`` and round to pence once, half-up.
No floats anywhere in financial paths.
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

PENNY = Decimal("0.01")
MAX_PENCE = 100_000_000_00          # 100m: anything larger is a typo

# Amounts are stored as integer minor units (pence / cents), so any currency with two decimal places works. The
# currency only changes how money is shown and which symbols are accepted when typing it (Settings -> Currency).
CURRENCIES = {"GBP": ("£", "Pound sterling"), "EUR": ("€", "Euro"), "USD": ("$", "US dollar"),
              "AUD": ("A$", "Australian dollar"), "CAD": ("C$", "Canadian dollar"), "NZD": ("NZ$", "New Zealand dollar")}
DEFAULT_CURRENCY = "GBP"
_currency = DEFAULT_CURRENCY


def set_currency(code: str | None) -> str:
    """Use this currency for display from now on (unknown codes fall back to the default)."""
    global _currency
    _currency = code if code in CURRENCIES else DEFAULT_CURRENCY
    return _currency


def currency() -> str:
    return _currency


def symbol() -> str:
    return CURRENCIES[_currency][0]


_SYMBOLS = "|".join(sorted((re.escape(x) for code, (sym, _) in CURRENCIES.items() for x in (sym, code)),
                           key=len, reverse=True))
_MONEY = re.compile(r"^(-)?\s*(?:" + _SYMBOLS + r")?\s*(-)?\s*([0-9][0-9,]*)(?:\.([0-9]{1,2}))?$", re.I)


def to_pence(value, *, allow_negative: bool = False, allow_zero: bool = True) -> int:
    """Parse '£1,234.56', '10', '10.5', 1234 (pence int is NOT accepted) -> pence.

    More than two decimal places is refused rather than silently rounded.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError("Enter an amount")
    if isinstance(value, Decimal):
        if value != value.quantize(PENNY):
            raise ValueError("Use at most two decimal places")
        pence = int(value * 100)
    else:
        text = str(value).strip().replace("−", "-")
        m = _MONEY.match(text)
        if not m:
            if re.match(r"^-?\s*(?:" + _SYMBOLS + r")?\s*-?[0-9][0-9,]*\.[0-9]{3,}$", text, re.I):
                raise ValueError("Use at most two decimal places")
            raise ValueError("Enter an amount like 10 or 10.50")
        neg = bool(m.group(1) or m.group(2))
        whole = m.group(3).replace(",", "")
        frac = (m.group(4) or "").ljust(2, "0")
        pence = int(whole) * 100 + int(frac or 0)
        if neg:
            pence = -pence
    if pence < 0 and not allow_negative:
        raise ValueError("Amount cannot be negative")
    if pence == 0 and not allow_zero:
        raise ValueError("Amount must be more than zero")
    if abs(pence) > MAX_PENCE:
        raise ValueError("Amount is implausibly large")
    return pence


def pounds(pence: int) -> Decimal:
    return (Decimal(pence) / 100).quantize(PENNY)


def round_pence(amount_pounds: Decimal) -> int:
    """Decimal pounds -> pence, half-up (e.g. 7.555 -> 756)."""
    return int((amount_pounds.quantize(PENNY, rounding=ROUND_HALF_UP)) * 100)


def fmt(pence: int | None, *, signed: bool = False) -> str:
    """1234 -> '£12.34'; -320 -> '−£3.20'; signed=True adds '+' for gains. The symbol is the chosen currency's."""
    if pence is None:
        return "—"
    sign = "−" if pence < 0 else ("+" if signed and pence > 0 else "")
    p = abs(int(pence))
    return f"{sign}{symbol()}{p // 100:,}.{p % 100:02d}"


def plain(pence: int | None) -> str:
    """For form values and CSV: '12.34' / '-3.20'."""
    if pence is None:
        return ""
    sign = "-" if pence < 0 else ""
    p = abs(int(pence))
    return f"{sign}{p // 100}.{p % 100:02d}"


# ---------------------------------------------------------------- odds / commission

def parse_odds(value) -> Decimal:
    """Decimal odds ('4', '4.2') or fractional ('7/2' -> 4.5). Must be > 1.01 and sensible."""
    text = str(value or "").strip()
    try:
        if "/" in text:
            num, den = (Decimal(x.strip()) for x in text.split("/", 1))
            if den <= 0 or num <= 0:
                raise ValueError
            odds = (Decimal(1) + num / den).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP).normalize()
        else:
            odds = Decimal(text)
    except (InvalidOperation, ValueError, ArithmeticError):
        raise ValueError("Enter odds like 4.2 or 7/2") from None
    if not odds.is_finite() or odds <= 1 or odds > 1000:
        raise ValueError("Odds must be above 1.0 and at most 1000")
    if odds.as_tuple().exponent < -4:
        raise ValueError("Odds have too many decimal places")
    return odds


def odds_text(odds: Decimal) -> str:
    """Canonical storage/display text: no trailing zeros beyond 2dp ('4.20' -> '4.2', '4' -> '4.00')."""
    q = odds.normalize()
    return f"{q:.2f}" if q == q.quantize(PENNY) else format(q, "f")


def parse_commission(value) -> int:
    """Commission as a percentage ('2', '2%', '0.5') -> basis points (200, 200, 50)."""
    text = str(value if value is not None else "").strip().rstrip("%").strip()
    if text == "":
        return 0
    try:
        pct = Decimal(text)
    except InvalidOperation:
        raise ValueError("Enter commission as a percentage, e.g. 2") from None
    if pct < 0 or pct >= 100:
        raise ValueError("Commission must be between 0 and 100%")
    bp = (pct * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    if bp != pct * 100:
        raise ValueError("Commission has too many decimal places")
    return int(bp)


def commission_fraction(bp: int) -> Decimal:
    return Decimal(bp) / Decimal(10000)


def commission_text(bp: int) -> str:
    return format((Decimal(bp) / 100).normalize(), "f") + "%"
