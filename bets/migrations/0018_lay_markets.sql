-- Which exchange market and selection a lay sits in, as the user says it. Lays on an exchange in one market net off
-- (only one selection can win), so available cash uses the market's worst outcome. Empty means not said: counted in full.
ALTER TABLE legs ADD COLUMN exchange_event TEXT NOT NULL DEFAULT '';
ALTER TABLE legs ADD COLUMN exchange_market TEXT NOT NULL DEFAULT '';
ALTER TABLE legs ADD COLUMN exchange_selection TEXT NOT NULL DEFAULT '';
