import pytest
from bets import services, metrics, exposure, outlook


def setup(conn, world):
    pid = services.create_position(conn, event='Example correct score', legs=[
        dict(account_id=world['betfred'], side='back', stake_pence=1000, odds='5.08'),
        dict(account_id=world['smarkets'], side='lay', stake_pence=564, odds='9', commission_bp=0),
        dict(account_id=world['smarkets'], side='lay', stake_pence=339, odds='15', commission_bp=0)])
    ids = [str(l['id']) for l in exposure.legs_for(conn,pid)]
    scenarios = [dict(label=label, back_wins=win, pnl=dict(zip(ids, pnl))) for label,win,pnl in [
        ('1-1',True,[4079,-4513,339]), ('2-2',True,[4079,564,-4743]), ('Other',False,[-1000,564,339])]]
    exposure.configure(conn,pid,scenarios,'Verified statement amounts and exhaustive market outcomes')
    return pid, ids


def test_exact_exposure_and_projection(conn,world):
    pid,ids=setup(conn,world)
    assert metrics.open_lay_liability(conn)==4179
    assert metrics.open_lay_liability(conn,world['smarkets'])==4179
    assert metrics.open_lay_liability(conn,world['betfred'])==0
    assert exposure.outcomes(conn,pid,lambda:None)==(-100,-97)
    assert outlook.still_to_come(conn)['locked']==-100
    assert services.position_pnl(conn,pid)==0


def test_other_positions_not_offset(conn,world):
    pid,ids=setup(conn,world)
    services.create_position(conn,event='Other event',legs=[dict(account_id=world['smarkets'],side='lay',stake_pence=809,odds='6.8')])
    assert metrics.open_lay_liability(conn)==8871


def test_edit_invalidates_scenarios(conn,world):
    pid,ids=setup(conn,world)
    services.update_leg(conn,int(ids[1]),{'stake_pence':600},note='Changed stake')
    assert exposure.valid(conn,pid) is None
    assert exposure.outcomes(conn,pid,lambda:(123,456))==(None,None)
    assert metrics.open_lay_liability(conn)==9546


def test_no_unsafe_bulk_settlement(conn,world):
    pid,ids=setup(conn,world)
    with pytest.raises(services.UserError,match='different selections'):
        services.quick_settle(conn,pid,'back_won')
    assert all(l['result']=='pending' for l in exposure.legs_for(conn,pid))
    services.quick_settle(conn,pid,'void')
    assert metrics.open_lay_liability(conn)==0


def test_invalid_coverage_rejected(conn,world):
    pid,ids=setup(conn,world)
    with pytest.raises(services.UserError):
        exposure.configure(conn,pid,[dict(label='A',back_wins=True,pnl={}),dict(label='B',back_wins=False,pnl={})],'Evidence')


def test_exchanges_cannot_offset(conn,world):
    pid,ids=setup(conn,world)
    other=services.create_account(conn,name='Other exchange',kind='exchange')
    c=conn.execute('SELECT scenarios_json FROM exposure_scenarios WHERE position_id=?',(pid,)).fetchone()[0]
    import json
    conn.execute('UPDATE legs SET account_id=? WHERE id=?',(other,int(ids[2])))
    exposure.configure(conn,pid,json.loads(c),'Separate accounts')
    assert metrics.open_lay_liability(conn)==9256
    assert metrics.open_lay_liability(conn,world['smarkets'])==4513
    assert metrics.open_lay_liability(conn,other)==4743


def test_position_page(browser,app):
    with app.state.db.tx() as c:
        w={'betfred':services.create_account(c,name='Book',kind='bookmaker'),
           'smarkets':services.create_account(c,name='Exchange',kind='exchange')}
        pid,ids=setup(c,w)
    page=browser.get(f'/bets/positions/{pid}')
    assert page.status_code==200
    assert 'Settle each bet below' in page.text
    assert '45.13' in page.text and '47.43' in page.text
    assert 'value="back_won"' not in page.text


def staged_double(conn, world, stake=700):
    """A £10 free bet double returning £120, with only the first leg laid so far."""
    free = services.create_free_bet(conn, account_id=world['betfred'], face_value=1000, kind='snr')
    pid = services.create_position(conn, event='Example double', kind='free_bet_snr', legs=[
        dict(account_id=world['betfred'], side='back', stake_pence=1000, odds='13', free_bet_id=free),
        dict(account_id=world['smarkets'], side='lay', stake_pence=stake, odds='3.5', commission_bp=0)])
    ids = [str(l['id']) for l in exposure.legs_for(conn, pid)]
    scenarios = [dict(label=label, back_wins=win, pnl=dict(zip(ids, pnl))) for label, win, pnl in [
        ('Both land', True, [12000, -1750]), ('First lands, second fails', False, [0, -1750]),
        ('First fails', False, [0, 700])]]
    exposure.configure(conn, pid, scenarios, 'Outcome model of the double')
    return pid, ids


