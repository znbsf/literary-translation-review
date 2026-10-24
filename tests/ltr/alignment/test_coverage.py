"""Synthetic whole-volume coverage of independent alignment proposals."""
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests.ltr.alignment import test_index as fixtures
from ltr.alignment import index as ai
from ltr import workbench as wb
from ltr.library.review_queue import fingerprint


class AlignmentCoverageTests(unittest.TestCase):
    setUp = fixtures.AlignmentIndexTests.setUp
    write_batch = fixtures.AlignmentIndexTests.write_batch
    make_row = fixtures.AlignmentIndexTests.make_row
    document = fixtures.AlignmentIndexTests.document
    write_file = fixtures.AlignmentIndexTests.write_file
    reset_inputs = fixtures.AlignmentIndexTests.reset_inputs
    review = fixtures.AlignmentIndexTests.review

    def store(self, configured=True):
        store = wb.WorkbenchStore(wb.WorkbenchConfig([
            wb.CorpusConfig('sample', 'Sample', self.source, alignment_index=self.index if configured else None)
        ], None))
        self.addCleanup(store.close)
        return store

    def overview(self, store=None):
        return (store or self.store()).overview()['corpora'][0]

    def build(self, rows=None, reviews=None):
        self.reset_inputs()
        self.write_file(self.document(rows if rows is not None else [self.row]), 'align-000.json')
        return ai.build(self.db, self.inputs, self.index, reviews)

    def change_index(self, sql, values=()):
        con = sqlite3.connect(self.index)
        try:
            # Deliberately forged fixtures must reach reader validation even
            # when the maintained builder normally enforces SQL CHECK flags.
            con.execute('PRAGMA ignore_check_constraints=ON')
            con.execute(sql, values)
            con.commit()
        finally:
            con.close()

    def assert_unavailable(self, overview):
        self.assertTrue(overview['available'])
        pending = overview['pending_alignments']
        self.assertFalse(pending['available'])
        self.assertFalse(pending['source_coverage']['available'])
        self.assertIsInstance(pending['source_coverage']['error'], str)
        self.assertNotIn('covered_dialogues', pending['source_coverage'])
        self.assertNotIn('source_dialogues', pending['source_coverage'])
        self.assertNotIn(str(self.root), json.dumps(pending))

    def test_partial_coverage_includes_unmatched_and_other_chinese_volumes(self):
        self.build([self.make_row('z1', ['j1']), self.make_row('z2', [], 'unmatched')])
        overview = self.overview()
        coverage = overview['pending_alignments']['source_coverage']
        self.assertTrue(coverage['available'])
        self.assertEqual(coverage['source_dialogues'], 13)
        self.assertEqual(coverage['covered_dialogues'], 2)
        self.assertEqual(coverage['missing_dialogues'], 11)
        editions = {row['edition_id']: row for row in coverage['editions']}
        self.assertEqual(set(editions), {'zh', 'zh2'})
        self.assertEqual(editions['zh']['source_dialogues'], 12)
        self.assertEqual(editions['zh']['proposal_dialogues'], 2)
        self.assertEqual(editions['zh']['matched_dialogues'], 1)
        self.assertEqual(editions['zh']['unmatched_dialogues'], 1)
        self.assertEqual(editions['zh']['unprocessed_dialogues'], 10)
        self.assertEqual(editions['zh']['conflicting_candidate_dialogues'], 0)
        self.assertEqual(editions['zh']['missing_dialogues'], 10)
        self.assertEqual(editions['zh']['source_dialogues'], editions['zh']['matched_dialogues']
                         + editions['zh']['unmatched_dialogues'] + editions['zh']['unprocessed_dialogues'])
        self.assertEqual(editions['zh']['source_paragraphs'], 1)
        self.assertEqual(editions['zh']['title'], 'Synthetic ZH')
        self.assertEqual(editions['zh']['same_book_key_japanese_editions'], 1)
        self.assertEqual(editions['zh2']['proposal_dialogues'], 0)
        self.assertEqual(editions['zh2']['matched_dialogues'], 0)
        self.assertEqual(editions['zh2']['unmatched_dialogues'], 0)
        self.assertEqual(editions['zh2']['unprocessed_dialogues'], 1)
        self.assertEqual(editions['zh2']['missing_dialogues'], 1)
        self.assertEqual(overview['counts']['alignments'], 0)
        self.assertNotIn(self.texts['z1'], json.dumps(coverage))

    def test_zero_dialogue_edition_with_paragraphs_is_retained(self):
        con = sqlite3.connect(self.db)
        con.executescript("""
            INSERT INTO editions VALUES ('empty-zh','Synthetic zero dialogue volume','zh','empty-book');
            INSERT INTO editions VALUES ('empty-ja','Synthetic peer','ja','empty-book');
            INSERT INTO paragraphs VALUES ('zero-p','empty-zh',0,'SYNTHETIC_PRIVATE_PARAGRAPH','{}');
        """)
        con.commit()
        con.close()
        self.sha = fingerprint(self.db)
        self.build()
        coverage = self.overview()['pending_alignments']['source_coverage']
        self.assertEqual(coverage['zero_dialogue_editions'], 1)
        empty = next(row for row in coverage['editions'] if row['edition_id'] == 'empty-zh')
        self.assertEqual((empty['source_dialogues'], empty['proposal_dialogues'], empty['missing_dialogues']), (0, 0, 0))
        self.assertEqual((empty['matched_dialogues'], empty['unmatched_dialogues'],
                          empty['unprocessed_dialogues'], empty['conflicting_candidate_dialogues']), (0, 0, 0, 0))
        self.assertEqual(empty['source_paragraphs'], 1)
        self.assertEqual(empty['same_book_key_japanese_editions'], 1)
        self.assertNotIn('complete', empty)
        self.assertNotIn('percentage', empty)
        self.assertNotIn('SYNTHETIC_PRIVATE_PARAGRAPH', json.dumps(coverage))

    def test_unconfigured_differs_from_valid_index_with_zero_coverage(self):
        unconfigured = self.overview(self.store(configured=False))['pending_alignments']['source_coverage']
        self.assertEqual(unconfigured, {'available': False, 'error': None})
        self.build()
        self.change_index('DELETE FROM candidates')
        coverage = self.overview()['pending_alignments']['source_coverage']
        self.assertTrue(coverage['available'])
        self.assertEqual(coverage['covered_dialogues'], 0)
        self.assertEqual(coverage['missing_dialogues'], coverage['source_dialogues'])

    def test_formal_alignment_table_never_counts_as_independent_proposal(self):
        con = sqlite3.connect(self.db)
        con.executescript("CREATE TABLE alignments(chinese_id TEXT,status TEXT); INSERT INTO alignments VALUES ('z2','aligned');")
        con.commit()
        con.close()
        self.sha = fingerprint(self.db)
        self.build()
        overview = self.overview()
        self.assertEqual(overview['counts']['alignments'], 1)
        self.assertEqual(overview['pending_alignments']['source_coverage']['covered_dialogues'], 1)

    def test_model_opinions_do_not_change_proposal_coverage(self):
        self.review(0, 'keep')
        self.review(1, 'reject')
        self.build(reviews=self.reviews)
        first = self.overview()['pending_alignments']
        self.assertEqual(first['review_status'], {'conflict': 1})
        before = first['source_coverage']
        self.change_index("UPDATE candidates SET model_review_status='unreviewed',observations_json='[]'")
        after = self.overview()['pending_alignments']['source_coverage']
        self.assertEqual(before, after)

    def test_same_book_key_is_only_exact_nonempty_metadata_count(self):
        con = sqlite3.connect(self.db)
        con.executescript("""
            INSERT INTO editions VALUES ('ja2','Synthetic second JA','ja','book');
            INSERT INTO editions VALUES ('no-key-zh','Synthetic missing key','zh',NULL);
            INSERT INTO editions VALUES ('no-key-ja','Synthetic missing key JA','ja',NULL);
        """)
        con.commit()
        con.close()
        self.sha = fingerprint(self.db)
        self.build()
        rows = {row['edition_id']: row for row in self.overview()['pending_alignments']['source_coverage']['editions']}
        self.assertEqual(rows['zh']['same_book_key_japanese_editions'], 2)
        self.assertEqual(rows['no-key-zh']['same_book_key_japanese_editions'], 0)

    def test_stale_index_or_changed_source_is_unavailable(self):
        self.build()
        self.change_index("UPDATE metadata SET value=? WHERE key='source_sha256'", ('0' * 64,))
        self.assert_unavailable(self.overview())

    def test_illegal_ids_edition_language_and_source_hash_fail_closed(self):
        self.build()
        before = self.index.read_bytes()
        for sql, values in (
            ("UPDATE candidates SET chinese_id=?", ('FAKE_PRIVATE_INVALID_ID',)),
            ("UPDATE candidates SET edition_id='zh2'", ()),
            ("UPDATE candidates SET chinese_id='j1',edition_id='ja',text=?,chinese_text_sha256=?",
             (self.texts['j1'], ai.text_hash(self.texts['j1']))),
            ("UPDATE candidates SET chinese_text_sha256=?", ('0' * 64,)),
            ("UPDATE candidates SET accepted=1", ()),
            ("UPDATE candidates SET japanese_json='[]'", ()),
        ):
            self.index.write_bytes(before)
            self.change_index(sql, values)
            self.assert_unavailable(self.overview())
        self.assertNotIn('FAKE_PRIVATE_INVALID_ID', json.dumps(self.overview()['pending_alignments']))

    def test_duplicate_chinese_id_across_proposal_rows_is_refused(self):
        self.build()
        con = sqlite3.connect(self.index)
        con.row_factory = sqlite3.Row
        row = dict(con.execute('SELECT * FROM candidates').fetchone())
        row['id'] = 'f' * 16
        columns = list(row)
        con.execute('INSERT INTO candidates (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')',
                    [row[column] for column in columns])
        con.commit()
        con.close()
        self.assert_unavailable(self.overview())

    def test_duplicate_referenced_japanese_source_id_is_static_unavailable(self):
        self.build()
        baseline_db = self.db.read_bytes()
        baseline_index = self.index.read_bytes()
        private_marker = 'SYNTHETIC_DUPLICATE_JAPANESE_SOURCE_TEXT'
        for scope in ('same_edition', 'cross_edition'):
            with self.subTest(scope=scope):
                self.db.write_bytes(baseline_db)
                self.index.write_bytes(baseline_index)
                con = sqlite3.connect(self.db)
                if scope == 'same_edition':
                    con.execute("INSERT INTO dialogues VALUES ('j1','ja',99,?,'A','B','duplicate-same')",
                                (private_marker,))
                else:
                    con.execute("INSERT INTO editions VALUES ('ja-duplicate','Synthetic duplicate JA','ja','book')")
                    con.execute("INSERT INTO dialogues VALUES ('j1','ja-duplicate',99,?,'A','B','duplicate-cross')",
                                (private_marker,))
                con.commit(); con.close()
                self.sha = fingerprint(self.db)
                con = sqlite3.connect(self.index)
                con.execute("UPDATE metadata SET value=? WHERE key='source_sha256'", (self.sha,))
                con.commit(); con.close()

                overview = self.overview(self.store())
                pending = overview['pending_alignments']
                self.assertFalse(pending['available'])
                self.assertEqual(pending['source_coverage'], {
                    'available': False, 'error': 'alignment_source_coverage_invalid'
                })
                encoded = json.dumps(pending, ensure_ascii=False)
                self.assertNotIn(private_marker, encoded)
                self.assertNotIn(self.texts['j1'], encoded)

    def test_same_chinese_across_valid_japanese_versions_counts_once(self):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO editions VALUES ('ja2','Synthetic alternate Japanese','ja','book')")
        con.execute("INSERT INTO dialogues VALUES ('j-alt','ja2',0,'Synthetic alternate Japanese quote','A','B','j-alt')")
        con.commit()
        con.close()
        self.sha = fingerprint(self.db)
        self.texts['j-alt'] = 'Synthetic alternate Japanese quote'
        self.reset_inputs()
        self.write_file(self.document([self.make_row('z1', ['j1'])]), 'align-001.json')
        self.write_file(self.document([self.make_row('z1', ['j-alt'])], ja='ja2'), 'align-002.json')
        ai.build(self.db, self.inputs, self.index)
        pending = self.overview()['pending_alignments']
        self.assertTrue(pending['available'])
        self.assertEqual(pending['total'], 2)
        coverage = pending['source_coverage']
        self.assertEqual(coverage['source_dialogues'], 13)
        self.assertEqual(coverage['covered_dialogues'], 1)
        self.assertEqual(coverage['missing_dialogues'], 12)
        zh = next(row for row in coverage['editions'] if row['edition_id'] == 'zh')
        self.assertEqual(zh['proposal_dialogues'], 1)
        self.assertEqual(zh['matched_dialogues'], 1)
        self.assertEqual(zh['unmatched_dialogues'], 0)
        self.assertEqual(zh['unprocessed_dialogues'], 11)
        self.assertEqual(zh['conflicting_candidate_dialogues'], 0)
        self.assertEqual(zh['same_book_key_japanese_editions'], 2)

    def test_status_conflict_across_japanese_editions_is_explicit_and_partitioned(self):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO editions VALUES ('ja2','Synthetic alternate JA','ja','book')")
        con.commit(); con.close()
        self.sha = fingerprint(self.db)
        self.reset_inputs()
        self.write_file(self.document([self.make_row('z1', ['j1'])], ja='ja'), 'align-001.json')
        self.write_file(self.document([self.make_row('z1', [], 'unmatched')], ja='ja2'), 'align-002.json')
        ai.build(self.db, self.inputs, self.index)
        before = self.db.read_bytes()
        coverage = self.overview()['pending_alignments']['source_coverage']
        self.assertTrue(coverage['available'])
        zh = next(row for row in coverage['editions'] if row['edition_id'] == 'zh')
        self.assertEqual(zh['source_dialogues'], 12)
        self.assertEqual(zh['proposal_dialogues'], 1)
        self.assertEqual(zh['matched_dialogues'], 1)
        self.assertEqual(zh['unmatched_dialogues'], 0)
        self.assertEqual(zh['unprocessed_dialogues'], 11)
        self.assertEqual(zh['conflicting_candidate_dialogues'], 1)
        self.assertEqual(zh['source_dialogues'], zh['matched_dialogues']
                         + zh['unmatched_dialogues'] + zh['unprocessed_dialogues'])
        self.assertEqual(coverage['source_dialogues'], coverage['matched_dialogues']
                         + coverage['unmatched_dialogues'] + coverage['unprocessed_dialogues'])
        self.assertEqual(coverage['conflicting_candidate_dialogues'], 1)
        self.assertEqual(self.db.read_bytes(), before)

    def test_no_candidates_are_all_unprocessed_and_duplicate_source_id_counts_once(self):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO dialogues VALUES ('z1','zh',100,'Duplicate synthetic quote','A','B','dup-z1')")
        con.commit(); con.close()
        self.sha = fingerprint(self.db)
        self.reset_inputs()
        self.write_file(self.document([self.make_row('q1', ['j1'])], zh='zh2'), 'align-000.json')
        ai.build(self.db, self.inputs, self.index)
        self.change_index('DELETE FROM candidates')
        coverage = self.overview()['pending_alignments']['source_coverage']
        zh = next(row for row in coverage['editions'] if row['edition_id'] == 'zh')
        self.assertEqual(zh['source_dialogues'], 12)
        self.assertEqual(zh['proposal_dialogues'], 0)
        self.assertEqual(zh['matched_dialogues'], 0)
        self.assertEqual(zh['unmatched_dialogues'], 0)
        self.assertEqual(zh['unprocessed_dialogues'], 12)
        self.assertEqual(coverage['source_dialogues'], coverage['unprocessed_dialogues'])
        self.assertEqual(coverage['conflicting_candidate_dialogues'], 0)

    def test_different_book_japanese_version_fails_despite_valid_text_binding(self):
        self.build()
        self.change_index('UPDATE candidates SET ja_edition_id=?,japanese_json=?',
                          ('other', json.dumps([{'id': 'j2', 'text_sha256': ai.text_hash(self.texts['j2'])}])))
        self.assert_unavailable(self.overview())

    def test_source_and_index_remain_unchanged_after_repeated_reads(self):
        self.build()
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in (self.db, self.index)]
        store = self.store()
        for _ in range(2):
            coverage = self.overview(store)['pending_alignments']['source_coverage']
            self.assertEqual(coverage['covered_dialogues'], 1)
        self.assertEqual(before, [(path.read_bytes(), path.stat().st_mtime_ns) for path in (self.db, self.index)])

    def test_late_source_sidecar_refuses_coverage(self):
        self.build()
        store = self.store()
        _, database = store.database('sample')
        original = database.query

        def changed(sql, bindings=()):
            result = original(sql, bindings)
            if sql.startswith('SELECT edition_id,COUNT(*)'):
                Path(str(self.db) + '-journal').write_bytes(b'synthetic concurrent writer')
            return result

        with patch.object(database, 'query', side_effect=changed):
            pending = store.overlay('sample', 'alignments')
            with self.assertRaises(wb.WorkbenchError):
                pending.overview()

    def test_late_source_content_change_refuses_coverage(self):
        self.build()
        store = self.store()
        _, database = store.database('sample')
        original = database.query

        def changed(sql, bindings=()):
            result = original(sql, bindings)
            if sql.startswith('SELECT edition_id,COUNT(*)'):
                connection = sqlite3.connect(self.db)
                try:
                    connection.execute("UPDATE paragraphs SET text='Synthetic changed paragraph'")
                    connection.commit()
                finally:
                    connection.close()
            return result

        with patch.object(database, 'query', side_effect=changed):
            with self.assertRaises(wb.WorkbenchError):
                store.overlay('sample', 'alignments').overview()

    def test_missing_database_exposes_unavailable_coverage(self):
        self.db.unlink()
        overview = self.overview()
        self.assertFalse(overview['available'])
        self.assertEqual(overview['pending_alignments']['source_coverage'],
                         {'available': False, 'error': 'database_unavailable'})


if __name__ == '__main__':
    unittest.main()
