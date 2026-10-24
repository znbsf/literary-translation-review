# -*- coding: utf-8 -*-
"""Controlled correction of canonical values in an append-only candidate table.

The pipeline's rule is "never update an existing row". This tool exists for the one
narrow case where that rule would force a full table rebuild to fix a *pure
canonical-value remap*: the information is already complete, the change is
deterministic (a registry variant -> its canonical), and it is reversible because the
surface forms are never touched.

Safeguards, all mandatory:
  * BEGIN IMMEDIATE; WAL-inclusive SQLite backup with logical verification
  * only ONE column may be named for correction
  * the set of changed dialogue_ids must equal the expected set exactly
  * for every changed row, every OTHER column must be byte-identical
  * the total row count must not change
  * every pre-existing table outside the candidate table must not change;
    the receipt outbox may append one new audit row
  * a change log (dialogue_id, old, new) goes into a transactionally saved receipt

Usage:
  python correct_canonical_values.py --db DB --column addressee_canonical \\
      --pair OLD=NEW [--pair OLD=NEW ...] --backup-dir DIR --receipt PATH --tag TAG

Read-only receipt recovery:
  python correct_canonical_values.py --db DB --list-receipts
  python correct_canonical_values.py --db DB --recover-receipt ID --receipt PATH
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path

try:
    from ltr.tools import candidate_write_safety as safety
except ImportError:  # direct script execution
    import candidate_write_safety as safety

WATCHED = ("editions", "paragraphs", "dialogues", "entries", "alignments",
           "chapter_candidates", "edition_pair_candidates", "scan_pages",
           "ocr_page_review", "ocr_reading_order_candidates",
           "ocr_recovery_candidates", "ocr_recovery_fragments")
TABLE = "attribution_candidates"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--receipt")
    ap.add_argument("--list-receipts", action="store_true", help="list committed receipt IDs read-only")
    ap.add_argument("--recover-receipt", help="export a committed outbox receipt read-only")
    ap.add_argument("--column")
    ap.add_argument("--pair", action="append", help="OLD=NEW")
    ap.add_argument("--backup-dir")
    ap.add_argument("--tag")
    a = ap.parse_args(argv)
    if a.list_receipts:
        if a.recover_receipt:
            ap.error("--list-receipts and --recover-receipt are mutually exclusive")
        print(json.dumps({"receipts": safety.list_receipts(a.db)}))
        return 0
    if a.recover_receipt:
        safety.require_args(ap, a, ("receipt",))
        safety.export_receipt(a.db, a.recover_receipt, a.receipt)
        print(json.dumps({"status": "receipt_exported", "receipt_id": a.recover_receipt}))
        return 0
    safety.require_args(ap, a, ("receipt", "column", "pair", "backup_dir", "tag"))
    if a.column not in ("speaker_canonical", "addressee_canonical"):
        raise ValueError("only the two canonical columns may be corrected")
    pairs = {}
    for spec in a.pair:
        if "=" not in spec:
            raise ValueError("--pair expects OLD=NEW")
        old, new = spec.split("=", 1)
        if old in pairs or old == new or not new.strip():
            raise ValueError("duplicate/no-op pair or blank target canonical value")
        pairs[old] = new
    if set(pairs).intersection(pairs.values()):
        raise ValueError("overlapping source/target correction pairs are ambiguous")
    db = Path(a.db).resolve(strict=True)
    receipt_path = safety.preflight_file(a.receipt)
    backup = Path(a.backup_dir) / f"before-correction-{a.tag}.sqlite"
    safety.preflight_file(backup)
    con = sqlite3.connect(db, isolation_level=None)
    con.row_factory = sqlite3.Row
    qt, qc = safety.identifier(TABLE), safety.identifier(a.column)
    try:
        con.execute("pragma foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        safety.check_health(con)
        safety.check_flags(con, TABLE)
        before = {r["dialogue_id"]: dict(r) for r in con.execute(f"select * from {qt}")}
        if len(before) != con.execute(f"select count(*) from {qt}").fetchone()[0]:
            raise ValueError("candidate dialogue IDs are not unique")
        wanted = {did: dict(row) for did, row in before.items()}
        log = []
        for did, row in before.items():
            old = row[a.column]
            if old in pairs:
                wanted[did][a.column] = pairs[old]
                log.append({"dialogue_id": did, "column": a.column, "old": old, "new": pairs[old]})
        if not log:
            raise ValueError("nothing matches those values; refusing to run")
        pre_watch = safety.snapshot(con, exclude=(TABLE, safety.OUTBOX))
        pre_outbox = safety.digest(con, safety.OUTBOX) if safety.OUTBOX in safety.table_names(con) else None
        pre_table = safety.digest(con, TABLE)
        backup_info = safety.backup_locked(db, backup, con)
        for item in log:
            con.execute(f"update {qt} set {qc}=? where dialogue_id=? and {qc} IS ?",
                        (item["new"], item["dialogue_id"], item["old"]))

        def guards():
            after = {r["dialogue_id"]: dict(r) for r in con.execute(f"select * from {qt}")}
            total = con.execute(f"select count(*) from {qt}").fetchone()[0]
            if after != wanted or total != len(before):
                raise ValueError("correction differs from exact expected rows; rolled back")
            if safety.snapshot(con, exclude=(TABLE, safety.OUTBOX)) != pre_watch:
                raise ValueError("an existing table changed; rolled back")
            safety.check_flags(con, TABLE)
            return safety.check_health(con)

        health = guards()
        receipt = {"schema": "haruhi-canonical-correction-receipt-v2", "tag": a.tag,
            "corrected_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "kind": "CORRECTION (not an append)", "column": a.column, "pairs": pairs,
            "rows_corrected": len(log), "expected_rows_corrected": len(log), "change_log": log,
            "backup": backup_info, "table_rows_before": len(before), "table_rows_after": len(before),
            "table_rows_unchanged": True, "table_digest_before": pre_table,
            "table_digest_after": safety.digest(con, TABLE), "other_columns_stable": True,
            "protected_table_digests_before": pre_watch,
            "protected_table_digests_after": safety.snapshot(con, exclude=(TABLE, safety.OUTBOX)),
            "preexisting_outbox_digest": pre_outbox,
            "other_column_differences": [], "unrelated_rows_stable": True,
            "residual_matching_values": 0, "existing_tables_unchanged": {t: True for t in pre_watch},
            "all_existing_unchanged": True,
            "flags": {"requires_review_sum": len(before), "reference_eligible_sum": 0,
                      "human_confirmed_sum": 0, "semantic_verified_sum": 0},
            "surface_columns_untouched": True, **health}
        receipt_id = safety.save_receipt(con, receipt, "correct_canonical_values")
        guards()  # Outbox triggers must pass the same mutation boundaries.
        safety.check_outbox_preserved(con, receipt_id, pre_outbox)
        con.commit()
    except BaseException:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()
    return safety.finish_export(db, receipt_id, receipt_path)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Exception, KeyboardInterrupt) as exc:
        print(json.dumps({"status": "failed", "error_code": type(exc).__name__}))
        sys.exit(1)
