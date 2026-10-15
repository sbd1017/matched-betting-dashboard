"""The outcome engine: profit and loss of a set of bets in every outcome bucket.

An outcome model is a list of mutually exclusive, exhaustive buckets (e.g. "1-1", "2-2", "Any other score"). Every
leg says which buckets its selection covers:
  back  wins in the buckets it covers, loses in the rest
  lay   loses (pays its liability) in the buckets its selection covers, wins (keeps the stake less commission) otherwise
Nothing is assumed about how lays relate to each other or to the back: two lays on 1-1 and 2-2 can't both lose, and
the table shows exactly that.

A simple bet needs no model: two buckets, "<selection>" and "Not <selection>", every leg covering the first.

Back legs: cash (stake at risk), SNR free bet (winnings only, nothing lost) or SR free bet (stake returned too).
Amounts are integer pence, rounded half-up once per leg, exactly like the ledger settles them (services).

`hedge` finds lay stakes: the standard single back / single lay case uses the app's existing calculator (calc.match)
so figures are identical to every other screen; anything else solves for equal profit in every bucket and then
applies a liability cap if one is given.
"""
from __future__ import annotations

from decimal import Decimal

from . import calc
from .money import commission_fraction, round_pence

FREE = {"free_bet_snr": "snr", "free_bet_sr": "sr"}
LOCKED_SPREAD = 50          # pence: buckets within this of each other count as locked (rounding)


class ModelError(ValueError):
    pass


def simple_buckets(selection: str) -> list[str]:
    sel = (selection or "Selection").strip()[:60]
    return [sel, f"Not {sel}"]


def leg_pnl(leg: dict, bucket: str) -> int:
    """One leg's P&L in one bucket. leg: side, stake_pence, odds, commission_bp, covers (list), free (None/snr/sr)."""
    stake, odds = int(leg["stake_pence"]), Decimal(str(leg["odds"]))
    covered = bucket in leg["covers"]
    if leg["side"] == "back":
        free = leg.get("free")
        if covered:
            if free == "sr":
                return round_pence(Decimal(stake) / 100 * odds)
            return round_pence(Decimal(stake) / 100 * (odds - 1))
        return 0 if free else -stake
    if covered:
        return -calc.lay_liability(stake, odds)
    return stake - round_pence(Decimal(stake) / 100 * commission_fraction(int(leg.get("commission_bp") or 0)))


def check_model(buckets: list[str], legs: list[dict]) -> None:
    if len(buckets) < 2 or len(set(buckets)) != len(buckets):
        raise ModelError("outcomes: at least two different outcome buckets")
    for i, leg in enumerate(legs, 1):
        bad = [c for c in leg["covers"] if c not in buckets]
        if bad:
            raise ModelError(f"leg {i}: covers unknown outcome(s) {', '.join(bad)}")
        if not leg["covers"]:
            raise ModelError(f"leg {i}: say which outcome(s) its selection covers")
        if set(leg["covers"]) == set(buckets):
            raise ModelError(f"leg {i}: covers every outcome, so it can't lose or can't win")


def table(buckets: list[str], legs: list[dict]) -> dict:
    """P&L of every leg in every bucket, the total per bucket, worst / best case and whether it is locked."""
    check_model(buckets, legs)
    rows = []
    for b in buckets:
        per_leg = [leg_pnl(l, b) for l in legs]
        rows.append({"outcome": b, "legs": per_leg, "total": sum(per_leg)})
    totals = [r["total"] for r in rows]
    worst, best = min(totals), max(totals)
    return {"buckets": rows, "worst": worst, "best": best, "worst_outcome": rows[totals.index(worst)]["outcome"],
            "best_outcome": rows[totals.index(best)]["outcome"], "spread": best - worst,
            "locked": best - worst <= LOCKED_SPREAD, "guaranteed": worst}


def exchange_exposure(buckets: list[str], legs: list[dict], account_id) -> int:
    """What an exchange holds back for these lays: the largest loss its lays can make together in any bucket."""
    lays = [l for l in legs if l["side"] == "lay" and l.get("account_id") == account_id]
    if not lays:
        return 0
    return max(0, -min(sum(leg_pnl(l, b) for l in lays) for b in buckets))


