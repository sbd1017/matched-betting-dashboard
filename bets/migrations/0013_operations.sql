-- Operations: bet plans (proposed matched bets, before and while they are placed), exchange orders and fills,
-- price revisions, evidence, expected rewards, offer lifecycle facts, funding linked to a plan, and dismissed issues.
-- Nothing here changes a balance, a bet or a ledger entry: plans hold no money until bets are recorded through the
-- normal services (which create the position and its legs).

CREATE TABLE bet_plans (
    id                 INTEGER PRIMARY KEY,
    status             TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'validated', 'ready_to_place',
                         'bookmaker_placed', 'exchange_partially_matched', 'fully_matched', 'cancelled',
                         'converted_to_position', 'settled')),
    kind               TEXT NOT NULL DEFAULT 'qualifying' CHECK (kind IN ('qualifying', 'free_bet_snr', 'free_bet_sr', 'other')),
    event              TEXT NOT NULL CHECK (length(trim(event)) BETWEEN 1 AND 160),
    event_key          TEXT NOT NULL DEFAULT '',
    event_at           TEXT,
    market             TEXT NOT NULL DEFAULT '',
    market_key         TEXT NOT NULL DEFAULT '',
    selection          TEXT NOT NULL DEFAULT '',
    selection_key      TEXT NOT NULL DEFAULT '',
    offer_id           INTEGER REFERENCES offers(id),
    free_bet_id        INTEGER REFERENCES free_bets(id),
    outcomes_json      TEXT NOT NULL DEFAULT '[]',       -- outcome buckets; [] = simple two-way (selection / not)
    target             TEXT NOT NULL DEFAULT 'equal_profit',
    max_liability_pence INTEGER,
    min_retention_pct  REAL,                              -- this plan's own minimum (else the policy)
    sport              TEXT NOT NULL DEFAULT '',
    builder_legs       INTEGER,                           -- number of selections in a bet builder
    position_id        INTEGER REFERENCES positions(id),
    validation_json    TEXT,
    validated_at       TEXT,
    notes              TEXT NOT NULL DEFAULT '',
    version            INTEGER NOT NULL DEFAULT 1,       -- optimistic lock: every change increments it
    cancel_reason      TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX bet_plans_status ON bet_plans (status);
-- one free bet can be allocated to one live plan only
CREATE UNIQUE INDEX bet_plans_one_token ON bet_plans (free_bet_id)
    WHERE free_bet_id IS NOT NULL AND status NOT IN ('cancelled', 'settled', 'converted_to_position');

CREATE TABLE plan_legs (
    id                 INTEGER PRIMARY KEY,
    plan_id            INTEGER NOT NULL REFERENCES bet_plans(id),
    side               TEXT NOT NULL CHECK (side IN ('back', 'lay')),
    account_id         INTEGER NOT NULL REFERENCES accounts(id),
    label              TEXT NOT NULL DEFAULT '',
    covers_json        TEXT NOT NULL DEFAULT '[]',       -- outcome buckets in which this leg's selection happens
    market             TEXT NOT NULL DEFAULT '',
    stake_pence        INTEGER CHECK (stake_pence IS NULL OR stake_pence > 0),
    stake_auto         INTEGER NOT NULL DEFAULT 1,       -- lay stake calculated by the server
    odds               TEXT NOT NULL,
    commission_bp      INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'placed', 'submitted',
                         'partially_matched', 'fully_matched', 'cancelled')),
    placed_stake_pence INTEGER,
    placed_odds        TEXT,
    placed_at          TEXT,
    reference          TEXT NOT NULL DEFAULT '',
    leg_id             INTEGER REFERENCES legs(id),     -- the position leg of a placed back bet
    order_stake_pence  INTEGER,                          -- exchange order submitted (matched or not)
    order_odds         TEXT,
    unmatched_cancelled_pence INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX plan_legs_plan ON plan_legs (plan_id);

CREATE TABLE plan_fills (
    id                 INTEGER PRIMARY KEY,
    plan_leg_id        INTEGER NOT NULL REFERENCES plan_legs(id),
    stake_pence        INTEGER NOT NULL CHECK (stake_pence > 0),
    odds               TEXT NOT NULL,
    matched_at         TEXT NOT NULL,
    leg_id             INTEGER REFERENCES legs(id),     -- each fill is a lay leg of the position
    created_at         TEXT NOT NULL
);
CREATE INDEX plan_fills_leg ON plan_fills (plan_leg_id);

