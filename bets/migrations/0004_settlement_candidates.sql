-- Settlement candidates: the result of an existing position, submitted for confirmation like any other entry.
-- SQLite cannot change a CHECK constraint in place, so import_candidates is rebuilt with 'settlement' added to
-- its kinds. Every row and column is copied unchanged; nothing else references this table.

CREATE TABLE import_candidates_new (
    id                  INTEGER PRIMARY KEY,
    source              TEXT NOT NULL,
    raw_json            TEXT NOT NULL,
    candidate_json      TEXT NOT NULL,
    warnings_json       TEXT NOT NULL DEFAULT '[]',
    status              TEXT NOT NULL DEFAULT 'needs_confirmation' CHECK (status IN
                          ('needs_confirmation', 'applied', 'rejected')),
    created_at          TEXT NOT NULL,
    decided_at          TEXT,
    applied_position_id INTEGER REFERENCES positions(id),
    applied_leg_id      INTEGER REFERENCES legs(id),
    kind                TEXT NOT NULL DEFAULT 'position'
                          CHECK (kind IN ('position', 'account', 'balance', 'transfer', 'offer', 'free_bet', 'note',
                                          'settlement')),
    public_id           TEXT,
    idempotency_key     TEXT,
    submitted_by        TEXT,
    applied_ref         TEXT,
    edited_at           TEXT,
    decided_by          TEXT,
    hold_reason         TEXT
);

INSERT INTO import_candidates_new (id, source, raw_json, candidate_json, warnings_json, status, created_at, decided_at,
                                   applied_position_id, applied_leg_id, kind, public_id, idempotency_key, submitted_by,
                                   applied_ref, edited_at, decided_by, hold_reason)
    SELECT id, source, raw_json, candidate_json, warnings_json, status, created_at, decided_at,
           applied_position_id, applied_leg_id, kind, public_id, idempotency_key, submitted_by,
           applied_ref, edited_at, decided_by, hold_reason
    FROM import_candidates;

DROP TABLE import_candidates;
ALTER TABLE import_candidates_new RENAME TO import_candidates;

CREATE INDEX import_status ON import_candidates (status, created_at);
CREATE UNIQUE INDEX import_public_id ON import_candidates (public_id);
CREATE UNIQUE INDEX import_idempotency ON import_candidates (source, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
