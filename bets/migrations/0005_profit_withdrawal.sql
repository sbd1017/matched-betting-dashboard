-- pragma: foreign_keys=off
-- Profit withdrawal: money taken out of the betting bankroll as winnings. Like every transfer it moves money
-- and is never profit or loss; it reduces retained profit, not own capital. Existing withdrawals keep their
-- classification (nothing is reinterpreted). SQLite cannot change a CHECK constraint in place, so transfers is
-- rebuilt with 'profit_withdrawal' added; every row and column is copied unchanged. ledger_entries refers to
-- transfers by name, so foreign keys are switched off for this one migration (the runner checks them before
-- committing).

CREATE TABLE transfers_new (
    id                 INTEGER PRIMARY KEY,
    from_account_id    INTEGER NOT NULL REFERENCES accounts(id),
    to_account_id      INTEGER NOT NULL REFERENCES accounts(id),
    amount_pence       INTEGER NOT NULL CHECK (amount_pence > 0),
    at                 TEXT NOT NULL,
    capital_type       TEXT NOT NULL CHECK (capital_type IN
                         ('temporary_funding', 'temporary_return', 'bankroll_contribution', 'bankroll_withdrawal',
                          'profit_withdrawal', 'internal')),
    temp_portion_pence INTEGER NOT NULL DEFAULT 0 CHECK (temp_portion_pence >= 0),
    notes              TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    voided_at          TEXT,
    CHECK (from_account_id <> to_account_id),
    CHECK (temp_portion_pence <= amount_pence)
);

INSERT INTO transfers_new (id, from_account_id, to_account_id, amount_pence, at, capital_type, temp_portion_pence,
                           notes, created_at, voided_at)
    SELECT id, from_account_id, to_account_id, amount_pence, at, capital_type, temp_portion_pence,
           notes, created_at, voided_at
    FROM transfers;

DROP TABLE transfers;
ALTER TABLE transfers_new RENAME TO transfers;
