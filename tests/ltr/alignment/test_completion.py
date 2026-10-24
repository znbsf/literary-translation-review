"""Synthetic-only tests for the read-only formal completion alignment view."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from ltr.alignment import completion as cv
from ltr import workbench as wb


SCHEMA = """
CREATE TABLE editions(id TEXT,title TEXT,language TEXT,book_key TEXT,origin_kind TEXT);
CREATE TABLE dialogues(id TEXT,edition_id TEXT,ordinal INTEGER,text TEXT,
    speaker TEXT,addressee TEXT,context_hash TEXT);
CREATE TABLE corpus_completion_alignment(
    quote_id TEXT,run_id TEXT,task_key TEXT,target_edition_id TEXT,source_edition_id TEXT,
    status TEXT,decision TEXT,source_ids_json TEXT,confidence_uncalibrated REAL,job_id TEXT,
    binding_sha256 TEXT,input_sha256 TEXT,contract_sha256 TEXT,evidence_ids_json TEXT,
    requires_review INTEGER,accepted INTEGER,human_confirmed INTEGER,semantic_verified INTEGER,
    reference_eligible INTEGER);
"""


class CompletionAlignmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.corpus_root = self.root / 'corpus'
        self.corpus_root.mkdir()
        self.db_path = self.corpus_root / 'dialogue.sqlite'
        con = sqlite3.connect(self.db_path)
        try:
            con.executescript(SCHEMA)
            con.executemany('INSERT INTO editions VALUES (?,?,?,?,?)', [
                ('zh1', 'Synthetic Chinese', 'zh', 'book-a', 'translation'),
                ('ja1', 'Synthetic Japanese', 'ja', 'book-a', 'japanese_source'),
                ('ja2', 'Other Japanese', 'ja', 'book-b', 'other_source'),
                ('en1', 'English', 'en', 'book-a', 'other_source'),
            ])
            con.executemany('INSERT INTO dialogues VALUES (?,?,?,?,?,?,?)', [
                ('z1', 'zh1', 1, 'Synthetic target dialogue', 'A', 'B', 'z1-hash'),
                ('j1', 'ja1', 4, '源句' * 2500, None, None, 'j1-hash'),
                ('j2', 'ja1', 5, 'Another synthetic source quote', None, None, 'j2-hash'),
                ('j3', 'ja2', 6, 'Cross-edition source quote', None, None, 'j3-hash'),
            ])
            self._insert_alignment(con, 'z1')
            con.commit()
        finally:
            con.close()
        self.store = self._new_store()
        self.addCleanup(lambda: self.store.close())
        self.view = cv.CompletionAlignmentView(self.store, 'sample')

    def _new_store(self):
        return wb.WorkbenchStore(wb.WorkbenchConfig(
            [wb.CorpusConfig('sample', 'Synthetic', self.corpus_root)], self.root / 'reviews'))

    def _insert_alignment(self, con, quote_id, *, target='zh1', source='ja1', status=None,
                          decision=None, ids=None, confidence=0.75, flags=(1, 0, 0, 0, 0),
                          hashes=('a' * 64, 'b' * 64, 'c' * 64)):
        status = status or 'unresolved_partial_candidate'
        decision = decision or cv.STATUS_DECISIONS.get(status, 'unknown')
        ids = ['j1'] if ids is None else ids
        con.execute('INSERT INTO corpus_completion_alignment VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (quote_id, 'synthetic-run', 'task-1', target, source, status, decision,
                     json.dumps(ids), confidence, 'synthetic-job', hashes[0], hashes[1], hashes[2],
                     '[]', *flags))

    def _mutate(self, statement, params=()):
        self.store.close()
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(statement, params)
            con.commit()
        finally:
            con.close()
        self.store = self._new_store()
        self.view = cv.CompletionAlignmentView(self.store, 'sample')

    def _assert_mismatch(self, *args):
        with self.assertRaises(wb.WorkbenchError) as caught:
            self.view.detail(*args)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, 'completion_alignment_source_mismatch')

    def test_overview_counts_pending_denominator_and_statuses(self):
        self._mutate(
            'INSERT INTO corpus_completion_alignment VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('missing', 'r', 't', 'zh1', 'ja1', 'future-private-status', 'secret-decision',
             '[]', None, 'j', 'd' * 64, 'e' * 64, 'f' * 64, '[]', 1, 0, 0, 0, 0))
        overview = self.view.overview()
        self.assertTrue(overview['available'])
        self.assertEqual(overview['pending_rows'], 2)
        self.assertEqual(overview['mapped_pending_rows'], 1)
        self.assertEqual(overview['unmapped_pending_rows'], 1)
        self.assertEqual(overview['statuses']['unresolved_partial_candidate'], 1)
        self.assertEqual(overview['statuses']['other'], 1)
        self.assertNotIn('future-private-status', json.dumps(overview))
        self.assertNotIn('secret-decision', json.dumps(overview))

    def test_detail_returns_bounded_source_and_unverified_flags(self):
        result = self.view.detail('z1', 'zh1')
        quote = result['source_quotes'][0]
        self.assertEqual(result['decision'], 'partial_candidate')
        self.assertEqual(result['status'], 'unresolved_partial_candidate')
        self.assertEqual(result['source_edition']['origin_kind'], 'japanese_source')
        self.assertTrue(result['same_book_key'])
        self.assertFalse(result['historical_contract_revalidated'])
        self.assertTrue(quote['text_truncated'])
        self.assertEqual(quote['text_total_chars'], 5000)
        self.assertLessEqual(len(quote['text']), wb.MAX_DIALOGUE_TEXT_CHARS)
        self.assertEqual(quote['text_sha256'], hashlib.sha256(('源句' * 2500).encode()).hexdigest())
        self.assertEqual(result['confidence_uncalibrated'], 0.75)
        self.assertTrue(result['requires_review'])
        for key in ('accepted', 'human_confirmed', 'semantic_verified', 'reference_eligible',
                    'semantic_identity_verified'):
            self.assertFalse(result[key])
        self.assertEqual(result['scope'], 'stored_completion_candidate')

    def test_empty_source_id_list_is_valid(self):
        self._mutate('UPDATE corpus_completion_alignment SET source_ids_json=?', ('[]',))
        self.assertEqual(self.view.detail('z1', 'zh1')['source_quotes'], [])

    def test_no_pending_row_returns_none(self):
        self._mutate('UPDATE corpus_completion_alignment SET requires_review=0')
        self.assertIsNone(self.view.detail('z1', 'zh1'))

    def test_missing_complete_schema_reports_unavailable(self):
        self.store.close()
        con = sqlite3.connect(self.db_path)
        try:
            con.execute('ALTER TABLE corpus_completion_alignment DROP COLUMN contract_sha256')
            con.commit()
        finally:
            con.close()
        self.store = self._new_store()
        self.view = cv.CompletionAlignmentView(self.store, 'sample')
        self.assertFalse(self.view.available())
        self.assertFalse(self.view.overview()['available'])
        self._assert_mismatch('z1', 'zh1')

    def test_available_requires_complete_schema(self):
        self.assertTrue(self.view.available())

    def test_duplicate_pending_rows_rejected(self):
        self._mutate(
            'INSERT INTO corpus_completion_alignment SELECT * FROM corpus_completion_alignment')
        self._assert_mismatch('z1', 'zh1')

    def test_requested_target_edition_must_match(self):
        self._assert_mismatch('z1', 'other-edition')

    def test_source_id_must_exist_in_declared_japanese_edition(self):
        self._mutate('UPDATE corpus_completion_alignment SET source_ids_json=?',
                     (json.dumps(['j3']),))
        self._assert_mismatch('z1', 'zh1')

    def test_source_edition_must_be_japanese(self):
        self._mutate('UPDATE corpus_completion_alignment SET source_edition_id=?', ('en1',))
        self._assert_mismatch('z1', 'zh1')

    def test_duplicate_source_dialogue_ids_are_rejected(self):
        self._mutate("INSERT INTO dialogues VALUES ('j1','ja1',7,'duplicate',NULL,NULL,'x')")
        self._assert_mismatch('z1', 'zh1')

    def test_invalid_source_id_json_shapes_are_rejected(self):
        for value in ('not json', '["j1","j1"]', json.dumps(['j1'] * 21),
                      json.dumps(['SYNTHETIC_SENTINEL'])):
            with self.subTest(value=value[:20]):
                self._mutate('UPDATE corpus_completion_alignment SET source_ids_json=?', (value,))
                self._assert_mismatch('z1', 'zh1')

    def test_invalid_flags_and_hashes_are_rejected_without_echo(self):
        self._mutate('UPDATE corpus_completion_alignment SET accepted=1')
        self._assert_mismatch('z1', 'zh1')
        self._mutate('UPDATE corpus_completion_alignment SET accepted=0,binding_sha256=?',
                     ('PRIVATE_SENTINEL',))
        with self.assertRaises(wb.WorkbenchError) as caught:
            self.view.detail('z1', 'zh1')
        self.assertEqual(caught.exception.code, 'completion_alignment_source_mismatch')
        self.assertNotIn('PRIVATE_SENTINEL', str(caught.exception))

    def test_unknown_status_and_confidence_are_sanitized(self):
        self._mutate('UPDATE corpus_completion_alignment SET status=?,decision=?,confidence_uncalibrated=?',
                     ('private-status-sentinel', 'private-decision-sentinel', 2.0))
        result = self.view.detail('z1', 'zh1')
        self.assertEqual((result['status'], result['decision']), ('other', 'unknown'))
        self.assertIsNone(result['confidence_uncalibrated'])
        self.assertNotIn('private-status-sentinel', json.dumps(result))
        self.assertNotIn('private-decision-sentinel', json.dumps(result))

    def test_source_hash_change_during_detail_rejects_result(self):
        _, db = self.store.database('sample')
        original = db.source_sha256(refresh=True)
        with mock.patch.object(db, 'source_sha256', side_effect=[original, '0' * 64]):
            self._assert_mismatch('z1', 'zh1')

    def test_late_source_read_error_refuses_detail_without_raw_error(self):
        _, db = self.store.database('sample')
        original = db.source_sha256(refresh=True)
        with mock.patch.object(db, 'source_sha256',
                               side_effect=[original, OSError('PRIVATE_ERROR_SENTINEL')]):
            self._assert_mismatch('z1', 'zh1')

    def test_source_identity_longer_than_display_bound_is_rejected(self):
        identifier = 'j' * 201
        self._mutate('UPDATE dialogues SET id=? WHERE id=?', (identifier, 'j1'))
        self._mutate('UPDATE corpus_completion_alignment SET source_ids_json=?',
                     (json.dumps([identifier]),))
        self._assert_mismatch('z1', 'zh1')

    def test_pending_filter_accepts_only_query_string_values(self):
        for params, expected in (({}, False), ({'pending_alignment': ''}, False),
                                 ({'pending_alignment': '0'}, False),
                                 ({'pending_alignment': '1'}, True)):
            self.assertEqual(cv.validate_pending_filter(params), expected)
        for params in ({'pending_alignment': True}, {'pending_alignment': 1},
                       {'pending_alignment': 'true'}, {'pending_alignment': ' 1'}):
            with self.subTest(params=params):
                with self.assertRaises(wb.WorkbenchError) as caught:
                    cv.validate_pending_filter(params)
                self.assertEqual(caught.exception.status, 400)


if __name__ == '__main__':
    unittest.main()
