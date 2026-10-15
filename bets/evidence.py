"""Evidence: what a screenshot, receipt or confirmation shows, attached to the record it supports.

The assistant reads screenshots itself; it records here what the evidence proves (`facts`, e.g. {"status": "FULLY
MATCHED", "stake": "8.09", "odds": "6.8"}) and links it to the plan, position, leg, offer, free bet, transfer, balance
check, expected reward or account. The file itself can be uploaded in the web app (stored under var/evidence, never
public, images and PDFs only). Attaching evidence never changes money. The same evidence attached twice is recognised
(same record, kind and description/time, or same file) and returned instead of duplicated.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path

from . import services, times
from .services import UserError

ENTITIES = {"plan": "bet_plans", "position": "positions", "leg": "legs", "offer": "offers", "free_bet": "free_bets",
            "transfer": "transfers", "reconciliation": "reconciliations", "expected_reward": "expected_rewards",
            "account": "accounts"}
KINDS = ("bet_slip", "bookmaker_confirmation", "exchange_confirmation", "fully_matched", "partially_matched",
         "offer_terms", "promo_banner", "reward_credited", "free_bet_wallet", "bank_transaction", "balance",
         "event_result", "settled_receipt", "other")
MAX_FILE = 5 * 1024 * 1024
MAGIC = {b"\x89PNG\r\n\x1a\n": ("image/png", ".png"), b"\xff\xd8\xff": ("image/jpeg", ".jpg"),
         b"%PDF-": ("application/pdf", ".pdf"), b"RIFF": ("image/webp", ".webp")}


def _check_record(conn, entity: str, entity_id: int) -> None:
    if entity not in ENTITIES:
        raise UserError("entity: " + ", ".join(ENTITIES))
    if not services.row(conn, ENTITIES[entity], int(entity_id)):
        raise UserError(f"No {entity.replace('_', ' ')} #{entity_id}")


def add(conn, *, entity: str, entity_id: int, kind: str, description: str = "", facts: dict | None = None,
        captured_at: str | None = None, source: str = "assistant", file: tuple[bytes, str] | None = None) -> tuple[int, bool]:
    """-> (evidence id, created). file = (bytes, original name), stored only if it is a PNG/JPEG/WebP/PDF <= 5 MB."""
    _check_record(conn, entity, entity_id)
    if kind not in KINDS:
        raise UserError("kind: " + ", ".join(KINDS))
    facts = facts or {}
    if not isinstance(facts, dict):
        raise UserError("facts: an object of what the evidence shows")
    text = (description or "").strip()[:1000]
    sha = None
    if file:
        data = file[0]
        if len(data) > MAX_FILE:
            raise UserError("The file is larger than 5 MB")
        kind_ok = next((v for k, v in MAGIC.items() if data.startswith(k)), None)
        if not kind_ok or (kind_ok[0] == "image/webp" and data[8:12] != b"WEBP"):
            raise UserError("Upload a PNG, JPEG, WebP image or a PDF")
        sha = hashlib.sha256(data).hexdigest()
        same = conn.execute("SELECT id FROM evidence WHERE file_sha256 = ? AND entity = ? AND entity_id = ? AND "
                            "deleted_at IS NULL", (sha, entity, int(entity_id))).fetchone()
        if same:
            return same[0], False
    else:
        same = conn.execute("SELECT id FROM evidence WHERE entity = ? AND entity_id = ? AND kind = ? AND lower(description) "
                            "= lower(?) AND coalesce(captured_at, '') = coalesce(?, '') AND deleted_at IS NULL",
                            (entity, int(entity_id), kind, text, captured_at)).fetchone()
        if same:
            return same[0], False
    stored = None
    if file:
        folder = evidence_dir(conn)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        stored = secrets.token_hex(12) + kind_ok[1]
        (folder / stored).write_bytes(file[0])
    cur = conn.execute("INSERT INTO evidence (entity, entity_id, kind, source, captured_at, description, facts_json, "
                       "file_name, file_type, file_size, file_sha256, actor, request_id, created_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (entity, int(entity_id), kind, source[:40], captured_at, text, json.dumps(facts, sort_keys=True)[:4000],
                        stored, kind_ok[0] if file else None, len(file[0]) if file else None, sha,
                        services.current_actor(), services._REQUEST.get(), times.now_iso()))
    eid = cur.lastrowid
    services.audit(conn, "evidence", eid, "attached", None, {"entity": entity, "entity_id": int(entity_id), "kind": kind,
                                                             "description": text, "facts": facts, "file": bool(file)})
    return eid, True


_DIR: list = []


def evidence_dir(conn=None) -> Path:
    if _DIR:
        return _DIR[0]
    from .config import load_settings
    return load_settings().data_dir / "evidence"


def set_dir(path: Path) -> None:
    _DIR[:] = [Path(path)]


def for_record(conn, entity: str, entity_id: int) -> list[dict]:
    from .readapi import t
    out = []
    for r in conn.execute("SELECT * FROM evidence WHERE entity = ? AND entity_id = ? AND deleted_at IS NULL ORDER BY id",
                          (entity, int(entity_id))):
        out.append({"id": r["id"], "kind": r["kind"], "source": r["source"], "captured_at": t(r["captured_at"]),
                    "description": r["description"] or None, "facts": json.loads(r["facts_json"] or "{}"),
                    "file": {"type": r["file_type"], "size": r["file_size"]} if r["file_name"] else None,
                    "recorded_at": t(r["created_at"]), "actor": r["actor"]})
    return out


def file_of(conn, eid: int) -> tuple[Path, str] | None:
    r = conn.execute("SELECT file_name, file_type FROM evidence WHERE id = ? AND deleted_at IS NULL", (eid,)).fetchone()
    if not r or not r["file_name"]:
        return None
    path = evidence_dir(conn) / r["file_name"]
    return (path, r["file_type"]) if path.is_file() else None
