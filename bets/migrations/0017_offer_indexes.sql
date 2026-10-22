-- Indexes for the per-offer lookups every page makes (offer_state.derive / sync, issues): without them each offer
-- scans the ledger, free bets, expected rewards and plans, so pages slowed down in step with the number of offers.
-- Indexes only: no rows or columns change.

CREATE INDEX IF NOT EXISTS ledger_offer ON ledger_entries (offer_id);
CREATE INDEX IF NOT EXISTS free_bets_offer ON free_bets (offer_id);
CREATE INDEX IF NOT EXISTS free_bets_expected_reward ON free_bets (expected_reward_id);
CREATE INDEX IF NOT EXISTS expected_rewards_offer ON expected_rewards (offer_id);
CREATE INDEX IF NOT EXISTS bet_plans_offer ON bet_plans (offer_id);
CREATE INDEX IF NOT EXISTS bet_plans_position ON bet_plans (position_id);
