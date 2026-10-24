"""Append machine candidates to a formal corpus without changing source rows.

The caller supplies already domain-validated normalized records. This module
proves storage/source lineage and locator identity; it grants no semantic or
human approval. Original artifact/source payload hashes are preserved, while
separately named hashes identify this archival envelope. Diagnostics/receipts
contain identifiers, counts and hashes, never payload prose.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from ltr.library import source_lineage as lineage
from ltr.tools import candidate_write_safety as safety

TABLE = "corpus_candidate_archive"
OPERATION = "append_candidate_archive_v1"
KINDS = frozenset({"identity", "alignment", "ocr_region", "ocr_vision",
                   "ocr_reading_order", "unquoted", "publisher_identifier"})
FLAGS = {"requires_review": 1, "accepted": 0, "human_confirmed": 0,
         "semantic_verified": 0, "reference_eligible": 0, "ocr_verified": 0,
         "human_verified": 0}
REQUIRED = frozenset({"kind", "candidate_id", "source_sha256", "artifact_sha256", "payload"})
OPTIONAL = frozenset({"source_payload_sha256", "edition_id", "dialogue_id", "paragraph_id", "page_id"})
MAX_RECORDS = 1_000_000
MAX_RECORD_BYTES = 1 << 20
MAX_TOTAL_BYTES = 256 << 20
DDL = """CREATE TABLE corpus_candidate_archive (
    record_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('identity','alignment','ocr_region','ocr_vision','ocr_reading_order','unquoted','publisher_identifier')),
    candidate_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    source_payload_sha256 TEXT,
    normalized_payload_sha256 TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    edition_id TEXT, dialogue_id TEXT, paragraph_id TEXT, page_id TEXT,
    record_json TEXT NOT NULL,
    import_batch TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    requires_review INTEGER NOT NULL CHECK(requires_review=1),
    accepted INTEGER NOT NULL CHECK(accepted=0),
    human_confirmed INTEGER NOT NULL CHECK(human_confirmed=0),
    semantic_verified INTEGER NOT NULL CHECK(semantic_verified=0),
    reference_eligible INTEGER NOT NULL CHECK(reference_eligible=0),
    ocr_verified INTEGER NOT NULL CHECK(ocr_verified=0),
    human_verified INTEGER NOT NULL CHECK(human_verified=0),
    UNIQUE(source_sha256,kind,candidate_id,artifact_sha256)
)"""
CORE_COLUMNS = ("record_key", "kind", "candidate_id", "source_sha256", "artifact_sha256",
                "source_payload_sha256", "normalized_payload_sha256", "record_sha256",
                "edition_id", "dialogue_id", "paragraph_id", "page_id", "record_json")
ALL_COLUMNS = (*CORE_COLUMNS, "import_batch", "created_at_utc", *FLAGS)


class ArchiveError(ValueError):
    """Source-free refusal before commit."""


class ArchiveCommittedError(ArchiveError):
    """The transaction committed; do not retry the import as an uncommitted run."""
    def __init__(self, receipt_id, code):
        self.receipt_id = receipt_id
        super().__init__(code)


def _need(condition, code):
    if not condition:
        raise ArchiveError(code)


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, UnicodeError):
        raise ArchiveError("candidate_archive_json_invalid") from None


def _sha(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", value) is not None


def _flags(value, source, depth=0):
    _need(depth <= 64, "candidate_archive_payload_limit")
    if isinstance(value, dict):
        _need(all(isinstance(key, str) for key in value), "candidate_archive_json_invalid")
        for key, item in value.items():
            if key in FLAGS:
                _need(type(item) in (int, bool) and item == FLAGS[key], "candidate_archive_approval_flag_invalid")
            if key in {"source_sha256", "database_sha256"}:
                _need(item == source, "candidate_archive_payload_source_mismatch")
            _flags(item, source, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _flags(item, source, depth + 1)
    else:
        _need(value is None or type(value) in (str, int, bool, float), "candidate_archive_json_invalid")


def normalize_records(records, source_sha256):
    """Freeze strict envelopes in memory; return reproducible, source-free IDs.

    Required: kind, candidate_id, source_sha256, artifact_sha256, payload(dict).
    Optional: source_payload_sha256 and edition_id/dialogue_id/paragraph_id/page_id.
    Missing optional values normalize to null. Original hashes are never relabeled
    as hashes of the new envelope. Input order does not affect batch identity.
    """
    _need(_hash(source_sha256), "candidate_archive_source_hash_invalid")
    _need(isinstance(records, (list, tuple)) and 0 < len(records) <= MAX_RECORDS,
          "candidate_archive_record_count_invalid")
    result, seen, total_bytes = [], set(), 0
    for supplied in records:
        _need(isinstance(supplied, dict) and REQUIRED <= set(supplied)
              and set(supplied) <= REQUIRED | OPTIONAL, "candidate_archive_record_invalid")
        record = dict(supplied)
        for key in OPTIONAL:
            record.setdefault(key, None)
        _need(isinstance(record["kind"], str) and record["kind"] in KINDS and _id(record["candidate_id"]),
              "candidate_archive_identity_invalid")
        _need(record["source_sha256"] == source_sha256 and _hash(record["artifact_sha256"])
              and (record["source_payload_sha256"] is None or _hash(record["source_payload_sha256"])),
              "candidate_archive_provenance_invalid")
        for key in ("edition_id", "dialogue_id", "paragraph_id", "page_id"):
            _need(record[key] is None or _id(record[key]), "candidate_archive_locator_invalid")
        if record["kind"] in {"identity", "alignment"}:
            _need(record["edition_id"] is not None and record["dialogue_id"] is not None,
                  "candidate_archive_locator_missing")
        if record["kind"] == "unquoted":
            _need(record["edition_id"] is not None and record["paragraph_id"] is not None,
                  "candidate_archive_locator_missing")
        if record["kind"].startswith("ocr_"):
            _need(record["page_id"] is not None, "candidate_archive_locator_missing")
        if record["kind"] == "publisher_identifier":
            _need(record["edition_id"] is not None, "candidate_archive_locator_missing")
        _need(isinstance(record["payload"], dict), "candidate_archive_payload_invalid")
        _flags(record["payload"], source_sha256)
        encoded = _json(record)
        try:
            size = len(encoded.encode("utf-8"))
        except UnicodeError:
            raise ArchiveError("candidate_archive_json_invalid") from None
        total_bytes += size
        _need(size <= MAX_RECORD_BYTES and total_bytes <= MAX_TOTAL_BYTES, "candidate_archive_payload_limit")
        record = json.loads(encoded)  # Freeze mutable caller-owned nested payloads.
        key = _sha({field: record[field] for field in ("source_sha256", "kind", "candidate_id", "artifact_sha256")})
        _need(key not in seen, "candidate_archive_duplicate_record")
        seen.add(key)
        core = {field: record[field] for field in ("kind", "candidate_id", "source_sha256", "artifact_sha256",
                                                  "source_payload_sha256", "edition_id", "dialogue_id", "paragraph_id", "page_id")}
        core.update(record_key=key, normalized_payload_sha256=_sha(record["payload"]),
                    record_sha256=_sha(record), record_json=encoded)
        result.append(core)
    return sorted(result, key=lambda row: row["record_key"])


def _table_exists(con, table):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _validate_schema(con):
    if _table_exists(con, TABLE):
        actual = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()[0]
        _need(actual == DDL, "candidate_archive_schema_invalid")



def summary(con):
    """Read only archive kind/count/approval metadata; never read payloads.

    An absent table is unconfigured, not a measured zero. A present but malformed
    archive, unknown kind, non-integer flag or nonzero approval refuses the entire
    summary. This is a storage-status view, not candidate semantic acceptance or
    a fresh verification of historical receipts and payloads.
    """
    try:
        schema = con.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (TABLE,)).fetchall()
        if not schema:
            return {"available": False}
        _need(len(schema) == 1 and schema[0][0] == "table" and schema[0][1] == DDL,
              "candidate_archive_summary_invalid")
        valid = " AND ".join('typeof("' + flag + '\")=\'integer\' AND "' + flag + '\"=' + str(expected)
                             for flag, expected in FLAGS.items())
        rows = con.execute("SELECT kind,COUNT(*),SUM(CASE WHEN " + valid
                           + " THEN 0 ELSE 1 END) FROM " + TABLE + " GROUP BY kind ORDER BY kind").fetchall()
        counts = {}
        for kind, count, invalid in rows:
            _need(isinstance(kind, str) and kind in KINDS and type(count) is int and count > 0
                  and invalid == 0, "candidate_archive_summary_invalid")
            counts[kind] = count
        _need([tuple(row) for row in con.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (TABLE,))]
              == [tuple(row) for row in schema], "candidate_archive_summary_invalid")
        return {"available": True, "total": sum(counts.values()), "by_kind": counts,
                "requires_review_all": True, "approval_gates_all_zero": True,
                "status": "machine_candidates_pending_review"}
    except ArchiveError:
        raise
    except (sqlite3.Error, TypeError, ValueError, KeyError):
        raise ArchiveError("candidate_archive_summary_invalid") from None


def _validate_locators(source, records):
    for record in records:
        edition = record["edition_id"]
        if edition is not None:
            _need(len(source.execute("SELECT id FROM editions WHERE id=?", (edition,)).fetchall()) == 1,
                  "candidate_archive_edition_unbound")
        for field, table in (("dialogue_id", "dialogues"), ("paragraph_id", "paragraphs")):
            if record[field] is None:
                continue
            identity_column = "id"
            if field == "dialogue_id" and record["kind"] == "identity":
                selection = record["payload"].get("selection_kind")
                _need(selection in {"completion_quote", "haruhi_unknown"},
                      "candidate_archive_identity_selection_invalid")
                if selection == "completion_quote":
                    table, identity_column = "corpus_completion_quote", "quote_id"
            sql = "SELECT edition_id FROM " + table + " WHERE " + identity_column + "=?"
            params = [record[field]]
            if (field == "paragraph_id" or table == "corpus_completion_quote") and edition is not None:
                sql += " AND edition_id=?"
                params.append(edition)
            rows = source.execute(sql, params).fetchall()
            _need(len(rows) == 1 and (edition is None or rows[0][0] == edition),
                  "candidate_archive_locator_unbound")
        if record["page_id"] is not None:
            _need(len(source.execute("SELECT id FROM scan_pages WHERE id=?", (record["page_id"],)).fetchall()) == 1,
                  "candidate_archive_page_unbound")


def _lineage(before, after):
    old = set(lineage.snapshot(before)["tables"])
    tables = {TABLE, safety.OUTBOX}
    return lineage.assert_append_only(before, after, append_tables=tables & old, added_tables=tables - old)


RECEIPT_KEYS = frozenset({"schema", "batch_id", "source_sha256", "inputs_sha256", "created_at_utc",
    "rows_requested", "rows_inserted", "rows_reused", "archive_rows_before", "archive_rows_after",
    "backup", "lineage", "approval_flags", "scope", "identity_locator_policy", "receipt_id",
    "receipt_storage", "database_state", "receipt_sha256", "archive_after", "archive_sequence",
    "previous_receipt_sha256", "record_keys", "inserted_record_keys", "reused_record_keys"})


def _receipt_digest(receipt):
    return _sha({key: value for key, value in receipt.items() if key != "receipt_sha256"})


def _archive_digest(con):
    # Only this table is the historical archive anchor. Source-table proofs are
    # performed separately; do not rehash every source table for each receipt.
    return lineage._table_digest(con, TABLE)


def _validate_receipt_fields(receipt):
    proof = receipt["lineage"]
    _need(isinstance(proof, dict) and set(proof) == {"schema_before_sha256", "schema_after_sha256",
          "protected_tables", "appended_rows", "added_tables", "old_rows_preserved"}
          and proof["old_rows_preserved"] is True and _hash(proof["schema_before_sha256"])
          and _hash(proof["schema_after_sha256"]), "candidate_archive_receipt_lineage_invalid")
    for key in ("protected_tables", "added_tables"):
        _need(isinstance(proof[key], dict), "candidate_archive_receipt_lineage_invalid")
        for table, digest in proof[key].items():
            _need(isinstance(table, str) and isinstance(digest, dict)
                  and set(digest) == {"rows", "sha256", "columns_sha256"}
                  and type(digest["rows"]) is int and digest["rows"] >= 0
                  and _hash(digest["sha256"]) and _hash(digest["columns_sha256"]),
                  "candidate_archive_receipt_lineage_invalid")
    _need(isinstance(proof["appended_rows"], dict)
          and all(isinstance(table, str) and type(count) is int and count >= 0
                  for table, count in proof["appended_rows"].items()), "candidate_archive_receipt_lineage_invalid")
    backup = receipt["backup"]
    _need(isinstance(backup, dict) and set(backup) == {"file", "sha256", "bytes", "verification"}
          and isinstance(backup["file"], str) and Path(backup["file"]).name == backup["file"]
          and _hash(backup["sha256"]) and type(backup["bytes"]) is int and backup["bytes"] > 0
          and backup["verification"] == "SQLite backup; schema and all table digests match locked source; quick_check and foreign_key_check pass",
          "candidate_archive_receipt_backup_invalid")
    _need(receipt["identity_locator_policy"] == {"completion_quote": "corpus_completion_quote.quote_id",
          "haruhi_unknown": "dialogues.id"}
          and receipt["scope"] == "Machine candidate archive only; source rows and historical approvals preserved."
          and receipt["database_state"] == "Validated within the write transaction; this receipt and the guarded changes commit together. Export or recovery time does not describe a new database after-state."
          and isinstance(receipt["created_at_utc"], str), "candidate_archive_receipt_invalid")
    try:
        parsed = datetime.fromisoformat(receipt["created_at_utc"])
        _need(parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0,
              "candidate_archive_receipt_invalid")
    except (ValueError, TypeError):
        raise ArchiveError("candidate_archive_receipt_invalid") from None


def _history(con):
    """Validate the committed archive against its full-row receipt chain.

    This detects changed/deleted historical rows before the next backup. The
    chain is an integrity checksum, not an authenticated signature against an
    adversary able to rewrite both the archive and all receipts consistently.
    Exported receipts provide an independent copy of each checksum/anchor.
    """
    receipts = []
    rows = (con.execute("SELECT receipt_id,receipt_json FROM " + safety.OUTBOX + " WHERE operation=?", (OPERATION,))
            if _table_exists(con, safety.OUTBOX) else ())
    for receipt_id, raw in rows:
        try:
            receipt = json.loads(raw)
        except (ValueError, TypeError):
            raise ArchiveError("candidate_archive_receipt_invalid") from None
        _need(isinstance(receipt, dict) and set(receipt) == RECEIPT_KEYS
              and receipt.get("schema") == "corpus-candidate-archive-receipt-v1"
              and receipt.get("receipt_id") == receipt_id
              and isinstance(receipt_id, str) and re.fullmatch(r"[0-9a-f]{32}", receipt_id) is not None
              and receipt.get("receipt_storage") == safety.OUTBOX
              and isinstance(receipt.get("database_state"), str)
              and receipt.get("receipt_sha256") == _receipt_digest(receipt),
              "candidate_archive_receipt_invalid")
        _need(receipt["approval_flags"] == FLAGS and all(type(receipt["approval_flags"][key]) is int for key in FLAGS)
              and _id(receipt["batch_id"]) and _hash(receipt["source_sha256"]) and _hash(receipt["inputs_sha256"]),
              "candidate_archive_receipt_invalid")
        _validate_receipt_fields(receipt)
        for key in ("rows_requested", "rows_inserted", "rows_reused", "archive_rows_before", "archive_rows_after", "archive_sequence"):
            _need(type(receipt[key]) is int and receipt[key] >= 0, "candidate_archive_receipt_invalid")
        for key in ("record_keys", "inserted_record_keys", "reused_record_keys"):
            value = receipt[key]
            _need(isinstance(value, list) and all(_hash(item) for item in value)
                  and value == sorted(set(value)), "candidate_archive_receipt_invalid")
        all_keys, inserted, reused = (set(receipt[key]) for key in ("record_keys", "inserted_record_keys", "reused_record_keys"))
        _need(not inserted & reused and inserted | reused == all_keys
              and receipt["rows_requested"] == len(all_keys)
              and receipt["rows_inserted"] == len(inserted) and receipt["rows_reused"] == len(reused)
              and receipt["archive_rows_after"] == receipt["archive_rows_before"] + len(inserted),
              "candidate_archive_receipt_counts_invalid")
        anchor = receipt["archive_after"]
        _need(isinstance(anchor, dict) and set(anchor) == {"rows", "sha256", "columns_sha256"}
              and anchor["rows"] == receipt["archive_rows_after"] and _hash(anchor["sha256"])
              and _hash(anchor["columns_sha256"]), "candidate_archive_receipt_anchor_invalid")
        receipts.append(receipt)
    _need(not receipts or _table_exists(con, TABLE), "candidate_archive_history_missing")
    if not receipts:
        _need(not _table_exists(con, TABLE), "candidate_archive_history_missing")
        return []
    receipts.sort(key=lambda receipt: receipt["archive_sequence"])
    previous, batches, inserted_by_batch, seen_records = None, {}, {}, set()
    for sequence, receipt in enumerate(receipts, 1):
        _need(receipt["archive_sequence"] == sequence and receipt["batch_id"] not in batches
              and receipt["previous_receipt_sha256"] == (previous["receipt_sha256"] if previous else None)
              and receipt["archive_rows_before"] == (previous["archive_rows_after"] if previous else 0),
              "candidate_archive_receipt_chain_invalid")
        _need(set(receipt["reused_record_keys"]) <= seen_records
              and not set(receipt["inserted_record_keys"]) & seen_records,
              "candidate_archive_receipt_counts_invalid")
        seen_records.update(receipt["inserted_record_keys"])
        batches[receipt["batch_id"]] = receipt
        inserted_by_batch[receipt["batch_id"]] = set(receipt["inserted_record_keys"])
        previous = receipt
    _validate_schema(con)
    stored = {}
    for values in con.execute("SELECT " + ",".join(ALL_COLUMNS) + " FROM " + TABLE):
        row = dict(zip(ALL_COLUMNS, values))
        try:
            envelope = json.loads(row["record_json"])
        except (ValueError, TypeError):
            raise ArchiveError("candidate_archive_stored_record_invalid") from None
        normalized = normalize_records([envelope], row["source_sha256"])[0]
        _need(tuple(row[key] for key in CORE_COLUMNS) + tuple(row[key] for key in FLAGS) == _expected_core(normalized),
              "candidate_archive_stored_record_invalid")
        batch = batches.get(row["import_batch"])
        _need(batch is not None and row["record_key"] in inserted_by_batch[row["import_batch"]]
              and row["created_at_utc"] == batch["created_at_utc"], "candidate_archive_stored_record_invalid")
        stored[row["record_key"]] = row
    _need(set(stored) == seen_records and _archive_digest(con) == receipts[-1]["archive_after"],
          "candidate_archive_history_anchor_mismatch")
    for receipt in receipts:
        _need(receipt["inputs_sha256"] == _sha({"source_sha256": receipt["source_sha256"],
              "records": [stored[key]["record_sha256"] for key in receipt["record_keys"]]}),
              "candidate_archive_receipt_inputs_invalid")
    return receipts


def _find_batch(con, batch_id, history=None):
    history = _history(con) if history is None else history
    return next((receipt for receipt in history if receipt["batch_id"] == batch_id), None)


def _save_receipt(con, receipt):
    # save_receipt uses a random UUID, not a payload-derived ID. Seal the entire
    # finalized new receipt (including that UUID) before committing it. Only our
    # newly inserted row is finalized; no historical receipt is ever rewritten.
    receipt_id = safety.save_receipt(con, receipt, OPERATION)
    receipt["receipt_sha256"] = _receipt_digest(receipt)
    encoded = _json(receipt)
    con.execute("UPDATE " + safety.OUTBOX + " SET receipt_json=? WHERE receipt_id=?", (encoded, receipt_id))
    _need(con.execute("SELECT receipt_json FROM " + safety.OUTBOX + " WHERE receipt_id=?", (receipt_id,)).fetchone()[0] == encoded,
          "candidate_archive_receipt_readback_mismatch")
    return receipt_id


def _stored_core(con, key):
    row = con.execute("SELECT " + ",".join((*CORE_COLUMNS, *FLAGS)) + " FROM " + TABLE + " WHERE record_key=?", (key,)).fetchone()
    return tuple(row) if row else None


def _expected_core(record):
    return tuple(record[column] for column in CORE_COLUMNS) + tuple(FLAGS.values())


def _check_records(con, records):
    for record in records:
        _need(_stored_core(con, record["record_key"]) == _expected_core(record),
              "candidate_archive_record_readback_mismatch")


def _check_new_rows(con, records, batch_id, created):
    for record in records:
        expected = {**record, "import_batch": batch_id, "created_at_utc": created, **FLAGS}
        actual = con.execute("SELECT " + ",".join(ALL_COLUMNS) + " FROM " + TABLE + " WHERE record_key=?",
                             (record["record_key"],)).fetchone()
        _need(actual is not None and tuple(actual) == tuple(expected[column] for column in ALL_COLUMNS),
              "candidate_archive_new_row_readback_mismatch")


def _result(receipt, status, *, exported):
    return {"status": status, "receipt_id": receipt["receipt_id"], "batch_id": receipt["batch_id"],
            "rows_inserted": receipt["rows_inserted"], "rows_reused": receipt["rows_reused"],
            "source_sha256": receipt["source_sha256"], "receipt_exported": exported,
            "approval_flags": dict(FLAGS)}


def _export(db, receipt, target, status):
    try:
        safety.export_receipt(db, receipt["receipt_id"], target)
    except Exception:
        return _result(receipt, "committed_receipt_pending", exported=False)
    return _result(receipt, status, exported=True)


def apply(database, source_snapshot_db, source_snapshot_sha256, records, *, backup_path, receipt_path, batch_id):
    """Apply one atomic archive batch; identical replay only reads the database.

    Source snapshot must be a byte-verified sibling. A first import preserves
    every preexisting schema object and every source field, allowing growth only
    in this dedicated archive and the receipt outbox. Commit and receipt export
    are separate outcomes. Recover exports with candidate_write_safety helpers.
    """
    _need(_id(batch_id), "candidate_archive_batch_invalid")
    planned = normalize_records(records, source_snapshot_sha256)
    inputs_sha = _sha({"source_sha256": source_snapshot_sha256,
                       "records": [row["record_sha256"] for row in planned]})
    try:
        db = Path(database).absolute()
        snapshot_path = lineage.validate_snapshot_path(db, source_snapshot_db, source_snapshot_sha256)
        with lineage.readonly(snapshot_path) as source, lineage.readonly(db) as current:
            source.execute("BEGIN")
            current.execute("BEGIN")
            _validate_schema(current)
            receipt = _find_batch(current, batch_id)
            if receipt is not None:
                _need(receipt.get("inputs_sha256") == inputs_sha and receipt.get("source_sha256") == source_snapshot_sha256,
                      "candidate_archive_batch_conflict")
                _lineage(source, current)
                _check_records(current, planned)
                lineage.validate_snapshot_path(db, snapshot_path, source_snapshot_sha256)
                return _export(db, receipt, receipt_path, "already_committed")
        safety.preflight_file(receipt_path)
        safety.preflight_file(backup_path)
        receipt = None
        with lineage.readonly(snapshot_path) as source, closing(sqlite3.connect(db, isolation_level=None)) as writer:
            source.execute("BEGIN")
            writer.execute("PRAGMA foreign_keys=ON")
            try:
                writer.execute("BEGIN IMMEDIATE")
                safety.check_health(writer)
                _validate_schema(writer)
                _lineage(source, writer)
                _validate_locators(source, [json.loads(row["record_json"]) for row in planned])
                history = _history(writer)
                previous = _find_batch(writer, batch_id, history)
                if previous is not None:
                    _need(previous.get("inputs_sha256") == inputs_sha, "candidate_archive_batch_conflict")
                    _check_records(writer, planned)
                    writer.rollback()
                    return _export(db, previous, receipt_path, "already_committed")
                existed = _table_exists(writer, TABLE)
                count_before = writer.execute("SELECT COUNT(*) FROM " + TABLE).fetchone()[0] if existed else 0
                old_outbox = safety.digest(writer, safety.OUTBOX) if _table_exists(writer, safety.OUTBOX) else None
                new_records, reused = [], 0
                for record in planned:
                    existing = _stored_core(writer, record["record_key"]) if existed else None
                    if existing is None:
                        new_records.append(record)
                    else:
                        _need(existing == _expected_core(record), "candidate_archive_record_conflict")
                        reused += 1
                lineage.validate_snapshot_path(db, snapshot_path, source_snapshot_sha256)
                backup = safety.backup_locked(db, backup_path, writer)
                if not existed:
                    writer.execute(DDL)
                created = datetime.now(timezone.utc).isoformat()
                for record in new_records:
                    row = {**record, "import_batch": batch_id, "created_at_utc": created, **FLAGS}
                    writer.execute("INSERT INTO " + TABLE + " (" + ",".join(ALL_COLUMNS) + ") VALUES ("
                                   + ",".join("?" for _ in ALL_COLUMNS) + ")", tuple(row[col] for col in ALL_COLUMNS))
                # Establish the final schema before capturing the receipt's
                # proof. The receipt row itself is appended/sealed afterwards.
                writer.execute("CREATE TABLE IF NOT EXISTS " + safety.OUTBOX + " ("
                               "receipt_id TEXT PRIMARY KEY, operation TEXT NOT NULL, receipt_json TEXT NOT NULL)")

                def guards():
                    _validate_schema(writer)
                    _check_records(writer, planned)
                    _check_new_rows(writer, new_records, batch_id, created)
                    _need(writer.execute("SELECT COUNT(*) FROM " + TABLE).fetchone()[0] == count_before + len(new_records),
                          "candidate_archive_unexpected_rows")
                    with lineage.readonly(backup_path) as before:
                        report = _lineage(before, writer)
                    lineage.validate_snapshot_path(db, snapshot_path, source_snapshot_sha256)
                    safety.check_health(writer)
                    return report

                proof = guards()
                receipt = {"schema": "corpus-candidate-archive-receipt-v1", "batch_id": batch_id,
                           "source_sha256": source_snapshot_sha256, "inputs_sha256": inputs_sha,
                           "created_at_utc": created, "rows_requested": len(planned),
                           "rows_inserted": len(new_records), "rows_reused": reused,
                           "archive_rows_before": count_before, "archive_rows_after": count_before + len(new_records),
                           "backup": backup, "lineage": proof, "approval_flags": dict(FLAGS),
                           "identity_locator_policy": {"completion_quote": "corpus_completion_quote.quote_id",
                                                       "haruhi_unknown": "dialogues.id"},
                           "archive_after": _archive_digest(writer), "archive_sequence": len(history) + 1,
                           "previous_receipt_sha256": history[-1]["receipt_sha256"] if history else None,
                           "record_keys": [row["record_key"] for row in planned],
                           "inserted_record_keys": [row["record_key"] for row in new_records],
                           "reused_record_keys": sorted(set(row["record_key"] for row in planned)
                                                       - set(row["record_key"] for row in new_records)),
                           "scope": "Machine candidate archive only; source rows and historical approvals preserved."}
                receipt_id = _save_receipt(writer, receipt)
                guards()
                safety.check_outbox_preserved(writer, receipt_id, old_outbox)
                _history(writer)
                writer.commit()
            except BaseException:
                if writer.in_transaction:
                    writer.rollback()
                raise
        # Independent, source-free readback after the writer has closed.
        try:
            with lineage.readonly(backup_path) as before, lineage.readonly(db) as after:
                after.execute("BEGIN")
                _lineage(before, after)
                _check_records(after, planned)
                _check_new_rows(after, new_records, batch_id, created)
                _need(_find_batch(after, batch_id) == receipt, "candidate_archive_receipt_readback_mismatch")
                lineage.validate_snapshot_path(db, snapshot_path, source_snapshot_sha256)
                safety.check_health(after)
        except Exception:
            raise ArchiveCommittedError(receipt["receipt_id"], "candidate_archive_committed_verification_failed") from None
        return _export(db, receipt, receipt_path, "committed")
    except ArchiveError:
        raise
    except lineage.LineageError as exc:
        raise ArchiveError(str(exc)) from None
    except (OSError, sqlite3.Error, ValueError, TypeError):
        raise ArchiveError("candidate_archive_storage_failed") from None
