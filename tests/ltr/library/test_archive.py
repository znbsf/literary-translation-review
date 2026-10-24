"""Pure synthetic DB/FTS/transaction/receipt tests of the archive API."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ltr.library import archive
from ltr.library import source_lineage as lineage


class CandidateArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "dialogue.sqlite"
        with closing(sqlite3.connect(self.db)) as con:
            con.executescript("""
                CREATE TABLE editions(id TEXT PRIMARY KEY, metadata_json TEXT);
                INSERT INTO editions VALUES ('ed1','{"synthetic":true}'),('ed2','{}');
                CREATE TABLE paragraphs(id TEXT,edition_id TEXT,text TEXT);
                INSERT INTO paragraphs VALUES ('p1','ed1','synthetic private source'),('p1','ed2','other synthetic source');
                CREATE TABLE dialogues(id TEXT PRIMARY KEY,edition_id TEXT,text TEXT);
                INSERT INTO dialogues VALUES ('d1','ed1','synthetic dialogue');
                CREATE TABLE entries(id TEXT PRIMARY KEY,reference_eligible INTEGER);
                INSERT INTO entries VALUES ('d1',0);
                CREATE TABLE scan_pages(id TEXT PRIMARY KEY,image_path TEXT);
                INSERT INTO scan_pages VALUES ('scan1','synthetic/not-opened.png');
                CREATE VIRTUAL TABLE paragraph_fts USING fts5(text);
                INSERT INTO paragraph_fts VALUES ('synthetic searchable source');
            """)
            con.commit()
        self.snapshot = self.root / "dialogue.source-original.sqlite"
        shutil.copyfile(self.db, self.snapshot)
        self.sha = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()
        self.record = {"kind": "identity", "candidate_id": "identity:0123456789abcdef",
                       "source_sha256": self.sha, "artifact_sha256": "a" * 64,
                       "source_payload_sha256": "b" * 64, "edition_id": "ed1", "dialogue_id": "d1",
                       "payload": {"selection_kind": "haruhi_unknown", "speaker": "Synthetic Person", "requires_review": 1,
                                   "accepted": 0, "human_confirmed": 0, "reference_eligible": 0}}
        self.backup = self.root / "backup" / "before.sqlite"
        self.receipt = self.root / "receipt.json"

    def apply(self, records=None, *, batch="batch1", backup=None, receipt=None):
        return archive.apply(self.db, self.snapshot, self.sha, records or [self.record],
                             backup_path=backup or self.backup, receipt_path=receipt or self.receipt,
                             batch_id=batch)

    def state(self):
        with lineage.readonly(self.db) as con:
            return lineage.snapshot(con)

    def sql(self, statement):
        with closing(sqlite3.connect(self.db)) as con:
            con.executescript(statement)
            con.commit()

    def test_first_commit_preserves_all_source_fields_fts_hashes_and_original_evidence(self):
        before = self.state()
        result = self.apply()
        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["rows_inserted"], 1)
        self.assertEqual(hashlib.sha256(self.snapshot.read_bytes()).hexdigest(), self.sha)
        self.assertNotEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), self.sha)
        with lineage.readonly(self.db) as con:
            stored = con.execute("SELECT source_sha256,source_payload_sha256,record_json," + ",".join(archive.FLAGS)
                                 + " FROM " + archive.TABLE).fetchone()
        self.assertEqual(stored[:2], (self.sha, "b" * 64))
        self.assertEqual(json.loads(stored[2])["payload"], self.record["payload"])
        self.assertEqual(stored[3:], tuple(archive.FLAGS.values()))
        after = self.state()
        self.assertEqual({t: after["tables"][t] for t in before["tables"]}, before["tables"])
        report = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.assertNotIn("synthetic private source", json.dumps(report))
        self.assertNotIn("Synthetic Person", json.dumps(report))
        self.assertTrue(report["lineage"]["old_rows_preserved"])

    def test_same_batch_replay_reads_only_no_new_backup_or_outbox(self):
        first = self.apply()
        before = self.state()
        with patch.object(archive.safety, "backup_locked", side_effect=AssertionError("must not back up a replay")):
            second = self.apply()
        self.assertEqual(second["status"], "already_committed")
        self.assertEqual(second["receipt_id"], first["receipt_id"])
        self.assertEqual(before, self.state())

    def test_same_batch_different_payload_is_refused_without_writes(self):
        self.apply()
        before = self.state()
        changed = json.loads(json.dumps(self.record))
        changed["payload"]["speaker"] = "Different Synthetic Person"
        with self.assertRaisesRegex(archive.ArchiveError, "candidate_archive_batch_conflict"):
            self.apply([changed])
        self.assertEqual(before, self.state())

    def test_new_batch_reuses_identical_record_without_updating_it(self):
        self.apply()
        result = self.apply(batch="batch2", backup=self.root / "before2.sqlite", receipt=self.root / "receipt2.json")
        self.assertEqual((result["rows_inserted"], result["rows_reused"]), (0, 1))
        with lineage.readonly(self.db) as con:
            self.assertEqual(con.execute("SELECT import_batch FROM " + archive.TABLE).fetchone()[0], "batch1")

    def test_same_key_conflict_in_new_batch_is_refused_before_backup(self):
        self.apply()
        before = self.state()
        changed = json.loads(json.dumps(self.record))
        changed["payload"]["speaker"] = "Different Synthetic Person"
        with self.assertRaisesRegex(archive.ArchiveError, "candidate_archive_record_conflict"):
            self.apply([changed], batch="batch2", backup=self.root / "before2.sqlite", receipt=self.root / "receipt2.json")
        self.assertEqual(before, self.state())
        self.assertFalse((self.root / "before2.sqlite").exists())

    def test_new_artifact_observation_appends_without_replacing_previous_candidate(self):
        self.apply()
        changed = dict(self.record, artifact_sha256="c" * 64)
        result = self.apply([changed], batch="batch2", backup=self.root / "before2.sqlite", receipt=self.root / "receipt2.json")
        self.assertEqual(result["rows_inserted"], 1)
        with lineage.readonly(self.db) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM " + archive.TABLE).fetchone()[0], 2)

    def test_any_protected_metadata_change_rejects_even_when_text_unchanged(self):
        self.sql("UPDATE editions SET metadata_json='{}' WHERE id='ed1';")
        before = self.state()
        with self.assertRaisesRegex(archive.ArchiveError, "source_lineage_protected_table_changed"):
            self.apply()
        self.assertEqual(before, self.state())
        self.assertFalse(self.backup.exists())

    def test_approval_flags_unknown_keys_bad_source_or_bad_locator_refuse(self):
        original = self.state()
        cases = []
        for flag in archive.FLAGS:
            record = json.loads(json.dumps(self.record))
            record["payload"][flag] = 0 if flag == "requires_review" else 1
            cases.append(record)
        cases += [dict(self.record, source_sha256="f" * 64), dict(self.record, unexpected="no"),
                  dict(self.record, dialogue_id="absent"), dict(self.record, kind=[])]
        for record in cases:
            with self.subTest(case=list(record)):
                with self.assertRaises(archive.ArchiveError):
                    self.apply([record])
                self.assertEqual(original, self.state())

    def test_paragraph_locator_is_edition_scoped(self):
        record = dict(self.record, kind="unquoted", paragraph_id="p1", dialogue_id=None)
        self.assertEqual(self.apply([record])["status"], "committed")

    def test_outbox_trigger_mutation_is_rolled_back(self):
        self.sql("CREATE TABLE candidate_write_receipt_outbox(receipt_id TEXT PRIMARY KEY,operation TEXT NOT NULL,receipt_json TEXT NOT NULL);"
                 "CREATE TRIGGER outbox_bad AFTER INSERT ON candidate_write_receipt_outbox BEGIN UPDATE entries SET reference_eligible=1; END;")
        shutil.copyfile(self.db, self.snapshot)
        self.sha = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()
        self.record["source_sha256"] = self.sha
        before = self.state()
        with self.assertRaisesRegex(archive.ArchiveError, "source_lineage_protected_table_changed"):
            self.apply()
        self.assertEqual(before, self.state())
        self.assertFalse(self.receipt.exists())

    def test_export_failure_is_committed_and_readonly_recovery_works(self):
        with patch.object(archive.safety, "export_receipt", side_effect=PermissionError):
            result = self.apply()
        self.assertEqual(result["status"], "committed_receipt_pending")
        self.assertFalse(self.receipt.exists())
        before = self.state()
        saved = archive.safety.export_receipt(self.db, result["receipt_id"], self.receipt)
        self.assertEqual(saved["receipt_id"], result["receipt_id"])
        self.assertEqual(before, self.state())

    def test_same_source_snapshot_wrong_hash_refuses_before_backup(self):
        self.snapshot.write_bytes(self.snapshot.read_bytes() + b"changed")
        with self.assertRaisesRegex(archive.ArchiveError, "source_snapshot_sha256_mismatch"):
            self.apply()
        self.assertFalse(self.backup.exists())

    def test_completion_quote_identity_uses_explicit_quote_only_target(self):
        self.sql("CREATE TABLE corpus_completion_quote(quote_id TEXT,edition_id TEXT,requires_review INTEGER);"
                 "INSERT INTO corpus_completion_quote VALUES ('q1','ed1',1),('q1','ed2',1);")
        shutil.copyfile(self.db, self.snapshot)
        self.sha = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()
        record = dict(self.record, dialogue_id="q1", source_sha256=self.sha,
                      payload={**self.record["payload"], "selection_kind": "completion_quote"})
        result = self.apply([record])
        self.assertEqual(result["status"], "committed")
        with lineage.readonly(self.db) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM dialogues WHERE id='q1'").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT dialogue_id FROM " + archive.TABLE).fetchone()[0], "q1")
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.assertEqual(receipt["identity_locator_policy"]["completion_quote"], "corpus_completion_quote.quote_id")

    def test_invalid_identity_selection_never_guesses_from_available_table(self):
        for selection in (None, "guess", "completion_quote_missing"):
            with self.subTest(selection=selection):
                record = dict(self.record, payload={**self.record["payload"], "selection_kind": selection})
                before = self.state()
                with self.assertRaisesRegex(archive.ArchiveError, "candidate_archive_identity_selection_invalid"):
                    self.apply([record])
                self.assertEqual(before, self.state())

    def test_historical_archive_deleted_or_modified_before_next_batch_refuses(self):
        self.apply()
        self.sql("UPDATE " + archive.TABLE + " SET created_at_utc='changed';")
        before = self.state()
        next_record = dict(self.record, artifact_sha256="c" * 64)
        with self.assertRaisesRegex(archive.ArchiveError, "candidate_archive_stored_record_invalid"):
            self.apply([next_record], batch="batch2", backup=self.root / "before2.sqlite", receipt=self.root / "receipt2.json")
        self.assertEqual(before, self.state())
        self.assertFalse((self.root / "before2.sqlite").exists())
        self.sql("DELETE FROM " + archive.TABLE + ";")
        before = self.state()
        with self.assertRaisesRegex(archive.ArchiveError, "candidate_archive_history_anchor_mismatch"):
            self.apply([next_record], batch="batch2", backup=self.root / "before2.sqlite", receipt=self.root / "receipt2.json")
        self.assertEqual(before, self.state())

    def edit_receipt(self, edit, *, reseal=False):
        with closing(sqlite3.connect(self.db)) as con:
            receipt_id, raw = con.execute("SELECT receipt_id,receipt_json FROM candidate_write_receipt_outbox WHERE operation=?", (archive.OPERATION,)).fetchone()
            receipt = json.loads(raw)
            edit(receipt)
            if reseal:
                receipt["receipt_sha256"] = archive._receipt_digest(receipt)
            con.execute("UPDATE candidate_write_receipt_outbox SET receipt_json=? WHERE receipt_id=?", (json.dumps(receipt), receipt_id))
            con.commit()

    def test_receipt_count_or_approval_tamper_cannot_be_replayed_and_exported(self):
        self.apply()
        self.edit_receipt(lambda receipt: receipt.update(rows_inserted=99))
        before = self.state()
        target = self.root / "tampered-export.json"
        with self.assertRaisesRegex(archive.ArchiveError, "candidate_archive_receipt_invalid"):
            self.apply(receipt=target)
        self.assertFalse(target.exists())
        self.assertEqual(before, self.state())

    def test_resealed_receipt_counts_still_must_match_independent_manifest(self):
        self.apply()
        self.edit_receipt(lambda receipt: receipt.update(rows_inserted=99), reseal=True)
        with self.assertRaisesRegex(archive.ArchiveError, "candidate_archive_receipt_counts_invalid"):
            self.apply(receipt=self.root / "tampered-export.json")

    def test_resealed_receipt_approval_flags_still_refuse(self):
        self.apply()
        self.edit_receipt(lambda receipt: receipt["approval_flags"].update(accepted=1), reseal=True)
        with self.assertRaisesRegex(archive.ArchiveError, "candidate_archive_receipt_invalid"):
            self.apply(receipt=self.root / "tampered-export.json")


if __name__ == "__main__":
    unittest.main()
