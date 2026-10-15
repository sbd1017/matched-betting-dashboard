-- Temporary funding: when the money has to go back to the bank (optional). Shown on the dashboard, the
-- accounts page and in the calendar. Only meaningful on temporary_funding transfers.

ALTER TABLE transfers ADD COLUMN due_at TEXT;
