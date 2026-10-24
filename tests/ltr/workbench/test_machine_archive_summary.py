"""Synthetic metadata-only archive status and current-formal Workbench tests."""
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
from ltr import workbench as wb
from tests.ltr.workbench.test_store import FULL_SCHEMA, seed_full


PAYLOAD_SENTINEL = "SYNTHETIC_PRIVATE_PAYLOAD_MUST_NOT_BE_READ"


def seed_archive(con, kinds):
    con.execute(archive.DDL)
    for number, kind in enumerate(kinds):
        row = {column: "synthetic" for column in archive.ALL_COLUMNS}
        row.update(record_key=str(number), kind=kind, candidate_id="candidate-" + str(number),
                   source_sha256="a" * 64, artifact_sha256="b" * 64,
                   source_payload_sha256="c" * 64, normalized_payload_sha256="d" * 64,
                   record_sha256="e" * 64, record_json=PAYLOAD_SENTINEL,
                   import_batch="synthetic", created_at_utc="2026-01-01T00:00:00+00:00", **archive.FLAGS)
        con.execute("INSERT INTO " + archive.TABLE + " (" + ",".join(archive.ALL_COLUMNS) + ") VALUES ("
                    + ",".join("?" for _ in archive.ALL_COLUMNS) + ")",
                    tuple(row[column] for column in archive.ALL_COLUMNS))
    con.commit()


class MachineArchiveSummaryTests(unittest.TestCase):
    def connection(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        return con

    def test_missing_table_is_unavailable_without_a_fake_zero(self):
        self.assertEqual(archive.summary(self.connection()), {"available": False})

    def test_present_empty_archive_is_a_measured_zero(self):
        con = self.connection()
        seed_archive(con, [])
        result = archive.summary(con)
        self.assertTrue(result["available"])
        self.assertEqual((result["total"], result["by_kind"]), (0, {}))

    def test_summary_counts_kinds_and_flags_without_reading_any_payload_column(self):
        con = self.connection()
        seed_archive(con, ["identity", "identity", "alignment", "ocr_region"])
        changes = con.total_changes
        allowed = {"kind", "", *archive.FLAGS}
        def authorizer(action, table, column, database, trigger):
            if action == sqlite3.SQLITE_READ and table == archive.TABLE and column not in allowed:
                return sqlite3.SQLITE_DENY
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        con.set_authorizer(authorizer)
        result = archive.summary(con)
        self.assertEqual(result, {"available": True, "total": 4,
            "by_kind": {"alignment": 1, "identity": 2, "ocr_region": 1},
            "requires_review_all": True, "approval_gates_all_zero": True,
            "status": "machine_candidates_pending_review"})
        self.assertEqual(con.total_changes, changes)
        self.assertNotIn(PAYLOAD_SENTINEL, json.dumps(result))

    def test_wrong_schema_or_view_refuses_instead_of_reporting_zero(self):
        for ddl in ("CREATE TABLE corpus_candidate_archive(kind TEXT)",
                    "CREATE VIEW corpus_candidate_archive AS SELECT 'identity' AS kind"):
            with self.subTest(ddl=ddl):
                con = self.connection()
                con.execute(ddl)
                with self.assertRaisesRegex(archive.ArchiveError, "candidate_archive_summary_invalid"):
                    archive.summary(con)

    def test_each_approval_or_review_flag_violation_refuses(self):
        for flag, expected in archive.FLAGS.items():
            with self.subTest(flag=flag):
                con = self.connection()
                seed_archive(con, ["identity"])
                con.execute("PRAGMA ignore_check_constraints=ON")
                con.execute("UPDATE " + archive.TABLE + " SET " + flag + "=?", (0 if expected else 1,))
                with self.assertRaisesRegex(archive.ArchiveError, "candidate_archive_summary_invalid"):
                    archive.summary(con)

    def test_unknown_kind_or_noninteger_flags_refuse(self):
        for column, value in (("kind", "unknown-kind"), ("accepted", "not-an-integer")):
            with self.subTest(column=column):
                con = self.connection()
                seed_archive(con, ["identity"])
                con.execute("PRAGMA ignore_check_constraints=ON")
                con.execute("UPDATE " + archive.TABLE + " SET " + column + "=?", (value,))
                with self.assertRaisesRegex(archive.ArchiveError, "candidate_archive_summary_invalid"):
                    archive.summary(con)


class MachineArchiveWorkbenchTests(unittest.TestCase):
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
        self.snapshot = self.corpus_root / "dialogue.source-synthetic.sqlite"
        shutil.copyfile(self.formal, self.snapshot)
        self.sha = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()
        self.config = wb.CorpusConfig("synthetic", "Synthetic", self.corpus_root,
                                     source_snapshot_db=self.snapshot, source_snapshot_sha256=self.sha)
        self.store = wb.WorkbenchStore(wb.WorkbenchConfig([self.config], None))
        self.addCleanup(self.store.close)

    def test_overview_reads_archive_from_current_formal_connection(self):
        with closing(sqlite3.connect(self.formal)) as con:
            seed_archive(con, ["identity", "alignment", "ocr_region"])
        current = self.store.database("synthetic")[1]
        evidence = self.store.evidence_database("synthetic")[1]
        self.assertFalse(evidence.has(archive.TABLE))
        summary = archive.summary
        with patch.object(archive, "summary", wraps=summary) as called:
            item = self.store.overview()["corpora"][0]
        self.assertIs(called.call_args.args[0], current.connect())
        self.assertEqual(item["machine_archive"]["total"], 3)
        self.assertEqual(item["counts"]["dialogues"], 5)
        self.assertEqual(item["evidence_source"]["source_sha256"], self.sha)
        self.assertNotIn(PAYLOAD_SENTINEL, json.dumps(item))

    def test_bad_archive_degrades_only_archive_without_fake_counts(self):
        with closing(sqlite3.connect(self.formal)) as con:
            con.execute("CREATE TABLE corpus_candidate_archive(kind TEXT)")
            con.commit()
        item = self.store.overview()["corpora"][0]
        self.assertTrue(item["available"])
        self.assertEqual(item["counts"]["dialogues"], 5)
        self.assertEqual(item["machine_archive"], {"available": False, "error": "candidate_archive_summary_invalid"})

    def test_missing_database_cannot_claim_an_archive_zero(self):
        self.store.close()
        missing = wb.CorpusConfig("missing", "Synthetic missing", self.root / "absent")
        store = wb.WorkbenchStore(wb.WorkbenchConfig([missing], None))
        self.addCleanup(store.close)
        item = store.overview()["corpora"][0]
        self.assertEqual(item["machine_archive"], {"available": False, "error": "database_unavailable"})


if __name__ == "__main__":
    unittest.main()
