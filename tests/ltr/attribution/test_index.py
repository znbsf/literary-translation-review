"""Synthetic, source-free tests for the private attribution suggestion index."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ltr.attribution import index as ai
from ltr import workbench as wb
from ltr.attribution.review import load_targets, validate_suggestions
from ltr.library.review_queue import fingerprint

SCHEMA = """
CREATE TABLE editions(id TEXT,title TEXT,language TEXT,book_key TEXT);
CREATE TABLE paragraphs(id TEXT,edition_id TEXT,ordinal INTEGER,text TEXT);
CREATE TABLE dialogues(id TEXT,edition_id TEXT,ordinal INTEGER,text TEXT,start_para INTEGER,
    end_para INTEGER,speaker TEXT,interlocutor TEXT,attribution_status TEXT);
CREATE TABLE attribution_candidates(dialogue_id TEXT,edition_id TEXT,speaker_surface TEXT,
    speaker_canonical TEXT,addressee_surface TEXT,addressee_canonical TEXT);
CREATE TABLE corpus_completion_quote(quote_id TEXT,edition_id TEXT,kind TEXT,speaker_id TEXT,
    addressee_id TEXT,speaker_status TEXT,addressee_status TEXT,status TEXT,requires_review INTEGER);
"""
PARAGRAPHS = [('p0', 'e1', 0, 'Narration zero.'), ('p1', 'e1', 1, 'Alice called out loudly.'),
              ('p2', 'e1', 2, 'The room was silent.'), ('p3', 'e1', 3, 'Bob answered softly.'),
              ('p4', 'e1', 4, 'End of scene.'),
              ('p0', 'e2', 0, 'e2 zero.'), ('p1', 'e2', 1, 'Someone else spoke.'),
              ('p2', 'e2', 2, 'e2 two.'), ('p3', 'e2', 3, 'e2 three.'), ('p4', 'e2', 4, 'e2 four.')]
DIALOGUES = [('d1', 'e1', 0, 'LINE_ONE_SENTINEL', 1, 2, 'Alice', 'Bob', 'unknown'),
             ('d2', 'e1', 1, 'LINE_TWO_SENTINEL', 2, 3, 'Bob', 'Alice', 'unknown'),
             ('dx', 'e2', 0, 'OTHER_EDITION_SENTINEL', 1, 2, 'Carol', 'Dave', 'unknown')]
CANDIDATES = [('d1', 'e1', 'Alice', 'unknown', 'Bob', ''),
              ('d2', 'e1', 'Bob', '', 'Alice', ''),
              ('dx', 'e2', 'Carol', 'unknown', 'Dave', '')]


class AttributionIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.db = self.source / 'dialogue.sqlite'
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        con.executemany("INSERT INTO editions VALUES (?,?,?,?)",
                        [('e1', 'One', 'zh', 'book'), ('e2', 'Two', 'zh', 'book')])
        con.executemany("INSERT INTO paragraphs VALUES (?,?,?,?)", PARAGRAPHS)
        con.executemany("INSERT INTO dialogues VALUES (?,?,?,?,?,?,?,?,?)", DIALOGUES)
        con.executemany("INSERT INTO attribution_candidates VALUES (?,?,?,?,?,?)", CANDIDATES)
        con.commit()
        con.close()
        self.sha = fingerprint(self.db)
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()
        self.index = self.root / 'attribution-index.sqlite'

    # -- fixtures ------------------------------------------------------
    def targets(self):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            return {t['dialogue_id']: t for t in load_targets(con, 'haruhi_unknown', 'e1')}
        finally:
            con.close()

    def loc(self, edition, paragraph_id, start, end):
        con = sqlite3.connect(self.db)
        ordinal, text = con.execute(
            "SELECT ordinal,text FROM paragraphs WHERE edition_id=? AND id=?",
            (edition, paragraph_id)).fetchone()
        con.close()
        return {'edition_id': edition, 'paragraph_id': paragraph_id, 'ordinal': ordinal,
                'start': start, 'end': end,
                'paragraph_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}

    def row(self, dialogue_id, speaker, addressee, kind, basis, evidence, edition='e1'):
        target = self.targets()[dialogue_id]
        return {'suggestion_id': ai.identity(self.sha, 'haruhi_unknown', edition, dialogue_id,
                                             target['context_sha256']),
                'dialogue_id': dialogue_id, 'edition_id': edition, 'ordinal': target['ordinal'],
                'context_sha256': target['context_sha256'],
                'context_window': target['context_window'],
                'dialogue_text_sha256': target['dialogue_text_sha256'],
                'source_candidate_sha256': target['source_candidate_sha256'],
                'speaker': speaker, 'addressee': addressee, 'kind': kind, 'basis': basis,
                'evidence': evidence, **ai.FLAGS}

    def write(self, name, rows, model='m1', served='served-m1', kind='haruhi_unknown', edition='e1'):
        document = {'schema': 'corpus-attribution-suggestions-v1', 'database_sha256': self.sha,
                    'kind': kind, 'edition_id': edition, 'model': model, 'served_model': served,
                    'input_sha256': 'input', 'session_id': 'session', 'rows': rows}
        (self.inputs / name).write_text(json.dumps(document), encoding='utf-8')

    def suggested(self):
        return self.row('d1', 'Alice', 'Bob', 'spoken', 'explicit_name', [self.loc('e1', 'p1', 0, 16)])

    def test_model_provenance_must_be_present(self):
        self.reject([self.suggested()],model=None)

    def test_long_quote_display_is_bounded_and_declared(self):
        con=sqlite3.connect(self.db);con.execute("UPDATE dialogues SET text=? WHERE id='d1'",['X'*5000]);con.commit();con.close()
        self.sha=fingerprint(self.db)
        self.write('suggestion-000.json',[self.suggested()]);ai.build(self.db,self.inputs,self.index)
        item=self.store().dialogues({'corpus':'sample','source':'identities'})['items'][0]
        self.assertLessEqual(len(item['text']),wb.MAX_DIALOGUE_TEXT_CHARS+1)
        self.assertTrue(item['text_truncated']);self.assertEqual(item['text_total_chars'],5000)

    def test_multiple_campaign_inputs_keep_distinct_records_and_deduplicate_copies(self):
        self.write('suggestion-000.json',[self.suggested()])
        self.write('suggestion-001.json',[self.unresolved()])
        second=self.root/'second';second.mkdir()
        (second/'suggestion-001.json').write_bytes((self.inputs/'suggestion-001.json').read_bytes())
        result=ai.build(self.db,[self.inputs,second],self.index)
        self.assertEqual(result['total'],2)
        self.assertEqual(result['observations'],2)

    def test_index_display_cannot_diverge_from_stored_observations(self):
        self.write('suggestion-000.json',[self.suggested()])
        ai.build(self.db,self.inputs,self.index)
        con=sqlite3.connect(self.index);con.execute("UPDATE records SET speaker='Other'");con.commit();con.close()
        with self.assertRaises(wb.WorkbenchError):self.store().dialogues({'corpus':'sample','source':'identities'})

    def unresolved(self):
        return self.row('d2', None, None, 'uncertain', 'insufficient_context', [])

    def store(self):
        store = wb.WorkbenchStore(wb.WorkbenchConfig([
            wb.CorpusConfig('sample', 'Sample', self.source, attribution_index=self.index)],
            self.root / 'labels'))
        self.addCleanup(store.close)
        return store

    def reject(self, rows, **overrides):
        self.write('suggestion-000.json', rows, **overrides)
        with self.assertRaises(ValueError):
            ai.build(self.db, self.inputs, self.index)
        (self.inputs / 'suggestion-000.json').unlink()
        self.assertFalse(self.index.exists())

    # -- tests ---------------------------------------------------------
    def test_overlapping_occurrences_are_not_unique_evidence(self):
        for source_text, evidence in [('aaaa', 'aaa'), ('哈哈哈哈', '哈哈哈')]:
            with self.subTest(source_text=source_text):
                con = sqlite3.connect(self.db)
                con.execute("UPDATE paragraphs SET text=? WHERE edition_id='e1' AND id='p1'", (source_text,))
                con.commit()
                con.close()
                target = self.targets()['d1']
                row = {'dialogue_id':'d1', 'speaker':'Alice', 'addressee':'Bob',
                       'kind':'spoken', 'basis':'explicit_name',
                       'evidence':[{'paragraph_id':'p1', 'text':evidence}]}
                with self.assertRaisesRegex(ValueError, '^non_unique_evidence_span$'):
                    validate_suggestions({'suggestions':[row]}, [target], ['Alice', 'Bob'])

    def test_unique_multichar_evidence_and_distinct_overlapping_spans_remain_valid(self):
        con = sqlite3.connect(self.db)
        con.execute("UPDATE paragraphs SET text='ZabcdeY' WHERE edition_id='e1' AND id='p1'")
        con.commit()
        con.close()
        target = self.targets()['d1']
        for fragments, offsets in [(['abc'], [(1, 4)]), (['abc', 'bcde'], [(1, 4), (2, 6)])]:
            with self.subTest(fragments=fragments):
                row = {'dialogue_id':'d1', 'speaker':'Alice', 'addressee':'Bob',
                       'kind':'spoken', 'basis':'explicit_name',
                       'evidence':[{'paragraph_id':'p1', 'text':text} for text in fragments]}
                result = validate_suggestions({'suggestions':[row]}, [target], ['Alice', 'Bob'])[0]
                self.assertEqual([(e['start'], e['end']) for e in result['evidence']], offsets)
                self.assertTrue(all(e['paragraph_sha256'] == hashlib.sha256(b'ZabcdeY').hexdigest()
                                    for e in result['evidence']))

    def test_index_revalidation_rejects_legacy_overlapping_evidence(self):
        for source_text, length in [('aaaa', 3), ('哈哈哈哈', 3)]:
            with self.subTest(source_text=source_text):
                con = sqlite3.connect(self.db)
                con.execute("UPDATE paragraphs SET text=? WHERE edition_id='e1' AND id='p1'", (source_text,))
                con.commit()
                con.close()
                self.sha = fingerprint(self.db)
                # Model the exact first-match locator formerly accepted by str.count.
                legacy = self.row('d1', 'Alice', 'Bob', 'spoken', 'explicit_name',
                                  [self.loc('e1', 'p1', 0, length)])
                self.write('suggestion-legacy.json', [legacy])
                before = self.db.read_bytes()
                with self.assertRaisesRegex(ValueError, '^non_unique_evidence_span$'):
                    ai.build(self.db, self.inputs, self.index)
                self.assertFalse(self.index.exists())
                self.assertEqual(self.db.read_bytes(), before)

    def test_build_reads_source_only_and_aggregates(self):
        self.write('suggestion-000.json', [self.suggested(), self.unresolved()])
        before = self.db.read_bytes()
        result = ai.build(self.db, self.inputs, self.index)
        self.assertEqual(before, self.db.read_bytes())
        self.assertTrue(result['source_unchanged'])
        self.assertFalse(result['corpus_updated'])
        self.assertEqual(result['schema'], ai.SCHEMA)
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['with_speaker'], 1)
        self.assertEqual(result['observations'], 2)
        self.assertEqual(result['statuses'], {'suggested': 1, 'unresolved': 1})
        with ai.read_index(self.index, self.sha) as con:
            self.assertEqual(dict(con.execute('SELECT key,value FROM metadata')),
                             {'schema': ai.SCHEMA, 'source_sha256': self.sha})
            with self.assertRaises(sqlite3.OperationalError):
                con.execute('DELETE FROM records')

    def test_overlay_list_detail_search_and_filters(self):
        self.write('suggestion-000.json', [self.suggested(), self.unresolved()])
        ai.build(self.db, self.inputs, self.index)
        store = self.store()
        overview = store.overview()['corpora'][0]
        self.assertTrue(overview['capabilities']['identities'])
        self.assertEqual(overview['pending_identities']['total'], 2)
        self.assertEqual(overview['pending_identities']['with_speaker'], 1)
        self.assertEqual(overview['pending_identities']['coverage']['haruhi_unknown'],
                         {'source_rows':3,'with_results':2,'without_results':1})
        edition = next(e for e in store.editions('sample')['items'] if e['id'] == 'e1')
        self.assertEqual(edition['identity_candidate_count'], 2)
        listing = store.dialogues({'corpus': 'sample', 'source': 'identities'})
        self.assertEqual(listing['source'], 'identities')
        self.assertEqual(len(listing['items']), 2)
        item = next(i for i in listing['items'] if i['speaker'] == 'Alice')
        self.assertEqual(item['text'], 'LINE_ONE_SENTINEL')
        self.assertEqual(item['attribution_status'], 'candidate')
        self.assertEqual(item['identity_status'], 'suggested')
        self.assertFalse(item['reference_eligible'])
        self.assertEqual(item['source'], 'identities')
        self.assertEqual(len(store.dialogues({'corpus': 'sample', 'source': 'identities',
                                              'q': 'LINE_ONE'})['items']), 1)
        self.assertEqual(len(store.dialogues({'corpus': 'sample', 'source': 'identities',
                                              'edition': 'e1'})['items']), 2)
        self.assertEqual(len(store.dialogues({'corpus': 'sample', 'source': 'identities',
                                              'status': 'suggested'})['items']), 1)
        with self.assertRaises(wb.WorkbenchError):
            store.dialogues({'corpus': 'sample', 'source': 'identities', 'status': 'bogus'})
        detail = store.dialogue({'corpus': 'sample', 'source': 'identities', 'id': item['id']})
        self.assertEqual(detail['item']['id'], item['id'])
        self.assertEqual(detail['context_anchor']['mode'], 'continuous_window')
        self.assertTrue(detail['context'])
        self.assertFalse(detail['entry']['reference_eligible'])
        suggestions = detail['identity_suggestions']
        self.assertEqual(suggestions['status'], 'suggested')
        self.assertEqual(suggestions['observations'][0]['evidence'][0]['text'], 'Alice called out')
        self.assertLessEqual(len(suggestions['observations'][0]['evidence'][0]['text']), 200)

    def test_any_speaker_filter_matches_conflicts(self):
        self.write('suggestion-000.json', [self.suggested()], model='m1')
        self.write('suggestion-001.json',
                   [self.row('d1', 'Bob', 'Alice', 'spoken', 'explicit_name', [self.loc('e1', 'p3', 0, 12)])],
                   model='m2')
        result = ai.build(self.db, self.inputs, self.index)
        self.assertEqual(result['statuses'], {'conflict': 1})
        store = self.store()
        for name in ('Alice', 'Bob'):
            items = store.dialogues({'corpus': 'sample', 'source': 'identities', 'speaker': name})['items']
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]['speaker'], '')
            self.assertEqual(items[0]['identity_status'], 'conflict')
        detail = store.dialogue({'corpus': 'sample', 'source': 'identities', 'id': items[0]['id']})
        self.assertEqual(len(detail['identity_suggestions']['observations']), 2)

    def test_duplicate_observations_dedup_conflicts_retained(self):
        duplicate = [self.suggested()]
        self.write('suggestion-000.json', duplicate, model='m1')
        self.write('suggestion-001.json', duplicate, model='m1')
        self.write('suggestion-002.json',
                   [self.row('d1', 'Bob', 'Alice', 'spoken', 'explicit_name', [self.loc('e1', 'p3', 0, 12)])],
                   model='m2')
        result = ai.build(self.db, self.inputs, self.index)
        self.assertEqual(result['observations'], 2)
        self.assertEqual(result['statuses'], {'conflict': 1})
        store = self.store()
        identity = store.dialogues({'corpus': 'sample', 'source': 'identities'})['items'][0]['id']
        detail = store.dialogue({'corpus': 'sample', 'source': 'identities', 'id': identity})
        self.assertEqual(len(detail['identity_suggestions']['observations']), 2)

    def test_bad_bounds_hash_and_cross_edition_are_refused(self):
        self.reject([self.row('d1', 'Alice', 'Bob', 'spoken', 'explicit_name',
                              [dict(self.loc('e1', 'p1', 0, 16), end=999)])])
        self.reject([self.row('d1', 'Alice', 'Bob', 'spoken', 'explicit_name',
                              [dict(self.loc('e1', 'p1', 0, 16), paragraph_sha256='wrong')])])
        self.reject([self.row('d1', 'Alice', 'Bob', 'spoken', 'explicit_name',
                              [self.loc('e2', 'p1', 0, 5)])])

    def test_malformed_flags_identity_and_empty_are_refused(self):
        bad = self.suggested()
        bad['extra'] = 1
        self.reject([bad])
        promoted = self.suggested()
        promoted['reference_eligible'] = 1
        self.reject([promoted])
        wrong = self.suggested()
        wrong['suggestion_id'] = '0' * 16
        self.reject([wrong])
        self.reject([self.row('d1', 'Zed', 'Bob', 'spoken', 'explicit_name',
                              [self.loc('e1', 'p1', 0, 16)])])
        with self.assertRaises(ValueError):
            ai.build(self.db, self.inputs, self.index)
        self.assertFalse(self.index.exists())

    def test_labels_are_separate_and_export_source(self):
        self.write('suggestion-000.json', [self.suggested()])
        ai.build(self.db, self.inputs, self.index)
        store = self.store()
        identity = store.dialogues({'corpus': 'sample', 'source': 'identities'})['items'][0]['id']
        before = self.db.read_bytes()
        saved = store.save_review({'corpus': 'sample', 'source': 'identities',
                                   'dialogue_id': identity, 'verdict': 'uncertain'})
        self.assertTrue(saved['saved'])
        self.assertFalse(saved['corpus_updated'])
        self.assertEqual(saved['review']['source'], 'identities')
        self.assertFalse(any(saved['review']['flags'].values()))
        self.assertEqual(before, self.db.read_bytes())
        exported = store.review_export({'corpus': 'sample'})['reviews']
        self.assertEqual(exported[0]['source'], 'identities')
        self.assertFalse(exported[0]['stale'])

    def test_changed_suggestion_marks_labels_stale(self):
        self.write('suggestion-000.json', [self.suggested()])
        ai.build(self.db, self.inputs, self.index)
        store = self.store()
        identity = store.dialogues({'corpus': 'sample', 'source': 'identities'})['items'][0]['id']
        store.save_review({'corpus': 'sample', 'source': 'identities',
                           'dialogue_id': identity, 'verdict': 'uncertain'})
        self.write('suggestion-000.json',
                   [self.row('d1', 'Bob', 'Alice', 'spoken', 'explicit_name', [self.loc('e1', 'p3', 0, 12)])])
        ai.build(self.db, self.inputs, self.index, refresh=True)
        self.assertTrue(store.review_export({'corpus': 'sample'})['reviews'][0]['stale'])

    def test_stale_index_does_not_hide_formal_stats(self):
        self.write('suggestion-000.json', [self.suggested()])
        ai.build(self.db, self.inputs, self.index)
        con = sqlite3.connect(self.db)
        con.execute("UPDATE paragraphs SET text='Changed' WHERE id='p1'")
        con.commit()
        con.close()
        store = self.store()
        overview = store.overview()['corpora'][0]
        self.assertTrue(overview['available'])
        self.assertEqual(overview['counts']['dialogues'], 3)
        with self.assertRaises(wb.WorkbenchError):
            store.dialogues({'corpus': 'sample', 'source': 'identities'})

    def test_unrelated_output_and_source_directory_protected(self):
        self.write('suggestion-000.json', [self.suggested()])
        with self.assertRaises(ValueError):
            ai.build(self.db, self.inputs, self.source / 'new.sqlite')
        self.index.write_bytes(b'preserve')
        with self.assertRaises((ValueError, sqlite3.Error)):
            ai.build(self.db, self.inputs, self.index, refresh=True)
        self.assertEqual(self.index.read_bytes(), b'preserve')

    def test_validate_record_rechecks_one_record(self):
        self.write('suggestion-000.json', [self.suggested()])
        ai.build(self.db, self.inputs, self.index)
        with ai.read_index(self.index, self.sha) as index:
            row = index.execute('SELECT * FROM records').fetchone()
        source = sqlite3.connect(self.db)
        source.row_factory = sqlite3.Row
        try:
            result = ai.validate_record(source, row['selection_kind'], row['edition_id'], row, self.sha)
        finally:
            source.close()
        self.assertEqual(result['target']['dialogue_id'], 'd1')
        self.assertEqual(result['observations'][0]['speaker'], 'Alice')


if __name__ == '__main__':
    unittest.main()
