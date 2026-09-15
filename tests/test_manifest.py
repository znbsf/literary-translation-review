import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from check_manifest import validate


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / 'assets/review-manifest.example.json').read_text('utf-8'))

    def test_complete_synthetic_declaration(self):
        self.assertEqual(validate(self.data), [])

    def test_missing_source_review(self):
        self.data['source_reviewed_ids'].pop()
        self.assertIn('source_reviewed_ids_coverage_or_order', validate(self.data))

    def test_duplicate_cannot_replace_missing(self):
        self.data['source_reviewed_ids'] = ['p1', 'p1']
        self.assertIn('source_reviewed_ids_duplicates', validate(self.data))

    def test_changes_must_be_rechecked(self):
        self.data['revision_rechecked_ids'] = []
        self.assertIn('changed_paragraphs_not_rechecked', validate(self.data))

    def test_each_understanding_dimension_must_change_a_translation_decision(self):
        self.data['whole_book_understanding']['decisions'].pop()
        self.assertIn('understanding_not_mapped_to_decisions', validate(self.data))

    def test_confirmed_glossary_must_be_complete_and_version_locked(self):
        self.data['confirmed_glossary']['locked'] = False
        self.assertIn('confirmed_glossary_not_version_locked', validate(self.data))

    def test_source_first_candidate_must_precede_comparison(self):
        self.data['source_first']['frozen_before_comparison_ids'].pop()
        self.assertIn('source_first_candidates_not_frozen_before_comparison', validate(self.data))

    def test_source_first_input_boundary_is_explicit(self):
        self.data['source_first']['excluded_inputs'].pop()
        self.assertIn('source_first_context_boundary_unverified', validate(self.data))

    def test_operator_exposure_requires_an_explicit_status(self):
        self.data['source_first']['operator_exposure_status'] = 'assumed-unseen'
        self.assertIn('operator_exposure_status_invalid', validate(self.data))

    def test_user_budget_is_a_hard_limit(self):
        self.data['execution']['budget_used']['value'] = 3
        self.assertIn('user_budget_hard_limit_exceeded', validate(self.data))

    def test_checkpoint_and_coverage_are_recorded(self):
        self.data['execution']['checkpoints'] = []
        self.assertIn('checkpoint_metadata_missing', validate(self.data))

    def test_invalid_note_anchor(self):
        self.data['notes'][0]['anchor_id'] = 'absent'
        self.assertIn('note_anchor_invalid', validate(self.data))

    def test_unresolved_does_not_get_erased(self):
        self.data['unresolved'] = [{'id': 'p2', 'issue': 'synthetic ambiguity'}]
        self.assertEqual(validate(self.data), [])

    def test_invalid_inputs_are_rejected(self):
        for value in [None, [], {}, {'schema': 'literary-review-manifest-v2'}]:
            with self.subTest(value=value):
                self.assertTrue(validate(value))

    def test_note_review_must_be_explicit(self):
        self.data['notes'][0]['reviewed'] = 'yes'
        self.assertIn('note_not_reviewed', validate(self.data))


if __name__ == '__main__':
    unittest.main()
