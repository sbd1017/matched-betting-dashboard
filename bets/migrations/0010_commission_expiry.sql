-- Promotional commission rates that end on a known date.
--   accounts.commission_expires_at  UTC ISO of 00:00 UK time on the day the recorded rate stops applying
--                                   (NULL = the rate has no known end). Exchanges only, like commission_bp.
-- Nothing changes commission_bp when the date passes: the app flags the stored rate as possibly stale until
-- the user records the rate that applies now. Existing bets keep the commission they were placed with.

ALTER TABLE accounts ADD COLUMN commission_expires_at TEXT;
