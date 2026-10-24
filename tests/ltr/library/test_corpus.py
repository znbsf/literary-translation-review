"""Private initialization, corrupt legacy data and CLI privacy on synthetic fixtures."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ltr.library.corpus import CorpusError, check_corpus, init_corpus
from ltr.workbench.config import CorpusConfig, WorkbenchConfig
from ltr.workbench.store import WorkbenchStore

REPO = Path(__file__).resolve().parents[3]


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.corpus = self.root / "private-corpus"
        result = init_corpus(self.corpus)
        self.assertEqual(result["status"], "initialized")
        self.db = self.corpus / "dialogue.sqlite"

    def execute(self, sql):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.executescript(sql)

    def populate(self):
        self.execute("""
            INSERT INTO editions(id,title,language,origin_kind) VALUES
                ('source','Synthetic source','ja','source_epub'),
                ('target','Synthetic translation','zh','translation');
            INSERT INTO paragraphs VALUES
                ('source',0,'p0','synthetic source zero','{}'),
                ('source',1,'p1','synthetic source one','{}'),
                ('target',0,'p0','synthetic target zero','{}');
            INSERT INTO dialogues(id,edition_id,ordinal,text,start_para,end_para) VALUES
                ('s0','source',0,'source fragment zero',0,0),
                ('s1','source',1,'source fragment one',1,1),
                ('t0','target',0,'target fragment zero',0,0);
            INSERT INTO alignments(chinese_id,japanese_ids_json,status) VALUES
                ('t0','["s0","s1"]','candidate');
        """)

    def codes(self):
        return {item["code"] for item in check_corpus(self.db)["issues"]}

    def cli(self, *args):
        return subprocess.run([sys.executable, "-X", "utf8", "-B", "scripts/ltr", "corpus", *map(str, args)],
                              cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=15)

    def test_empty_initialized_corpus_and_workbench(self):
        result = check_corpus(self.db)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["schema_version"], "ltr-corpus-v1")
        self.assertEqual(result["warnings"], [])
        self.assertTrue(result["source_unchanged"])
        store = WorkbenchStore(WorkbenchConfig([CorpusConfig("synthetic", "Synthetic", self.corpus)], None))
        self.addCleanup(store.close)
        self.assertEqual(store.editions("synthetic")["items"], [])
        self.assertEqual(store.dialogues({"corpus": "synthetic"})["items"], [])

    def test_one_to_many_alignment_readonly_and_workbench(self):
        self.populate()
        before = self.db.read_bytes()
        result = check_corpus(self.db)
        self.assertEqual(result["status"], "pass", result["issues"])
        self.assertEqual(result["counts"]["dialogues"], 3)
        self.assertEqual(result["source_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(self.db.read_bytes(), before)
        store = WorkbenchStore(WorkbenchConfig([CorpusConfig("synthetic", "Synthetic", self.corpus)], None))
        self.addCleanup(store.close)
        self.assertEqual(len(store.editions("synthetic")["items"]), 2)
        self.assertEqual(len(store.dialogues({"corpus": "synthetic"})["items"]), 3)
        self.assertEqual(self.db.read_bytes(), before)

    def test_refuses_existing_directory_without_changing_contents(self):
        before = self.db.read_bytes()
        with self.assertRaisesRegex(CorpusError, "corpus_already_exists"):
            init_corpus(self.corpus)
        self.assertEqual(self.db.read_bytes(), before)
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(CorpusError, "corpus_already_exists"):
            init_corpus(empty)

    def test_git_file_and_git_directory_are_private_boundaries(self):
        for name, is_file in (("git-file", True), ("git-dir", False)):
            root = self.root / name
            root.mkdir()
            if is_file:
                (root / ".git").write_text("synthetic worktree marker")
            else:
                (root / ".git").mkdir()
            with self.assertRaisesRegex(CorpusError, "private_root_inside_git"):
                init_corpus(root / "nested")
            self.assertFalse((root / "nested").exists())

    def test_failed_initialization_cleans_only_its_new_directory(self):
        target = self.root / "failed-init"
        with patch("ltr.library.corpus.check_corpus", side_effect=CorpusError("synthetic_failure")):
            with self.assertRaisesRegex(CorpusError, "synthetic_failure"):
                init_corpus(target)
        self.assertFalse(target.exists())
        self.assertTrue(self.db.exists())

    def test_unknown_schema_rejected_without_echoing_value(self):
        self.execute("""UPDATE metadata SET value_json='"SYNTHETIC_PRIVATE_SENTINEL"' WHERE key='schema';""")
        report = check_corpus(self.db)
        self.assertIn("schema_version_unsupported", {i["code"] for i in report["issues"]})
        self.assertNotIn("SYNTHETIC_PRIVATE_SENTINEL", json.dumps(report))

    def test_unversioned_legacy_compatible_and_stays_unversioned(self):
        self.populate()
        self.execute("DELETE FROM metadata WHERE key='schema';")
        before = self.db.read_bytes()
        report = check_corpus(self.db)
        self.assertEqual(report["status"], "pass")
        self.assertIsNone(report["schema_version"])
        self.assertEqual(report["warnings"], [{"code": "schema_version_missing"}])
        self.assertEqual(self.db.read_bytes(), before)

    def test_checker_checks_legacy_rows_without_relying_on_constraints(self):
        self.populate()
        self.execute("""
            ALTER TABLE paragraphs RENAME TO prior_paragraphs;
            CREATE TABLE paragraphs AS SELECT * FROM prior_paragraphs;
            INSERT INTO paragraphs SELECT * FROM prior_paragraphs LIMIT 1;
            UPDATE paragraphs SET text=NULL, locator_json='invalid';
            PRAGMA ignore_check_constraints=ON;
            UPDATE dialogues SET start_para=99,end_para=1 WHERE id='t0';
            UPDATE alignments SET japanese_ids_json='["missing","missing",3]';
        """)
        self.assertTrue({"paragraphs_duplicate_id", "paragraphs_duplicate_ordinal",
                         "paragraphs_invalid_text", "paragraphs_invalid_json",
                         "dialogues_invalid_range", "dialogues_missing_paragraph",
                         "alignments_missing_source", "alignments_duplicate_member",
                         "alignments_invalid_member_id"} <= self.codes())

    def test_dangling_editions_and_alignment_target(self):
        self.populate()
        self.execute("""
            UPDATE paragraphs SET edition_id='missing' WHERE edition_id='target';
            UPDATE dialogues SET edition_id='missing' WHERE id='t0';
            UPDATE alignments SET chinese_id='missing';
        """)
        self.assertTrue({"paragraphs_missing_edition", "dialogues_missing_edition",
                         "alignments_missing_target"} <= self.codes())

    def test_invalid_alignment_json_is_reported_without_raising(self):
        self.populate()
        self.execute("""
            PRAGMA ignore_check_constraints=ON;
            UPDATE alignments SET japanese_ids_json='[invalid';
        """)
        self.assertIn("alignments_invalid_members", self.codes())

    def test_missing_tables_and_columns(self):
        self.execute("DROP TABLE alignments; ALTER TABLE editions DROP COLUMN book_key;")
        issues = check_corpus(self.db)["issues"]
        self.assertIn({"code": "missing_table", "table": "alignments"}, issues)
        self.assertIn({"code": "missing_column", "table": "editions", "column": "book_key"}, issues)

    def test_generated_constraints_reject_invalid_range_and_json(self):
        self.populate()
        for sql in ("UPDATE dialogues SET start_para=2,end_para=1",
                    "UPDATE paragraphs SET ordinal=-1",
                    "UPDATE metadata SET value_json='invalid'",
                    "UPDATE alignments SET japanese_ids_json='{}'"):
            with self.subTest(sql=sql), closing(sqlite3.connect(self.db)) as con:
                with self.assertRaises(sqlite3.IntegrityError):
                    con.execute(sql)

    def test_readonly_connection_enforces_no_writes(self):
        from ltr.library.source_lineage import readonly
        with readonly(self.db) as con:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("DELETE FROM metadata")

    def test_live_sidecars_refused(self):
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(self.db) + suffix)
            sidecar.write_bytes(b"synthetic")
            try:
                with self.assertRaisesRegex(CorpusError, "stable_snapshot_required"):
                    check_corpus(self.db)
            finally:
                sidecar.unlink()

    def test_changed_source_is_not_a_pass(self):
        from ltr.library.corpus import _sha256
        real = _sha256(self.db)
        with patch("ltr.library.corpus._sha256", side_effect=[real, "0" * 64]):
            report = check_corpus(self.db)
        self.assertEqual(report["status"], "fail")
        self.assertFalse(report["source_unchanged"])
        self.assertIn({"code": "database_changed_during_check"}, report["issues"])

    def test_cli_dispatch_help_init_check_and_exit_codes(self):
        for args in (("--help",), ("init", "--help"), ("check", "--help")):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
        target = self.root / "cli-created"
        result = self.cli("init", target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "initialized")
        result = self.cli("check", target / "dialogue.sqlite")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "pass")
        self.execute("DROP TABLE alignments;")
        result = self.cli("check", self.db)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "fail")

    def test_cli_refusals_do_not_leak_arguments_or_sqlite_errors(self):
        sentinel = self.root / "SYNTHETIC_PRIVATE_SENTINEL"
        sentinel.write_bytes(b"not a database: SYNTHETIC_PRIVATE_BODY")
        for args in (("check", sentinel), ("check", sentinel / "missing"),
                     ("--SYNTHETIC_PRIVATE_SENTINEL",), ("init", self.corpus)):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("SYNTHETIC_PRIVATE", result.stdout + result.stderr)
            self.assertNotIn(str(self.root), result.stdout + result.stderr)
            self.assertEqual(result.stderr, "")
        self.assertFalse((sentinel / "missing").exists())


if __name__ == "__main__":
    unittest.main()