def test_staged_double_counts_as_if_the_next_lay_goes_on(conn, world):
    pid, ids = staged_double(conn, world)
    assert exposure.after_next_lay(conn, pid) == 700
    proj = outlook.position_projection(conn, pid)
    assert proj['locked'] == 700 and proj['awaiting_next_lay']
    assert proj['if_back_loses'] == -1750                                # the real worst case stays visible
    assert metrics.open_lay_liability(conn) == 1750                      # and the exchange still holds it
    come = outlook.still_to_come(conn)
    assert come['locked'] == 700 and come['awaiting_next_lay'] == 1


def test_staged_double_is_locked_normally_once_the_next_lay_is_on(conn, world):
    pid, ids = staged_double(conn, world)
    second = services.add_leg(conn, pid, dict(account_id=world['smarkets'], side='lay', stake_pence=2450, odds='4.9',
                                              commission_bp=0))
    legs = [str(l['id']) for l in exposure.legs_for(conn, pid)]
    assert len(legs) == 3 and second
    scenarios = [dict(label=label, back_wins=win, pnl=dict(zip(legs, pnl))) for label, win, pnl in [
        ('Both land', True, [12000, -1750, -9555]), ('Second fails', False, [0, -1750, 2450])]]
    exposure.configure(conn, pid, scenarios, 'Next lay placed')
    assert exposure.after_next_lay(conn, pid) is None
    proj = outlook.position_projection(conn, pid)
    assert proj['locked'] == 695 and not proj['awaiting_next_lay']


def test_other_positions_keep_the_worse_outcome(conn, world):
    pid, ids = setup(conn, world)                                         # correct scores: every lay wins if the back loses
    assert exposure.after_next_lay(conn, pid) is None
    assert outlook.position_projection(conn, pid)['locked'] == -100
    single = services.create_position(conn, event='Plain single', legs=[
        dict(account_id=world['betfred'], side='back', stake_pence=1000, odds='3'),
        dict(account_id=world['smarkets'], side='lay', stake_pence=1000, odds='3.1', commission_bp=0)])
    assert exposure.after_next_lay(conn, single) is None
    assert not outlook.position_projection(conn, single)['awaiting_next_lay']


def _lay(conn, world, event, stake, odds, account='smarkets'):
    pid = services.create_position(conn, event=event, legs=[
        dict(account_id=world[account], side='lay', stake_pence=stake, odds=odds, commission_bp=0)])
    return pid, exposure.legs_for(conn, pid)[0]['id']


def _available(conn, world):
    return services.expected_balance(conn, world['smarkets'], 'available')['available']


def test_lays_in_one_market_hold_its_worst_outcome_across_positions(conn, world):
    _, draw = _lay(conn, world, 'Double one', 4000, '3')
    _, home = _lay(conn, world, 'Double two', 2000, '2.5')
    added_up = 8000 + 3000
    assert metrics.open_lay_liability(conn, world['smarkets']) == added_up
    before = _available(conn, world)
    exposure.set_market(conn, [(draw, 'Draw'), (home, 'Example FC')], 'Example FC v Sample Town', 'Full-time result')
    # If the draw wins: -80.00 + 20.00. If the home side wins: +40.00 - 30.00.
    assert metrics.open_lay_liability(conn, world['smarkets']) == 6000
    assert metrics.open_lay_liability(conn) == 6000
    assert _available(conn, world) == before + added_up - 6000


def test_one_position_still_shows_its_own_liability(conn, world):
    first, draw = _lay(conn, world, 'Double one', 4000, '3')
    _, home = _lay(conn, world, 'Double two', 2000, '2.5')
    exposure.set_market(conn, [(draw, 'Draw'), (home, 'Example FC')], 'Example FC v Sample Town', 'Full-time result')
    assert metrics.open_lay_liability(conn, position_id=first) == 8000
    assert services.position_pnl(conn, first) == 0


def test_same_selection_adds_up_and_spellings_of_it_match(conn, world):
    _, a = _lay(conn, world, 'One', 1000, '3')
    _, b = _lay(conn, world, 'Two', 1000, '3')
    exposure.set_market(conn, [(a, 'Draw')], 'Man Utd v Spurs', 'Match Odds')
    exposure.set_market(conn, [(b, 'The Draw')], 'Manchester United vs Tottenham', 'Full-time result')
    assert metrics.open_lay_liability(conn) == 4000


