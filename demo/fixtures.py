"""Made-up fixtures on plausible dates: real clubs, players, courses and competitions, at the times they usually play.

Every date has a fixed set of fixtures (seeded from the date itself), so the same day always shows the same matches.
What is on follows the calendar: club football from August to May (Champions League on its midweeks), the 2026 World
Cup in June and July 2026, tennis during the tournaments it is known for, and racing every day with the big festivals
on their usual weeks. Prices are generated per bet from each selection's chance of winning.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
SALT = "bets-demo-fixtures"

# Strength ratings (rough, for prices only).
PREMIER_LEAGUE = {
    "Liverpool": 88, "Arsenal": 88, "Manchester City": 86, "Chelsea": 83, "Newcastle United": 80, "Aston Villa": 79,
    "Tottenham Hotspur": 77, "Manchester United": 77, "Brighton": 76, "Nottingham Forest": 75, "Crystal Palace": 75,
    "Bournemouth": 74, "Brentford": 74, "Fulham": 73, "Everton": 72, "West Ham United": 72, "Wolves": 70,
}
CHAMPIONSHIP = {
    "Middlesbrough": 66, "Sheffield United": 67, "Norwich City": 64, "West Bromwich Albion": 65, "Bristol City": 63,
    "Millwall": 62, "Stoke City": 61, "Hull City": 60, "Watford": 62, "Blackburn Rovers": 62, "Derby County": 60,
    "Swansea City": 61, "Preston North End": 61, "Queens Park Rangers": 60, "Portsmouth": 59, "Sheffield Wednesday": 59,
}
CL_ENGLISH = ["Arsenal", "Liverpool", "Manchester City", "Chelsea", "Tottenham Hotspur", "Newcastle United", "Aston Villa"]
CL_EUROPE = {
    "Real Madrid": 88, "Barcelona": 87, "Bayern Munich": 87, "Paris Saint-Germain": 87, "Inter Milan": 84,
    "Atlético Madrid": 82, "Borussia Dortmund": 80, "Bayer Leverkusen": 80, "Napoli": 80, "Juventus": 79,
    "Benfica": 77, "Sporting CP": 76, "PSV Eindhoven": 75, "Club Brugge": 73,
}
NATIONS = {
    "Spain": 88, "France": 88, "Argentina": 87, "England": 86, "Brazil": 85, "Portugal": 85, "Germany": 84,
    "Netherlands": 83, "Belgium": 80, "Morocco": 79, "Croatia": 79, "Uruguay": 78, "Colombia": 78, "Japan": 77,
    "USA": 76, "Mexico": 75, "Canada": 72,
}
MEN = {"Jannik Sinner": 92, "Carlos Alcaraz": 92, "Novak Djokovic": 88, "Alexander Zverev": 86, "Taylor Fritz": 84,
       "Jack Draper": 84, "Ben Shelton": 83, "Alex de Minaur": 82, "Lorenzo Musetti": 82, "Daniil Medvedev": 82,
       "Holger Rune": 81, "Casper Ruud": 81, "Tommy Paul": 80, "Andrey Rublev": 80, "Frances Tiafoe": 78,
       "Cameron Norrie": 76}
WOMEN = {"Aryna Sabalenka": 92, "Iga Swiatek": 90, "Coco Gauff": 89, "Elena Rybakina": 87, "Jessica Pegula": 86,
         "Mirra Andreeva": 86, "Madison Keys": 84, "Jasmine Paolini": 84, "Qinwen Zheng": 84, "Emma Navarro": 82,
         "Emma Raducanu": 80, "Paula Badosa": 80, "Elina Svitolina": 80, "Katie Boulter": 76}

JUMPS = ["Sandown", "Kempton", "Newbury", "Haydock", "Ascot", "Wetherby", "Exeter", "Doncaster", "Huntingdon",
         "Chepstow", "Fontwell", "Plumpton", "Warwick", "Wincanton", "Taunton", "Leicester"]
FLAT = ["Newmarket", "York", "Goodwood", "Epsom", "Ascot", "Sandown", "Haydock", "Doncaster", "Newbury", "Chester",
        "Windsor", "Salisbury", "Bath", "Lingfield", "Nottingham", "Leicester"]
ALL_WEATHER = ["Wolverhampton", "Kempton", "Lingfield", "Chelmsford City", "Newcastle", "Southwell"]
HORSES = """Bold Endeavour, Kilcarra Lass, Silver Thistle, Mister Mulligan, Copper Haze, Dancing Brig, Harbour Lights,
Glencairn Rose, Monbeg Quest, Saint Cuthbert, Tinker's Gold, Ballyhale Star, Northern Reel, Corran Bay, Lough Derg Lad,
Highland Drover, Morning Calm, Ardmore Flyer, Cappawhite, The Kerry Way, Bramble Hill, Westport Lady, Sea Holly,
Oakmont Prince, Rathgar Boy, Willow Copse, Kingsdown, Red Kite Rising, Blue Fenland, Castle Comer, Doolin Bay,
Fiddler's Green, Gortmore, Hazelbank, Inchicore, Jubilee Lane, Kilmallock, Lismore Lass, Mountshannon, Nenagh Knight,
Old Harrow, Portmagee, Quarry Lane, Rossmore Rocket, Slaney Valley, Tullow Tiger, Upton Grey, Valentia Isle,
Wexford Wanderer, Yarrow Dale, Ashgrove Annie, Bantry Blue, Clonmel Clipper, Drumlin Boy, Errigal, Fenton Park,
Galtee Mor, Holycross, Inishowen, Jamesons Lad, Kenmare Bay, Lady Ardee, Maam Cross, Nephin Beg, Owenmore,
Pallas Green, Quiet Harbour, Rathdrum, Sliabh Bloom, Tanderagee, Uisce Beatha, Vinegar Hill, Whitewater Way,
Ballinrobe, Castlebar Blue, Derryveagh, Eagle Ridge, Foxhollow, Glenbeigh, Hollymount, Killaloe Lass, Lettermore,
Moyvalley, Newtown Belle, Oranmore, Pontoon Bridge, Rosmuck, Strandhill, Templemore, Union Hall""".replace("\n", " ")
HORSES = [h.strip() for h in HORSES.split(",") if h.strip()]

TENNIS_EVENTS = [  # (first day, last day as (month, day)), name, UK start times
    ((1, 12), (2, 1), "Australian Open", ["00:30", "02:00", "04:00", "08:30"]),
    ((3, 5), (3, 16), "Indian Wells", ["19:00", "21:00", "23:00"]),
    ((3, 18), (3, 30), "Miami Open", ["16:00", "18:00", "20:00"]),
    ((4, 24), (5, 4), "Madrid Open", ["11:00", "13:00", "17:00"]),
    ((5, 7), (5, 18), "Italian Open", ["11:00", "13:00", "18:00"]),
    ((5, 25), (6, 8), "French Open", ["10:00", "12:30", "14:30", "19:15"]),
    ((6, 15), (6, 21), "Queen's Club", ["12:30", "14:00", "16:00"]),
    ((6, 29), (7, 12), "Wimbledon", ["11:00", "13:30", "15:00", "17:00"]),
    ((8, 10), (8, 18), "Cincinnati Open", ["16:00", "18:00", "20:00"]),
    ((8, 24), (9, 7), "US Open", ["16:00", "18:00", "00:00"]),
    ((10, 1), (10, 12), "Shanghai Masters", ["06:30", "08:30", "11:30"]),
    ((11, 9), (11, 16), "ATP Finals", ["12:00", "19:00"]),
]
WORLD_CUP = (date(2026, 6, 11), date(2026, 7, 19))


@dataclass
class Selection:
    name: str
    p: float                     # chance of winning (fair)


@dataclass
class Fixture:
    sport: str                   # football / racing / tennis
    competition: str             # pl / champ / cl / wc / racing / tennis name
    event: str
    at: datetime                 # UTC
    market: str
    selections: list[Selection] = field(default_factory=list)
    home: str = ""
    away: str = ""

    @property
    def end(self) -> datetime:
        return self.at + {"football": timedelta(minutes=115), "racing": timedelta(minutes=8),
                          "tennis": timedelta(hours=2, minutes=20)}[self.sport]


def uk(d: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=UK).astimezone(timezone.utc)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def cheltenham(year: int) -> list[date]:
    start = nth_weekday(year, 3, 1, 2)                      # the second Tuesday of March
    return [start + timedelta(days=i) for i in range(4)]


def grand_national(year: int) -> date:
    return cheltenham(year)[-1] + timedelta(days=22)         # the Saturday about three weeks after the Gold Cup


def royal_ascot(year: int) -> list[date]:
    start = nth_weekday(year, 6, 1, 3)
    return [start + timedelta(days=i) for i in range(5)]


def football_season(d: date) -> bool:
    return (d.month, d.day) >= (8, 9) or (d.month, d.day) <= (5, 24)


def cl_night(d: date) -> bool:
    if d.weekday() not in (1, 2):
        return False
    wk = d.isocalendar()[1]
    if (9, 15) <= (d.month, d.day) <= (12, 12) or (1, 19) <= (d.month, d.day) <= (1, 30):
        return wk % 3 == 0
    if (2, 15) <= (d.month, d.day) <= (5, 8):
        return wk % 2 == 0
    return False


def cl_final(d: date) -> bool:
    last_sat = date(d.year, 5, 31)
    last_sat -= timedelta(days=(last_sat.weekday() - 5) % 7)
    return d == last_sat


def _match_probs(home: int, away: int, neutral: bool = False) -> tuple[float, float, float]:
    diff = home - away + (0 if neutral else 4)
    draw = max(0.18, min(0.29, 0.28 - 0.0016 * abs(diff)))
    ph = (1 - draw) / (1 + math.exp(-diff / 8.5))
    return ph, draw, 1 - draw - ph


def _pairs(rng: random.Random, teams: list[str], n: int) -> list[tuple[str, str]]:
    pool = teams[:]
    rng.shuffle(pool)
    return [(pool[2 * i], pool[2 * i + 1]) for i in range(min(n, len(pool) // 2))]


def _football(rng, d, comp, ratings, n, times_, neutral=False, pairs=None) -> list[Fixture]:
    out = []
    for i, (h, a) in enumerate(pairs or _pairs(rng, list(ratings), n)):
        ph, pd, pa = _match_probs(ratings[h], ratings[a], neutral)
        out.append(Fixture("football", comp, f"{h} v {a}", uk(d, times_[i % len(times_)]), "Match Odds",
                           [Selection(h, ph), Selection("Draw", pd), Selection(a, pa)], h, a))
    return out


def _race(rng, d, course, hhmm, title="") -> Fixture:
    runners = rng.randint(7, 14)
    names = rng.sample(HORSES, runners)
    weights = sorted((rng.random() ** 2.2 for _ in names), reverse=True)
    total = sum(weights)
    sels = [Selection(n, w / total) for n, w in zip(names, weights)]
    event = f"{hhmm} {course}" + (f" ({title})" if title else "")
    return Fixture("racing", "racing", event, uk(d, hhmm), "Win", sels)


FESTIVAL_RACES = {
    "Cheltenham": [["Supreme Novices' Hurdle", "Arkle Chase", "Ultima Handicap Chase", "Champion Hurdle", "Mares' Hurdle"],
                   ["Ballymore Novices' Hurdle", "Brown Advisory Novices' Chase", "Coral Cup", "Queen Mother Champion Chase",
                    "Cross Country Chase"],
                   ["Turners Novices' Chase", "Pertemps Final", "Ryanair Chase", "Stayers' Hurdle", "Plate Handicap Chase"],
                   ["Triumph Hurdle", "County Hurdle", "Albert Bartlett Novices' Hurdle", "Gold Cup", "Hunters' Chase"]],
}


@lru_cache(maxsize=None)
def on(d: date) -> tuple[Fixture, ...]:
    """Everything on a date (deterministic per date)."""
    rng = random.Random(f"{SALT}:{d.isoformat()}")
    out: list[Fixture] = []
    wd = d.weekday()
    # ---------------------------------------------------------------- football
    if WORLD_CUP[0] <= d <= WORLD_CUP[1]:
        out += _football(rng, d, "wc", NATIONS, 3, ["17:00", "20:00", "23:00"], neutral=True)
    elif cl_final(d):
        h = rng.choice(CL_ENGLISH)
        a = rng.choice(list(CL_EUROPE))
        ratings = {**PREMIER_LEAGUE, **CL_EUROPE}
        out += _football(rng, d, "cl", ratings, 1, ["20:00"], neutral=True, pairs=[(h, a)])
    elif football_season(d):
        if wd == 5:
            pl = _pairs(rng, list(PREMIER_LEAGUE), 6)
            out += _football(rng, d, "pl", PREMIER_LEAGUE, 6, ["12:30", "15:00", "15:00", "15:00", "15:00", "17:30"], pairs=pl)
            out += _football(rng, d, "champ", CHAMPIONSHIP, 6, ["15:00"])
        elif wd == 6:
            out += _football(rng, d, "pl", PREMIER_LEAGUE, 2, ["14:00", "16:30"])
        elif wd == 0:
            out += _football(rng, d, "pl", PREMIER_LEAGUE, 1, ["20:00"])
        elif wd == 4:
            out += _football(rng, d, "champ", CHAMPIONSHIP, 1, ["20:00"])
        if wd in (1, 2) and cl_night(d):
            ratings = {**PREMIER_LEAGUE, **CL_EUROPE}
            eng = rng.sample(CL_ENGLISH, 2)
            eur = rng.sample(list(CL_EUROPE), 4)
            pairs = [(eng[0], eur[0]), (eur[1], eng[1]), (eur[2], eur[3])]
            out += _football(rng, d, "cl", ratings, 3, ["20:00", "20:00", "17:45"], pairs=pairs)
        elif wd == 1:
            out += _football(rng, d, "champ", CHAMPIONSHIP, 4, ["19:45"])
    # ---------------------------------------------------------------- tennis
    for (m1, d1), (m2, d2), name, slots in TENNIS_EVENTS:
        if (m1, d1) <= (d.month, d.day) <= (m2, d2):
            tour = MEN if rng.random() < 0.6 else WOMEN
            for i, (a, b) in enumerate(_pairs(rng, list(tour), len(slots))):
                pa = 1 / (1 + math.exp(-(tour[a] - tour[b]) / 5.5))
                at = uk(d, slots[i])
                out.append(Fixture("tennis", name, f"{a} v {b} ({name})", at, "Match Odds",
                                   [Selection(a, pa), Selection(b, 1 - pa)], a, b))
            break
    # ---------------------------------------------------------------- racing
    chelt = cheltenham(d.year)
    if d in chelt:
        names = FESTIVAL_RACES["Cheltenham"][chelt.index(d)]
        for hhmm, title in zip(["13:20", "13:55", "14:30", "15:20", "16:00"], names):
            out.append(_race(rng, d, "Cheltenham", hhmm, title))
    elif d == grand_national(d.year):
        for hhmm, title in (("13:45", "Mildmay Novices' Chase"), ("15:00", "Aintree Hurdle"), ("16:00", "Grand National")):
            out.append(_race(rng, d, "Aintree", hhmm, title))
    elif d in royal_ascot(d.year):
        for hhmm, title in zip(["14:30", "15:05", "15:40", "16:20"],
                               [["Queen Anne Stakes", "King Charles III Stakes", "St James's Palace Stakes", "Ascot Stakes"],
                                ["Queen Mary Stakes", "Duke of Cambridge Stakes", "Prince of Wales's Stakes", "Royal Hunt Cup"],
                                ["Norfolk Stakes", "Hampton Court Stakes", "Gold Cup", "Britannia Stakes"],
                                ["Albany Stakes", "King Edward VII Stakes", "Commonwealth Cup", "Duke of Edinburgh Stakes"],
                                ["Chesham Stakes", "Hardwicke Stakes", "Queen Elizabeth II Jubilee Stakes", "Wokingham Stakes"]
                                ][royal_ascot(d.year).index(d)]):
            out.append(_race(rng, d, "Ascot", hhmm, title))
    else:
        jumps = d.month in (10, 11, 12, 1, 2, 3, 4)
        for course in rng.sample(JUMPS if jumps else FLAT, 2):
            for hhmm in rng.sample(["13:30", "14:05", "14:40", "15:15", "15:50"], 3):
                out.append(_race(rng, d, course, hhmm))
    if wd != 6:
        course = rng.choice(ALL_WEATHER)
        for hhmm in ("17:45", "18:15", "18:45"):
            out.append(_race(rng, d, course, hhmm))
    return tuple(sorted(out, key=lambda f: f.at))


# ---------------------------------------------------------------- prices

FRACTIONAL = [Decimal(x) for x in (
    "1.2 1.25 1.3 1.33 1.36 1.4 1.44 1.5 1.53 1.57 1.62 1.67 1.73 1.8 1.83 1.91 2.0 2.1 2.2 2.25 2.38 2.5 2.62 2.75 "
    "2.88 3.0 3.1 3.25 3.4 3.5 3.75 4.0 4.33 4.5 5.0 5.5 6.0 6.5 7.0 7.5 8.0 8.5 9.0 9.5 10.0 11.0 12.0 13.0 15.0 "
    "17.0 21.0 26.0".split())]


def tick(o: Decimal) -> Decimal:
    for limit, step in ((2, "0.01"), (3, "0.02"), (4, "0.05"), (6, "0.1"), (10, "0.2"), (20, "0.5"), (30, "1")):
        if o < limit:
            return Decimal(step)
    return Decimal("2")


def to_tick(o: float, up: bool = True) -> Decimal:
    x = Decimal(str(round(o, 4)))
    t = tick(x)
    q = (x / t).to_integral_value(rounding="ROUND_CEILING" if up else "ROUND_FLOOR") * t
    return max(Decimal("1.01"), q.quantize(Decimal("0.01")))


def bookie_price(rng: random.Random, p: float) -> Decimal:
    """A bookmaker's fractional price for a selection with chance p (a few % worse than fair, sometimes boosted)."""
    target = (1 / p) * rng.uniform(0.94, 1.03)
    below = [f for f in FRACTIONAL if f <= Decimal(str(target))]
    return below[-1] if below else FRACTIONAL[0]


def lay_price(rng: random.Random, p: float, sport: str, exchange: str = "") -> Decimal:
    """The exchange's best lay price: just above fair, tighter where there is more money (Betfair, football)."""
    spread = {"football": (1.0, 1.02), "tennis": (1.0, 1.025), "racing": (1.003, 1.04)}[sport]
    if exchange == "Betfair Exchange":
        spread = (spread[0], 1 + (spread[1] - 1) * 0.6)
    return to_tick((1 / p) * rng.uniform(*spread))


def pounds(pence: int) -> str:
    return str((Decimal(pence) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
