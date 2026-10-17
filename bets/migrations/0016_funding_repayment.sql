-- pragma: foreign_keys=off
-- Funding repayment: a bank paying temporary funding back to the bank that lent it, e.g. the matched-betting hub
-- bank (where every bookmaker and exchange pays out) sending the money back to the lender. Betting accounts only
-- ever deal with the hub bank, so the lender is repaid bank to bank, not by a betting withdrawal. Like every
-- transfer it is capital, never profit or loss. SQLite cannot change a CHECK constraint in place, so transfers is
-- rebuilt with 'funding_repayment' added; every row and column is copied unchanged (as in 0005). ledger_entries
-- refers to transfers, so foreign keys are off for this migration (the runner checks them before committing).

CREATE TABLE transfers_new (
    id                 INTEGER PRIMARY KEY,
    from_account_id    INTEGER NOT NULL REFERENCES accounts(id),
    to_account_id      INTEGER NOT NULL REFERENCES accounts(id),
    amount_pence       INTEGER NOT NULL CHECK (amount_pence > 0),
    at                 TEXT NOT NULL,
    capital_type       TEXT NOT NULL CHECK (capital_type IN
                         ('temporary_funding', 'temporary_return', 'bankroll_contribution', 'bankroll_withdrawal',
                          'profit_withdrawal', 'internal', 'funding_repayment')),
    temp_portion_pence INTEGER NOT NULL DEFAULT 0 CHECK (temp_portion_pence >= 0),
    notes              TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    voided_at          TEXT,
    due_at             TEXT,
    in_transit         INTEGER NOT NULL DEFAULT 0,
    arrived_at         TEXT,
    plan_id            INTEGER REFERENCES bet_plans(id),
    CHECK (from_account_id <> to_account_id),
    CHECK (temp_portion_pence <= amount_pence)
);

INSERT INTO transfers_new (id, from_account_id, to_account_id, amount_pence, at, capital_type, temp_portion_pence,
                           notes, created_at, voided_at, due_at, in_transit, arrived_at, plan_id)
    SELECT id, from_account_id, to_account_id, amount_pence, at, capital_type, temp_portion_pence,
           notes, created_at, voided_at, due_at, in_transit, arrived_at, plan_id
    FROM transfers;

DROP TABLE transfers;
ALTER TABLE transfers_new RENAME TO transfers;
