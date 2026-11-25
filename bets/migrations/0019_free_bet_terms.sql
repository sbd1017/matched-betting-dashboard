-- What a free bet can be used on and from when, and whether its expiry is only an estimate. Additive only.
--   usable_from: not usable before this time (NULL = usable now)
--   restriction: a short note shown with it, e.g. 'in-play only, 3+ legs'
--   expiry_estimated: 1 when expires_at is a best guess (shown as 'about 23:59')
ALTER TABLE free_bets ADD COLUMN usable_from TEXT;
ALTER TABLE free_bets ADD COLUMN restriction TEXT NOT NULL DEFAULT '';
ALTER TABLE free_bets ADD COLUMN expiry_estimated INTEGER NOT NULL DEFAULT 0 CHECK (expiry_estimated IN (0, 1));
