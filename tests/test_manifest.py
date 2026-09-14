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

    def test_invalid_note_anchor(self):
        self.data['notes'][0]['anchor_id'] = 'absent'
        self.assertIn('note_anchor_invalid', validate(self.data))

    def test_unresolved_does_not_get_erased(self):
        self.data['unresolved'] = [{'id': 'p2', 'issue': 'synthetic ambiguity'}]
        self.assertEqual(validate(self.data), [])

    def test_invalid_inputs_are_rejected(self):
        for value in [None, [], {}, {'schema': 'literary-review-manifest-v1'}]:
            with self.subTest(value=value):
                self.assertTrue(validate(value))

    def test_note_review_must_be_explicit(self):
        self.data['notes'][0]['reviewed'] = 'yes'
        self.assertIn('note_not_reviewed', validate(self.data))


if __name__ == '__main__':
    unittest.main()
