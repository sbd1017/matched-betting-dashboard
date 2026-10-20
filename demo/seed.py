"""Demo records: about 18 months of made-up matched betting, recorded through the app's own services.

    python -m demo.seed --data-dir DIR [--today 2026-10-20] [--days 548]

The history is a simulation run forward in time: every deposit, offer, bet, settlement, reward, free bet, withdrawal,
loan and balance check is recorded by bets.services / bets.plans with the app's clock set to the moment it happens.
So the ledger (pence, superseded never edited), the audit trail and each offer's lifecycle are exactly what the app
would have written, and every page balances.

Deterministic: the same --today gives the same records (fixtures are fixed per date, decisions come from one seeded
random generator). Dates are worked out back from --today, so a nightly rebuild keeps the demo current.
The data directory must be new or empty: nothing is ever added to an existing database.
"""
from __future__ import annotations

import argparse
import heapq
import random
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from bets import calc, exposure, metrics, outcomes, plans, services, times
from bets.db import Database

from . import fixtures as fx
from .fixtures import UK, pounds

SEED = 1017
HUB, LENDER = "Monzo", "Nationwide"
EXCHANGES = {"Smarkets": 200, "Betfair Exchange": 500, "Matchbook": 150}
PROMO_EXCHANGE = "Betdaq"                       # opened late on for its 0% commission offer (which has now ended)
ODDS_TEXT = {"1.2": "1/5", "1.25": "1/4", "1.4": "2/5", "1.5": "1/2", "1.75": "3/4", "2.0": "evens", "4.0": "3/1"}

# bookmaker, offer title, qualifying stake, min odds, free bets, each, expiry days, reward trigger
SIGNUPS = [
    ("Bet365", "Bet £10 Get £30 in Free Bets", 1000, "1.2", 3, 1000, 30, "bet_settled"),
    ("Sky Bet", "Bet £10 Get £40 in Free Bets", 1000, "1.5", 4, 1000, 30, "bet_placed"),
    ("William Hill", "Bet £10 Get £30 in Free Bets", 1000, "1.5", 3, 1000, 30, "bet_settled"),
    ("Paddy Power", "Bet £5 Get £20 in Free Bets", 500, "1.5", 4, 500, 30, "bet_settled"),
    ("Betfred", "Bet £10 Get £50 in Free Bets", 1000, "2.0", 5, 1000, 7, "bet_settled"),
    ("Coral", "Bet £5 Get £20 in Free Bets", 500, "1.5", 4, 500, 7, "bet_settled"),
    ("Ladbrokes", "Bet £5 Get £20 in Free Bets", 500, "1.5", 4, 500, 7, "bet_settled"),
    ("BetVictor", "Bet £10 Get £40 in Free Bets", 1000, "2.0", 4, 1000, 7, "bet_settled"),
    ("Betway", "Bet £10 Get £10 Free Bet", 1000, "1.75", 1, 1000, 7, "bet_settled"),
    ("Unibet", "Bet £10 Get £40 in Free Bets", 1000, "1.4", 4, 1000, 7, "bet_settled"),
    ("888sport", "Bet £10 Get £30 in Free Bets", 1000, "2.0", 3, 1000, 7, "bet_settled"),
    ("BoyleSports", "Bet £10 Get £20 in Free Bets", 1000, "1.5", 2, 1000, 7, "bet_settled"),
    ("Virgin Bet", "Bet £10 Get £20 in Free Bets", 1000, "2.0", 2, 1000, 7, "bet_settled"),
    ("Midnite", "Bet £10 Get £20 in Free Bets", 1000, "1.5", 2, 1000, 7, "bet_settled"),
    ("Kwiff", "Bet £10 Get £30 in Free Bets", 1000, "1.5", 3, 1000, 7, "bet_settled"),
    ("LiveScore Bet", "Bet £10 Get £20 in Free Bets", 1000, "1.5", 2, 1000, 7, "bet_settled"),
    ("BetUK", "Bet £10 Get £30 in Free Bets", 1000, "1.5", 3, 1000, 7, "bet_settled"),
    ("Star Sports", "Bet £10 Get £20 in Free Bets", 1000, "2.0", 2, 1000, 7, "bet_settled"),
]

