"""Synthetic, source-free coverage of immutable snapshot and append-only proofs."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from ltr.library import source_lineage as lineage


class SourceLineageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.formal = self.root / "dialogue.sqlite"
        con = sqlite3.connect(self.formal)
        con.executescript("""
            CREATE TABLE paragraphs(id TEXT PRIMARY KEY, text TEXT, metadata BLOB, rank REAL);
            INSERT INTO paragraphs VALUES ('p1','synthetic source',X'0102',1.25);
            CREATE TABLE candidates(id TEXT, body TEXT);
            INSERT INTO candidates VALUES ('c1','synthetic candidate'),('c1','synthetic candidate');
        """)
        con.commit()
        con.close()
        self.snapshot = self.root / "dialogue.source-original.sqlite"
        shutil.copyfile(self.formal, self.snapshot)
        self.sha = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()

    def compare(self, **kwargs):
        with lineage.readonly(self.snapshot) as before, lineage.readonly(self.formal) as after:
            return lineage.assert_append_only(before, after, **kwargs)

    def mutate(self, sql):
        with closing(sqlite3.connect(self.formal)) as con:
            con.executescript(sql)
            con.commit()

    def test_real_sibling_snapshot_keeps_original_bytes(self):
        self.mutate("CREATE TABLE archive(id TEXT PRIMARY KEY, flags INTEGER); INSERT INTO archive VALUES ('a',0);")
        self.assertEqual(lineage.validate_snapshot_path(self.formal, self.snapshot, self.sha), self.snapshot)
        self.assertNotEqual(hashlib.sha256(self.formal.read_bytes()).hexdigest(), self.sha)
        report = self.compare(added_tables={"archive"})
        self.assertTrue(report["old_rows_preserved"])
        self.assertEqual(report["added_tables"]["archive"]["rows"], 1)
        self.assertNotIn("synthetic source", json.dumps(report))

    def test_reject_same_database_wrong_sha_other_parent_missing_and_directory(self):
        outside = self.root / "outside"
        outside.mkdir()
        other = outside / "snapshot.sqlite"
        shutil.copyfile(self.snapshot, other)
        for path, sha in ((self.formal, self.sha), (self.snapshot, "0" * 64),
                          (other, self.sha), (self.root / "absent.sqlite", self.sha),
                          (self.root, self.sha), (self.snapshot, "BAD")):
            with self.subTest(path=path.name, sha=sha):
                with self.assertRaises(lineage.LineageError):
                    lineage.validate_snapshot_path(self.formal, path, sha)

    def test_reject_journal_and_hardlink(self):
        for suffix in ("-wal", "-shm", "-journal"):
            journal = Path(str(self.snapshot) + suffix)
            journal.touch()
            with self.subTest(suffix=suffix):
                with self.assertRaisesRegex(lineage.LineageError, "source_snapshot_unstable"):
                    lineage.validate_snapshot_path(self.formal, self.snapshot, self.sha)
            journal.unlink()
        link = self.root / "hardlink.sqlite"
        try:
            link.hardlink_to(self.snapshot)
        except OSError:
            self.skipTest("hardlinks unavailable on fixture filesystem")
        with self.assertRaisesRegex(lineage.LineageError, "source_snapshot_path_unsafe"):
            lineage.validate_snapshot_path(self.formal, link, self.sha)

    def test_snapshot_readonly_never_creates_missing_db_and_refuses_writes(self):
        with lineage.readonly(self.snapshot) as con:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("DELETE FROM paragraphs")
        missing = self.root / "missing.sqlite"
        with self.assertRaises((FileNotFoundError, sqlite3.Error)):
            with lineage.readonly(missing):
                pass
        self.assertFalse(missing.exists())

    def test_any_protected_field_change_fails(self):
        self.mutate("UPDATE paragraphs SET metadata=X'0103' WHERE id='p1';")
        with self.assertRaisesRegex(lineage.LineageError, "source_lineage_protected_table_changed"):
            self.compare()

    def test_append_preserves_duplicate_multiplicity_and_all_values(self):
        self.mutate("INSERT INTO candidates VALUES ('c2','new synthetic observation');")
        report = self.compare(append_tables={"candidates"})
        self.assertEqual(report["appended_rows"], {"candidates": 1})
        self.mutate("DELETE FROM candidates WHERE rowid=(SELECT MIN(rowid) FROM candidates WHERE id='c1');")
        with self.assertRaisesRegex(lineage.LineageError, "source_lineage_old_rows_changed"):
            self.compare(append_tables={"candidates"})

    def test_schema_change_is_not_hidden_by_unchanged_rows(self):
        self.mutate("CREATE INDEX new_index ON paragraphs(text);")
        with self.assertRaisesRegex(lineage.LineageError, "source_lineage_unexpected_schema_object"):
            self.compare()
        self.assertTrue(self.compare(added_schema_objects={("index", "new_index", "paragraphs")})["old_rows_preserved"])

    def test_new_tables_need_explicit_allowlist(self):
        self.mutate("CREATE TABLE archive(id TEXT PRIMARY KEY);")
        with self.assertRaisesRegex(lineage.LineageError, "source_lineage_unexpected_table"):
            self.compare()
        self.assertTrue(self.compare(added_tables={"archive"})["old_rows_preserved"])

    def test_old_schema_cannot_be_replaced_even_with_same_rows(self):
        self.mutate("ALTER TABLE candidates ADD COLUMN note TEXT;")
        with self.assertRaisesRegex(lineage.LineageError, "source_lineage_schema_changed"):
            self.compare(append_tables={"candidates"})

    def seed_fts(self):
        self.mutate("CREATE VIRTUAL TABLE docs USING fts5(body); INSERT INTO docs VALUES ('synthetic searchable text');")
        shutil.copyfile(self.formal, self.snapshot)

    def test_fts_noop_is_stable_between_two_readonly_connections(self):
        self.seed_fts()
        with lineage.readonly(self.snapshot) as before, lineage.readonly(self.formal) as after:
            self.assertEqual(lineage.snapshot(before), lineage.snapshot(after))
            report = lineage.assert_append_only(before, after)
        self.assertIn("docs_data", report["protected_tables"])
        self.assertIn("docs_content", report["protected_tables"])

    def test_fts_persistent_content_change_is_rejected(self):
        self.seed_fts()
        self.mutate("UPDATE docs SET body='changed synthetic content' WHERE rowid=1;")
        with self.assertRaisesRegex(lineage.LineageError, "source_lineage_protected_table_changed"):
            self.compare()

    def test_fts_shadow_content_change_is_rejected(self):
        self.seed_fts()
        self.mutate("UPDATE docs_content SET c0='changed synthetic shadow content' WHERE id=1;")
        with self.assertRaisesRegex(lineage.LineageError, "source_lineage_protected_table_changed"):
            self.compare()


if __name__ == "__main__":
    unittest.main()
