-- Import candidates of every type (not only positions), for the Assistant Intake API.
-- Candidates still change nothing until the user confirms them; confirming runs the normal services.

ALTER TABLE import_candidates ADD COLUMN kind TEXT NOT NULL DEFAULT 'position'
    CHECK (kind IN ('position', 'account', 'balance', 'transfer', 'offer', 'free_bet', 'note'));
-- Opaque id given to outside tools (sequential ids are not shared).
ALTER TABLE import_candidates ADD COLUMN public_id TEXT;
-- A retried submission with the same key returns the first candidate instead of a duplicate.
ALTER TABLE import_candidates ADD COLUMN idempotency_key TEXT;
-- Which credential sent it (assistant token id; never the token itself).
ALTER TABLE import_candidates ADD COLUMN submitted_by TEXT;
-- What confirming created, e.g. 'account:4' or 'transfer:9'.
ALTER TABLE import_candidates ADD COLUMN applied_ref TEXT;
ALTER TABLE import_candidates ADD COLUMN edited_at TEXT;

UPDATE import_candidates SET public_id = lower(hex(randomblob(8))) WHERE public_id IS NULL;
UPDATE import_candidates SET applied_ref = 'position:' || applied_position_id WHERE applied_position_id IS NOT NULL;

CREATE UNIQUE INDEX import_public_id ON import_candidates (public_id);
CREATE UNIQUE INDEX import_idempotency ON import_candidates (source, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