def lay_liability_total(legs: list[dict]) -> int:
    return sum(calc.lay_liability(int(l["stake_pence"]), Decimal(str(l["odds"]))) for l in legs if l["side"] == "lay")


# ---------------------------------------------------------------- hedging

def _solve(a: list[list[float]], y: list[float]) -> list[float]:
    """Least squares a·x ≈ y by the normal equations and Gaussian elimination (tiny systems)."""
    n = len(a[0])
    ata = [[sum(a[r][i] * a[r][j] for r in range(len(a))) for j in range(n)] for i in range(n)]
    aty = [sum(a[r][i] * y[r] for r in range(len(a))) for i in range(n)]
    m = [row[:] + [aty[i]] for i, row in enumerate(ata)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            raise ModelError("The lays can't balance these outcomes: each lay needs a different outcome set")
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                m[r] = [x - f * c for x, c in zip(m[r], m[col])]
    return [m[i][n] / m[i][i] for i in range(n)]


def hedge(buckets: list[str], backs: list[dict], lays: list[dict], *, max_liability: int | None = None,
          kind: str = "qualifying", fixed: list[dict] = ()) -> dict:
    """Lay stakes (pence) for `lays` (each: covers, odds, commission_bp; stake ignored) that equalise the result in
    every bucket, scaled down to fit `max_liability` if given. Returns stakes, the table and how it was solved."""
    if not lays:
        raise ModelError("Add at least one lay to hedge")
    simple = (not fixed and len(buckets) == 2 and len(backs) == 1 and len(lays) == 1
              and backs[0]["covers"] == lays[0]["covers"] == [buckets[0]])
    method = "standard"
    if simple:
        b, l = backs[0], lays[0]
        calc_kind = {"snr": "free_bet_snr", "sr": "free_bet_sr"}.get(b.get("free"), "qualifying")
        m = calc.match(calc_kind, int(b["stake_pence"]), Decimal(str(b["odds"])), Decimal(str(l["odds"])),
                       int(l.get("commission_bp") or 0))
        stakes = [m.lay_stake]
    else:
        method = "equal_profit"
        base = [sum(leg_pnl({**bk, "side": "back"}, bk_b) for bk in backs) + sum(leg_pnl({**f, "side": "lay"}, bk_b) for f in fixed)
                for bk_b in buckets]
        a, y = [], []
        for i, bucket in enumerate(buckets):
            row = []
            for l in lays:
                odds = float(Decimal(str(l["odds"])))
                c = float(commission_fraction(int(l.get("commission_bp") or 0)))
                row.append(-(odds - 1) if bucket in l["covers"] else (1 - c))
            a.append(row + [-1.0])
            y.append(-float(base[i]))
        sol = _solve(a, y)
        stakes = [max(0, int(round(s))) for s in sol[:-1]]
        if any(s <= 0 for s in stakes):
            raise ModelError("No positive lay stakes balance these outcomes; check which outcomes each lay covers")
    full = [{**l, "stake_pence": s, "side": "lay"} for l, s in zip(lays, stakes)]
    capped = False
    if max_liability is not None and lay_liability_total(full) > max_liability > 0:
        factor = Decimal(max_liability) / Decimal(lay_liability_total(full))
        stakes = [max(1, int((Decimal(s) * factor).to_integral_value(rounding="ROUND_FLOOR"))) for s in stakes]
        full = [{**l, "stake_pence": s} for l, s in zip(full, stakes)]
        capped, method = True, method + "+liability_cap"
    result = table(buckets, [{**b, "side": "back"} for b in backs] + [{**f, "side": "lay"} for f in fixed] + full)
    return {"lay_stakes": stakes, "method": method, "capped": capped, "table": result,
            "liability": lay_liability_total(full),
            "exposure": max(0, -min(sum(leg_pnl(l, b) for l in full) for b in buckets))}
