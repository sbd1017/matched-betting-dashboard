-- When an offer's reward is due: only ever from its terms or a date someone gave, never a guess. Additive only.
--   offers.reward_credited_by: the bookmaker credits the reward by this time (NULL = not known)
--   offers.reward_due_from: what reward_delay_hours counts from: the qualifier (NULL, as before) or the offer's end
--                           (weekly offers that pay after the week ends)
--   expected_rewards.due_source: where expected_by came from: 'given' by a person or the assistant, or worked out from
--                                the 'offer' (NULL = recorded before this was kept)
ALTER TABLE offers ADD COLUMN reward_credited_by TEXT;
ALTER TABLE offers ADD COLUMN reward_due_from TEXT
    CHECK (reward_due_from IS NULL OR reward_due_from IN ('qualifier', 'offer_end'));
ALTER TABLE expected_rewards ADD COLUMN due_source TEXT
    CHECK (due_source IS NULL OR due_source IN ('given', 'offer'));
