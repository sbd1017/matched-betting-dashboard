"""Explicit, audited outcome sets for positions whose legs do not share one selection.

Amounts are net P&L in pence, including back stakes. All outcomes must be supplied
from the known market structure; never infer exclusivity from event names.
"""
import json
from decimal import Decimal
from . import calc, normalise, services


def fingerprint(legs):
    keys = ('id', 'account_id', 'side', 'stake_pence', 'odds', 'commission_bp', 'free_bet_id')
    return [{k: l[k] for k in keys} for l in legs]


def legs_for(conn, pid):
    return [dict(r) for r in conn.execute(
        'SELECT * FROM legs WHERE position_id=? AND deleted_at IS NULL ORDER BY id', (pid,))]


def configure(conn, pid, scenarios, note):
    legs = legs_for(conn, pid)
    ids = {str(l['id']) for l in legs}
    if not note.strip() or not legs or len(scenarios) < 2:
        raise services.UserError('Supply complete market outcomes and supporting evidence')
    if not {True, False} == {s['back_wins'] for s in scenarios}:
        raise services.UserError('Include outcomes where the back wins and loses')
    for s in scenarios:
        if not s['label'] or set(s['pnl']) != ids or any(type(v) is not int for v in s['pnl'].values()):
            raise services.UserError('Each outcome must include every leg in integer pence')
    old = conn.execute('SELECT * FROM exposure_scenarios WHERE position_id=?', (pid,)).fetchone()
    conn.execute('INSERT OR REPLACE INTO exposure_scenarios VALUES (?,?,?,?)',
                 (pid, json.dumps(fingerprint(legs)), json.dumps(scenarios), note))
    services.audit(conn, 'position', pid, 'exposure_scenarios', dict(old) if old else None, scenarios, note)


def configured(conn, pid):
    return conn.execute('SELECT 1 FROM exposure_scenarios WHERE position_id=?', (pid,)).fetchone() is not None


def valid(conn, pid):
    row = conn.execute('SELECT * FROM exposure_scenarios WHERE position_id=?', (pid,)).fetchone()
    legs = legs_for(conn, pid)
    if not row or json.loads(row['legs_json']) != fingerprint(legs):
        return None
    return legs, json.loads(row['scenarios_json'])


def liability(conn, pid, account_id=None, skip=frozenset()):
    """A position's own worst case on each exchange. Lays in `skip` are already counted in their market's hold."""
    data = valid(conn, pid)
    if data is None:
        return None
    legs, scenarios = data
    open_lays = [l for l in legs if l['side']=='lay' and l['result']=='pending' and l['id'] not in skip]
    # Separate exchanges cannot offset each other's collateral.
    accounts = {l['account_id'] for l in open_lays}
    if account_id is not None:
        accounts &= {account_id}
    total = 0
    for account in accounts:
        ids = [str(l['id']) for l in open_lays if l['account_id']==account]
        total += max(0, -min(sum(s['pnl'][lid] for lid in ids) for s in scenarios))
    return total


