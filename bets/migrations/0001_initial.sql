-- Matched betting: initial schema. All money is INTEGER pence. Times are UTC ISO-8601 text.
-- Nothing financial is ever hard-deleted: rows carry deleted_at / superseded_at instead.

CREATE TABLE accounts (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK (length(trim(name)) BETWEEN 1 AND 60),
    -- bookmaker / exchange = betting bankroll; bank = an external funding source (e.g. Lloyds)
    kind          TEXT NOT NULL CHECK (kind IN ('bookmaker', 'exchange', 'bank')),
    commission_bp INTEGER NOT NULL DEFAULT 0 CHECK (commission_bp BETWEEN 0 AND 9999),
    active        INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    notes         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE offers (
    id                 INTEGER PRIMARY KEY,
    account_id         INTEGER REFERENCES accounts(id),
    title              TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 120),
    offer_type         TEXT NOT NULL DEFAULT 'sign_up' CHECK (offer_type IN
                         ('sign_up', 'reload', 'free_bet', 'bet_builder', 'enhanced_odds', 'qualifying', 'recurring', 'other')),
    requirement        TEXT NOT NULL DEFAULT '',
    reward             TEXT NOT NULL DEFAULT '',
    reward_value_pence INTEGER NOT NULL DEFAULT 0 CHECK (reward_value_pence >= 0),
    starts_at          TEXT,
    expires_at         TEXT,
    status             TEXT NOT NULL DEFAULT 'not_started' CHECK (status IN
                         ('not_started', 'qualifying', 'awaiting_reward', 'reward_received', 'converting',
                          'complete', 'expired', 'abandoned')),
    reward_credited    INTEGER NOT NULL DEFAULT 0 CHECK (reward_credited IN (0, 1)),
    reward_used        INTEGER NOT NULL DEFAULT 0 CHECK (reward_used IN (0, 1)),
    notes              TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    deleted_at         TEXT
);
CREATE INDEX offers_status ON offers (status, expires_at);

CREATE TABLE free_bets (
    id               INTEGER PRIMARY KEY,
    account_id       INTEGER NOT NULL REFERENCES accounts(id),
    offer_id         INTEGER REFERENCES offers(id),
    face_value_pence INTEGER NOT NULL CHECK (face_value_pence > 0),
    kind             TEXT NOT NULL DEFAULT 'snr' CHECK (kind IN ('snr', 'sr')),   -- stake not returned / returned
    credited_at      TEXT,
    expires_at       TEXT,
    status           TEXT NOT NULL DEFAULT 'available' CHECK (status IN ('available', 'used', 'expired', 'void')),
    notes            TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    deleted_at       TEXT
);
CREATE INDEX free_bets_status ON free_bets (status, expires_at);

-- One betting opportunity/event; holds any number of legs (back, lay, partial lays, free bets...).
CREATE TABLE positions (
    id                 INTEGER PRIMARY KEY,
    kind               TEXT NOT NULL DEFAULT 'qualifying' CHECK (kind IN ('qualifying', 'free_bet_snr', 'free_bet_sr', 'other')),
    event              TEXT NOT NULL CHECK (length(trim(event)) BETWEEN 1 AND 160),
    market             TEXT NOT NULL DEFAULT '',
    selection          TEXT NOT NULL DEFAULT '',
    event_at           TEXT,
    offer_id           INTEGER REFERENCES offers(id),
    status             TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'settled', 'void')),
    expected_pnl_pence INTEGER,
    notes              TEXT NOT NULL DEFAULT '',
    source             TEXT NOT NULL DEFAULT 'manual',
    settled_at         TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    deleted_at         TEXT
);
CREATE INDEX positions_status ON positions (status, event_at);
CREATE INDEX positions_offer ON positions (offer_id);

CREATE TABLE legs (
    id                    INTEGER PRIMARY KEY,
    position_id           INTEGER NOT NULL REFERENCES positions(id),
    account_id            INTEGER NOT NULL REFERENCES accounts(id),
    side                  TEXT NOT NULL CHECK (side IN ('back', 'lay')),
    stake_pence           INTEGER NOT NULL CHECK (stake_pence > 0),
    odds                  TEXT NOT NULL,                     -- exact decimal text, e.g. '4.20'
    commission_bp         INTEGER NOT NULL DEFAULT 0 CHECK (commission_bp BETWEEN 0 AND 9999),
    free_bet_id           INTEGER REFERENCES free_bets(id),  -- back leg paid with a free bet
    placed_at             TEXT NOT NULL,
    result                TEXT NOT NULL DEFAULT 'pending' CHECK (result IN
                            ('pending', 'won', 'lost', 'void', 'cashout', 'manual')),
    settled_amount_pence  INTEGER,   -- back: amount credited; lay: net movement before commission
    commission_paid_pence INTEGER,
    settled_at            TEXT,
    external_ref          TEXT NOT NULL DEFAULT '',
    source                TEXT NOT NULL DEFAULT 'manual',
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    deleted_at            TEXT
);
CREATE INDEX legs_position ON legs (position_id);
CREATE INDEX legs_account ON legs (account_id, result);
-- A free bet is used by at most one live leg (a void leg hands the free bet back).
CREATE UNIQUE INDEX legs_one_use_per_free_bet ON legs (free_bet_id)
    WHERE free_bet_id IS NOT NULL AND deleted_at IS NULL AND result <> 'void';

