import json
from pathlib import Path
import tempfile
import unittest

from ltr.project.series_registry import (RegistryError, initialize, register_book,
    register_profile, accept_profile, create_run, run_status, import_cycle, build_context_pack,
    register_background)
from ltr.project.storage import write_json


class SeriesRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "series"
        initialize(self.root, "S01")
        register_book(self.root, "B01", "a" * 64, 1, 100)
        register_book(self.root, "B02", "b" * 64, 2, 100)

    def rule(self, scope="author_candidate", book="B01", paragraph=10):
        value = {"id": "D01", "scope": scope, "rule": "Preserve narrative tense.",
                 "known_at": {"book_id": book, "paragraph": paragraph}}
        if scope == "scene": value["end_paragraph"] = 20
        if scope == "character": value["character_id"] = "C01"
        return value

    def propose(self, rule=None):
        return register_profile(self.root, "P01", [rule or self.rule()], {"kind": "synthetic"})

    def test_order_basis_preserves_publication_default_and_corpus_metadata(self):
        state = json.loads((self.root / "series.json").read_text(encoding="utf-8"))
        self.assertEqual(state["books"]["B01"]["order_basis"], "publication")
        record = register_book(self.root, "B03", "c" * 64, 3, 20,
                               order_basis="corpus_traversal")
        self.assertEqual(record["order_basis"], "corpus_traversal")
        self.assertEqual(record["publication_order"], 3)
        with self.assertRaises(RegistryError):
            register_book(self.root, "B04", "d" * 64, 4, 20, order_basis="guessed")

    def test_corpus_order_does_not_remove_full_series_background(self):
        register_book(self.root, "B03", "c" * 64, 3, 20,
                      order_basis="corpus_traversal")
        register_background(self.root, "future_corpus", "B03", {"synthetic": "later traversal book"})
        create_run(self.root, "corpus_run", "B01", 0, experimental=True)
        pack = build_context_pack(self.root, "corpus_run")
        self.assertTrue(pack["full_series_context"])
        self.assertEqual(pack["full_series_background"][0]["book_id"], "B03")

    def test_proposals_require_explicit_experimental_run(self):
        self.propose()
        self.assertEqual(create_run(self.root, "R01", "B02", 0)["rules"], [])
        result = create_run(self.root, "R02", "B02", 0, experimental=True)
        self.assertEqual(len(result["rules"]), 1)
        self.assertEqual(result["rules"][0]["review_status"], "proposed")
        self.assertTrue(result["full_series_context"])
        self.assertTrue(result["exclude_target_translation_examples"])

    def test_explicit_acceptance_creates_immutable_revision(self):
        original = self.propose()
        with self.assertRaises(RegistryError): accept_profile(self.root, "P01")
        confirmed = accept_profile(self.root, "P01", explicit_user_acceptance=True)
        self.assertEqual(confirmed["revision"], 2)
        self.assertEqual(confirmed["status"], "confirmed")
        saved = json.loads((self.root / "profiles/P01/revision-1.json").read_text())
        self.assertEqual(saved, original)
        self.assertEqual(len(create_run(self.root, "R01", "B02", 0)["rules"]), 1)

    def test_future_knowledge_is_available_with_revelation_constraint(self):
        self.propose(self.rule(book="B02", paragraph=50))
        result = create_run(self.root, "R01", "B01", 10, experimental=True)
        self.assertEqual(len(result["rules"]), 1)
        self.assertEqual(result["rules"][0]["revelation_constraint"],
                         "background_only_do_not_add_to_translation")

    def test_scene_does_not_leak_to_other_books_or_outside_scene(self):
        self.propose(self.rule(scope="scene"))
        for run, book, position, count in [("R01", "B01", 5, 0), ("R02", "B01", 15, 1),
                                            ("R03", "B01", 21, 0), ("R04", "B02", 15, 0)]:
            self.assertEqual(len(create_run(self.root, run, book, position, experimental=True)["rules"]), count)

    def test_character_rules_require_target_character_mapping(self):
        self.propose(self.rule(scope="character"))
        self.assertEqual(create_run(self.root, "R01", "B02", 0, experimental=True)["rules"], [])
        self.assertEqual(len(create_run(self.root, "R02", "B02", 0, experimental=True,
                                        character_ids=["C01"])["rules"]), 1)

    def test_source_change_invalidates_evidence_and_existing_runs(self):
        self.propose()
        create_run(self.root, "R01", "B02", 0, experimental=True)
        register_book(self.root, "B01", "c" * 64, 1, 100)
        self.assertIn("evidence_source_changed", run_status(self.root, "R01")["stale_reasons"])
        self.assertEqual(create_run(self.root, "R02", "B02", 0, experimental=True)["rules"], [])
        with self.assertRaises(RegistryError):
            accept_profile(self.root, "P01", explicit_user_acceptance=True)

    def test_profile_revision_invalidates_manifest_without_overwriting_it(self):
        self.propose()
        create_run(self.root, "R01", "B02", 0, experimental=True)
        original = (self.root / "runs/R01.json").read_bytes()
        self.propose()
        self.assertEqual(run_status(self.root, "R01")["status"], "stale")
        self.assertEqual((self.root / "runs/R01.json").read_bytes(), original)
        with self.assertRaises(RegistryError): create_run(self.root, "R01", "B02", 0)

    def test_import_preserves_two_proposed_revisions_and_private_provenance(self):
        cycle = Path(self.temp.name) / "cycle"
        write_json(cycle / "dimensions-v1.json", {"dimensions": [self.rule()]})
        write_json(cycle / "dimensions-v2.json", {"changes": [
            {"dimension_id": "D01", "new_rule": "Preserve tense and pauses."}]})
        revisions = import_cycle(self.root, cycle, "P01", "B01", 20)
        self.assertEqual([r["revision"] for r in revisions], [1, 2])
        self.assertTrue(all(r["status"] == "proposed" for r in revisions))
        self.assertEqual(revisions[-1]["rules"][0]["rule"], "Preserve tense and pauses.")
        self.assertNotIn(str(cycle), json.dumps(revisions))

    def test_rejects_path_ids_duplicate_order_invalid_horizon_and_tampering(self):
        with self.assertRaises(RegistryError): register_book(self.root, "../bad", "c" * 64, 3, 1)
        with self.assertRaises(RegistryError): register_book(self.root, "B03", "c" * 64, 2, 1)
        with self.assertRaises(RegistryError): create_run(self.root, "R01", "B01", 100)
        self.propose()
        path = self.root / "profiles/P01/revision-1.json"
        value = json.loads(path.read_text()); value["status"] = "confirmed"
        write_json(path, value)
        with self.assertRaises(RegistryError): create_run(self.root, "R01", "B01", 10)

    def test_import_supports_merged_dimension_revision_without_losing_changes(self):
        cycle = Path(self.temp.name) / "merged-cycle"
        original = self.rule()
        changed = {**original, "rule": "Retain hesitation without added explanation.",
                   "previous_rule": original["rule"], "revision_reason": "Synthetic comparison."}
        write_json(cycle / "dimensions-v1.json", {"dimensions": [original]})
        write_json(cycle / "dimensions-v2.json", {"dimensions": [changed],
                   "status": "single_passage_experimental", "promotion": "not_series_validated"})
        revisions = import_cycle(self.root, cycle, "P01", "B01", 20)
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[1]["rules"][0]["rule"], changed["rule"])
        self.assertEqual(revisions[1]["rules"][0]["previous_rule"], original["rule"])
        self.assertEqual(revisions[1]["rules"][0]["revision_reason"], changed["revision_reason"])
        self.assertEqual(revisions[1]["status"], "proposed")

    def test_context_pack_preserves_rules_and_checks_manifest_and_profile_files(self):
        self.propose()
        manifest = create_run(self.root, "R01", "B02", 0, experimental=True)
        pack = build_context_pack(self.root, "R01")
        self.assertEqual(pack["rules"], manifest["rules"])
        self.assertEqual(pack["source_digest"], "b" * 64)
        self.assertTrue(pack["full_series_context"])
        path = self.root / "runs/R01.json"
        altered = {**manifest, "rules": []}
        write_json(path, altered)
        with self.assertRaisesRegex(RegistryError, "manifest_changed_on_disk"):
            build_context_pack(self.root, "R01")
        write_json(path, manifest)
        profile_path = self.root / "profiles/P01/revision-1.json"
        profile = json.loads(profile_path.read_text()); profile["status"] = "confirmed"
        write_json(profile_path, profile)
        with self.assertRaisesRegex(RegistryError, "profile_changed_on_disk"):
            build_context_pack(self.root, "R01")

    def test_context_pack_rejects_stale_profile_and_source_dependencies(self):
        self.propose()
        create_run(self.root, "R01", "B02", 0, experimental=True)
        self.propose()
        with self.assertRaisesRegex(RegistryError, "stale_run"):
            build_context_pack(self.root, "R01")
        create_run(self.root, "R02", "B02", 0, experimental=True)
        register_book(self.root, "B02", "d" * 64, 2, 100)
        with self.assertRaisesRegex(RegistryError, "stale_run"):
            build_context_pack(self.root, "R02")

    def test_background_includes_future_books_as_proposals_without_following_paths(self):
        content = {"characters": [{"id": "C01", "voice": "Reserved."}],
                   "story": "Synthetic later revelation.", "file": "missing-file.json",
                   "status": "confirmed"}
        register_background(self.root, "BG01", "B02", content)
        create_run(self.root, "R01", "B01", 0)
        pack = build_context_pack(self.root, "R01")
        self.assertEqual(len(pack["full_series_background"]), 1)
        self.assertEqual(pack["full_series_background"][0]["status"], "proposed")
        self.assertEqual(pack["full_series_background"][0]["content"], content)

    def test_background_revisions_are_immutable_and_invalidate_existing_runs(self):
        register_background(self.root, "BG01", "B01", {"story": "Version one."})
        old_path = self.root / "backgrounds/BG01/revision-1.json"
        original = old_path.read_bytes()
        create_run(self.root, "R01", "B02", 0)
        updated = register_background(self.root, "BG01", "B01", {"story": "Version two."})
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(old_path.read_bytes(), original)
        self.assertIn("background_changed", run_status(self.root, "R01")["stale_reasons"])
        with self.assertRaisesRegex(RegistryError, "stale_run"):
            build_context_pack(self.root, "R01")

    def test_background_source_change_rejects_old_and_new_runs(self):
        register_background(self.root, "BG01", "B01", {"story": "Synthetic."})
        create_run(self.root, "R01", "B02", 0)
        register_book(self.root, "B01", "c" * 64, 1, 100)
        self.assertIn("background_source_changed", run_status(self.root, "R01")["stale_reasons"])
        with self.assertRaisesRegex(RegistryError, "stale_run"):
            build_context_pack(self.root, "R01")
        with self.assertRaisesRegex(RegistryError, "stale_background"):
            create_run(self.root, "R02", "B02", 0)

    def test_background_file_and_recorded_dependency_digests_are_checked(self):
        register_background(self.root, "BG01", "B01", {"story": "Synthetic."})
        create_run(self.root, "R01", "B02", 0)
        path = self.root / "backgrounds/BG01/revision-1.json"
        data = json.loads(path.read_text()); data["content"]["story"] = "Tampered."
        write_json(path, data)
        with self.assertRaisesRegex(RegistryError, "background_changed_on_disk"):
            build_context_pack(self.root, "R01")

    def test_background_registration_requires_valid_id_book_and_object(self):
        for background_id, book_id, content in [("../bad", "B01", {"x": 1}),
                                                 ("BG01", "missing", {"x": 1}),
                                                 ("BG01", "B01", {})]:
            with self.assertRaises(RegistryError):
                register_background(self.root, background_id, book_id, content)

    def test_new_background_marks_preexisting_full_series_pack_stale(self):
        create_run(self.root, "R01", "B01", 0)
        register_background(self.root, "BG01", "B02", {"story": "New knowledge."})
        self.assertIn("background_catalogue_changed", run_status(self.root, "R01")["stale_reasons"])
        with self.assertRaisesRegex(RegistryError, "stale_run"):
            build_context_pack(self.root, "R01")


if __name__ == "__main__":
    unittest.main()