def market_holds(conn, account_id=None):
    """What each exchange holds for the lays the user has placed in a named market, across every position.

    Only one selection in a market can win, so the exchange holds the market's worst outcome, not every liability added
    up. The market is only ever the one the user named (set_market), never worked out from event names. Different
    markets, even in one match, and different exchanges stay separate, and lays with no market are counted in full.
    """
    sql = ("SELECT l.* FROM legs l JOIN positions p ON p.id=l.position_id WHERE l.side='lay' AND l.result='pending' "
           "AND l.deleted_at IS NULL AND p.deleted_at IS NULL AND l.exchange_market<>''")
    rows = conn.execute(sql + (" AND l.account_id=?" if account_id is not None else ""),
                        (account_id,) if account_id is not None else ()).fetchall()
    names = normalise.aliases(conn) if rows else {}
    markets = {}
    for l in rows:
        key = (l['account_id'], normalise.event_key(l['exchange_event'], names), normalise.market_key(l['exchange_market']))
        markets.setdefault(key, []).append(l)
    out = []
    for (account, _, _), lays in markets.items():
        stakes = sum(l['stake_pence'] for l in lays)
        by_selection = {}
        for l in lays:
            sel = by_selection.setdefault(normalise.selection_key(l['exchange_selection'], names),
                                          {'selection': l['exchange_selection'], 'if_wins': stakes})
            sel['if_wins'] -= l['stake_pence'] + calc.lay_liability(l['stake_pence'], Decimal(l['odds']))
        selections = list(by_selection.values())
        out.append({'account_id': account, 'event': lays[0]['exchange_event'], 'market': lays[0]['exchange_market'],
                    'hold': max(0, -min(s['if_wins'] for s in selections)), 'selections': selections,
                    'leg_ids': [l['id'] for l in lays],
                    'added_up': sum(calc.lay_liability(l['stake_pence'], Decimal(l['odds'])) for l in lays)})
    return out


def set_market(conn, lays, event, market, note=''):
    """Say which exchange market some open lays are in. `lays` is [(leg id, selection)]; an empty market clears them."""
    event, market = (event or '').strip(), (market or '').strip()
    if bool(event) != bool(market):
        raise services.UserError('Give both the match and the market, or leave both empty to clear them')
    accounts = set()
    found = []
    for leg_id, selection in lays:
        leg = services.row(conn, 'legs', leg_id)
        selection = (selection or '').strip() if market else ''
        if not leg or leg['deleted_at'] or leg['side'] != 'lay':
            raise services.UserError(f'Bet #{leg_id} is not a lay')
        if leg['result'] != 'pending':
            raise services.UserError(f'Lay #{leg_id} has already settled, so it holds nothing')
        if market and not selection:
            raise services.UserError(f'Say which selection lay #{leg_id} is on')
        accounts.add(leg['account_id'])
        found.append((leg, selection))
    if not found:
        raise services.UserError('Choose at least one lay')
    if market and len(accounts) > 1:
        raise services.UserError('Those lays are on different exchanges, which cannot net off')
    for leg, selection in found:
        after = {'exchange_event': event, 'exchange_market': market, 'exchange_selection': selection}
        conn.execute('UPDATE legs SET exchange_event=?, exchange_market=?, exchange_selection=? WHERE id=?',
                     (event, market, selection, leg['id']))
        services.audit(conn, 'leg', leg['id'], 'exchange_market', {k: leg[k] for k in after}, after, note)


def outcomes(conn, pid, fallback):
    data = valid(conn, pid)
    if data is None:
        return (None, None) if configured(conn, pid) else fallback()
    _, scenarios = data
    return tuple(min(sum(s['pnl'].values()) for s in scenarios if s['back_wins']==win) for win in (True, False))


def after_next_lay(conn, pid):
    """What a position laid one leg at a time (a double or acca) is set to make once its next lay is placed.

    Such a table has a row where the back loses and every lay loses too: a later leg failed after the first landed,
    with nothing placed against it. That row only happens if the next lay is never placed, so this is the worst of the
    other rows. None for every other position, including one whose next lay is already in its table.
    """
    data = valid(conn, pid)
    if data is None:
        return None
    legs, scenarios = data
    lays = [str(l['id']) for l in legs if l['side']=='lay']
    if not lays:
        return None
    uncovered = [s for s in scenarios if not s['back_wins'] and all(s['pnl'][lid] < 0 for lid in lays)]
    others = [s for s in scenarios if s not in uncovered]
    if not uncovered or not any(not s['back_wins'] for s in others):
        return None
    return min(sum(s['pnl'].values()) for s in others)


def leg_liability(conn, leg, fallback):
    data = valid(conn, leg['position_id'])
    if data is None:
        return fallback
    return max(0, -min(s['pnl'][str(leg['id'])] for s in data[1]))
