import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ltr.alignment import index as ai
from ltr import workbench as wb
from ltr.library.review_queue import fingerprint


class AlignmentIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.source = self.root/'source'; self.source.mkdir()
        self.db = self.source/'dialogue.sqlite'
        con = sqlite3.connect(self.db)
        con.executescript('''CREATE TABLE editions(id TEXT,title TEXT,language TEXT,book_key TEXT);
          INSERT INTO editions VALUES ('zh','Synthetic ZH','zh','book'),('zh2','Synthetic ZH2','zh','book'),
            ('ja','Synthetic JA','ja','book'),('other','Other','ja','other');
          CREATE TABLE dialogues(id TEXT,edition_id TEXT,ordinal INTEGER,text TEXT,speaker TEXT,addressee TEXT,context_hash TEXT);
          CREATE TABLE paragraphs(id TEXT,edition_id TEXT,ordinal INTEGER,text TEXT,locator_json TEXT);
          INSERT INTO paragraphs VALUES ('p','zh',0,'Synthetic Chinese quote','{}');''')
        self.texts = {'z1':'Synthetic Chinese quote','q1':'ZH2 quote','j1':'Synthetic Japanese quote',
                      'j2':'Other Japanese quote','j3':'Another Japanese quote'}
        dialogues = [('z1','zh',0,'Synthetic Chinese quote')]
        for number in range(2,13):
            self.texts['z%d'%number] = 'Chinese quote %d'%number
            dialogues.append(('z%d'%number,'zh',(number-1)*5,self.texts['z%d'%number]))
        dialogues += [('q1','zh2',0,'ZH2 quote'),('j1','ja',0,'Synthetic Japanese quote'),
                      ('j2','other',0,'Other Japanese quote'),('j3','ja',1,'Another Japanese quote')]
        con.executemany('INSERT INTO dialogues VALUES (?,?,?,?,?,?,?)',
                        [(i,e,o,t,'A','B',i) for (i,e,o,t) in dialogues])
        con.commit();con.close(); self.sha = fingerprint(self.db)
        self.inputs = self.root/'inputs';self.inputs.mkdir()
        self.reviews = self.root/'reviews';self.reviews.mkdir()
        self.index = self.root/'alignment.sqlite'
        self.row = {'chinese_id':'z1','japanese_ids':['j1'],'japanese':[{'id':'j1','text_sha256':ai.text_hash('Synthetic Japanese quote')}],
                    'chinese_text_sha256':ai.text_hash('Synthetic Chinese quote'),'status':'candidate','confidence':'medium',**ai.FLAGS}
        self.batch = {'schema':'haruhi-alignment-candidates-v1','database_sha256':self.sha,'book_key':'book',
                      'zh_edition_id':'zh','ja_edition_id':'ja','requested_model':'producer','served_model':'reported-producer','rows':[self.row]}
        self.write_batch()

    def write_batch(self):
        (self.inputs/'align-00000-00000.json').write_text(json.dumps(self.batch),encoding='utf-8')

    # -- reuse fixtures -------------------------------------------------
    def make_row(self, chinese_id, japanese_ids, status='candidate'):
        references = [{'id': jid, 'text_sha256': ai.text_hash(self.texts[jid])} for jid in japanese_ids]
        return {'chinese_id': chinese_id, 'japanese_ids': list(japanese_ids), 'japanese': references,
                'chinese_text_sha256': ai.text_hash(self.texts[chinese_id]), 'status': status,
                'confidence': 'medium', **ai.FLAGS}

    def document(self, rows, zh='zh', ja='ja'):
        return {'schema': 'haruhi-alignment-candidates-v1', 'database_sha256': self.sha, 'book_key': 'book',
                'zh_edition_id': zh, 'ja_edition_id': ja, 'requested_model': 'producer',
                'served_model': 'reported-producer', 'rows': rows}

    def write_file(self, doc, name):
        (self.inputs/name).write_text(json.dumps(doc), encoding='utf-8')

    def reset_inputs(self):
        for path in self.inputs.glob('align-*.json'):
            path.unlink()


    def store(self):
        store = wb.WorkbenchStore(wb.WorkbenchConfig([wb.CorpusConfig('sample','Sample',self.source,alignment_index=self.index)],self.root/'labels'))
        self.addCleanup(store.close);return store

    def review(self, number, decision):
        rid = ai.identity(self.sha,'zh','ja',self.row)
        doc = {'schema':'haruhi-alignment-model-review-v1','database_sha256':self.sha,'reviewer_requested_model':'reviewer'+str(number),
               'producer_requested_model':'producer','rows':[{'review_id':rid,'decision':decision,'basis':'semantic_match',
                    'zh_edition_id':'zh','ja_edition_id':'ja','chinese_id':self.row['chinese_id'],
                    'chinese_text_sha256':self.row['chinese_text_sha256'],'japanese_ids':self.row['japanese_ids'],
                    'japanese_hashes':[j['text_sha256'] for j in self.row['japanese']],**ai.FLAGS}]}
        (self.reviews/f'review-{number}.json').write_text(json.dumps(doc))

    def test_source_unchanged_and_counts_stay_separate(self):
        before = self.db.read_bytes();result = ai.build(self.db,self.inputs,self.index)
        self.assertTrue(result['source_unchanged']);self.assertEqual(self.db.read_bytes(),before)
        store = self.store();overview = store.overview()['corpora'][0]
        self.assertEqual(overview['counts']['alignments'],0)
        self.assertEqual(overview['pending_alignments']['total'],1)
        self.assertTrue(overview['capabilities']['alignment_candidates'])
        edition = next(r for r in store.editions('sample')['items'] if r['id']=='zh')
        self.assertEqual(edition['alignment_candidate_count'],1)
        item = store.dialogues({'corpus':'sample','source':'alignments'})['items'][0]
        detail = store.dialogue({'corpus':'sample','source':'alignments','id':item['id']})
        self.assertEqual(detail['alignment_candidate']['japanese'][0]['text'],'Synthetic Japanese quote')
        self.assertFalse(detail['alignment_candidate']['ocr_verified'])
        self.assertFalse(detail['entry']['reference_eligible'])

    def test_labels_export_and_payload_change_staleness(self):
        ai.build(self.db,self.inputs,self.index);store=self.store()
        rid=store.dialogues({'corpus':'sample','source':'alignments'})['items'][0]['id']
        saved=store.save_review({'corpus':'sample','source':'alignments','dialogue_id':rid,'verdict':'uncertain'})
        self.assertFalse(saved['corpus_updated']);self.assertEqual(saved['review']['source'],'alignments')
        self.assertFalse(any(saved['review']['flags'].values()))
        self.row['confidence']='low';self.write_batch();ai.build(self.db,self.inputs,self.index,refresh=True)
        self.assertTrue(store.review_export({'corpus':'sample'})['reviews'][0]['stale'])

    def test_model_disagreement_and_unmatched_are_preserved(self):
        self.review(0,'keep');self.review(1,'reject')
        result=ai.build(self.db,self.inputs,self.index,self.reviews)
        self.assertEqual(result['review_status'],{'conflict':1})
        with ai.read_index(self.index,self.sha) as con:
            with self.assertRaises(sqlite3.OperationalError):con.execute('DELETE FROM candidates')
        self.row.update(status='unmatched',japanese_ids=[],japanese=[]);self.write_batch()
        result=ai.build(self.db,self.inputs,self.index,refresh=True)
        self.assertEqual(result['unmatched'],1)
        self.assertEqual(result['reviewed'],0)

    def test_forged_source_hash_is_refused(self):
        self.row['chinese_text_sha256']='wrong';self.write_batch()
        with self.assertRaises(ValueError):ai.build(self.db,self.inputs,self.index)
        self.assertFalse(self.index.exists())

    def test_review_identity_cannot_hide_changed_source_bindings(self):
        self.review(0,'keep')
        path=self.reviews/'review-0.json';doc=json.loads(path.read_text())
        doc['rows'][0]['japanese_hashes']=['wrong'];path.write_text(json.dumps(doc))
        with self.assertRaises(ValueError):ai.build(self.db,self.inputs,self.index,self.reviews)
        self.assertFalse(self.index.exists())

    def test_alignment_config_requires_external_index(self):
        config=self.root/'config.json'
        config.write_text(json.dumps({'corpora':[{'id':'sample','path':str(self.source),
                                                'alignment_index':str(self.source/'index.sqlite')}]}))
        with self.assertRaises(wb.WorkbenchError):wb.WorkbenchConfig.load(config)

    def test_cross_edition_japanese_is_refused(self):
        self.row['japanese_ids']=['j2'];self.row['japanese']=[{'id':'j2','text_sha256':ai.text_hash('Other Japanese quote')}];self.write_batch()
        with self.assertRaises(ValueError):ai.build(self.db,self.inputs,self.index)

    def test_approval_flags_and_duplicate_proposals_are_refused(self):
        self.row['ocr_verified']=1;self.write_batch()
        with self.assertRaises(ValueError):ai.build(self.db,self.inputs,self.index)
        self.row['ocr_verified']=0;self.batch['rows']=[self.row,self.row];self.write_batch()
        with self.assertRaises(ValueError):ai.build(self.db,self.inputs,self.index)

    def test_unrelated_output_and_source_directory_are_protected(self):
        with self.assertRaises(ValueError):ai.build(self.db,self.inputs,self.source/'new.sqlite')
        self.index.write_bytes(b'preserve')
        with self.assertRaises((ValueError,sqlite3.Error)):ai.build(self.db,self.inputs,self.index,refresh=True)
        self.assertEqual(self.index.read_bytes(),b'preserve')

    def test_stale_index_does_not_hide_formal_database(self):
        ai.build(self.db,self.inputs,self.index)
        con=sqlite3.connect(self.db);con.execute("UPDATE dialogues SET text='Changed' WHERE id='z1'");con.commit();con.close()
        overview=self.store().overview()['corpora'][0]
        self.assertTrue(overview['available']);self.assertFalse(overview['pending_alignments']['available'])

    # -- exact ordered-target reuse diagnostics -------------------------
    def test_target_reuse_list_intersects_filters_and_pagination_read_only(self):
        self.reset_inputs()
        self.write_file(self.document([
            self.make_row('z1', ['j1']), self.make_row('z2', ['j1']),
            self.make_row('z3', ['j1', 'j3']), self.make_row('z4', ['j3', 'j1']),
            self.make_row('z5', [], status='unmatched'),
        ]), 'align-00000-zh.json')
        self.write_file(self.document([self.make_row('q1', ['j1'])], zh='zh2'), 'align-00001-zh2.json')
        self.review(0, 'keep')
        before = self.db.read_bytes()
        ai.build(self.db, self.inputs, self.index, self.reviews)
        index_before = self.index.read_bytes()
        store = self.store()
        self.assertIs(store.overview()['corpora'][0]['pending_alignments']['target_reuse_filter_available'], True)
        params = {'corpus': 'sample', 'source': 'alignments', 'target_reuse': '1'}
        first = store.dialogues({**params, 'limit': '1'})
        self.assertTrue(first['has_more'])
        self.assertEqual(first['items'][0]['ordinal'], 0)
        second = store.dialogues({**params, 'limit': '1', 'offset': '1'})
        self.assertFalse(second['has_more'])
        self.assertEqual(second['items'][0]['ordinal'], 5)
        # Group membership is established before text/status filters, so a single
        # matching member remains visible rather than losing its shared group.
        self.assertEqual([row['ordinal'] for row in store.dialogues(
            {**params, 'q': 'quote 2', 'edition': 'zh', 'speaker': 'A', 'status': 'unreviewed'})['items']], [5])
        self.assertEqual([row['ordinal'] for row in store.dialogues({**params, 'status': 'keep'})['items']], [0])
        self.assertEqual(store.dialogues({**params, 'edition': 'zh2'})['items'], [])
        self.assertEqual(store.dialogues({**params, 'q': 'missing'})['items'], [])
        self.assertEqual(len(store.dialogues({**params, 'target_reuse': '0'})['items']), 6)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.index.read_bytes(), index_before)
        for value in [True, 1, 'true', '1 OR 1=1', None]:
            with self.assertRaises(wb.WorkbenchError) as caught:
                store.dialogues({**params, 'target_reuse': value})
            self.assertEqual(caught.exception.code, 'invalid_alignment_target_reuse')

    def test_target_reuse_groups_span_shared_japanese_tuple(self):
        self.reset_inputs()
        rows = [self.make_row('z1',['j1']), self.make_row('z2',['j1']),
                self.make_row('z3',['j3']), self.make_row('z4',[],status='unmatched')]
        self.write_file(self.document(rows), 'align-00000-00000.json')
        result = ai.build(self.db, self.inputs, self.index)
        self.assertEqual(result['matched'], 3)
        self.assertEqual(result['unmatched'], 1)
        reuse = result['target_reuse']
        self.assertEqual(reuse['groups_with_multiple_chinese'], 1)
        self.assertEqual(reuse['affected_chinese_records'], 2)
        self.assertEqual(reuse['largest_group'], 2)
        self.assertEqual(reuse['largest_chinese_ordinal_span'], 5)
        overview = self.store().overview()['corpora'][0]
        self.assertEqual(overview['pending_alignments']['target_reuse']['largest_group'], 2)

    def test_target_reuse_does_not_merge_across_editions_or_tuples(self):
        self.reset_inputs()
        rows = [self.make_row('z1',['j1']), self.make_row('z2',['j1']), self.make_row('z3',['j3'])]
        self.write_file(self.document(rows), 'align-00000-zh.json')
        self.write_file(self.document([self.make_row('q1',['j1'])], zh='zh2'), 'align-00001-zh2.json')
        result = ai.build(self.db, self.inputs, self.index)
        reuse = result['target_reuse']
        self.assertEqual(reuse['groups_with_multiple_chinese'], 1)
        self.assertEqual(reuse['largest_group'], 2)  # the zh2 record must not merge into the zh group
        self.assertEqual(reuse['affected_chinese_records'], 2)

    def test_target_reuse_binds_canonical_ordered_tuple(self):
        self.reset_inputs()
        first = self.make_row('z1',['j1'])
        second = self.make_row('z2',['j1'])
        # Same ordered tuple, but the proposal file lists the keys in a different order.
        second['japanese'] = [{'text_sha256': ai.text_hash(self.texts['j1']), 'id': 'j1'}]
        self.write_file(self.document([first, second]), 'align-00000-00000.json')
        result = ai.build(self.db, self.inputs, self.index)
        self.assertEqual(result['target_reuse']['largest_group'], 2)
        self.assertEqual(result['target_reuse']['groups_with_multiple_chinese'], 1)

    def test_target_reuse_detail_preview_cap_and_self_exclusion(self):
        self.reset_inputs()
        chinese = ['z%d'%number for number in range(1,13)]
        self.write_file(self.document([self.make_row(cid,['j1']) for cid in chinese]), 'align-00000-00000.json')
        ai.build(self.db, self.inputs, self.index)
        store = self.store()
        items = store.dialogues({'corpus':'sample','source':'alignments','limit':50})['items']
        self.assertEqual(len(items), 12)
        anchor = next(item for item in items if item['ordinal'] == 0)
        detail = store.dialogue({'corpus':'sample','source':'alignments','id':anchor['id']})
        reuse = detail['alignment_candidate']['target_reuse']
        self.assertEqual(reuse['chinese_count'], 12)
        self.assertEqual(reuse['total'], 12)
        self.assertEqual(reuse['preview_limit'], 10)
        self.assertEqual((reuse['ordinal_min'], reuse['ordinal_max'], reuse['ordinal_span']), (0, 55, 55))
        self.assertEqual(len(reuse['examples']), 10)
        self.assertTrue(reuse['has_more'])
        example_ids = [example['id'] for example in reuse['examples']]
        self.assertNotIn(anchor['id'], example_ids)  # self is excluded from links
        for example in reuse['examples']:
            self.assertRegex(example['id'], r'^alignment:[0-9a-f]{16}$')
        linked = store.dialogue({'corpus':'sample','source':'alignments','id':example_ids[0]})
        self.assertEqual(linked['item']['source'], 'alignments')
        self.assertFalse(linked['alignment_candidate']['reference_eligible'])
        self.assertFalse(linked['alignment_candidate']['ocr_verified'])

    def test_target_reuse_does_not_merge_reordered_japanese_ids(self):
        self.reset_inputs()
        self.write_file(self.document([
            self.make_row('z1', ['j1', 'j3']),
            self.make_row('z2', ['j3', 'j1']),
        ]), 'align-00000-00000.json')
        result = ai.build(self.db, self.inputs, self.index)
        self.assertEqual(result['target_reuse']['groups_with_multiple_chinese'], 0)
        self.assertEqual(result['target_reuse']['largest_group'], 1)

    def test_target_reuse_single_group_and_unmatched_detail(self):
        self.reset_inputs()
        self.write_file(self.document([self.make_row('z3',['j3'])]), 'align-00000-00000.json')
        ai.build(self.db, self.inputs, self.index)
        store = self.store()
        item = store.dialogues({'corpus':'sample','source':'alignments'})['items'][0]
        reuse = store.dialogue({'corpus':'sample','source':'alignments','id':item['id']})['alignment_candidate']['target_reuse']
        self.assertEqual(reuse['chinese_count'], 1)
        self.assertEqual(reuse['examples'], [])
        self.assertEqual(reuse['ordinal_span'], 0)
        self.assertFalse(reuse['has_more'])
        # An unmatched proposal has no applicable reuse group.
        self.reset_inputs()
        self.write_file(self.document([self.make_row('z3',[],status='unmatched')]), 'align-00000-00000.json')
        ai.build(self.db, self.inputs, self.index, refresh=True)
        item = store.dialogues({'corpus':'sample','source':'alignments'})['items'][0]
        detail = store.dialogue({'corpus':'sample','source':'alignments','id':item['id']})
        self.assertIsNone(detail['alignment_candidate']['target_reuse'])

    def test_target_reuse_preserves_flags_and_read_only_source(self):
        self.reset_inputs()
        self.write_file(self.document([self.make_row('z1',['j1']), self.make_row('z2',['j1'])]), 'align-00000-00000.json')
        before = self.db.read_bytes()
        ai.build(self.db, self.inputs, self.index)
        self.assertEqual(self.db.read_bytes(), before)
        with ai.read_index(self.index, self.sha) as con:
            flags = con.execute('SELECT SUM(accepted),SUM(human_confirmed),SUM(semantic_verified),'
                                'SUM(reference_eligible),SUM(ocr_verified) FROM candidates').fetchone()
            self.assertEqual(tuple(flags), (0, 0, 0, 0, 0))
            with self.assertRaises(sqlite3.OperationalError):
                con.execute('UPDATE candidates SET accepted=1')


if __name__ == '__main__':unittest.main()
