-- Maintenance: who made each change, how a balance check was read, and changes from the assistant.
--   audit_log.actor        user / assistant:<token id> / assistant+user (confirmed) / system (CLI, repairs)
--   audit_log.request_id   the assistant's idempotency key for the change, when there is one
--   audit_log.reverses_id  the audit row a reversal undoes, when known
--   reconciliations.basis  which figure was observed: 'total' (all funds) or 'available' (an exchange's free cash,
--                          lay liability excluded); expected (app_balance_pence) is for the same figure
--   reconciliations.liability_pence  open lay liability at the time (what separates the two figures)
--   reconciliations.outcome  matched / adjusted (a P&L entry was posted) / unresolved (difference shown, nothing
--                          posted) / dismissed / reversed (its adjustment withdrawn)
--   assistant_ops          maintenance changes from the assistant: idempotent by request_key, held until the user
--                          confirms when their permission level asks for it
-- No balance, bet or ledger entry changes here.

ALTER TABLE audit_log ADD COLUMN actor TEXT;
ALTER TABLE audit_log ADD COLUMN request_id TEXT;
ALTER TABLE audit_log ADD COLUMN reverses_id INTEGER;
CREATE INDEX IF NOT EXISTS audit_log_entity ON audit_log (entity, entity_id);

ALTER TABLE reconciliations ADD COLUMN basis TEXT NOT NULL DEFAULT 'total' CHECK (basis IN ('total', 'available'));
ALTER TABLE reconciliations ADD COLUMN liability_pence INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reconciliations ADD COLUMN outcome TEXT NOT NULL DEFAULT 'adjusted'
    CHECK (outcome IN ('matched', 'adjusted', 'unresolved', 'dismissed', 'reversed'));
ALTER TABLE reconciliations ADD COLUMN reversed_at TEXT;
ALTER TABLE reconciliations ADD COLUMN reversal_note TEXT NOT NULL DEFAULT '';
UPDATE reconciliations SET outcome = 'matched' WHERE difference_pence = 0;

CREATE TABLE assistant_ops (
    id            INTEGER PRIMARY KEY,
    public_id     TEXT NOT NULL UNIQUE,
    request_key   TEXT UNIQUE,
    op            TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    level         TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('held', 'applied', 'already_done', 'rejected', 'failed', 'blocked')),
    summary       TEXT NOT NULL DEFAULT '',
    reason        TEXT NOT NULL DEFAULT '',
    hold_reason   TEXT NOT NULL DEFAULT '',
    result_json   TEXT,
    submitted_by  TEXT,
    created_at    TEXT NOT NULL,
    decided_at    TEXT,
    decided_by    TEXT
);
