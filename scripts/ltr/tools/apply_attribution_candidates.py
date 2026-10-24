# -*- coding: utf-8 -*-
"""Materialise attribution candidates for one or MORE editions into the corpus DB.

Multi-run form: several editions are applied in a single transaction behind a SINGLE
database backup. That matters: a full backup is ~282 MB, and applying volume by
volume would leave 13+ full copies for the remaining volumes.

Discipline:
  1. Lock writers with BEGIN IMMEDIATE and take a WAL-inclusive SQLite backup.
  2. Snapshot all pre-existing tables (rows + content digest) and, if the candidate
     table already exists, the digest of ITS pre-existing rows.
  3. Create the candidate table only if missing. Otherwise append rows only -- never
     update or delete an existing row.
  4. Store the reproducible detector basis, never the run's own basis label.
  5. Prove afterwards that pre-existing table contents and prior candidate rows
     are unchanged, store a receipt in the same transaction, then export it
     atomically. The receipt outbox may append one new audit row.

Flags are pinned for every row: requires_review=1, human_confirmed=0,
semantic_verified=0, reference_eligible=0.

Usage:
  python apply_attribution_candidates.py --db DB --backup-dir DIR --receipt PATH \\
      --batch-tag TAG --run RUNDIR:ALIASMAP [--run RUNDIR:ALIASMAP ...] \\
      [--merged NAME] [--summary NAME] [--model-surface TEXT] [--backup-name NAME] \\
      [--table NAME]

Read-only receipt recovery (including after a hard crash):
  python apply_attribution_candidates.py --db DB --list-receipts
  python apply_attribution_candidates.py --db DB --recover-receipt ID --receipt PATH
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path
import re

try:
    from ltr.tools import candidate_write_safety as safety
except ImportError:  # direct script execution
    import candidate_write_safety as safety
try:
    from ltr.tools.basis_detector_v2 import STRONG_VERBS, LIMIT
except ImportError:  # direct script execution
    from basis_detector_v2 import STRONG_VERBS, LIMIT

WATCHED = ("editions", "paragraphs", "dialogues", "entries", "alignments",
           "chapter_candidates", "edition_pair_candidates", "scan_pages",
           "ocr_page_review", "ocr_reading_order_candidates",
           "ocr_recovery_candidates", "ocr_recovery_fragments")

DDL = """
CREATE TABLE {table} (
  dialogue_id TEXT PRIMARY KEY REFERENCES dialogues(id),
  edition_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  speaker_surface TEXT,
  speaker_canonical TEXT,
  addressee_surface TEXT,
  addressee_canonical TEXT,
  basis_detector TEXT NOT NULL
    CHECK(basis_detector IN ('literal_named','pronoun_named','inferred')),
  detector_evidence_json TEXT NOT NULL,
  run_id TEXT NOT NULL,
  run_model_surface TEXT NOT NULL,
  created_at TEXT NOT NULL,
  requires_review INTEGER NOT NULL CHECK(requires_review IN (0,1)),
  human_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(human_confirmed=0),
  semantic_verified INTEGER NOT NULL DEFAULT 0 CHECK(semantic_verified=0),
  reference_eligible INTEGER NOT NULL DEFAULT 0 CHECK(reference_eligible=0)
)
"""


def canonical_form(s: str | None) -> str | None:
    """Uniform shape for `*_canonical`: no whitespace, ASCII digits/letters.

    Applies to every canonical value regardless of what the alias map says, so the
    column cannot drift between volumes. CJK punctuation is deliberately preserved --
    full-width parentheses （） are correct Chinese typography, not a defect.
    """
    if not s:
        return s
    out = []
    for ch in s:
        if ch.isspace():
            continue
        o = ord(ch)
        if 0xFF10 <= o <= 0xFF19 or 0xFF21 <= o <= 0xFF3A or 0xFF41 <= o <= 0xFF5A:
            out.append(chr(o - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def pick(run_dir, preferred, fallback):
    for name in (preferred, fallback):
        path = run_dir / name
        if path.is_file():
            return path
    raise ValueError(f"neither {preferred} nor {fallback} found under {run_dir}")


def split_run(spec):
    # A separator before C:\\alias must not split at that alias drive colon.
    windows_pair = re.match(r"^(.*):([A-Za-z]:[\\/].*)$", spec)
    if windows_pair:
        return windows_pair.group(1), windows_pair.group(2)
    if ":" not in spec:
        raise ValueError("--run expects run_dir:alias_map; use --run-pair for two paths")
    return tuple(spec.rsplit(":", 1))


def json_file(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def unique_rows(rows, label):
    if not isinstance(rows, list):
        raise ValueError(f"{label} must be a list")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("dialogue_id"), str) or not row["dialogue_id"]:
            raise ValueError(f"{label} row has invalid dialogue_id")
        if row["dialogue_id"] in result:
            raise ValueError(f"duplicate dialogue_id in {label}")
        result[row["dialogue_id"]] = row
    return result


def load_runs(args):
    pairs = [split_run(spec) for spec in (args.run or [])] + (args.run_pair or [])
    if not pairs:
        raise ValueError("at least one --run or --run-pair is required")
    planned = []
    for run_path, alias_path in pairs:
        run_dir = Path(run_path)
        merged_path = pick(run_dir, args.merged, "merged-candidates.json")
        summary_path = pick(run_dir, args.summary, "run-summary.json")
        summary, merged, detector, alias = (json_file(summary_path), json_file(merged_path),
            json_file(run_dir / "basis-detector-v2.json"), json_file(alias_path))
        # The vol01 attribution contract predates identity candidates: its model
        # review boolean was a triage suggestion, never evidence of human review.
        # Only that explicit schema plus its two legacy filenames may ignore it.
        legacy_model_review = (
            summary.get("schema") == "haruhi-attribution-run-summary-v1"
            and merged_path.name == "merged-candidates.json"
            and summary_path.name == "run-summary.json")
        edition = summary.get("edition_id")
        if not isinstance(edition, str) or not edition or alias.get("edition_id") != edition or detector.get("edition_id") != edition:
            raise ValueError("summary/alias/detector edition mismatch")
        merged_by_id = unique_rows(merged, "merged candidates")
        detector_by_id = unique_rows(detector.get("results"), "detector")
        if not merged or set(merged_by_id) != set(detector_by_id):
            raise ValueError("merged/detector coverage differs or is empty")
        if type(summary.get("attempted")) is not int or summary["attempted"] != len(merged):
            raise ValueError("summary attempted count differs from merged candidates")
        amap = alias.get("map")
        if not isinstance(amap, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in amap.items()):
            raise ValueError("alias map must contain string keys and values")
        for row in merged:
            for key in ("speaker_candidate", "addressee_candidate"):
                if row.get(key) is not None and not isinstance(row[key], str):
                    raise ValueError(f"invalid {key}")
            for key, value in safety.FLAGS.items():
                if key == "requires_review" and legacy_model_review and type(row.get(key)) is bool:
                    continue  # Always materialised as 1, including model false.
                if key in row and (type(row[key]) not in (int, bool) or row[key] != value):
                    raise ValueError(f"input safety flag invalid: {key}")
            if "edition_id" in row and row["edition_id"] != edition:
                raise ValueError("merged row edition differs")
        planned.append({"run_id": run_dir.name, "edition": edition, "merged": merged,
                        "detector": detector_by_id, "detector_payload": detector, "alias": alias,
                        "requires_review_input_policy": ("legacy_model_boolean_ignored_and_pinned_to_1"
                            if legacy_model_review else "strict_requires_review_1"),
                        "legacy_model_requires_review_false_rows_normalised": (
                            sum(row.get("requires_review") is False for row in merged) if legacy_model_review else 0),
                        "sources": {"merged": merged_path.name, "summary": summary_path.name,
                                    "alias_map": Path(alias_path).name,
                                    "alias_map_schema": alias.get("schema", "unknown")}})
    if len({p["edition"] for p in planned}) != len(planned):
        raise ValueError("the same edition was supplied twice")
    return planned


def validate_bindings(con, planned):
    for p in planned:
        source = {r[0]: r for r in con.execute(
            "select id,ordinal,start_para,end_para from dialogues where edition_id=?", (p["edition"],))}
        if set(source) != {r["dialogue_id"] for r in p["merged"]}:
            raise ValueError("candidate IDs must exactly cover actual dialogues for the target edition")
        payload = p["detector_payload"]
        vocab = payload.get("referent_vocabulary")
        if not isinstance(vocab, list) or any(not isinstance(v, str) or not v for v in vocab):
            raise ValueError("invalid detector referent vocabulary")
        if payload.get("distance_limit_chars") != LIMIT:
            raise ValueError("unexpected detector distance limit")
        paragraphs = {r[0] for r in con.execute("select ordinal from paragraphs where edition_id=?", (p["edition"],))}
        for did, d in p["detector"].items():
            src = source[did]
            if type(d.get("ordinal")) is not int or d["ordinal"] != src[1]:
                raise ValueError("detector ordinal differs from source dialogue")
            if "edition_id" in d and d["edition_id"] != p["edition"]:
                raise ValueError("detector row edition differs")
            basis, evidence = d.get("basis_detector"), d.get("evidence")
            if basis not in ("inferred", "literal_named", "pronoun_named") or not isinstance(evidence, list):
                raise ValueError("invalid detector basis/evidence")
            if (basis == "inferred") != (len(evidence) == 0):
                raise ValueError("detector basis contradicts evidence")
            for ev in evidence:
                keys = {"ordinal", "referent", "verb", "distance_chars", "same_paragraph_as_quote"}
                if not isinstance(ev, dict) or set(ev) != keys:
                    raise ValueError("invalid detector evidence shape")
                if (type(ev["ordinal"]) is not int or ev["ordinal"] not in paragraphs
                    or not max(0, min(src[2], src[3]) - 1) <= ev["ordinal"] <= max(src[2], src[3]) + 1
                    or not isinstance(ev["referent"], str) or ev["referent"] not in [*vocab, "我"]
                    or ev["verb"] not in STRONG_VERBS or type(ev["distance_chars"]) is not int
                    or not 1 <= ev["distance_chars"] <= LIMIT
                    or type(ev["same_paragraph_as_quote"]) is not bool
                    or ev["same_paragraph_as_quote"] != (ev["ordinal"] == src[2])):
                    raise ValueError("invalid detector evidence values")
            named = any(e["referent"] != "我" for e in evidence)
            if evidence and ((basis == "literal_named") != named):
                raise ValueError("detector basis contradicts referent class")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--receipt")
    ap.add_argument("--list-receipts", action="store_true", help="list committed receipt IDs read-only")
    ap.add_argument("--recover-receipt", help="export this committed receipt ID read-only; no write arguments needed")
    ap.add_argument("--backup-dir")
    ap.add_argument("--batch-tag")
    ap.add_argument("--run", action="append", help="run_dir:alias_map; --run-pair avoids path separator ambiguity")
    ap.add_argument("--run-pair", nargs=2, action="append", metavar=("RUN_DIR", "ALIAS_MAP"))
    ap.add_argument("--merged", default="merged-identity.json")
    ap.add_argument("--summary", default="identity-summary.json")
    ap.add_argument("--model-surface", default="current-execution-host subagents")
    ap.add_argument("--backup-name")
    ap.add_argument("--table", default="attribution_candidates")
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
    safety.require_args(ap, a, ("receipt", "backup_dir", "batch_tag"))
    planned = load_runs(a)
    table = a.table
    qt = safety.identifier(table)
    if table in (*WATCHED, safety.OUTBOX) or table.startswith("sqlite_"):
        raise ValueError("reserved candidate table name")
    db = Path(a.db).resolve(strict=True)
    receipt_path = safety.preflight_file(a.receipt)
    backup = Path(a.backup_dir) / (a.backup_name or f"before-{table}-{a.batch_tag}.sqlite")
    safety.preflight_file(backup)
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    editions = [p["edition"] for p in planned]
    con = sqlite3.connect(db, isolation_level=None)
    try:
        con.execute("pragma foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        safety.check_health(con)
        validate_bindings(con, planned)
        existed = table in safety.table_names(con)
        if existed:
            safety.check_flags(con, table)
            for edition in editions:
                if con.execute(f"select count(*) from {qt} where edition_id=?", (edition,)).fetchone()[0]:
                    raise ValueError("edition already present; refusing (no backup taken)")
        pre = safety.snapshot(con, exclude=(table, safety.OUTBOX))
        pre_cand = safety.digest(con, table) if existed else None
        pre_outbox = safety.digest(con, safety.OUTBOX) if safety.OUTBOX in safety.table_names(con) else None
        backup_info = safety.backup_locked(db, backup, con)
        if not existed:
            con.execute(DDL.format(table=qt))
        per_edition = []
        for p in planned:
            amap = p["alias"]["map"]
            canon = lambda value: canonical_form(amap.get(value, value))
            rows = []
            for c in p["merged"]:
                d = p["detector"][c["dialogue_id"]]
                rows.append((c["dialogue_id"], p["edition"], d["ordinal"],
                    c.get("speaker_candidate"), canon(c.get("speaker_candidate")),
                    c.get("addressee_candidate"), canon(c.get("addressee_candidate")),
                    d["basis_detector"], json.dumps(d["evidence"], ensure_ascii=False, separators=(",", ":")),
                    p["run_id"], a.model_surface, created_at, 1))
            con.executemany(f"insert into {qt} (dialogue_id,edition_id,ordinal,speaker_surface,speaker_canonical,"
                "addressee_surface,addressee_canonical,basis_detector,detector_evidence_json,run_id,"
                "run_model_surface,created_at,requires_review) values (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            per_edition.append({"edition_id": p["edition"], "run_id": p["run_id"], "rows": len(rows),
                                "expected": len(rows), "sources": p["sources"],
                                "requires_review_input_policy": p["requires_review_input_policy"],
                                "legacy_model_requires_review_false_rows_normalised":
                                    p["legacy_model_requires_review_false_rows_normalised"]})
        placeholders = ",".join("?" for _ in editions)
        expected = sum(len(p["merged"]) for p in planned)

        def guards():
            if safety.snapshot(con, exclude=(table, safety.OUTBOX)) != pre:
                raise ValueError("an existing table changed; rolled back")
            post_old = safety.digest(con, table, f"edition_id NOT IN ({placeholders})", editions)
            if (pre_cand is not None and post_old != pre_cand) or (pre_cand is None and post_old["rows"]):
                raise ValueError("pre-existing candidate rows changed; rolled back")
            wanted = {}
            for p in planned:
                amap = p["alias"]["map"]
                for c in p["merged"]:
                    d = p["detector"][c["dialogue_id"]]
                    wanted[c["dialogue_id"]] = (c["dialogue_id"], p["edition"], d["ordinal"],
                        c.get("speaker_candidate"), canonical_form(amap.get(c.get("speaker_candidate"), c.get("speaker_candidate"))),
                        c.get("addressee_candidate"), canonical_form(amap.get(c.get("addressee_candidate"), c.get("addressee_candidate"))),
                        d["basis_detector"], json.dumps(d["evidence"], ensure_ascii=False, separators=(",", ":")),
                        p["run_id"], a.model_surface, created_at, 1, 0, 0, 0)
            # Explicit projection makes this guard safe for pre-existing schemas
            # with a different column order or extra audit columns.
            columns = "dialogue_id,edition_id,ordinal,speaker_surface,speaker_canonical,addressee_surface,addressee_canonical,basis_detector,detector_evidence_json,run_id,run_model_surface,created_at,requires_review,human_confirmed,semantic_verified,reference_eligible"
            current = {r[0]: tuple(r) for r in con.execute(f"select {columns} from {qt} where edition_id IN ({placeholders})", editions)}
            if current != wanted or len(current) != expected:
                raise ValueError("inserted rows differ from validated input")
            safety.check_flags(con, table)
            return safety.check_health(con)

        health = guards()
        total = con.execute(f"select count(*) from {qt}").fetchone()[0]
        receipt = {"schema": "haruhi-attribution-apply-receipt-v3", "applied_at_utc": created_at,
            "batch_tag": a.batch_tag, "editions": editions, "table": table,
            "table_created_this_run": not existed, "backup": backup_info,
            "rows_inserted": expected, "expected_rows": expected, "per_edition": per_edition,
            "table_rows_before": pre_cand["rows"] if pre_cand else 0, "table_rows_after": total,
            "candidate_table_digest_before": pre_cand,
            "candidate_table_digest_after": safety.digest(con, table),
            "protected_table_digests_before": pre,
            "protected_table_digests_after": safety.snapshot(con, exclude=(table, safety.OUTBOX)),
            "preexisting_outbox_digest": pre_outbox,
            "requires_review_true": expected, "reference_eligible_sum": 0, "human_confirmed_sum": 0,
            "semantic_verified_sum": 0, "existing_tables_unchanged": {t: True for t in pre},
            "all_existing_unchanged": True, "preexisting_candidate_rows_preserved": True,
            "by_basis_detector": {e: dict(con.execute(f"select basis_detector,count(*) from {qt} where edition_id=? group by 1", (e,))) for e in editions},
            "speaker_canonical_null": {e: con.execute(f"select count(*) from {qt} where edition_id=? and speaker_canonical is null", (e,)).fetchone()[0] for e in editions},
            "speaker_canonical_distinct": {e: con.execute(f"select count(distinct speaker_canonical) from {qt} where edition_id=?", (e,)).fetchone()[0] for e in editions},
            "scope_note": "Machine candidates only; requires_review=1, human_confirmed=semantic_verified=reference_eligible=0.", **health}
        receipt_id = safety.save_receipt(con, receipt, "apply_attribution_candidates")
        guards()  # Includes any side effects caused by outbox triggers.
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
