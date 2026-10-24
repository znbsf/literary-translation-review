"""Synthetic source-snapshot/current-DB routing and request-boundary tests.

All databases, candidate artifacts, images and labels are temporary fixtures.
Import the production backend normally; no patch application or local paths.
"""
from contextlib import closing
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ltr import workbench as wb
from ltr.library import publisher_identifiers, run_snapshot, source_lineage
from ltr.ocr import reading_order_index, reading_units
from ltr.quotes import index as ci
from tests.ltr.workbench.test_store import FULL_SCHEMA, seed_full


class EvidenceWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.corpus_root = self.root / "source"
        self.corpus_root.mkdir()
        self.formal = self.corpus_root / "dialogue.sqlite"
        with closing(sqlite3.connect(self.formal)) as con:
            con.executescript(FULL_SCHEMA)
            seed_full(con)
            con.commit()
        self.original_sha = hashlib.sha256(self.formal.read_bytes()).hexdigest()
        self.snapshot = self.corpus_root / ("dialogue.source-" + self.original_sha + ".sqlite")
        shutil.copyfile(self.formal, self.snapshot)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.index = self.root / "candidates.sqlite"
        self.row = {"paragraph_id": "par1", "ordinal": 1, "start": 0, "end": 9,
                    "text": "paragraph", "speaker": None, "addressee": None,
                    "kind": "uncertain", "paragraph_sha256": hashlib.sha256(b"paragraph 1").hexdigest(),
                    **ci.FLAGS}
        self.batch = {"schema": "unquoted-dialogue-candidates-v1", "database_sha256": self.original_sha,
                      "edition_id": "ed1", "model": "synthetic-model", "candidates": [self.row]}
        self.batch_path = self.inputs / "batch-synthetic.json"
        self.batch_path.write_text(json.dumps(self.batch), encoding="utf-8")
        ci.build(self.snapshot, self.inputs, self.index)
        self.candidate_id = "candidate:" + ci.candidate_id("ed1", self.row, self.original_sha)
        self.store = self.make_store()
        self.addCleanup(self.store.close)

    def make_store(self, **kwargs):
        config = wb.CorpusConfig("sample", "Synthetic", self.corpus_root, candidate_index=self.index,
                                source_snapshot_db=self.snapshot, source_snapshot_sha256=self.original_sha,
                                **kwargs)
        return wb.WorkbenchStore(wb.WorkbenchConfig([config], self.root / "labels"))

    def append_formal(self):
        with closing(sqlite3.connect(self.formal)) as con:
            con.execute("CREATE TABLE formal_archive(id TEXT PRIMARY KEY, requires_review INTEGER, accepted INTEGER)")
            con.execute("INSERT INTO formal_archive VALUES ('synthetic-candidate',1,0)")
            con.commit()

    def test_current_and_evidence_use_two_real_connections_and_old_overlay_survives(self):
        artifact_before = self.batch_path.read_bytes()
        identity_before = self.store.dialogues({"corpus": "sample", "source": "candidates"})["items"][0]["id"]
        self.append_formal()
        corpus, current = self.store.database("sample")
        evidence, old = self.store.evidence_database("sample")
        self.assertNotEqual(current.path, old.path)
        self.assertIsNot(current.connect(), old.connect())
        self.assertNotEqual(current.source_sha256(), self.original_sha)
        self.assertEqual(old.source_sha256(), self.original_sha)
        self.assertEqual(current.scalar("SELECT count(*) FROM formal_archive"), 1)
        self.assertFalse(old.has("formal_archive"))
        self.assertEqual(corpus.root, evidence.root)
        self.assertEqual(corpus.scan_root, evidence.scan_root)
        self.assertEqual(evidence.db_path.parent, corpus.db_path.parent)
        item = self.store.dialogues({"corpus": "sample", "source": "candidates"})["items"][0]
        self.assertEqual(item["id"], identity_before)
        self.assertEqual(self.batch_path.read_bytes(), artifact_before)
        overview = self.store.overview()["corpora"][0]
        self.assertEqual(overview["counts"]["dialogues"], 5)
        self.assertEqual(overview["pending_candidates"]["total"], 1)
        self.assertTrue(overview["capabilities"]["candidates"])

    def test_overlay_reviews_use_old_fingerprint_formal_reviews_still_stale(self):
        old_fingerprint = self.store.evidence_database("sample")[1].fingerprint()
        self.store.save_review({"corpus": "sample", "dialogue_id": "d1", "verdict": "uncertain"})
        self.store.save_review({"corpus": "sample", "source": "candidates",
                               "dialogue_id": self.candidate_id, "verdict": "uncertain"})
        self.append_formal()
        exported = {r["dialogue_id"]: r for r in self.store.review_export({"corpus": "sample"})["reviews"]}
        self.assertFalse(exported[self.candidate_id]["stale"])
        self.assertTrue(exported["d1"]["stale"])
        result = self.store.save_review({"corpus": "sample", "source": "candidates",
                                        "dialogue_id": self.candidate_id, "verdict": "correct"})
        self.assertEqual(result["review"]["corpus_fingerprint"], old_fingerprint)
        raw_review = self.store.reviews.get("sample", self.candidate_id)
        for flag in ("accepted", "human_confirmed", "semantic_verified", "reference_eligible"):
            self.assertFalse(raw_review[flag])
        exported = {r["dialogue_id"]: r for r in self.store.review_export({"corpus": "sample"})["reviews"]}
        self.assertFalse(exported[self.candidate_id]["stale"])

    def test_config_pair_explicit_path_and_invalid_snapshots(self):
        config_path = self.root / "fixture-config.json"
        def load(**fields):
            config_path.write_text(json.dumps({"corpora": [{"id": "sample", "path": str(self.corpus_root), **fields}]}), encoding="utf-8")
            return wb.WorkbenchConfig.load(config_path)
        config = load(source_snapshot_db="source/" + self.snapshot.name, source_snapshot_sha256=self.original_sha)
        self.assertEqual(config.corpus("sample").source_snapshot_db, self.snapshot)
        for fields in ({"source_snapshot_db": str(self.snapshot)},
                       {"source_snapshot_sha256": self.original_sha},
                       {"source_snapshot_db": str(self.formal), "source_snapshot_sha256": self.original_sha},
                       {"source_snapshot_db": str(self.snapshot), "source_snapshot_sha256": "0" * 64},
                       {"source_snapshot_db": None, "source_snapshot_sha256": None}):
            with self.subTest(fields=list(fields)):
                with self.assertRaises(wb.WorkbenchError):
                    load(**fields)

    def test_same_size_mtime_snapshot_replacement_is_rejected_per_request(self):
        self.store.dialogues({"corpus": "sample", "source": "candidates"})
        info = self.snapshot.stat()
        with closing(sqlite3.connect(self.snapshot)) as con:
            con.execute("UPDATE editions SET title='Title Two' WHERE id='ed1'")
            con.commit()
        self.assertEqual(self.snapshot.stat().st_size, info.st_size)
        os.utime(self.snapshot, ns=(info.st_atime_ns, info.st_mtime_ns))
        with self.assertRaisesRegex(wb.WorkbenchError, "source_snapshot_(sha256_mismatch|changed)"):
            self.store.dialogues({"corpus": "sample", "source": "candidates"})
        self.assertEqual(len(self.store.dialogues({"corpus": "sample"})["items"]), 5)

    def test_before_after_guard_detects_change_during_overlay_operation(self):
        from ltr.quotes.overlay import CandidateOverlay
        def changed(overlay):
            with closing(sqlite3.connect(self.snapshot)) as con:
                con.execute("UPDATE editions SET title='Title Two' WHERE id='ed1'")
                con.commit()
            return {"available": True}
        with patch.object(CandidateOverlay, "overview", changed):
            with self.assertRaisesRegex(wb.WorkbenchError, "source_snapshot_(sha256_mismatch|changed)"):
                self.store.overlay("sample").overview()

    def test_full_sha_work_is_constant_across_many_queries_and_fingerprint_refreshes(self):
        database = self.store.evidence_database("sample")[1]
        database.source_sha256()  # Warm only the real baseline fingerprint.
        actual = source_lineage.validate_snapshot_path
        for queries in (1, 100):
            with self.subTest(queries=queries), patch.object(source_lineage, "validate_snapshot_path", wraps=actual) as verify:
                with database.verified_read():
                    for _ in range(queries):
                        self.assertEqual(database.scalar("SELECT COUNT(*) FROM dialogues"), 5)
                        self.assertEqual(database.source_sha256(refresh=True), self.original_sha)
                self.assertEqual(verify.call_count, 2)

    def test_full_sha_work_is_constant_for_whole_overview_and_review_export(self):
        self.store.overview()
        actual = source_lineage.validate_snapshot_path
        with patch.object(source_lineage, "validate_snapshot_path", wraps=actual) as verify:
            marker = self.store.overview()["corpora"][0]["evidence_source"]
            self.assertEqual(marker, {"available": True, "binding": "source_snapshot", "source_sha256": self.original_sha})
            self.assertEqual(verify.call_count, 2)
        self.store.save_review({"corpus": "sample", "source": "candidates", "dialogue_id": self.candidate_id, "verdict": "uncertain"})
        with patch.object(source_lineage, "validate_snapshot_path", wraps=actual) as verify:
            self.assertFalse(self.store.review_export({"corpus": "sample"})["reviews"][0]["stale"])
            self.assertEqual(verify.call_count, 2)

    def test_same_token_change_is_hash_rejected_and_instance_stays_invalid_after_restore(self):
        database = self.store.evidence_database("sample")[1]
        database.source_sha256()
        original = self.snapshot.read_bytes()
        token = database._token()
        with patch.object(database, "_token", return_value=token):
            with self.assertRaisesRegex(wb.WorkbenchError, "source_snapshot_sha256_mismatch"):
                with database.verified_read():
                    with closing(sqlite3.connect(self.snapshot)) as con:
                        con.execute("UPDATE editions SET title='Title Two' WHERE id='ed1'")
                        con.commit()
                    self.assertEqual(database.scalar("SELECT COUNT(*) FROM editions"), 1)
        self.snapshot.write_bytes(original)
        with self.assertRaisesRegex(wb.WorkbenchError, "source_snapshot_sha256_mismatch"):
            database.require()

    def test_ocr_details_and_images_route_to_matching_evidence_corpus(self):
        self.append_formal()
        def check(corpus, db, *args, **kwargs):
            self.assertEqual(corpus.db_path, self.snapshot)
            self.assertEqual(db.path, self.snapshot)
            self.assertEqual(corpus.db_path.parent, self.corpus_root)
            self.assertEqual(corpus.scan_root, self.corpus_root / "private" / "scans")
            self.assertEqual(db.source_sha256(), self.original_sha)
            return {"available": True}
        with patch.object(reading_order_index, "list_candidates", side_effect=check), \
             patch.object(reading_order_index, "read_candidate", side_effect=check), \
             patch.object(reading_units, "read_reading_units", side_effect=check):
            self.assertTrue(self.store.ocr_reading_order_candidates({"corpus": "sample"})["available"])
            self.assertTrue(self.store.ocr_reading_order_candidate({"corpus": "sample", "id": "p1"})["available"])
            self.assertTrue(self.store.ocr_reading_units({"corpus": "sample", "id": "p1"})["available"])

    def test_publisher_identifiers_and_run_snapshot_keep_real_source_path(self):
        self.append_formal()
        corpus = self.store.config.corpus("sample")
        corpus.publisher_identifiers = self.root / "synthetic-identifiers.json"
        corpus.bibliography = self.root / "synthetic-bibliography.json"
        corpus.run_snapshot = self.root / "synthetic-run.json"
        with patch.object(publisher_identifiers, "overview", return_value={"available": True}) as identifiers, \
             patch.object(run_snapshot, "read_run_snapshot", return_value={"available": True}) as run:
            self.assertTrue(self.store._publisher_identifiers(corpus)["available"])
            self.assertEqual(identifiers.call_args.args[1], self.snapshot)
            view = self.store._evidence_overview(corpus)
            self.assertTrue(view["run_snapshot"]["available"])
            self.assertEqual(run.call_args.args[1], self.original_sha)

    def test_no_snapshot_keeps_existing_formal_connection_and_capabilities(self):
        store = wb.WorkbenchStore(wb.WorkbenchConfig([wb.CorpusConfig("legacy", "Legacy", self.corpus_root)], None))
        self.addCleanup(store.close)
        self.assertIs(store.database("legacy")[1], store.evidence_database("legacy")[1])
        self.assertFalse(store.overview()["corpora"][0]["capabilities"]["candidates"])

    def source_store(self, db_path, **fields):
        sha = hashlib.sha256(db_path.read_bytes()).hexdigest()
        snapshot = db_path.with_name("dialogue.source-" + sha + ".sqlite")
        shutil.copyfile(db_path, snapshot)
        with closing(sqlite3.connect(db_path)) as con:
            con.execute("CREATE TABLE formal_archive(id TEXT PRIMARY KEY, accepted INTEGER)")
            con.execute("INSERT INTO formal_archive VALUES ('synthetic-archive-row',0)")
            con.commit()
        store = wb.WorkbenchStore(wb.WorkbenchConfig([
            wb.CorpusConfig("fixture", "Synthetic", db_path.parent, source_snapshot_db=snapshot,
                            source_snapshot_sha256=sha, **fields)], db_path.parent.parent / "labels"))
        self.addCleanup(store.close)
        return store

    def test_actual_alignment_index_coverage_detail_and_review_survive_formal_append(self):
        fixtures = importlib.import_module("tests.ltr.alignment.test_index")
        case = fixtures.AlignmentIndexTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        fixtures.ai.build(case.db, case.inputs, case.index)
        store = self.source_store(case.db, alignment_index=case.index)
        overview = store.overview()["corpora"][0]
        self.assertTrue(overview["pending_alignments"]["available"])
        self.assertTrue(overview["pending_alignments"]["source_coverage"]["available"])
        item = store.dialogues({"corpus": "fixture", "source": "alignments"})["items"][0]
        detail = store.dialogue({"corpus": "fixture", "source": "alignments", "id": item["id"]})
        self.assertEqual(detail["alignment_candidate"]["japanese"][0]["text"], "Synthetic Japanese quote")
        store.save_review({"corpus": "fixture", "source": "alignments", "dialogue_id": item["id"], "verdict": "uncertain"})
        self.assertFalse(store.review_export({"corpus": "fixture"})["reviews"][0]["stale"])

    def test_actual_identity_index_vocabulary_target_detail_survive_formal_append(self):
        fixtures = importlib.import_module("tests.ltr.attribution.test_index")
        case = fixtures.AttributionIndexTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.write("suggestion-000.json", [case.suggested()])
        fixtures.ai.build(case.db, case.inputs, case.index)
        store = self.source_store(case.db, attribution_index=case.index)
        overview = store.overview()["corpora"][0]
        self.assertTrue(overview["pending_identities"]["available"])
        item = store.dialogues({"corpus": "fixture", "source": "identities"})["items"][0]
        detail = store.dialogue({"corpus": "fixture", "source": "identities", "id": item["id"]})
        self.assertEqual(detail["item"]["speaker"], "Alice")
        store.save_review({"corpus": "fixture", "source": "identities", "dialogue_id": item["id"], "verdict": "uncertain"})
        self.assertFalse(store.review_export({"corpus": "fixture"})["reviews"][0]["stale"])


if __name__ == "__main__":
    unittest.main()
