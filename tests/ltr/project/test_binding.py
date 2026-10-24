"""Synthetic end-to-end corpus binding, migration and publication fault recovery."""
import copy
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

from ltr.library.corpus import check_corpus
from ltr.project import binding
from ltr.project.demo import demo
from ltr.project.epub import import_items
from ltr.project.storage import StorageError, apply_edits, read_project, write_json, digest
from ltr.workbench.config import CorpusConfig, WorkbenchConfig
from ltr.workbench.store import WorkbenchStore

REPO = Path(__file__).resolve().parents[3]


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.legacy = self.root / "legacy"
        self.project = demo(self.legacy)
        self.imported = import_items(self.legacy / "synthetic.epub", ["scene"])

    def edits(self, project):
        return {"book_hash": project["book_hash"], "source_digest": project["source_digest"],
                "base_revision": project["revision"],
                "units": [{"id": u["id"], "target": "synthetic revision", "status": "accepted"}
                          for u in project["units"]]}

    def bound(self, name="bound"):
        root = self.root / name
        project = binding.create_bound_project(root, self.imported, root / "corpus")
        return root, project

    def cli(self, *args):
        return subprocess.run([sys.executable, "-X", "utf8", "-B", "scripts/ltr", "project", *map(str, args)],
                              cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=20)

    def test_import_has_one_source_floor_and_workbench_dialogue(self):
        root, project = self.bound()
        disk = json.loads((root / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(disk["format"], binding.FORMAT)
        self.assertNotIn("source", disk)
        self.assertEqual(project["source"], self.project["source"])
        self.assertEqual(read_project(root)["source_digest"], self.project["source_digest"])
        self.assertEqual(check_corpus(root / "corpus/dialogue.sqlite")["status"], "pass")
        store = WorkbenchStore(WorkbenchConfig([CorpusConfig("sample", "Synthetic", root / "corpus")], None))
        self.addCleanup(store.close)
        self.assertEqual(len(store.editions("sample")["items"]), 1)
        rows = store.dialogues({"corpus": "sample"})["items"]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["reference_eligible"])
        self.assertEqual(rows[0]["attribution_status"], "unresolved")
        self.assertFalse((root / binding.PENDING).exists())

    def test_migration_preserves_targets_revisions_and_all_history_bytes(self):
        project = apply_edits(self.legacy, self.edits(self.project))
        project = apply_edits(self.legacy, self.edits(project))
        history = {p.name: p.read_bytes() for p in (self.legacy / "history").iterdir()}
        original = (self.legacy / "project.json").read_bytes()
        migrated = binding.migrate_project(self.legacy, self.legacy / "corpus")
        for key in ("units", "revision", "source", "source_digest", "source_snapshot_digest", "context", "issues"):
            self.assertEqual(migrated[key], project[key], key)
        self.assertEqual(history, {p.name: p.read_bytes() for p in (self.legacy / "history").iterdir()})
        backup = self.legacy / "migration" / (hashlib.sha256(original).hexdigest() + ".json")
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(read_project(self.legacy), migrated)
        with self.assertRaisesRegex(StorageError, "project_already_bound"):
            binding.migrate_project(self.legacy, self.legacy / "another")

    def test_bound_edits_and_new_history_do_not_reintroduce_source_copy(self):
        root, project = self.bound()
        changed = apply_edits(root, self.edits(project))
        self.assertEqual(changed["revision"], 2)
        self.assertTrue(all(u["target"] == "synthetic revision" for u in read_project(root)["units"]))
        for path in [root / "project.json", root / "history/revision-1.json"]:
            self.assertNotIn("source", json.loads(path.read_text(encoding="utf-8")))

    def test_binding_existing_corpus_is_exact_and_readonly(self):
        owner, project = self.bound()
        db = owner / "corpus/dialogue.sqlite"
        before = db.read_bytes()
        result = binding.bind_project(self.legacy, db, project["corpus_binding"]["edition_id"])
        self.assertEqual(result["source"], self.project["source"])
        self.assertEqual(db.read_bytes(), before)
        self.assertNotIn("source", json.loads((self.legacy / "project.json").read_text(encoding="utf-8")))
        self.assertEqual(binding.recover_project(self.legacy), result)

    def test_binding_mismatch_does_not_change_project_or_database(self):
        owner, project = self.bound()
        db = owner / "corpus/dialogue.sqlite"
        before_db, before_project = db.read_bytes(), (self.legacy / "project.json").read_bytes()
        with self.assertRaisesRegex(StorageError, "bound_paragraph_missing"):
            binding.bind_project(self.legacy, db, "nonexistent")
        self.assertEqual(db.read_bytes(), before_db)
        self.assertEqual((self.legacy / "project.json").read_bytes(), before_project)

    def test_existing_corpus_is_never_overwritten_by_import_or_migrate(self):
        before = (self.legacy / "project.json").read_bytes()
        target = self.legacy / "corpus"
        target.mkdir()
        sentinel = target / "dialogue.sqlite"
        sentinel.write_bytes(b"synthetic protected database")
        with self.assertRaisesRegex(StorageError, "corpus_already_exists"):
            binding.migrate_project(self.legacy, target)
        self.assertEqual(sentinel.read_bytes(), b"synthetic protected database")
        self.assertEqual((self.legacy / "project.json").read_bytes(), before)
        with self.assertRaisesRegex(StorageError, "project_already_exists"):
            binding.create_bound_project(self.legacy, self.imported, self.legacy / "new-corpus")

    def test_new_corpus_must_be_a_direct_child_of_private_project(self):
        for target in (self.root / "external", self.legacy / "history", self.legacy / "nested/corpus"):
            with self.subTest(target=target), self.assertRaises(StorageError):
                binding.migrate_project(self.legacy, target)
        self.assertEqual(read_project(self.legacy)["format"], "translation-project-v1")

    def test_build_failure_retains_old_project_and_can_recover(self):
        with patch.object(binding, "_make_corpus", side_effect=RuntimeError("synthetic fault")):
            with self.assertRaises(RuntimeError):
                binding.migrate_project(self.legacy, self.legacy / "corpus")
        self.assertEqual(read_project(self.legacy)["format"], "translation-project-v1")
        with self.assertRaisesRegex(StorageError, "project_recovery_required"):
            apply_edits(self.legacy, self.edits(self.project))
        recovered = binding.recover_project(self.legacy)
        self.assertEqual(recovered["source"], self.project["source"])
        self.assertEqual(recovered["format"], binding.FORMAT)
        self.assertEqual(binding.recover_project(self.legacy), recovered)

    def test_interrupted_database_build_does_not_overwrite_its_partial_files(self):
        def fail(root, project, target):
            target.mkdir()
            (target / "partial.bin").write_bytes(b"synthetic interrupted data")
            raise RuntimeError("synthetic fault")
        with patch.object(binding, "_make_corpus", side_effect=fail):
            with self.assertRaises(RuntimeError):
                binding.migrate_project(self.legacy, self.legacy / "corpus")
        partial = next(self.legacy.glob(".corpus-building-*/partial.bin"))
        result = binding.recover_project(self.legacy)
        self.assertEqual(result["format"], binding.FORMAT)
        self.assertEqual(partial.read_bytes(), b"synthetic interrupted data")
        receipt = json.loads((self.legacy / binding.RECEIPT).read_text())
        self.assertIn(partial.parent.name, receipt["retained_staging"])

    def test_recovery_after_database_rename_before_ready_marker(self):
        with patch.object(binding, "_bind_value", side_effect=RuntimeError("synthetic fault")):
            with self.assertRaises(RuntimeError):
                binding.migrate_project(self.legacy, self.legacy / "corpus")
        db = self.legacy / "corpus/dialogue.sqlite"
        before = db.read_bytes()
        result = binding.recover_project(self.legacy)
        self.assertEqual(result["format"], binding.FORMAT)
        self.assertEqual(db.read_bytes(), before)

    def test_recovery_after_commit_before_receipt(self):
        original = binding.write_json
        def fail_receipt(path, value):
            if path.name == binding.RECEIPT:
                raise RuntimeError("synthetic fault")
            return original(path, value)
        with patch.object(binding, "write_json", side_effect=fail_receipt):
            with self.assertRaises(RuntimeError):
                binding.migrate_project(self.legacy, self.legacy / "corpus")
        self.assertEqual(read_project(self.legacy)["format"], binding.FORMAT)
        result = binding.recover_project(self.legacy)
        self.assertEqual(result["revision"], self.project["revision"])
        self.assertFalse((self.legacy / binding.PENDING).exists())

    def test_manual_project_change_during_pending_publication_is_a_conflict(self):
        with patch.object(binding, "_make_corpus", side_effect=RuntimeError("synthetic fault")):
            with self.assertRaises(RuntimeError):
                binding.migrate_project(self.legacy, self.legacy / "corpus")
        modified = copy.deepcopy(self.project)
        modified["revision"] += 1
        write_json(self.legacy / "project.json", modified)
        with self.assertRaisesRegex(StorageError, "project_publication_conflict"):
            binding.recover_project(self.legacy)
        self.assertEqual(read_project(self.legacy)["revision"], modified["revision"])

    def test_immutable_database_and_snapshot_tamper_are_refused(self):
        root, project = self.bound()
        with closing(sqlite3.connect(root / "corpus/dialogue.sqlite")) as con, con:
            con.execute("UPDATE paragraphs SET text='synthetic changed source'")
        with self.assertRaisesRegex(StorageError, "bound_paragraph_changed"):
            read_project(root)
        other, _ = self.bound("other")
        with closing(sqlite3.connect(other / "corpus/dialogue.sqlite")) as con, con:
            con.execute("UPDATE editions SET title='synthetic metadata change'")
        with self.assertRaisesRegex(StorageError, "bound_corpus_changed"):
            read_project(other)
        last, _ = self.bound("last")
        write_json(last / "source-snapshot.json", {"changed": True})
        with self.assertRaisesRegex(StorageError, "source_snapshot_changed"):
            read_project(last)

    def test_linked_database_and_git_output_are_refused(self):
        target = self.root / "git-target"
        target.mkdir()
        (target / ".git").write_text("synthetic worktree")
        with self.assertRaisesRegex(StorageError, "private_root_inside_git"):
            binding.create_bound_project(target / "project", self.imported, target / "project/corpus")
        # A live WAL is a deterministic refusal on every supported platform.
        owner, project = self.bound()
        db = owner / "corpus/dialogue.sqlite"
        wal = Path(str(db) + "-wal")
        wal.write_bytes(b"synthetic")
        with self.assertRaises(ValueError):
            binding.bind_project(self.legacy, db, project["corpus_binding"]["edition_id"])

    def test_bundle_relocation_uses_relative_binding(self):
        root, project = self.bound()
        moved = self.root / "relocated"
        root.rename(moved)
        self.assertEqual(read_project(moved)["source"], project["source"])

    def test_cli_import_migrate_bind_recover_and_private_errors(self):
        imported = self.root / "cli-import"
        result = self.cli("import", self.legacy / "synthetic.epub", "--items", "scene",
                          "--project", imported, "--corpus", imported / "corpus")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(read_project(imported)["format"], binding.FORMAT)
        result = self.cli("migrate", "--project", self.legacy, "--corpus", self.legacy / "corpus")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        result = self.cli("recover", "--project", self.legacy)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for command in ("import", "migrate", "bind", "recover"):
            self.assertEqual(self.cli(command, "--help").returncode, 0)
        result = self.cli("bind", "--project", self.root / "SYNTHETIC_PRIVATE_SENTINEL",
                          "--corpus", self.root / "missing", "--edition", "SYNTHETIC_PRIVATE_SENTINEL")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("SYNTHETIC_PRIVATE", result.stdout + result.stderr)
        self.assertNotIn(str(self.root), result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")

    def test_recover_import_before_any_project_is_published(self):
        root = self.root / "interrupted-import"
        with patch.object(binding, "_make_corpus", side_effect=RuntimeError("synthetic fault")):
            with self.assertRaises(RuntimeError):
                binding.create_bound_project(root, self.imported, root / "corpus")
        self.assertFalse((root / "project.json").exists())
        recovered = binding.recover_project(root)
        self.assertEqual(recovered["format"], binding.FORMAT)
        self.assertEqual(recovered["source"], self.project["source"])

    def test_migration_backup_keeps_original_lf_bytes(self):
        path = self.legacy / "project.json"
        original = path.read_bytes().replace(b"\r\n", b"\n")
        path.write_bytes(original)
        binding.migrate_project(self.legacy, self.legacy / "corpus")
        backup = self.legacy / "migration" / (hashlib.sha256(original).hexdigest() + ".json")
        self.assertEqual(backup.read_bytes(), original)

    def test_nested_cross_paragraph_and_unclosed_quotes_keep_ranges(self):
        imported = copy.deepcopy(self.imported)
        paragraphs = imported["items"][0]["paragraphs"]
        paragraphs[0]["text"] = "「synthetic 『nested』"
        paragraphs[1]["text"] = "synthetic continued」"
        paragraphs[2]["text"] = "『synthetic unclosed"
        root = self.root / "cross"
        binding.create_bound_project(root, imported, root / "corpus")
        with closing(sqlite3.connect(root / "corpus/dialogue.sqlite")) as con, con:
            rows = con.execute("SELECT start_para,end_para,closed FROM dialogues ORDER BY ordinal").fetchall()
        self.assertEqual(rows, [(0, 1, 1), (0, 0, 1), (2, 2, 0)])


if __name__ == "__main__":
    unittest.main()
