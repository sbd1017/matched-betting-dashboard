-- Alert state for next actions (expiring free bets, offer deadlines, overdue rewards, hedges near kick-off, finished
-- events awaiting settlement, temporary funding to return): which alert was last sent, so external reminders
-- (ChatGPT, Telegram) do not repeat themselves. An alert comes back when it escalates (priority or deadline change).
-- Additive only: no existing table changes.

CREATE TABLE alert_state (
    alert_key   TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    notified_at TEXT NOT NULL,
    actor       TEXT,
    request_id  TEXT
);
