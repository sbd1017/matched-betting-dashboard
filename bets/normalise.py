"""Normalised names for events, markets and selections, so the same match typed two ways is recognised as one
(duplicate detection, plan <-> position matching, settlement). Display text is never changed.

    event_key("Man Utd vs Tottenham Hotspur FC")  -> "manchester united v tottenham hotspur"
    market_key("Full Time Result")               -> "match_odds"
    selection_key("The Draw")                    -> "draw"

Team aliases can be extended in the settings table (key 'name_aliases': {"spurs": "tottenham hotspur"}).
"""
from __future__ import annotations

import re

ALIASES = {"man utd": "manchester united", "man united": "manchester united", "man city": "manchester city",
           "spurs": "tottenham hotspur", "tottenham": "tottenham hotspur", "wolves": "wolverhampton wanderers",
           "atletico madrid": "atletico de madrid", "atlético madrid": "atletico de madrid",
           "atlético de madrid": "atletico de madrid", "psg": "paris saint-germain",
           "paris sg": "paris saint-germain", "newcastle": "newcastle united", "coventry": "coventry city",
           "leeds": "leeds united", "brighton": "brighton and hove albion", "west ham": "west ham united",
           "nottm forest": "nottingham forest", "forest": "nottingham forest", "inter": "inter milan",
           "bayern": "bayern munich", "barca": "barcelona"}
MARKETS = {"match_odds": ("match odds", "1x2", "full time result", "match result", "win draw win", "win-draw-win",
                          "fulltime result", "match betting", "result"),
           "correct_score": ("correct score", "cs"),
           "btts": ("both teams to score", "btts", "both teams score"),
           "bet_builder": ("bet builder", "betbuilder", "build a bet", "bet builder+", "request a bet"),
           "over_under": ("over/under", "total goals", "over under", "goals over/under"),
           "draw_no_bet": ("draw no bet", "dnb"),
           "win_market": ("win", "to win", "win only")}
_SUFFIX = re.compile(r"\b(fc|afc|a\.f\.c\.|cf|sc)\b")
_SPLIT = re.compile(r"\s+(?:v|vs|vs\.|versus|-|@)\s+", re.I)


def _clean(text: str) -> str:
    t = (text or "").lower().strip()
    t = _SUFFIX.sub("", t)
    t = re.sub(r"[^\w\s'+./-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def team(name: str, aliases: dict | None = None) -> str:
    t = _clean(name)
    table = {**ALIASES, **(aliases or {})}
    return table.get(t, t)


def event_key(event: str, aliases: dict | None = None) -> str:
    parts = _SPLIT.split((event or "").strip())
    if len(parts) == 2:
        return f"{team(parts[0], aliases)} v {team(parts[1], aliases)}"
    return _clean(event)


def market_key(market: str) -> str:
    t = _clean(market)
    for key, names in MARKETS.items():
        if t in names or t == key:
            return key
    if re.match(r"(over|under) \d+(\.\d+)?", t) or "goals" in t:
        return "over_under"
    return t


def selection_key(selection: str, aliases: dict | None = None) -> str:
    t = _clean(selection)
    if t in ("draw", "the draw", "x", "tie"):
        return "draw"
    return team(t, aliases)


def aliases(conn) -> dict:
    from . import services
    found = services.get_setting(conn, "name_aliases", {}) or {}
    return {str(k).lower(): str(v).lower() for k, v in found.items()} if isinstance(found, dict) else {}
