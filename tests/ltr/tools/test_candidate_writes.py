"""Synthetic-only transactional write regressions; no corpus paths are opened."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from ltr.tools import apply_attribution_candidates as apply
from ltr.tools import candidate_write_safety as safety
from ltr.tools import correct_canonical_values as correct


class CandidateWrites(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "synthetic.sqlite"
        self.run = self.root / "run"
        self.run.mkdir()
        self.alias = self.root / "alias.json"
        self.receipt = self.root / "receipt.json"
        con = sqlite3.connect(self.db)
        con.executescript("""
            CREATE TABLE editions(id TEXT PRIMARY KEY);
            CREATE TABLE paragraphs(id TEXT PRIMARY KEY,edition_id TEXT,ordinal INTEGER,text TEXT);
            CREATE TABLE dialogues(id TEXT PRIMARY KEY,edition_id TEXT,ordinal INTEGER,start_para INTEGER,end_para INTEGER);
            CREATE TABLE entries(id TEXT PRIMARY KEY,value TEXT);
            INSERT INTO editions VALUES ('E'),('OTHER');
            INSERT INTO paragraphs VALUES ('P1','E',1,'synthetic'),('P2','E',2,'synthetic');
            INSERT INTO dialogues VALUES ('D1','E',1,1,1),('D2','E',2,2,2),('OLD','OTHER',1,1,1);
            INSERT INTO entries VALUES ('entry','original');
        """)
        con.close()
        self.merged = [{"dialogue_id": did, "speaker_candidate": " Ａ ", "addressee_candidate": None}
                       for did in ("D1", "D2")]
        self.summary = {"edition_id": "E", "attempted": 2}
        self.detector = {"edition_id": "E", "referent_vocabulary": ["A"], "distance_limit_chars": 12,
                         "results": [{"dialogue_id": did, "ordinal": n, "basis_detector": "inferred", "evidence": []}
                                     for n, did in enumerate(("D1", "D2"), 1)]}
        self.alias_data = {"edition_id": "E", "map": {" Ａ ": " Ａ "}}
        self.write_inputs()

    def write_inputs(self, legacy=False):
        for path, data in [(self.run / ("merged-candidates.json" if legacy else "merged-identity.json"), self.merged),
                           (self.run / ("run-summary.json" if legacy else "identity-summary.json"), self.summary),
                           (self.run / "basis-detector-v2.json", self.detector), (self.alias, self.alias_data)]:
            path.write_text(json.dumps(data), encoding="utf-8")

    def args(self, tag="one", receipt=None):
        return ["--db", str(self.db), "--backup-dir", str(self.root / "backups"),
                "--receipt", str(receipt or self.receipt), "--batch-tag", tag,
                "--run-pair", str(self.run), str(self.alias)]

    def call(self, fn=apply.main, args=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = fn(args or self.args())
        return result, output.getvalue()

    def state(self):
        con = safety.readonly(self.db)
        try:
            return safety.schema_snapshot(con), safety.snapshot(con)
        finally:
            con.close()

    def execute(self, sql):
        con = sqlite3.connect(self.db)
        try:
            con.executescript(sql)
        finally:
            con.close()

    def candidate_table(self, old=False):
        self.execute(apply.DDL.format(table="attribution_candidates"))
        if old:
            con = sqlite3.connect(self.db)
            con.execute("""INSERT INTO attribution_candidates
                (dialogue_id,edition_id,ordinal,speaker_surface,speaker_canonical,addressee_surface,
                 addressee_canonical,basis_detector,detector_evidence_json,run_id,run_model_surface,created_at,requires_review)
                VALUES ('OLD','OTHER',1,'old','old','recipient','recipient','inferred','[]','old','synthetic','now',1)""")
            con.commit()
            con.close()

    def correction_args(self, tag="fix", receipt=None):
        return ["--db", str(self.db), "--column", "speaker_canonical", "--pair", "A=B",
                "--backup-dir", str(self.root / "backups"), "--receipt", str(receipt or self.root / "correct.json"), "--tag", tag]

    def test_apply_success_backup_and_idempotence(self):
        before = self.state()
        self.assertEqual(self.call()[0], 0)
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.assertEqual(receipt["rows_inserted"], 2)
        self.assertEqual(receipt["reference_eligible_sum"], 0)
        self.assertNotIn("database_sha256_after", receipt)
        self.assertIn("transaction", receipt["database_state"])
        self.assertEqual(receipt["protected_table_digests_before"], receipt["protected_table_digests_after"])
        self.assertEqual(receipt["candidate_table_digest_after"]["rows"], 2)
        con = sqlite3.connect(self.root / "backups" / receipt["backup"]["file"])
        try:
            self.assertEqual((safety.schema_snapshot(con), safety.snapshot(con)), before)
        finally:
            con.close()
        stable = self.state()
        with self.assertRaisesRegex(ValueError, "already present"):
            self.call(args=self.args("two", self.root / "two.json"))
        self.assertEqual(self.state(), stable)
        self.assertFalse((self.root / "backups" / "before-attribution_candidates-two.sqlite").exists())

    def test_legacy_files_and_windows_run_path_split(self):
        (self.run / "merged-identity.json").unlink()
        (self.run / "identity-summary.json").unlink()
        self.summary["schema"] = "haruhi-attribution-run-summary-v1"
        for n, row in enumerate(self.merged):
            row["requires_review"] = bool(n)
            row["speaker_basis"] = "unresolved"
            row["confidence_uncalibrated"] = 0.5
            row["context_paragraph_ordinals"] = [n + 1]
            row["note"] = "synthetic"
        self.write_inputs(legacy=True)
        args = self.args()
        args[-3:] = ["--run", str(self.run) + ":" + str(self.alias)]
        self.assertEqual(self.call(args=args)[0], 0)
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.assertEqual(receipt["requires_review_true"], 2)
        self.assertEqual(receipt["human_confirmed_sum"], 0)
        self.assertEqual(receipt["reference_eligible_sum"], 0)
        self.assertEqual(receipt["per_edition"][0]["legacy_model_requires_review_false_rows_normalised"], 1)
        self.assertEqual(receipt["per_edition"][0]["requires_review_input_policy"],
                         "legacy_model_boolean_ignored_and_pinned_to_1")
        self.assertEqual(apply.split_run(r"C:\runs\one:C:\maps\alias.json"),
                         (r"C:\runs\one", r"C:\maps\alias.json"))
        self.assertEqual(apply.split_run(r"C:\runs\one:alias.json"), (r"C:\runs\one", "alias.json"))

    def test_legacy_review_exception_requires_schema_and_both_filenames(self):
        before = self.state()
        self.merged[0]["requires_review"] = False
        for schema, legacy_names in (("haruhi-attribution-run-summary-v1", False),
                                     ("haruhi-identity-run-summary-v1", True)):
            with self.subTest(schema=schema, legacy_names=legacy_names):
                self.summary["schema"] = schema
                for name in ("identity-summary.json", "merged-identity.json", "run-summary.json", "merged-candidates.json"):
                    (self.run / name).unlink(missing_ok=True)
                self.write_inputs(legacy=legacy_names)
                with self.assertRaisesRegex(ValueError, "requires_review"):
                    self.call()
                self.assertEqual(self.state(), before)

    def test_legacy_review_exception_never_allows_review_or_reference_flags(self):
        (self.run / "merged-identity.json").unlink()
        (self.run / "identity-summary.json").unlink()
        self.summary["schema"] = "haruhi-attribution-run-summary-v1"
        self.merged[0]["requires_review"] = False
        before = self.state()
        for flag in ("human_confirmed", "semantic_verified", "reference_eligible"):
            with self.subTest(flag=flag):
                self.merged[0][flag] = 1
                self.write_inputs(legacy=True)
                with self.assertRaisesRegex(ValueError, flag): self.call()
                self.assertEqual(self.state(), before)
                del self.merged[0][flag]
        # Only historical JSON booleans get the exception; integer zero does not.
        self.merged[0]["requires_review"] = 0
        self.write_inputs(legacy=True)
        with self.assertRaisesRegex(ValueError, "requires_review"): self.call()
        self.assertEqual(self.state(), before)

    def test_existing_rows_preserved(self):
        self.candidate_table(old=True)
        con = safety.readonly(self.db)
        try:
            old = safety.digest(con, "attribution_candidates")
        finally:
            con.close()
        self.call()
        con = safety.readonly(self.db)
        try:
            self.assertEqual(old, safety.digest(con, "attribution_candidates", "edition_id='OTHER'"))
        finally:
            con.close()

    def test_apply_trigger_old_table_change_rolls_back(self):
        self.candidate_table(old=True)
        self.execute("CREATE TRIGGER mutate AFTER INSERT ON attribution_candidates BEGIN UPDATE entries SET value='changed'; END;")
        before = self.state()
        with self.assertRaisesRegex(ValueError, "existing table changed"):
            self.call()
        self.assertEqual(self.state(), before)
        self.assertFalse(self.receipt.exists())

    def test_apply_trigger_old_candidate_change_rolls_back(self):
        self.candidate_table(old=True)
        self.execute("CREATE TRIGGER mutate AFTER INSERT ON attribution_candidates BEGIN UPDATE attribution_candidates SET speaker_surface='changed' WHERE dialogue_id='OLD'; END;")
        before = self.state()
        with self.assertRaisesRegex(ValueError, "pre-existing candidate"):
            self.call()
        self.assertEqual(self.state(), before)

    def test_first_creation_failure_leaves_no_schema(self):
        before = self.state()
        with mock.patch.object(safety, "save_receipt", side_effect=RuntimeError("injected receipt storage failure")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.call()
        self.assertEqual(self.state(), before)
        self.assertFalse(self.receipt.exists())

    def test_baseexception_rolls_back_and_releases_lock(self):
        before = self.state()
        with mock.patch.object(safety, "save_receipt", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.call()
        self.assertEqual(self.state(), before)
        con = sqlite3.connect(self.db, timeout=0)
        con.execute("BEGIN IMMEDIATE")
        con.rollback()
        con.close()

    def test_output_directory_or_existing_file_rejected_before_write(self):
        before = self.state()
        for mode in ("directory", "file"):
            target = self.root / mode
            if mode == "directory": target.mkdir()
            else: target.write_text("existing", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                self.call(args=self.args(receipt=target))
            self.assertEqual(self.state(), before)
        self.assertFalse((self.root / "backups").exists())

    def test_receipt_unwritable_preflight_does_not_write_db(self):
        before = self.state()
        with mock.patch.object(safety.tempfile, "mkstemp", side_effect=PermissionError("synthetic ACL denial")):
            with self.assertRaises(PermissionError): self.call()
        self.assertEqual(self.state(), before)

    def test_backup_no_clobber(self):
        backup = self.root / "backups" / "before-attribution_candidates-one.sqlite"
        backup.parent.mkdir()
        backup.write_bytes(b"keep")
        before = self.state()
        with self.assertRaises(FileExistsError): self.call()
        self.assertEqual(backup.read_bytes(), b"keep")
        self.assertEqual(self.state(), before)

    def test_export_failure_commits_outbox_and_readonly_recovery(self):
        with mock.patch.object(safety, "export_receipt", side_effect=OSError("synthetic export failure")):
            result, output = self.call()
        self.assertEqual(result, 2)
        pending = json.loads(output)
        self.assertEqual(pending["status"], "committed_receipt_pending")
        rid = pending["receipt_id"]
        con = safety.readonly(self.db)
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM attribution_candidates").fetchone()[0], 2)
            self.assertEqual(con.execute(f"SELECT count(*) FROM {safety.OUTBOX} WHERE receipt_id=?", (rid,)).fetchone()[0], 1)
        finally:
            con.close()
        before = self.state()
        connect = sqlite3.connect
        seen = []
        def tracked(*args, **kwargs):
            seen.append((args[0], kwargs))
            return connect(*args, **kwargs)
        recovery = ["--db", str(self.db), "--recover-receipt", rid, "--receipt", str(self.receipt)]
        with mock.patch.object(safety.sqlite3, "connect", side_effect=tracked):
            self.assertEqual(self.call(args=recovery)[0], 0)
        self.assertTrue(all("mode=ro" in path and kw.get("uri") for path, kw in seen))
        self.assertEqual(self.state(), before)
        self.assertEqual(self.call(args=recovery)[0], 0)  # exact same receipt idempotent
        formatted = json.dumps(json.loads(self.receipt.read_text(encoding="utf-8")), indent=4)
        self.receipt.write_text(formatted, encoding="utf-8")
        self.assertEqual(self.call(args=recovery)[0], 0)  # equivalent JSON also idempotent
        self.assertEqual(self.receipt.read_text(encoding="utf-8"), formatted)
        self.receipt.write_text("different", encoding="utf-8")
        with self.assertRaises(FileExistsError): self.call(args=recovery)
        self.assertEqual(self.state(), before)

    def test_interrupt_during_export_reports_committed_pending(self):
        with mock.patch.object(safety, "export_receipt", side_effect=KeyboardInterrupt):
            code, output = self.call()
        self.assertEqual(code, 2)
        rid = json.loads(output)["receipt_id"]
        self.assertEqual(safety.export_receipt(self.db, rid, self.receipt)["rows_inserted"], 2)

    def test_readonly_list_discovers_id_after_unreported_commit(self):
        self.assertEqual(safety.list_receipts(self.db), [])
        with mock.patch.object(safety, "export_receipt", side_effect=KeyboardInterrupt):
            self.call()  # Deliberately discard the status/ID as a crashed caller would.
        before = self.state()
        for tool in (apply.main, correct.main):
            code, output = self.call(tool, ["--db", str(self.db), "--list-receipts"])
            self.assertEqual(code, 0)
            found = json.loads(output)["receipts"]
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["operation"], "apply_attribution_candidates")
            self.assertEqual(len(found[0]["receipt_id"]), 32)
            self.assertEqual(self.state(), before)
        safety.export_receipt(self.db, found[0]["receipt_id"], self.receipt)
        self.assertEqual(self.state(), before)

    def test_atomic_publish_race_never_overwrites(self):
        self.call()
        rid = json.loads(self.receipt.read_text(encoding="utf-8"))["receipt_id"]
        other = self.root / "race.json"
        real_link = safety.os.link
        def race(src, dst):
            Path(dst).write_bytes(b"other writer")
            return real_link(src, dst)
        with mock.patch.object(safety.os, "link", side_effect=race):
            with self.assertRaises(FileExistsError): safety.export_receipt(self.db, rid, other)
        self.assertEqual(other.read_bytes(), b"other writer")
        self.assertFalse(list(self.root.glob(".candidate-receipt-*")))

    def test_wal_backup_includes_both_committed_rows(self):
        # Keep an owning connection open so SQLite does not remove/checkpoint WAL.
        writer = sqlite3.connect(self.db)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("DELETE FROM entries")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("INSERT INTO entries VALUES ('W1','one')")
        writer.commit()
        writer.execute("INSERT INTO entries VALUES ('W2','two')")
        writer.commit()
        self.assertTrue(Path(str(self.db) + "-wal").stat().st_size > 0)
        self.call()
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        con = sqlite3.connect(self.root / "backups" / receipt["backup"]["file"])
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM entries").fetchone()[0], 2)
            self.assertEqual(list(con.execute("SELECT id FROM entries ORDER BY id")), [("W1",), ("W2",)])
        finally:
            con.close()

    def test_invalid_inputs_leave_no_database_change(self):
        mutations = {
            "wrong summary edition": lambda: self.summary.update(edition_id="OTHER"),
            "wrong alias edition": lambda: self.alias_data.update(edition_id="OTHER"),
            "wrong detector edition": lambda: self.detector.update(edition_id="OTHER"),
            "wrong ordinal": lambda: self.detector["results"][0].update(ordinal=99),
            "duplicate merged ID": lambda: self.merged.append(dict(self.merged[0])),
            "duplicate detector ID": lambda: self.detector["results"].append(dict(self.detector["results"][0])),
            "missing merged coverage": lambda: self.merged.pop(),
            "wrong actual binding": lambda: (self.merged[0].update(dialogue_id="OLD"), self.detector["results"][0].update(dialogue_id="OLD")),
            "bad evidence type": lambda: self.detector["results"][0].update(evidence={}),
            "basis empty evidence": lambda: self.detector["results"][0].update(basis_detector="literal_named"),
            "bad flags": lambda: self.merged[0].update(reference_eligible=1),
            "false review": lambda: self.merged[0].update(requires_review=0),
        }
        original = json.dumps([self.merged, self.summary, self.detector, self.alias_data])
        before = self.state()
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                self.merged, self.summary, self.detector, self.alias_data = json.loads(original)
                mutate()
                self.write_inputs()
                with self.assertRaises(ValueError): self.call()
                self.assertEqual(self.state(), before)
                self.assertFalse(self.receipt.exists())
                self.assertFalse(list((self.root / "backups").glob("*.sqlite")))

    def test_named_evidence_shapes_and_class(self):
        ev = {"ordinal": 1, "referent": "A", "verb": "说", "distance_chars": 1, "same_paragraph_as_quote": True}
        self.detector["results"][0].update(basis_detector="literal_named", evidence=[ev])
        self.write_inputs()
        self.call()
        self.assertEqual(json.loads(self.receipt.read_text(encoding="utf-8"))["by_basis_detector"]["E"]["literal_named"], 1)

    def test_existing_bad_flag_rejected(self):
        self.candidate_table(old=True)
        self.execute("UPDATE attribution_candidates SET requires_review=0 WHERE dialogue_id='OLD'")
        before = self.state()
        with self.assertRaisesRegex(ValueError, "flags"): self.call()
        self.assertEqual(self.state(), before)

    def test_outbox_trigger_side_effect_is_guarded(self):
        self.execute(f"CREATE TABLE {safety.OUTBOX}(receipt_id TEXT PRIMARY KEY,operation TEXT NOT NULL,receipt_json TEXT NOT NULL);"
                     f"CREATE TRIGGER outbox_mutate AFTER INSERT ON {safety.OUTBOX} BEGIN UPDATE entries SET value='changed'; END;")
        before = self.state()
        with self.assertRaisesRegex(ValueError, "existing table changed"): self.call()
        self.assertEqual(self.state(), before)

    def test_correction_success_unrelated_rows_stable_and_rerun_refused(self):
        self.candidate_table(old=True)
        self.call()
        con = safety.readonly(self.db)
        try: old = tuple(con.execute("SELECT * FROM attribution_candidates WHERE dialogue_id='OLD'").fetchone())
        finally: con.close()
        self.assertEqual(self.call(correct.main, self.correction_args())[0], 0)
        receipt = json.loads((self.root / "correct.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["rows_corrected"], 2)
        con = safety.readonly(self.db)
        try:
            self.assertEqual(tuple(con.execute("SELECT * FROM attribution_candidates WHERE dialogue_id='OLD'").fetchone()), old)
            self.assertEqual(con.execute("SELECT count(*) FROM attribution_candidates WHERE speaker_canonical='B'").fetchone()[0], 2)
        finally: con.close()
        stable = self.state()
        with self.assertRaisesRegex(ValueError, "nothing matches"):
            self.call(correct.main, self.correction_args("again", self.root / "again.json"))
        self.assertEqual(self.state(), stable)

    def test_correction_trigger_old_table_rolls_back(self):
        self.call()
        self.execute("CREATE TRIGGER correction_mutate AFTER UPDATE ON attribution_candidates BEGIN UPDATE entries SET value='changed'; END;")
        before = self.state()
        with self.assertRaisesRegex(ValueError, "existing table changed"):
            self.call(correct.main, self.correction_args())
        self.assertEqual(self.state(), before)

    def test_correction_unrelated_row_change_rolls_back(self):
        self.candidate_table(old=True)
        self.call()
        self.execute("CREATE TRIGGER correction_mutate AFTER UPDATE ON attribution_candidates WHEN new.dialogue_id <> 'OLD' BEGIN UPDATE attribution_candidates SET speaker_surface='changed' WHERE dialogue_id='OLD'; END;")
        before = self.state()
        with self.assertRaisesRegex(ValueError, "exact expected rows"):
            self.call(correct.main, self.correction_args())
        self.assertEqual(self.state(), before)

    def test_correction_interrupt_rollback_and_pending_export_recovery(self):
        self.call()
        before = self.state()
        with mock.patch.object(safety, "save_receipt", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.call(correct.main, self.correction_args())
        self.assertEqual(self.state(), before)
        with mock.patch.object(safety, "export_receipt", side_effect=OSError("synthetic export failure")):
            code, output = self.call(correct.main, self.correction_args("retry"))
        self.assertEqual(code, 2)
        rid = json.loads(output)["receipt_id"]
        stable = self.state()
        args = ["--db", str(self.db), "--recover-receipt", rid, "--receipt", str(self.root / "recovered.json")]
        self.assertEqual(self.call(correct.main, args)[0], 0)
        self.assertEqual(self.state(), stable)
        self.assertEqual(json.loads((self.root / "recovered.json").read_text(encoding="utf-8"))["rows_corrected"], 2)


if __name__ == "__main__":
    unittest.main()
