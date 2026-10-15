-- Offer terms as fields, so the app can pre-fill bets, check they qualify, create the reward's free bets when
-- it arrives and move the offer on by itself. All optional; the free-text requirement/reward stay as written.
--   qualifying_stake_pence  the stake the qualifying bet needs
--   min_odds                minimum back odds for it to qualify (exact decimal text, like legs.odds)
--   reward_count            how many free bets (or 1 for a cash reward)
--   reward_each_pence       value of each free bet (or the cash amount)
--   reward_kind             'snr', 'sr' or 'cash'
--   reward_expiry_days      free bets expire this many days after they arrive

ALTER TABLE offers ADD COLUMN qualifying_stake_pence INTEGER;
ALTER TABLE offers ADD COLUMN min_odds TEXT;
ALTER TABLE offers ADD COLUMN reward_count INTEGER;
ALTER TABLE offers ADD COLUMN reward_each_pence INTEGER;
ALTER TABLE offers ADD COLUMN reward_kind TEXT;
ALTER TABLE offers ADD COLUMN reward_expiry_days INTEGER;
