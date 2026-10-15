"""Back/lay matching calculator. It only tells you what to place; it places nothing.

Standard formulae (c = exchange commission as a fraction):

* Qualifying bet:   lay = back_stake * back_odds / (lay_odds - c)
* Free bet, SNR:    lay = free * (back_odds - 1) / (lay_odds - c)
* Free bet, SR:     lay = free * back_odds / (lay_odds - c)

Lay stake is rounded to the penny (half-up), as exchanges take pence; every outcome
is then computed from that rounded stake so the figures match what you will see.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .money import commission_fraction, round_pence

KINDS = ("qualifying", "free_bet_snr", "free_bet_sr")


@dataclass(frozen=True)
class Match:
    kind: str
    back_stake: int          # pence (for free bets: face value)
    back_odds: Decimal
    lay_odds: Decimal
    commission_bp: int
    lay_stake: int           # pence
    liability: int           # pence
    if_back_wins: int        # pence: net result if the back selection wins
    if_lay_wins: int         # pence: net result if it loses (lay wins)

    @property
    def worst(self) -> int:
        return min(self.if_back_wins, self.if_lay_wins)

    @property
    def expected(self) -> int:
        """The figure to record as expected profit/loss: the worse of the two outcomes
        (they differ by at most a penny or two after rounding)."""
        return self.worst

    @property
    def retention_pct(self) -> Decimal | None:
        """Free bets: % of face value kept as cash."""
        if self.kind == "qualifying" or not self.back_stake:
            return None
        return (Decimal(self.worst) * 100 / Decimal(self.back_stake)).quantize(Decimal("0.1"))


def match(kind: str, back_stake: int, back_odds: Decimal, lay_odds: Decimal, commission_bp: int) -> Match:
    if kind not in KINDS:
        raise ValueError("Unknown bet type")
    if back_stake <= 0:
        raise ValueError("Back stake must be more than zero")
    c = commission_fraction(commission_bp)
    if lay_odds - c <= 0:
        raise ValueError("Lay odds are too low for this commission")
    stake = Decimal(back_stake) / 100
    if kind == "qualifying":
        lay = round_pence(stake * back_odds / (lay_odds - c))
    elif kind == "free_bet_snr":
        lay = round_pence(stake * (back_odds - 1) / (lay_odds - c))
    else:
        lay = round_pence(stake * back_odds / (lay_odds - c))
    liability = lay_liability(lay, lay_odds)
    lay_net_win = lay - round_pence(Decimal(lay) / 100 * c)   # lay stake kept, less commission
    if kind == "qualifying":
        back_profit = round_pence(stake * (back_odds - 1))
        if_back = back_profit - liability
        if_lay = lay_net_win - back_stake
    elif kind == "free_bet_snr":
        if_back = round_pence(stake * (back_odds - 1)) - liability
        if_lay = lay_net_win
    else:  # SR: the free stake comes back too
        if_back = round_pence(stake * back_odds) - liability
        if_lay = lay_net_win
    return Match(kind, back_stake, back_odds, lay_odds, commission_bp, lay, liability, if_back, if_lay)


def lay_liability(lay_stake: int, lay_odds: Decimal) -> int:
    return round_pence(Decimal(lay_stake) / 100 * (lay_odds - 1))
