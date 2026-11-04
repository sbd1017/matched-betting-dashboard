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