def test_different_markets_and_exchanges_and_unnamed_lays_do_not_net(conn, world):
    _, a = _lay(conn, world, 'One', 1000, '3')
    _, b = _lay(conn, world, 'Two', 1000, '3')
    _, c = _lay(conn, world, 'Three', 1000, '3')
    exposure.set_market(conn, [(a, 'Draw')], 'A v B', 'Full-time result')
    exposure.set_market(conn, [(b, '1-1')], 'A v B', 'Correct score')
    assert metrics.open_lay_liability(conn) == 6000

    other = services.create_account(conn, name='Other exchange', kind='exchange')
    pid = services.create_position(conn, event='Four', legs=[
        dict(account_id=other, side='lay', stake_pence=1000, odds='3', commission_bp=0)])
    leg = exposure.legs_for(conn, pid)[0]['id']
    with pytest.raises(services.UserError):
        exposure.set_market(conn, [(c, 'A'), (leg, 'B')], 'A v B', 'Full-time result')
    exposure.set_market(conn, [(c, 'B')], 'A v B', 'Full-time result')
    exposure.set_market(conn, [(leg, 'A')], 'A v B', 'Full-time result')
    assert metrics.open_lay_liability(conn, world['smarkets']) == 1000 + 2000
    assert metrics.open_lay_liability(conn, other) == 2000


def test_a_double_with_its_outcome_table_nets_only_its_named_lay(conn, world):
    pid = services.create_position(conn, event='Double', legs=[
        dict(account_id=world['betfred'], side='back', stake_pence=1000, odds='7', free_bet_id=None),
        dict(account_id=world['smarkets'], side='lay', stake_pence=700, odds='3.5', commission_bp=0),
        dict(account_id=world['smarkets'], side='lay', stake_pence=2000, odds='2.5', commission_bp=0)])
    ids = [l['id'] for l in exposure.legs_for(conn, pid)]
    keys = [str(i) for i in ids]
    exposure.configure(conn, pid, [
        dict(label='Both land', back_wins=True, pnl=dict(zip(keys, [6000, -1750, -3000]))),
        dict(label='Second fails', back_wins=False, pnl=dict(zip(keys, [-1000, -1750, 2000]))),
        dict(label='First fails', back_wins=False, pnl=dict(zip(keys, [-1000, 700, 2000])))], 'Double')
    assert metrics.open_lay_liability(conn) == 1750 + 3000
    _, draw = _lay(conn, world, 'Double two', 4000, '3')
    exposure.set_market(conn, [(draw, 'Draw'), (ids[2], 'Example FC')], 'Example FC v Sample Town', 'Full-time result')
    assert metrics.open_lay_liability(conn) == 1750 + 6000
    assert exposure.valid(conn, pid) is not None


def test_market_lets_go_when_a_lay_settles_and_can_be_cleared(conn, world):
    a_pos, a = _lay(conn, world, 'One', 4000, '3')
    _, b = _lay(conn, world, 'Two', 2000, '2.5')
    exposure.set_market(conn, [(a, 'Draw'), (b, 'Example FC')], 'Example FC v Sample Town', 'Full-time result')
    exposure.set_market(conn, [(a, ''), (b, '')], '', '')
    assert metrics.open_lay_liability(conn) == 8000 + 3000
    exposure.set_market(conn, [(a, 'Draw'), (b, 'Example FC')], 'Example FC v Sample Town', 'Full-time result')
    services.settle_leg(conn, a, 'lost')
    assert metrics.open_lay_liability(conn) == 3000
    with pytest.raises(services.UserError):
        exposure.set_market(conn, [(a, 'Draw')], 'Example FC v Sample Town', 'Full-time result')


@pytest.mark.parametrize('lays,event,market', [
    ([], 'A v B', 'Full-time result'),
    ([(0, 'Draw')], 'A v B', 'Full-time result'),
    ('no selection', 'A v B', 'Full-time result'),
    ('with selection', 'A v B', ''),
    ('back', 'A v B', 'Full-time result')])
def test_set_market_refuses_what_it_cannot_net(conn, world, lays, event, market):
    pid = services.create_position(conn, event='One', legs=[
        dict(account_id=world['betfred'], side='back', stake_pence=1000, odds='3'),
        dict(account_id=world['smarkets'], side='lay', stake_pence=1000, odds='3', commission_bp=0)])
    back, lay = [l['id'] for l in exposure.legs_for(conn, pid)]
    chosen = {'no selection': [(lay, ' ')], 'with selection': [(lay, 'Draw')], 'back': [(back, 'Draw')]}
    with pytest.raises(services.UserError):
        exposure.set_market(conn, chosen.get(lays, lays) if isinstance(lays, str) else lays, event, market)