CREATE TABLE plan_revisions (
    id                 INTEGER PRIMARY KEY,
    plan_id            INTEGER NOT NULL REFERENCES bet_plans(id),
    revision           INTEGER NOT NULL,
    reason             TEXT NOT NULL DEFAULT '',
    legs_json          TEXT NOT NULL,                    -- odds and stakes of every leg at this revision
    figures_json       TEXT NOT NULL,                    -- lay stake, liability, locked result, retention
    actor              TEXT,
    created_at         TEXT NOT NULL
);
CREATE INDEX plan_revisions_plan ON plan_revisions (plan_id);

CREATE TABLE evidence (
    id                 INTEGER PRIMARY KEY,
    entity             TEXT NOT NULL CHECK (entity IN ('plan', 'position', 'leg', 'offer', 'free_bet', 'transfer',
                                                       'reconciliation', 'expected_reward', 'account')),
    entity_id          INTEGER NOT NULL,
    kind               TEXT NOT NULL,
    source             TEXT NOT NULL DEFAULT 'assistant',
    captured_at        TEXT,
    description        TEXT NOT NULL DEFAULT '',
    facts_json         TEXT NOT NULL DEFAULT '{}',       -- what the evidence shows, as structured facts
    file_name          TEXT,                             -- stored file (var/evidence), when one was uploaded
    file_type          TEXT,
    file_size          INTEGER,
    file_sha256        TEXT,
    actor              TEXT,
    request_id         TEXT,
    created_at         TEXT NOT NULL,
    deleted_at         TEXT
);
CREATE INDEX evidence_record ON evidence (entity, entity_id);

CREATE TABLE expected_rewards (
    id                 INTEGER PRIMARY KEY,
    offer_id           INTEGER REFERENCES offers(id),
    account_id         INTEGER NOT NULL REFERENCES accounts(id),
    source_position_id INTEGER REFERENCES positions(id),
    kind               TEXT NOT NULL CHECK (kind IN ('snr', 'sr', 'cash')),
    count              INTEGER NOT NULL DEFAULT 1 CHECK (count BETWEEN 1 AND 20),
    each_pence         INTEGER NOT NULL CHECK (each_pence > 0),
    expected_by        TEXT,
    expiry_days        INTEGER,
    status             TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'partially_received', 'received', 'void')),
    received_at        TEXT,
    notes              TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX expected_rewards_status ON expected_rewards (status);
ALTER TABLE free_bets ADD COLUMN expected_reward_id INTEGER REFERENCES expected_rewards(id);

ALTER TABLE transfers ADD COLUMN plan_id INTEGER REFERENCES bet_plans(id);

-- offer lifecycle facts (the stage itself is worked out from these and the linked records)
ALTER TABLE offers ADD COLUMN opt_in_required INTEGER NOT NULL DEFAULT 0;
ALTER TABLE offers ADD COLUMN opted_in_at TEXT;
ALTER TABLE offers ADD COLUMN deposit_required INTEGER NOT NULL DEFAULT 0;
ALTER TABLE offers ADD COLUMN deposit_done_at TEXT;
ALTER TABLE offers ADD COLUMN reviewed_at TEXT;
ALTER TABLE offers ADD COLUMN required_sport TEXT;
ALTER TABLE offers ADD COLUMN required_market TEXT;
ALTER TABLE offers ADD COLUMN min_legs INTEGER;
ALTER TABLE offers ADD COLUMN reward_delay_hours INTEGER;

CREATE TABLE issue_dismissals (
    id                 INTEGER PRIMARY KEY,
    issue_key          TEXT NOT NULL,
    fingerprint        TEXT NOT NULL,                    -- the facts it was reviewed against
    records_json       TEXT NOT NULL DEFAULT '[]',
    reason             TEXT NOT NULL,
    actor              TEXT,
    request_id         TEXT,
    created_at         TEXT NOT NULL,
    reopened_at        TEXT,                             -- the facts changed, so the issue came back
    reopen_note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX issue_dismissals_key ON issue_dismissals (issue_key);
