"""Synthetic ledger contracts, byte-preserving export and concurrent writers."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import check_manifest
from ltr.project import ledger
from ltr.project.binding import create_bound_project
from ltr.project.storage import StorageError, create_project, project_lock, read_project, write_json

REPO = Path(__file__).resolve().parents[3]


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.imported = {"book_hash": "synthetic", "language": "ja", "items": [{"paragraphs": [
            {"id": "p1", "text": "Synthetic first paragraph."},
            {"id": "p2", "text": "Synthetic second paragraph."}]}]}
        create_project(self.project, self.imported)
        self.path = self.root / "ledger.json"
        ledger.init_ledger(self.path, self.project)
        self.manifest = self.root / "manifest.json"
        self.manifest.write_bytes((REPO / "assets/review-manifest.example.json").read_bytes())

    def add(self, **changes):
        fields = dict(dimension="character_arcs", kind="fact", locator="p1", observation="Synthetic observation")
        fields.update(changes)
        return ledger.change_ledger(self.path, "add", **fields)

    def fill(self):
        for dimension in ledger.DIMENSIONS:
            self.add(dimension=dimension, decision="Synthetic decision")

    def cli(self, *args):
        return subprocess.run([sys.executable, "-X", "utf8", "-B", "scripts/ltr", "ledger", *map(str, args)],
                              cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=20)

    def test_init_v1_scope_and_existing_refused(self):
        data = ledger._load(self.path)
        self.assertEqual(data["scope"], {"paragraph_ids": ["p1", "p2"], "complete_book_read": False, "read_confirmation": None})
        self.assertEqual(len(data["project_sha256"]), 64)
        with self.assertRaisesRegex(ledger.LedgerError, "ledger_already_exists"):
            ledger.init_ledger(self.path, self.project)

    def test_init_v2_uses_hydrated_project(self):
        root = self.root / "bound"
        create_bound_project(root, self.imported, root / "corpus")
        path = self.root / "bound-ledger.json"
        data = ledger.init_ledger(path, root)
        self.assertEqual(data["scope"]["paragraph_ids"], [p["id"] for p in read_project(root)["source"]])
        self.assertEqual(ledger.check_ledger(path, root)["errors"],
                         ["ledger_book_read_unconfirmed", "ledger_dimensions_missing"])

    def test_invalid_add_is_atomic(self):
        before = self.path.read_bytes()
        for changes in ({"locator": "outside"}, {"dimension": "invented"}, {"kind": "invented"},
                        {"decision": "   "}, {"observation": " "}, {"range": ["p1", "outside"]},
                        {"range": ["p2", "p1"]}, {"range": ["p2", "p2"]}):
            with self.subTest(changes=changes), self.assertRaises(ledger.LedgerError):
                self.add(**changes)
            self.assertEqual(self.path.read_bytes(), before)

    def test_edit_decide_remove_preserve_declaration_and_history(self):
        self.add(range=["p1", "p2"])
        ledger.change_ledger(self.path, "confirm-read", by="Synthetic reader", note="Synthetic declaration")
        confirmation = ledger._load(self.path)["scope"]
        data = ledger.change_ledger(self.path, "decide", id="d1", decision="Synthetic choice")
        self.assertEqual(data["entries"][0]["status"], "decided")
        data = ledger.change_ledger(self.path, "edit", id="d1", kind="interpretation", pending=True, clear_range=True)
        self.assertEqual(data["entries"][0]["status"], "pending")
        self.assertIsNone(data["entries"][0]["source_range"])
        data = ledger.change_ledger(self.path, "remove", id="d1")
        self.assertEqual(data["scope"], confirmation)
        self.assertEqual(data["entries"][0]["status"], "withdrawn")
        self.assertEqual(data["entries"][0]["observation"], "Synthetic observation")
        self.assertEqual(ledger.understanding(data)["dimensions"], [])
        self.assertEqual(ledger.check_ledger(self.path)["pending_count"], 0)
        self.assertEqual(self.add()["entries"][1]["id"], "d2")
        with self.assertRaisesRegex(ledger.LedgerError, "ledger_entry_withdrawn"):
            ledger.change_ledger(self.path, "edit", id="d1", decision="Synthetic")

    def test_check_reports_all_problems_without_mutating(self):
        data = self.add()
        data["entries"].append(dict(data["entries"][0], id="d2", status="decided", source_locator="outside"))
        write_json(self.path, data)
        before = self.path.read_bytes()
        result = ledger.check_ledger(self.path)
        self.assertEqual(result["errors"], ["ledger_book_read_unconfirmed", "ledger_decision_required",
            "ledger_dimensions_missing", "ledger_locator_out_of_scope", "ledger_pending_entries"])
        self.assertEqual(len(result["missing_dimensions"]), 7)
        self.assertEqual(self.path.read_bytes(), before)
        output = self.cli("check", "--ledger", self.path)
        self.assertEqual(output.returncode, 1, output.stdout)
        self.assertNotIn("Synthetic observation", output.stdout + output.stderr)

    def test_changed_project_digest_and_scope(self):
        value = read_project(self.project)
        value["revision"] += 1
        write_json(self.project / "project.json", value)
        self.assertIn("ledger_project_changed", ledger.check_ledger(self.path, self.project)["errors"])
        data = ledger._load(self.path)
        data["scope"]["paragraph_ids"] = ["p2", "p1"]
        write_json(self.path, data)
        self.assertIn("ledger_project_scope_changed", ledger.check_ledger(self.path, self.project)["errors"])

    def test_export_never_infers_read_and_full_manifest_passes_after_confirmation(self):
        self.fill()
        result = ledger.export_ledger(self.path, self.manifest)
        self.assertFalse(result["complete_book_read"])
        manifest = json.loads(self.manifest.read_bytes())
        manifest["whole_book_understanding"] = result
        self.assertEqual(check_manifest.validate(manifest), ["whole_book_understanding_incomplete"])
        ledger.change_ledger(self.path, "confirm-read", by="Synthetic reader")
        self.assertEqual(ledger.check_ledger(self.path, self.project)["errors"], [])
        manifest["whole_book_understanding"] = ledger.export_ledger(self.path, self.manifest, write=True)
        self.assertEqual(check_manifest.validate(manifest), [])
        self.assertEqual(json.loads(self.manifest.read_bytes()), manifest)

    def test_write_preserves_every_byte_outside_target_value(self):
        self.add(decision="Synthetic decision")
        prefix = b'{\r\n "schema" : "literary-review-manifest-v2", "nested":{"whole_book_understanding":0},\r\n "whole_book_understanding" : '
        suffix = b', "other" : [1e2, "\\u0061", {"x":"}\\\""}] \r\n}\r\n'
        self.manifest.write_bytes(prefix + b'{"old":true}' + suffix)
        result = ledger.export_ledger(self.path, self.manifest, write=True)
        expected = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")
        self.assertEqual(self.manifest.read_bytes(), prefix + expected + suffix)

    def test_write_adds_absent_section_and_rejects_duplicates(self):
        raw = b'{"schema":"literary-review-manifest-v2", "x":1e2\r\n}'
        self.manifest.write_bytes(raw)
        ledger.export_ledger(self.path, self.manifest, write=True)
        self.assertIn(b'"x":1e2\r\n', self.manifest.read_bytes())
        self.assertIn("whole_book_understanding", json.loads(self.manifest.read_bytes()))
        duplicate = b'{"schema":"literary-review-manifest-v2","x":1,"x":2}'
        self.manifest.write_bytes(duplicate)
        with self.assertRaises(ledger.LedgerError):
            ledger.export_ledger(self.path, self.manifest, write=True)
        self.assertEqual(self.manifest.read_bytes(), duplicate)

    def test_git_corpus_and_link_paths_rejected(self):
        git = self.root / "git"
        git.mkdir()
        (git / ".git").write_text("gitdir: synthetic", encoding="utf-8")
        corpus = self.root / "corpus"
        corpus.mkdir()
        (corpus / "dialogue.sqlite").touch()
        for target in (git / "ledger.json", corpus / "nested/ledger.json"):
            with self.subTest(target=target), self.assertRaises((StorageError, ledger.LedgerError)):
                ledger.init_ledger(target, self.project)
            self.assertFalse(target.exists())
        with patch.object(Path, "is_symlink", return_value=True), self.assertRaises(StorageError):
            ledger._load(self.path)

    def test_lock_rejects_competing_process_and_releases(self):
        before = self.path.read_bytes()
        with project_lock(self.path.parent):
            output = self.cli("confirm-read", "--ledger", self.path, "--by", "Synthetic reader")
            self.assertEqual(output.returncode, 2, output.stdout)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertTrue((self.root / ".writer.lock").exists())
        self.assertFalse((self.root / ".writer.lock").exists())
        output = self.cli("confirm-read", "--ledger", self.path, "--by", "Synthetic reader")
        self.assertEqual(output.returncode, 0, output.stdout)

    def test_failed_atomic_publish_retains_original_and_unlocks(self):
        before = self.path.read_bytes()
        with patch("ltr.project.storage.os.replace", side_effect=OSError("Synthetic secret")):
            with self.assertRaises(OSError):
                self.add()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.root / ".writer.lock").exists())

    def test_errors_do_not_echo_arguments_paths_or_content(self):
        secret = "SYNTHETIC-PRIVATE-MARKER"
        for args in (("add", "--ledger", self.path, "--dimension", secret),
                     ("list", "--ledger", self.root / secret),
                     ("decide", "--ledger", self.path, "--id", secret, "--decision", secret)):
            output = self.cli(*args)
            self.assertEqual(output.returncode, 2)
            self.assertNotIn(secret, output.stdout + output.stderr)
            self.assertNotIn(str(self.root), output.stdout + output.stderr)
            self.assertEqual(output.stderr, "")
            self.assertEqual(set(json.loads(output.stdout)), {"status", "code"})

    def test_corrupt_entries_and_confirmation_are_reported(self):
        original = self.add(decision="Synthetic choice")
        for change in ({"kind": []}, {"dimension": {}}, {"status": "invalid"}, {"translation_decision": " "},
                       {"source_range": ["p2", "p1"]}):
            data = copy.deepcopy(original)
            data["entries"][0].update(change)
            write_json(self.path, data)
            self.assertEqual(ledger.check_ledger(self.path)["status"], "fail")
            with self.assertRaises(ledger.LedgerError):
                ledger.export_ledger(self.path, self.manifest)
        data = copy.deepcopy(original)
        data["scope"]["complete_book_read"] = True
        write_json(self.path, data)
        self.assertIn("ledger_read_confirmation_invalid", ledger.check_ledger(self.path)["errors"])

    def test_cli_help_and_list_filters(self):
        for mode in (None, "init", "add", "decide", "edit", "remove", "confirm-read", "check", "export", "list"):
            output = self.cli(*([mode] if mode else []), "--help")
            self.assertEqual(output.returncode, 0, output.stderr)
            self.assertIn("usage:", output.stdout)
        self.add(kind="narration")
        self.add(dimension="plot_knowledge", decision="Synthetic decision")
        output = self.cli("list", "--ledger", self.path, "--dimension", "character_arcs", "--status", "pending")
        self.assertEqual(output.returncode, 0, output.stdout)
        self.assertEqual([e["id"] for e in json.loads(output.stdout)["entries"]], ["d1"])

    def test_cli_end_to_end_synthetic_manifest(self):
        self.path = self.root / "cli-ledger.json"
        output = self.cli("init", "--ledger", self.path, "--project", self.project)
        self.assertEqual(output.returncode, 0, output.stdout)
        for number, dimension in enumerate(ledger.DIMENSIONS):
            options = ["--decision", "Synthetic decision"] if number else []
            output = self.cli("add", "--ledger", self.path, "--dimension", dimension,
                              "--kind", "fact", "--locator", "p1", "--observation", "Synthetic observation", *options)
            self.assertEqual(output.returncode, 0, output.stdout)
        output = self.cli("check", "--ledger", self.path, "--project", self.project)
        self.assertEqual(output.returncode, 1, output.stdout)
        self.assertEqual(json.loads(output.stdout)["errors"], ["ledger_book_read_unconfirmed", "ledger_pending_entries"])
        self.assertEqual(json.loads(output.stdout)["pending_count"], 1)
        before = self.manifest.read_bytes()
        output = self.cli("export", "--ledger", self.path, "--manifest", self.manifest)
        self.assertEqual(output.returncode, 0, output.stdout)
        self.assertFalse(json.loads(output.stdout)["complete_book_read"])
        self.assertEqual(self.manifest.read_bytes(), before)
        for mode, options in (("decide", ["--id", "d1", "--decision", "Synthetic decision"]),
                              ("confirm-read", ["--by", "Synthetic reader"]),
                              ("check", ["--project", self.project]),
                              ("export", ["--manifest", self.manifest, "--write"])):
            output = self.cli(mode, "--ledger", self.path, *options)
            self.assertEqual(output.returncode, 0, output.stdout)
        result = subprocess.run([sys.executable, "scripts/check_manifest.py", str(self.manifest)],
                                cwd=REPO, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["errors"], [])

    def test_export_writer_lock_and_invalid_manifest_preserve_files(self):
        before = self.manifest.read_bytes()
        with project_lock(self.manifest.parent), self.assertRaises(StorageError):
            ledger.export_ledger(self.path, self.manifest, write=True)
        self.assertEqual(self.manifest.read_bytes(), before)
        with self.assertRaisesRegex(ledger.LedgerError, "ledger_manifest_conflict"):
            ledger.export_ledger(self.path, self.path, write=True)
        self.manifest.write_bytes(b'{"schema":"wrong"}')
        with self.assertRaisesRegex(ledger.LedgerError, "ledger_manifest_invalid"):
            ledger.export_ledger(self.path, self.manifest, write=True)
        self.assertEqual(self.manifest.read_bytes(), b'{"schema":"wrong"}')


if __name__ == "__main__":
    unittest.main()
