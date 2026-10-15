"""Explicit, audited outcome sets for positions whose legs do not share one selection.

Amounts are net P&L in pence, including back stakes. All outcomes must be supplied
from the known market structure; never infer exclusivity from event names.
"""
import json
from . import services


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


def liability(conn, pid, account_id=None):
    data = valid(conn, pid)
    if data is None:
        return None
    legs, scenarios = data
    # Separate exchanges cannot offset each other's collateral.
    accounts = {l['account_id'] for l in legs if l['side']=='lay' and l['result']=='pending'}
    if account_id is not None:
        accounts &= {account_id}
    total = 0
    for account in accounts:
        ids = [str(l['id']) for l in legs if l['account_id']==account and l['side']=='lay' and l['result']=='pending']
        total += max(0, -min(sum(s['pnl'][lid] for lid in ids) for s in scenarios))
    return total


def outcomes(conn, pid, fallback):
    data = valid(conn, pid)
    if data is None:
        return (None, None) if configured(conn, pid) else fallback()
    _, scenarios = data
    return tuple(min(sum(s['pnl'].values()) for s in scenarios if s['back_wins']==win) for win in (True, False))


def leg_liability(conn, leg, fallback):
    data = valid(conn, leg['position_id'])
    if data is None:
        return fallback
    return max(0, -min(s['pnl'][str(leg['id'])] for s in data[1]))