# bookmaker, title, offer type, what it is on, stake, min odds, free bets, each, weekdays, chance a week, trigger
RELOADS = [
    ("William Hill", "Bet £10 on the Premier League, get a £5 free bet", "reload", "pl", 1000, "1.5", 1, 500, [5, 6], 0.8, "bet_settled"),
    ("Paddy Power", "Bet Builder Club: bet £10 on a Bet Builder, get a £5 free bet", "bet_builder", "bb", 1000, "4.0", 1, 500, [5, 6], 0.8, "bet_settled"),
    ("Sky Bet", "Bet £10 on the Champions League, get a £5 free bet", "reload", "cl", 1000, "1.5", 1, 500, [1, 2], 0.85, "bet_placed"),
    ("Ladbrokes", "Bet £5 on Saturday football, get a £5 free bet", "reload", "football", 500, "1.5", 1, 500, [5], 0.8, "bet_settled"),
    ("Coral", "Bet £10 on the Championship, get a £5 free bet", "reload", "champ", 1000, "1.5", 1, 500, [5, 1, 4], 0.75, "bet_settled"),
    ("Betfred", "Bet £10 on a Bet Builder, get a £5 free bet", "bet_builder", "bb", 1000, "4.0", 1, 500, [1, 2, 5], 0.7, "bet_settled"),
    ("Betfred", "Super Sunday: bet £20, get a £10 free bet", "reload", "football", 2000, "1.5", 1, 1000, [6], 0.6, "bet_settled"),
    ("BetVictor", "Bet £20 on Saturday racing, get a £10 free bet", "reload", "racing", 2000, "2.0", 1, 1000, [5], 0.7, "bet_settled"),
    ("Betway", "Bet £10 on the tennis, get a £5 free bet", "reload", "tennis", 1000, "1.5", 1, 500, [2, 3, 4], 0.8, "bet_settled"),
    ("Unibet", "Weekly reload: bet £10 on racing, get a £5 free bet", "reload", "racing", 1000, "2.0", 1, 500, [2, 3], 0.7, "bet_settled"),
    ("BoyleSports", "Monday Club: bet £10, get a £5 free bet", "reload", "any", 1000, "1.5", 1, 500, [0], 0.7, "bet_settled"),
    ("Virgin Bet", "Bet £10 on the Saturday football, get a £5 free bet", "reload", "football", 1000, "1.5", 1, 500, [5], 0.65, "bet_settled"),
    ("888sport", "Bet £10 on the Premier League, get a £5 free bet", "reload", "pl", 1000, "1.5", 1, 500, [6, 0], 0.6, "bet_settled"),
    ("Midnite", "Bet £10 on any football, get a £5 free bet", "reload", "football", 1000, "1.5", 1, 500, [1, 5], 0.5, "bet_settled"),
    ("Kwiff", "Bet £10 get a £5 free bet", "reload", "any", 1000, "1.5", 1, 500, [3, 4], 0.4, "bet_settled"),
    ("Paddy Power", "Bet £10 on any race, get a £5 free bet", "reload", "racing", 1000, "2.0", 1, 500, [0, 1, 2, 3, 4], 0.65, "bet_settled"),
    ("Sky Bet", "Bet £10 on Saturday racing, get a £5 free bet", "reload", "racing", 1000, "2.0", 1, 500, [5], 0.6, "bet_settled"),
    ("William Hill", "Bet £10 on any race, get a £5 free bet", "reload", "racing", 1000, "2.0", 1, 500, [2, 3, 4], 0.55, "bet_settled"),
    ("Ladbrokes", "Bet £10 on the Premier League, get a £5 free bet", "reload", "pl", 1000, "1.5", 1, 500, [6, 0], 0.6, "bet_settled"),
    ("Coral", "Bet £10 on Sunday football, get a £5 free bet", "reload", "football", 1000, "1.5", 1, 500, [6], 0.6, "bet_settled"),
    ("LiveScore Bet", "Bet £10 on the Premier League, get a £5 free bet", "reload", "pl", 1000, "1.5", 1, 500, [5], 0.5, "bet_settled"),
]
SEASONAL = {  # festival offers: bookmaker, title template, stake, free bet each
    "cheltenham": [("Betfred", "Cheltenham Day {n}: bet £10, get a £5 free bet", 1000, 500),
                   ("Coral", "Cheltenham Day {n}: bet £10, get a £5 free bet", 1000, 500),
                   ("Ladbrokes", "Cheltenham Day {n}: bet £10, get a £5 free bet", 1000, 500),
                   ("William Hill", "Cheltenham Day {n}: bet £10, get a £5 free bet", 1000, 500)],
    "national": [("Betfred", "Grand National: bet £10, get a £10 free bet", 1000, 1000),
                 ("Paddy Power", "Grand National: bet £10, get a £10 free bet", 1000, 1000),
                 ("Coral", "Grand National: bet £10, get a £10 free bet", 1000, 1000),
                 ("Sky Bet", "Grand National: bet £10, get a £10 free bet", 1000, 1000),
                 ("BetVictor", "Grand National: bet £10, get a £10 free bet", 1000, 1000)],
    "ascot": [("William Hill", "Royal Ascot Day {n}: bet £10, get a £5 free bet", 1000, 500),
              ("Coral", "Royal Ascot Day {n}: bet £10, get a £5 free bet", 1000, 500)],
    "worldcup": [("William Hill", "World Cup: bet £10, get a £5 free bet", 1000, 500),
                 ("Paddy Power", "World Cup: bet £10 on a Bet Builder, get a £5 free bet", 1000, 500),
                 ("Ladbrokes", "World Cup: bet £5, get a £5 free bet", 500, 500),
                 ("Sky Bet", "World Cup: bet £10, get a £5 free bet", 1000, 500),
                 ("Coral", "World Cup: bet £10, get a £5 free bet", 1000, 500)],
}
COMPETITION_OFFERS = {
    "pl": ("Bet £10 on the Premier League, get a £5 free bet", "pl"),
    "champ": ("Bet £10 on the Championship, get a £5 free bet", "champ"),
    "cl": ("Bet £10 on the Champions League, get a £5 free bet", "cl"),
    "wc": ("World Cup: bet £10, get a £5 free bet", "football"),
    "tennis": ("Bet £10 on the tennis, get a £5 free bet", "tennis"),
    "racing": ("Bet £10 on any race, get a £5 free bet", "racing"),
}
SCOPE = {"pl": "any Premier League match", "champ": "any Championship match", "cl": "any Champions League match",
         "football": "any football match", "racing": "any UK or Irish race", "tennis": "any tennis match",
         "any": "any sport", "bb": "a Bet Builder with 3 or more selections"}


def iso(dt: datetime) -> str:
    return times.iso(dt)


class Clock:
    t = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _frozen_now() -> datetime:
    return Clock.t