-- Money moved between two of your own accounts (bank <-> betting, betting <-> betting).
CREATE TABLE transfers (
    id                 INTEGER PRIMARY KEY,
    from_account_id    INTEGER NOT NULL REFERENCES accounts(id),
    to_account_id      INTEGER NOT NULL REFERENCES accounts(id),
    amount_pence       INTEGER NOT NULL CHECK (amount_pence > 0),
    at                 TEXT NOT NULL,
    -- temporary_funding:  bank -> betting, to be returned (not bankroll, not profit)
    -- temporary_return:   betting -> bank, repays temporary funding (excess = bankroll withdrawal)
    -- bankroll_contribution / bankroll_withdrawal: deliberate long-term capital changes
    -- internal:           betting -> betting
    capital_type       TEXT NOT NULL CHECK (capital_type IN
                         ('temporary_funding', 'temporary_return', 'bankroll_contribution', 'bankroll_withdrawal', 'internal')),
    temp_portion_pence INTEGER NOT NULL DEFAULT 0 CHECK (temp_portion_pence >= 0),
    notes              TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    voided_at          TEXT,
    CHECK (from_account_id <> to_account_id),
    CHECK (temp_portion_pence <= amount_pence)
);

-- The ledger. amount_pence is the signed effect on the account balance.
-- class: capital (never profit), stake (money at risk; profit once its leg settles), pnl (profit/loss).
-- System postings carry a posting_key; at most one ACTIVE entry exists per key, so editing a bet
-- supersedes the old entry (kept, linked) instead of duplicating or deleting money.
CREATE TABLE ledger_entries (
    id             INTEGER PRIMARY KEY,
    at             TEXT NOT NULL,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    amount_pence   INTEGER NOT NULL,
    category       TEXT NOT NULL CHECK (category IN
                     ('opening_balance', 'transfer', 'stake', 'winnings', 'lay_win', 'lay_loss', 'commission',
                      'refund', 'free_bet_winnings', 'promotion_reward', 'bonus_cash', 'adjustment',
                      'reconciliation', 'other')),
    class          TEXT NOT NULL CHECK (class IN ('capital', 'stake', 'pnl')),
    position_id    INTEGER REFERENCES positions(id),
    leg_id         INTEGER REFERENCES legs(id),
    offer_id       INTEGER REFERENCES offers(id),
    transfer_id    INTEGER REFERENCES transfers(id),
    free_bet_id    INTEGER REFERENCES free_bets(id),
    posting_key    TEXT,
    description    TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    superseded_at  TEXT,
    superseded_by  INTEGER REFERENCES ledger_entries(id),
    supersede_note TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX ledger_one_active_posting ON ledger_entries (posting_key)
    WHERE posting_key IS NOT NULL AND superseded_at IS NULL;
CREATE INDEX ledger_account ON ledger_entries (account_id, at);
CREATE INDEX ledger_position ON ledger_entries (position_id);
CREATE INDEX ledger_leg ON ledger_entries (leg_id);
CREATE INDEX ledger_active ON ledger_entries (superseded_at, at);

CREATE TABLE reconciliations (
    id                   INTEGER PRIMARY KEY,
    account_id           INTEGER NOT NULL REFERENCES accounts(id),
    app_balance_pence    INTEGER NOT NULL,
    actual_balance_pence INTEGER NOT NULL,
    difference_pence     INTEGER NOT NULL,
    entry_id             INTEGER REFERENCES ledger_entries(id),
    note                 TEXT NOT NULL DEFAULT '',
    at                   TEXT NOT NULL
);

-- Untrusted input (future iPhone Shortcut, pasted text, CSV...) waits here until confirmed.
CREATE TABLE import_candidates (
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
    applied_leg_id      INTEGER REFERENCES legs(id)
);
CREATE INDEX import_status ON import_candidates (status, created_at);

CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    entity      TEXT NOT NULL,
    entity_id   INTEGER,
    action      TEXT NOT NULL,
    before_json TEXT,
    after_json  TEXT,
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX audit_entity ON audit_log (entity, entity_id);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
