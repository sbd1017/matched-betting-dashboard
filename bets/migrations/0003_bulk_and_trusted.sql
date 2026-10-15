-- Bulk confirmation and Trusted Assistant mode.
-- decided_by: who confirmed/rejected a candidate: 'user' (one at a time), 'user-bulk' (Confirm selected /
-- Confirm all valid) or 'assistant-auto' (Trusted Assistant mode). hold_reason: why Trusted Assistant mode left
-- an assistant submission for review instead of confirming it.

ALTER TABLE import_candidates ADD COLUMN decided_by TEXT;
ALTER TABLE import_candidates ADD COLUMN hold_reason TEXT;

UPDATE import_candidates SET decided_by = 'user' WHERE status <> 'needs_confirmation' AND decided_by IS NULL;
