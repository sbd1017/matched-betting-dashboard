-- Bookmaker standing and money in transit.
--   accounts.standing     ok / restricted (gubbed: stakes limited or promotions removed) / closed
--   accounts.standing_at  when the standing last changed
--   accounts.verified     1 once ID/verification is done
--   transfers.in_transit  1 for a withdrawal that has left the betting account but not reached the bank yet
--   transfers.arrived_at  when it arrived (in_transit goes back to 0)
-- Neither changes any balance: the betting account was debited when the money left it.

ALTER TABLE accounts ADD COLUMN standing TEXT NOT NULL DEFAULT 'ok' CHECK (standing IN ('ok', 'restricted', 'closed'));
ALTER TABLE accounts ADD COLUMN standing_at TEXT;
ALTER TABLE accounts ADD COLUMN verified INTEGER NOT NULL DEFAULT 0;
ALTER TABLE transfers ADD COLUMN in_transit INTEGER NOT NULL DEFAULT 0;
ALTER TABLE transfers ADD COLUMN arrived_at TEXT;