class Demo:
    def __init__(self, conn, today: date, days: int):
        self.conn = conn
        self.rng = random.Random(SEED)
        self.end = datetime(today.year, today.month, today.day, 4, 0, tzinfo=UK).astimezone(timezone.utc)
        self.start = self.end - timedelta(days=days)
        self.start_day = self.start.astimezone(UK).date()
        self.q: list = []
        self.n = 0
        self.acc: dict[str, int] = {}
        self.restricted: set[str] = set()
        self.used: set[tuple] = set()
        self.plan_from = self.end - timedelta(days=75)            # bet plans were taken up for the last ten weeks
        self.promo = (self.end - timedelta(days=45), self.end - timedelta(days=6))

    # ------------------------------------------------------------------ the clock and the queue

    def at(self, when: datetime, fn, *args, **kw) -> None:
        heapq.heappush(self.q, (max(when, self.start, Clock.t), self.n, fn, args, kw))
        self.n += 1

    def run(self) -> None:
        while self.q:
            when, _, fn, args, kw = heapq.heappop(self.q)
            if when > self.end:
                break
            Clock.t = when.replace(microsecond=0)
            fn(*args, **kw)
        Clock.t = self.end

    def local(self, d: date, hhmm: str) -> datetime:
        return fx.uk(d, hhmm)

    def jitter(self, base: datetime, lo_min: float, hi_min: float) -> datetime:
        return base + timedelta(minutes=self.rng.uniform(lo_min, hi_min))

    def awake(self, dt: datetime) -> datetime:
        """People do this between 07:30 and 23:30: anything at night happens the next morning."""
        loc = dt.astimezone(UK)
        if loc.hour < 7 or (loc.hour == 7 and loc.minute < 30):
            return self.jitter(fx.uk(loc.date(), "07:40"), 0, 120)
        if loc.hour >= 23 and loc.minute >= 30:
            return self.jitter(fx.uk(loc.date() + timedelta(days=1), "07:40"), 0, 120)
        return dt

    # ------------------------------------------------------------------ money

    def balance(self, name: str) -> int:
        return services.balance(self.conn, self.acc[name])

    def available(self, name: str) -> int:
        aid = self.acc[name]
        return services.balance(self.conn, aid) - metrics.open_lay_liability(self.conn, aid) \
            - plans.unmatched_order_reserve(self.conn, aid)

    def transfer(self, src: str, dst: str, amount: int, capital_type: str, notes: str = "", **kw) -> int:
        if self.conn.execute("SELECT 1 FROM transfers WHERE from_account_id = ? AND to_account_id = ? AND amount_pence = ? "
                             "AND at >= ?", (self.acc[src], self.acc[dst], amount,
                                             iso(Clock.t - timedelta(minutes=20)))).fetchone():
            amount += 500                                    # a second deposit straight after is never the same amount
        return services.create_transfer(self.conn, from_id=self.acc[src], to_id=self.acc[dst], amount=amount,
                                        at=times.now_iso(), capital_type=capital_type, notes=notes, **kw)

    def fund_bookie(self, name: str, need: int) -> None:
        bal = self.balance(name)
        if bal >= need:
            return
        short = need - bal
        amount = max(1000, -(-short // 500) * 500 + self.rng.choice([0, 0, 500, 1000]))
        self.transfer(HUB, name, amount, "bankroll_contribution")

    def fund_exchange(self, name: str, liability: int) -> None:
        avail = self.available(name)
        if avail >= liability + 1000:
            return
        amount = -(-(liability + 1000 - avail) // 5000) * 5000 + self.rng.choice([0, 5000, 10000])
        self.transfer(HUB, name, amount, "bankroll_contribution")

    def withdrawal_type(self, amount: int) -> str:
        """Winnings come out as a profit withdrawal while there is retained profit to cover it."""
        s = metrics.summary(self.conn)
        return "profit_withdrawal" if s.retained_profit - 15000 >= amount else "bankroll_withdrawal"

    # ------------------------------------------------------------------ prices

    def exchange_for(self, sport: str) -> str:
        return self.exchanges_for(sport)[0]

    def exchanges_for(self, sport: str) -> list[str]:
        """The exchanges worth comparing: racing mostly goes to Betfair for its liquidity."""
        if self.promo[0] <= Clock.t < self.promo[1] and PROMO_EXCHANGE in self.acc:
            return [PROMO_EXCHANGE, "Smarkets"]
        if sport == "racing":
            return ["Betfair Exchange"] if self.rng.random() < 0.75 else ["Smarkets", "Betfair Exchange"]
        out = ["Smarkets", "Betfair Exchange"] + (["Matchbook"] if "Matchbook" in self.acc else [])
        self.rng.shuffle(out)
        return out

    def commission(self, exchange: str) -> int:
        return services.row(self.conn, "accounts", self.acc[exchange])["commission_bp"]

    def matches_scope(self, f: fx.Fixture, scope: str) -> bool:
        if scope in ("pl", "champ", "cl"):
            return f.competition == scope
        if scope in ("football", "bb"):
            return f.sport == "football"
        if scope in ("racing", "tennis"):
            return f.sport == scope
        return True

    def candidates(self, day_from: date, days: int, after: datetime, before: datetime | None, scope: str):
        for i in range(days):
            for f in fx.on(day_from + timedelta(days=i)):
                if f.at > after and (before is None or f.at < before) and self.matches_scope(f, scope):
                    yield f

    def best_bet(self, book: str, kind: str, stake: int, fixtures, lo: float, hi: float, min_odds: str | None):
        """What an oddsmatcher would show: the closest back/lay match among these fixtures."""
        best = None
        for f in fixtures:
            exchanges = self.exchanges_for(f.sport)
            for s in f.selections:
                if not lo <= 1 / s.p <= hi or (book, f.event, s.name) in self.used:
                    continue
                back = fx.bookie_price(self.rng, s.p)
                if min_odds and back < Decimal(min_odds):
                    continue
                for ex in exchanges:
                    lay = max(fx.lay_price(self.rng, s.p, f.sport, ex), back)
                    m = calc.match(kind, stake, back, lay, self.commission(ex))
                    score = m.worst + self.rng.uniform(0, 6)       # not always the very best: people take a good one
                    if best is None or score > best[0]:
                        best = (score, f, s, back, lay, ex, m)
        return best[1:] if best else None

    # ------------------------------------------------------------------ recording bets

    def place_simple(self, *, kind: str, book: str, f: fx.Fixture, s: fx.Selection, back: Decimal, lay: Decimal,
                     ex: str, m: calc.Match, stake: int, offer_id=None, free_bet_id=None, settle: bool = True) -> int:
        self.used.add((book, f.event, s.name))
        if not free_bet_id:
            self.fund_bookie(book, stake)
        self.fund_exchange(ex, m.liability)
        if Clock.t >= self.plan_from:
            data = {"event": f.event, "event_at": iso(f.at), "market": f.market, "selection": s.name, "kind": kind,
                    "bookmaker": book, "back_odds": str(back), "exchange": ex, "lay_odds": str(lay), "sport": f.sport,
                    "offer_id": offer_id, "free_bet_id": free_bet_id, "allow_duplicate": True}
            if not free_bet_id:
                data["back_stake"] = pounds(stake)
            plan_id = plans.create(self.conn, data)
            plans.validate(self.conn, plan_id)
            plans.record_matched_bet(self.conn, plan_id, back={"stake": pounds(stake), "odds": str(back)})
            pid = plans.load(self.conn, plan_id)["position_id"]
        else:
            now = times.now_iso()
            pid = services.create_position(
                self.conn, event=f.event, market=f.market, selection=s.name, event_at=iso(f.at), kind=kind,
                offer_id=offer_id, expected_pnl=m.worst, legs=[
                    {"account_id": self.acc[book], "side": "back", "stake_pence": stake, "odds": str(back),
                     "free_bet_id": free_bet_id, "placed_at": now},
                    {"account_id": self.acc[ex], "side": "lay", "stake_pence": m.lay_stake, "odds": str(lay),
                     "placed_at": now}])
        if settle:
            won = self.rng.random() < s.p
            self.at(self.settle_time(f), self.settle_simple, pid, won)
        return pid

    def settle_time(self, f: fx.Fixture) -> datetime:
        done = f.end + timedelta(minutes=self.rng.uniform(5, 45))
        if self.rng.random() < 0.35:
            done += timedelta(hours=self.rng.uniform(1, 14))       # settled when next looked at
        done = self.awake(done)
        if f.end < self.end <= done:                               # finished before the demo's today: settled already
            done = max(f.end + timedelta(minutes=5), self.end - timedelta(minutes=5))
        return done

    def settle_simple(self, pid: int, won: bool) -> None:
        pos = services.row(self.conn, "positions", pid)
        if pos["status"] == "open":
            services.quick_settle(self.conn, pid, "back_won" if won else "back_lost")

    def place_builder(self, *, book: str, f: fx.Fixture, stake: int, offer_id=None) -> int | None:
        """A Bet Builder covered by correct score lays: Draw / BTTS / Under 4.5 (1-1 or 2-2), or Home / BTTS / Under
        3.5 (2-1). The outcome table is what the plan records, so liability and settlement use the real outcomes."""
        home, away = f.home, f.away
        ex = self.exchange_for("football")
        com = self.commission(ex)
        if self.rng.random() < 0.6:
            p11, p22 = self.rng.uniform(0.105, 0.13), self.rng.uniform(0.05, 0.065)
            back = Decimal(str(round(1 / (p11 + p22) * self.rng.uniform(0.83, 0.9), 2)))
            buckets = ["1-1", "2-2", "Any other score"]
            covers = ["1-1", "2-2"]
            lays = [("1-1", fx.to_tick(1 / p11 * self.rng.uniform(1.0, 1.04))),
                    ("2-2", fx.to_tick(1 / p22 * self.rng.uniform(1.0, 1.05)))]
            selection = "Draw / Both teams to score / Under 4.5 goals"
            probs = {"1-1": p11, "2-2": p22}
        else:
            p21 = self.rng.uniform(0.085, 0.105)
            back = Decimal(str(round(1 / p21 * self.rng.uniform(0.86, 0.93), 2)))
            buckets = ["2-1", "Any other score"]
            covers = ["2-1"]
            lays = [("2-1", fx.to_tick(1 / p21 * self.rng.uniform(1.0, 1.035)))]
            selection = f"{home} / Both teams to score / Under 3.5 goals"
            probs = {"2-1": p21}
        h = outcomes.hedge(buckets, [{"stake_pence": stake, "odds": str(back), "covers": covers}],
                           [{"odds": str(o), "covers": [b], "commission_bp": com} for b, o in lays])
        if h["table"]["worst"] < -150:
            return None
        self.used.add((book, f.event, selection))
        self.fund_bookie(book, stake)
        self.fund_exchange(ex, h.get("exposure") or h["liability"])
        plan_id = plans.create(self.conn, {
            "event": f.event, "event_at": iso(f.at), "market": "Bet Builder", "selection": selection,
            "kind": "qualifying", "bookmaker": book, "back_stake": pounds(stake), "back_odds": str(back),
            "outcomes": buckets, "back_covers": covers, "offer_id": offer_id, "sport": "football",
            "builder_legs": 3, "allow_duplicate": True,
            "lays": [{"exchange": ex, "odds": str(o), "covers": [b], "label": f"Lay {b}", "market": "Correct Score"}
                     for b, o in lays]})
        plans.validate(self.conn, plan_id)
        plans.record_matched_bet(self.conn, plan_id, back={"stake": pounds(stake), "odds": str(back)})
        pid = plans.load(self.conn, plan_id)["position_id"]
        r, score = self.rng.random(), "Any other score"
        for b, p in probs.items():
            if r < p:
                score = b
                break
            r -= p
        self.at(self.settle_time(f), self.settle_by_score, pid, score)
        return pid

    def settle_by_score(self, pid: int, score: str) -> None:
        data = exposure.valid(self.conn, pid)
        if not data:
            return
        legs, scenarios = data
        s = next(x for x in scenarios if x["label"] == score)
        for leg in legs:
            if leg["result"] == "pending":
                services.settle_leg(self.conn, leg["id"], "won" if s["pnl"][str(leg["id"])] > 0 else "lost",
                                    note=f"final score {score}")

    # ------------------------------------------------------------------ offers

    def create_offer(self, *, book: str, title: str, offer_type: str, scope: str, stake: int, min_odds: str | None,
                     count: int, each: int, expiry_days: int, trigger: str, expires: datetime) -> int:
        frac = ODDS_TEXT.get(min_odds or "", "")
        odds = f" at odds of {frac} ({min_odds}) or bigger" if min_odds else ""
        when = "as soon as the bet is placed" if trigger == "bet_placed" else "once the bet settles"
        requirement = (f"Place a £{stake // 100} bet on {SCOPE[scope]}{odds}. Free bet{'s' if count > 1 else ''} "
                       f"credited {when}, valid for {expiry_days} days.")
        return services.create_offer(
            self.conn, title=title, account_id=self.acc[book], offer_type=offer_type, requirement=requirement,
            starts_at=times.now_iso(), expires_at=iso(expires),
            terms={"qualifying_stake_pence": stake, "min_odds": min_odds, "reward_count": count,
                   "reward_each_pence": each, "reward_kind": "snr", "reward_expiry_days": expiry_days,
                   "reward_trigger": trigger})

    def qualify(self, offer_id: int, book: str, scope: str, stake: int, min_odds: str | None, trigger: str,
                fixtures: list, flags: dict) -> None:
        """Place the qualifying bet (from what the oddsmatcher shows now), then settle it and collect the reward."""
        if scope == "bb":
            tries = [f for f in fixtures if f.at > Clock.t + timedelta(minutes=20)] * 3
            if not any(self.place_builder(book=book, f=f, stake=stake, offer_id=offer_id) for f in tries):
                return self.skip(offer_id)
        else:
            lo, hi = (1.7, 4.0) if scope != "racing" else (2.5, 7.0)
            pick = self.best_bet(book, "qualifying", stake, [f for f in fixtures if f.at > Clock.t + timedelta(minutes=20)],
                                 lo, hi, min_odds)
            if not pick:
                return self.skip(offer_id)
            f, s, back, lay, ex, m = pick
            self.place_simple(kind="qualifying", book=book, f=f, s=s, back=back, lay=lay, ex=ex, m=m, stake=stake,
                              offer_id=offer_id, settle=not flags.get("no_settle"))
            if flags.get("no_settle"):
                return
        if flags.get("no_reward"):
            return
        if trigger == "bet_placed":
            self.at(self.jitter(Clock.t, 5, 90), self.reward, offer_id, book)
        else:
            self.at(self.reward_time(), self.reward_after_settle, offer_id, book)

    def skip(self, offer_id: int) -> None:
        """No close enough match before the deadline: left alone (people skip the odd offer)."""
        services.set_offer_status(self.conn, offer_id, "abandoned", note="no close enough match in time, skipped")

    def reward_time(self) -> datetime:
        return Clock.t + timedelta(minutes=10)

    def reward_after_settle(self, offer_id: int, book: str) -> None:
        """Wait for the qualifier to settle, then for the bookmaker to credit the reward."""
        o = services.row(self.conn, "offers", offer_id)
        if o["status"] == "qualifying":
            self.at(Clock.t + timedelta(hours=2), self.reward_after_settle, offer_id, book)
            return
        if o["status"] != "awaiting_reward":
            return
        delay = self.rng.uniform(5, 180) if self.rng.random() < 0.65 else self.rng.uniform(600, 1200)
        self.at(self.awake(self.jitter(Clock.t, delay, delay)), self.reward, offer_id, book)

    def reward(self, offer_id: int, book: str) -> None:
        o = services.row(self.conn, "offers", offer_id)
        if o["status"] in ("complete", "expired", "abandoned"):
            return
        for fid in services.receive_reward(self.conn, offer_id):
            if Clock.t > self.end - timedelta(days=6.7) and self.rng.random() < 0.6:
                continue                                    # recent free bets still waiting to be used (and expiring)
            self.at(self.awake(self.jitter(Clock.t, 20, 60 * 24 * self.rng.choice([0.3, 0.8, 1.5, 2.5, 4]))),
                    self.use_free_bet, fid, book)

    def use_free_bet(self, fid: int, book: str) -> None:
        fb = services.row(self.conn, "free_bets", fid)
        if fb["status"] != "available":
            return
        expires = times.parse(fb["expires_at"]) if fb["expires_at"] else Clock.t + timedelta(days=7)
        if self.rng.random() < 0.012:
            return                                          # forgotten: it expires unused (it happens)
        today = Clock.t.astimezone(UK).date()
        for scope in ("football", "tennis", "racing"):
            fixtures = list(self.candidates(today, 2, Clock.t + timedelta(minutes=30), expires, scope))
            if fixtures:
                break
        lo, hi = (4.0, 9.5) if scope != "racing" else (4.5, 12.0)
        pick = self.best_bet(book, "free_bet_snr", fb["face_value_pence"], fixtures, lo, hi, None)
        if not pick:
            if Clock.t + timedelta(days=1) < expires:
                self.at(Clock.t + timedelta(days=1), self.use_free_bet, fid, book)
            return
        f, s, back, lay, ex, m = pick
        if m.worst < fb["face_value_pence"] * 0.72 and Clock.t + timedelta(days=1, hours=6) < expires:
            self.at(Clock.t + timedelta(hours=self.rng.uniform(8, 20)), self.use_free_bet, fid, book)
            return                                          # not good enough yet: look again later
        self.place_simple(kind="free_bet_snr", book=book, f=f, s=s, back=back, lay=lay, ex=ex, m=m,
                          stake=fb["face_value_pence"], offer_id=fb["offer_id"], free_bet_id=fid)

    # ------------------------------------------------------------------ the calendar of offers

    def open_account(self, book: str) -> None:
        self.acc[book] = services.create_account(self.conn, name=book, kind="bookmaker")
        self.at(self.jitter(Clock.t, 60 * 20, 60 * 40), self.verify, book)

    def verify(self, book: str) -> None:
        services.update_account(self.conn, self.acc[book], verified=True)

    def sign_up(self, spec) -> None:
        book, title, stake, min_odds, count, each, expiry, trigger = spec
        self.open_account(book)
        oid = self.create_offer(book=book, title=title, offer_type="sign_up", scope="any", stake=stake,
                                min_odds=min_odds, count=count, each=each, expiry_days=expiry, trigger=trigger,
                                expires=Clock.t + timedelta(days=30))
        today = Clock.t.astimezone(UK).date()
        fixtures = list(self.candidates(today, 3, Clock.t + timedelta(minutes=40), None, "football")) or \
            list(self.candidates(today, 2, Clock.t + timedelta(minutes=40), None, "any"))
        self.at(self.jitter(Clock.t, 10, 45), self.qualify, oid, book, "any", stake, min_odds, trigger, fixtures, {})

    def reload(self, spec, day: date, flags: dict | None = None) -> None:
        book, title, otype, scope, stake, min_odds, count, each, _, _, trigger = spec
        if book not in self.acc or book in self.restricted:
            return
        fixtures = [f for f in fx.on(day) if self.matches_scope(f, scope) and f.at > Clock.t + timedelta(hours=1)]
        if not fixtures:
            return
        oid = self.create_offer(book=book, title=title, offer_type=otype, scope=scope, stake=stake, min_odds=min_odds,
                                count=count, each=each, expiry_days=7, trigger=trigger,
                                expires=fx.uk(day, "23:59") + timedelta(days=8))
        first = min(f.at for f in fixtures)
        place = min(first - timedelta(minutes=30), self.awake(self.jitter(Clock.t, 30, 60 * 30)))
        self.at(max(place, Clock.t + timedelta(minutes=5)), self.qualify, oid, book, scope, stake, min_odds, trigger,
                fixtures, flags or {})

    def plan_week(self, monday: date) -> None:
        for spec in RELOADS:
            if self.rng.random() >= spec[9]:
                continue
            day = monday + timedelta(days=self.rng.choice(spec[8]))
            post = fx.uk(day - timedelta(days=self.rng.choice([1, 2, 3])), "09:00")
            self.at(self.jitter(max(post, Clock.t), 0, 240), self.reload, spec, day)
        for d in (monday + timedelta(days=i) for i in range(7)):
            self.festivals(d)
        self.at(fx.uk(monday + timedelta(days=7), "08:00"), self.plan_week, monday + timedelta(days=7))

    def festivals(self, d: date) -> None:
        def offer(book, title, stake, each, scope, day):
            spec = (book, title, "reload", scope, stake, "2.0" if scope == "racing" else "1.5", 1, each, [], 1,
                    "bet_settled")
            self.at(self.jitter(fx.uk(day - timedelta(days=1), "10:00"), 0, 300), self.reload, spec, day)
        chelt = fx.cheltenham(d.year)
        if d in chelt:
            for book, title, stake, each in SEASONAL["cheltenham"]:
                offer(book, title.format(n=chelt.index(d) + 1), stake, each, "racing", d)
        if d == fx.grand_national(d.year):
            for book, title, stake, each in SEASONAL["national"]:
                offer(book, title, stake, each, "racing", d)
        ascot = fx.royal_ascot(d.year)
        if d in ascot:
            for book, title, stake, each in SEASONAL["ascot"]:
                offer(book, title.format(n=ascot.index(d) + 1), stake, each, "racing", d)
        if fx.WORLD_CUP[0] <= d <= fx.WORLD_CUP[1] and d.weekday() in (2, 5):
            for book, title, stake, each in SEASONAL["worldcup"]:
                offer(book, title, stake, each, "bb" if "Bet Builder" in title else "football", d)

    # ------------------------------------------------------------------ routine money management

    def weekly_sweep(self) -> None:
        """Mondays: bookmaker balances over £40 with nothing running go back to the bank."""
        for book, aid in self.acc.items():
            acc = services.row(self.conn, "accounts", aid)
            if acc["kind"] != "bookmaker" or metrics.unsettled_back_stakes(self.conn, aid):
                continue
            bal = self.balance(book)
            if bal >= 4000 or (book in self.restricted and bal >= 500):
                amount = bal // 100 * 100
                tid = self.transfer(book, HUB, amount, self.withdrawal_type(amount), in_transit=True)
                self.at(self.jitter(Clock.t, 60 * 16, 60 * 70), self.arrived, tid)
        self.at(Clock.t + timedelta(days=7), self.weekly_sweep)

    def arrived(self, tid: int) -> None:
        if services.row(self.conn, "transfers", tid)["in_transit"]:
            services.mark_arrived(self.conn, tid)

    def monthly_sweep(self) -> None:
        for ex in [*EXCHANGES, PROMO_EXCHANGE]:
            if ex not in self.acc:
                continue
            spare = self.available(ex) - (60000 if ex in EXCHANGES else 0)
            if spare >= 30000 or (ex == PROMO_EXCHANGE and Clock.t > self.promo[1] and spare >= 1000):
                amount = spare // 5000 * 5000 if ex in EXCHANGES else spare // 100 * 100
                if amount:
                    self.transfer(ex, HUB, amount, self.withdrawal_type(amount))
        loc = Clock.t.astimezone(UK).date()
        nxt = (loc.replace(day=1) + timedelta(days=32)).replace(day=1)
        self.at(self.jitter(fx.uk(nxt, "10:00"), 0, 600), self.monthly_sweep)

    def balance_checks(self) -> None:
        """Every other Sunday evening: check the real balances of accounts with nothing running."""
        for name, aid in self.acc.items():
            acc = services.row(self.conn, "accounts", aid)
            if acc["kind"] == "bank" or self.rng.random() < 0.3:
                continue
            if metrics.unsettled_back_stakes(self.conn, aid) or metrics.open_lay_liability(self.conn, aid):
                continue
            if self.balance(name):
                services.reconcile(self.conn, aid, self.balance(name), at=times.now_iso())
        self.at(Clock.t + timedelta(days=14), self.balance_checks)

    def daily(self) -> None:
        services.expire_overdue(self.conn)
        self.at(Clock.t + timedelta(days=1), self.daily)

    def restrict(self, book: str) -> None:
        self.restricted.add(book)
        services.update_account(self.conn, self.acc[book], standing="restricted",
                                notes="Stakes limited to a few pounds and no more offers since "
                                      f"{Clock.t.astimezone(UK):%B %Y}. Withdraw what is left.")

    # ------------------------------------------------------------------ funding from the second bank

    def loan(self, to: str, amount: int, due_days: int, notes: str, repay_days: float | None, by_hub: bool) -> None:
        tid = self.transfer(LENDER, to, amount, "temporary_funding", notes=notes,
                            due_at=iso(Clock.t + timedelta(days=due_days)))
        if repay_days is not None:
            self.at(self.jitter(Clock.t + timedelta(days=repay_days), 0, 300), self.repay, to, amount, by_hub, tid)

    def repay(self, src: str, amount: int, by_hub: bool, tid: int) -> None:
        if by_hub:
            services.record_funding_repayment(self.conn, from_id=self.acc[HUB], lender_id=self.acc[LENDER],
                                              amount=amount, at=times.now_iso(), notes="Paid back from Monzo")
            return
        part = min(amount, max(0, self.available(src) - 2000) // 100 * 100)
        if part:
            self.transfer(src, LENDER, part, "temporary_return", notes="Returning the money borrowed for the busy week")
        if part < amount:
            services.record_funding_repayment(self.conn, from_id=self.acc[HUB], lender_id=self.acc[LENDER],
                                              amount=amount - part, at=times.now_iso(), notes="The rest, from Monzo")

    # ------------------------------------------------------------------ things left for the visitor to see

    def plant_started(self) -> None:
        """Last night's qualifier that nobody has settled yet (Issues: Started, enter the result)."""
        yday = (self.end - timedelta(days=1)).astimezone(UK).date()
        fixtures = [f for f in fx.on(yday) if f.end < self.end and f.sport == "football"] or \
            [f for f in fx.on(yday) if f.end < self.end and f.sport != "racing"] or \
            [f for f in fx.on(yday) if f.end < self.end]
        if fixtures:
            f = fixtures[-1]
            title, scope = COMPETITION_OFFERS[f.competition if f.competition in COMPETITION_OFFERS else f.sport]
            self.reload_on(("Coral", title, "reload", scope, 1000, "1.5", 1, 500, [], 1, "bet_settled"), [f],
                           {"no_settle": True})

    def plant_overdue(self) -> None:
        """A reward that hasn't arrived two days after the qualifier settled (Issues: Reward)."""
        day = (self.end - timedelta(days=4)).astimezone(UK).date()
        fixtures = [f for f in fx.on(day) if f.sport in ("football", "tennis")] or list(fx.on(day))
        spec = ("Betfred", "Bet £10 on any sport, get a £5 free bet", "reload", fixtures[0].sport, 1000, "1.5", 1, 500,
                [], 1, "bet_settled")
        self.reload_on(spec, fixtures, {"no_reward": True})

    def reload_on(self, spec, fixtures, flags) -> None:
        book, title, otype, scope, stake, min_odds, count, each, _, _, trigger = spec
        if book not in self.acc or not fixtures:
            return
        oid = self.create_offer(book=book, title=title, offer_type=otype, scope=scope, stake=stake, min_odds=min_odds,
                                count=count, each=each, expiry_days=7, trigger=trigger,
                                expires=fixtures[-1].at + timedelta(days=8))
        self.qualify(oid, book, scope, stake, min_odds, trigger, fixtures, flags)

    def plant_plans(self) -> None:
        """A plan placed at the bookmaker but not laid yet, and one ready to place with a free bet (Operations)."""
        today = self.end.astimezone(UK).date()
        later = [f for f in self.candidates(today, 4, self.end + timedelta(hours=8), None, "football")] or \
            [f for f in self.candidates(today, 4, self.end + timedelta(hours=8), None, "any")]
        if not later:
            return
        book = "William Hill"
        if book not in self.acc:
            return
        upcoming = later
        first = later[0]
        title, scope = COMPETITION_OFFERS[first.competition if first.competition in COMPETITION_OFFERS else first.sport]
        later = [f for f in later if self.matches_scope(f, scope) and f.competition == first.competition] or later
        oid = self.create_offer(book=book, title=title, offer_type="reload", scope=scope, stake=1000, min_odds="1.5",
                                count=1, each=500, expiry_days=7, trigger="bet_settled",
                                expires=later[0].at + timedelta(days=8))
        pick = self.best_bet(book, "qualifying", 1000, later[:6], 1.7, 4.0, "1.5")
        if pick:
            f, s, back, lay, ex, m = pick
            self.fund_bookie(book, 1000)
            self.used.add((book, f.event, s.name))
            plan_id = plans.create(self.conn, {"event": f.event, "event_at": iso(f.at), "market": f.market,
                                               "selection": s.name, "kind": "qualifying", "bookmaker": book,
                                               "back_stake": "10.00", "back_odds": str(back), "exchange": ex,
                                               "lay_odds": str(lay), "sport": f.sport, "offer_id": oid,
                                               "allow_duplicate": True})
            plans.validate(self.conn, plan_id)
            plans.place_back(self.conn, plan_id, stake="10.00", odds=str(back))
        football = [f for f in upcoming if f.sport == "football"]
        if football and "Paddy Power" in self.acc:             # an open Bet Builder: its outcome table and liability
            oid = self.create_offer(book="Paddy Power", title="Bet Builder Club: bet £10 on a Bet Builder, get a £5 free "
                                    "bet", offer_type="bet_builder", scope="bb", stake=1000, min_odds="4.0", count=1,
                                    each=500, expiry_days=7, trigger="bet_settled",
                                    expires=football[0].at + timedelta(days=8))
            any(self.place_builder(book="Paddy Power", f=f, stake=1000, offer_id=oid) for f in football * 3)
        tokens = self.conn.execute("SELECT f.id, a.name FROM free_bets f JOIN accounts a ON a.id = f.account_id "
                                   "WHERE f.status = 'available' AND f.deleted_at IS NULL ORDER BY f.expires_at").fetchall()
        soon = [f for f in self.candidates(today, 3, self.end + timedelta(hours=8), None, "any") if f.sport != "racing"]
        best = None
        for fid, book in tokens:
            fb = services.row(self.conn, "free_bets", fid)
            pick = self.best_bet(book, "free_bet_snr", fb["face_value_pence"], soon, 4.0, 9.5, None)
            if pick and (best is None or pick[-1].worst / fb["face_value_pence"] > best[0]):
                best = (pick[-1].worst / fb["face_value_pence"], fid, book, pick)
        if best and best[0] >= 0.70:
            _, fid, book, (f, s, back, lay, ex, m) = best
            self.used.add((book, f.event, s.name))
            self.fund_exchange(ex, m.liability)
            plan_id = plans.create(self.conn, {"event": f.event, "event_at": iso(f.at), "market": f.market,
                                               "selection": s.name, "kind": "free_bet_snr", "free_bet_id": fid,
                                               "back_odds": str(back), "exchange": ex, "lay_odds": str(lay),
                                               "sport": f.sport, "allow_duplicate": True})
            plans.validate(self.conn, plan_id)

    def plant_expiring(self) -> None:
        """A free bet the bookmaker gave without an offer, expiring tomorrow (Free bets: expiring soon)."""
        if "Paddy Power" not in self.acc:
            return
        services.create_free_bet(self.conn, account_id=self.acc["Paddy Power"], face_value=500, kind="snr",
                                 credited_at=times.now_iso(), expires_at=iso(self.end + timedelta(hours=30)),
                                 notes="Surprise free bet in the app, any sport")

    def plant_transit(self) -> None:
        """A withdrawal that left six days ago and still hasn't arrived (Issues: Withdrawal)."""
        rows = [(self.balance(b), b) for b, aid in self.acc.items()
                if services.row(self.conn, "accounts", aid)["kind"] == "bookmaker"
                and not metrics.unsettled_back_stakes(self.conn, aid)]
        rows.sort(reverse=True)
        if rows and rows[0][0] >= 2000:
            amount = rows[0][0] // 100 * 100
            self.transfer(rows[0][1], HUB, amount, self.withdrawal_type(amount), in_transit=True)

    def plant_unresolved(self) -> None:
        """A balance check 50p short that nothing explains (Issues: Balance, with the suggested fixes)."""
        for book in ("Coral", "Ladbrokes", "Paddy Power", "Betway", "Unibet"):
            aid = self.acc.get(book)
            if aid and self.balance(book) >= 200 and not metrics.unsettled_back_stakes(self.conn, aid):
                services.reconcile(self.conn, aid, self.balance(book) - 50, adjust=False, at=times.now_iso(),
                                   note="The bookmaker shows 50p less than the app")
                return

    def promo_on(self) -> None:
        self.acc[PROMO_EXCHANGE] = services.create_account(
            self.conn, name=PROMO_EXCHANGE, kind="exchange", commission_bp=200,
            notes="Opened for the new-customer offer: 0% commission for five weeks")
        services.update_account(self.conn, self.acc[PROMO_EXCHANGE], commission_bp=0,
                                commission_expires_at=iso(self.promo[1]))
        self.transfer(HUB, PROMO_EXCHANGE, 20000, "bankroll_contribution")

    # ------------------------------------------------------------------ the whole history

    def build(self) -> None:
        t0 = fx.uk(self.start_day, "19:30")
        self.at(t0, self.setup)
        self.run()
        services.expire_overdue(self.conn)
        from bets import offer_state
        offer_state.sync_all(self.conn)

    def setup(self) -> None:
        for name in (HUB, LENDER):
            self.acc[name] = services.create_account(self.conn, name=name, kind="bank",
                                                     notes="Matched betting hub: deposits leave from here and "
                                                           "withdrawals land here" if name == HUB else
                                                     "Everyday current account: lends for busy weeks, paid back after")
        for name, bp in EXCHANGES.items():
            self.acc[name] = services.create_account(self.conn, name=name, kind="exchange", commission_bp=bp)
        self.transfer(HUB, "Smarkets", 60000, "bankroll_contribution", notes="Starting bankroll")
        self.transfer(HUB, "Betfair Exchange", 40000, "bankroll_contribution", notes="Starting bankroll")
        self.at(Clock.t + timedelta(days=40), lambda: self.transfer(HUB, "Matchbook", 20000, "bankroll_contribution"))
        # sign-ups: two or three a week, the big ones first
        when = Clock.t + timedelta(minutes=20)
        for i, spec in enumerate(SIGNUPS):
            self.at(self.awake(when), self.sign_up, spec)
            when += timedelta(days=self.rng.uniform(1.5, 4.5) if i else 0.01)
        first_monday = self.start_day + timedelta(days=(7 - self.start_day.weekday()) % 7 + 14)
        self.at(fx.uk(first_monday, "08:00"), self.plan_week, first_monday)
        self.at(fx.uk(first_monday, "09:30"), self.weekly_sweep)
        self.at(fx.uk(self.start_day + timedelta(days=35), "10:00"), self.monthly_sweep)
        self.at(fx.uk(first_monday - timedelta(days=1), "21:00"), self.balance_checks)
        self.at(fx.uk(self.start_day + timedelta(days=1), "06:00"), self.daily)
        self.at(self.start + timedelta(days=int(len(SIGNUPS) * 3.2) + 230), self.restrict, "BetVictor")
        self.at(self.promo[0], self.promo_on)
        # money borrowed from the second bank
        self.at(self.start + timedelta(days=150, hours=2), self.loan, "Betfair Exchange", 80000, 14,
                "Extra lay liability for a big weekend", 10, False)
        self.at(self.start + timedelta(days=400, hours=3), self.loan, "Smarkets", 50000, 21,
                "Cover for a run of offers", 18, True)
        self.at(self.end - timedelta(days=9, hours=10), self.loan, "Betfair Exchange", 60000, 14,
                "Lay liability for the weekend", None, False)
        # left for the visitor
        self.at(self.end - timedelta(days=6, hours=-6), self.plant_transit)
        self.at(self.end - timedelta(days=5, hours=-6), self.plant_overdue)
        self.at(self.end - timedelta(hours=12), self.plant_started)
        self.at(self.end - timedelta(hours=31), self.plant_unresolved)
        self.at(self.end - timedelta(hours=8), self.plant_plans)
        self.at(self.end - timedelta(days=3, hours=-7), self.plant_expiring)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", required=True, type=Path)
    ap.add_argument("--today", type=date.fromisoformat, default=None, help="the demo's today (default: today, UK)")
    ap.add_argument("--days", type=int, default=548)
    args = ap.parse_args(argv)
    db_path = args.data_dir / "bets.sqlite3"
    if db_path.exists():
        print(f"{db_path} already exists: seed into a new, empty directory", file=sys.stderr)
        return 1
    args.data_dir.mkdir(parents=True, exist_ok=True)
    args.data_dir.chmod(0o700)
    today = args.today or datetime.now(UK).date()
    db = Database(db_path)
    db.migrate()
    real_now = times.now
    times.now = _frozen_now
    try:
        with db.tx() as conn, services.acting("user"):
            demo = Demo(conn, today, args.days)
            demo.build()
            services.set_setting(conn, "demo", {"seeded_for": today.isoformat()})     # demo.serve checks for this
            s = metrics.summary(conn)
            n = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                 for t in ("offers", "positions", "legs", "free_bets", "transfers", "ledger_entries", "bet_plans",
                           "audit_log")}
    finally:
        times.now = real_now
    print(f"demo seeded for {today}: profit £{s.profit_lifetime / 100:,.2f}, betting balance £{s.betting_balance / 100:,.2f}, "
          + ", ".join(f"{v} {k}" for k, v in n.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
