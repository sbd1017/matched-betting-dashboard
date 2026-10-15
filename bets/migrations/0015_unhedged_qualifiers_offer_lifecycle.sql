-- Deliberately unhedged qualifiers and the single offer lifecycle. Additive only.
--   positions.hedge_strategy: matched / intentionally_unhedged / pending (NULL = worked out from the legs)
--   positions.accepted_exposure_pence, unhedged_reason: what was knowingly left unhedged, and why
--   offers.reward_trigger: what makes the reward due (bet_placed / bet_fully_matched / bet_settled / bet_lost / other;
--                          NULL = bet_settled, as before)
--   offers.qualified_at: the bookmaker itself showed the qualifier as done (a fact, like opted_in_at)

ALTER TABLE positions ADD COLUMN hedge_strategy TEXT
    CHECK (hedge_strategy IS NULL OR hedge_strategy IN ('matched', 'intentionally_unhedged', 'pending'));
ALTER TABLE positions ADD COLUMN accepted_exposure_pence INTEGER;
ALTER TABLE positions ADD COLUMN unhedged_reason TEXT;
ALTER TABLE offers ADD COLUMN reward_trigger TEXT
    CHECK (reward_trigger IS NULL OR reward_trigger IN ('bet_placed', 'bet_fully_matched', 'bet_settled', 'bet_lost', 'other'));
ALTER TABLE offers ADD COLUMN qualified_at TEXT;
