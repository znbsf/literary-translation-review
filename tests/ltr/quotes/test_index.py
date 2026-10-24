import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ltr.quotes import index as ci
from ltr import workbench as wb
from ltr.library.review_queue import fingerprint


class CandidateIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'source'
        self.source.mkdir()
        self.db = self.source/'dialogue.sqlite'
        con=sqlite3.connect(self.db)
        con.executescript("""
          CREATE TABLE editions(id TEXT PRIMARY KEY,title TEXT,language TEXT,book_key TEXT,origin_kind TEXT);
          INSERT INTO editions VALUES ('e1','Synthetic volume','zh','synthetic','synthetic'),('e2','Other edition','zh','other','synthetic');
          CREATE TABLE paragraphs(edition_id TEXT,id TEXT,ordinal INTEGER,text TEXT,locator_json TEXT);
          INSERT INTO paragraphs VALUES ('e1','p1',0,'Synthetic speech.','{}'),('e2','p1',0,'Different source.','{}');
          CREATE TABLE dialogues(id TEXT,edition_id TEXT,ordinal INTEGER,text TEXT);
        """)
        con.close()
        self.sha=fingerprint(self.db)
        self.inputs=self.root/'inputs';self.inputs.mkdir()
        self.reviews=self.root/'model-reviews';self.reviews.mkdir()
        self.index=self.root/'candidate-index.sqlite'
        self.row={'paragraph_id':'p1','ordinal':0,'start':0,'end':16,'text':'Synthetic speech',
                  'speaker':'A','addressee':None,'kind':'spoken',
                  'paragraph_sha256':hashlib.sha256(b'Synthetic speech.').hexdigest(),**ci.FLAGS}
        self.batch={'schema':'unquoted-dialogue-candidates-v1','database_sha256':self.sha,
                    'edition_id':'e1','model':'extractor','candidates':[self.row]}
        self.write_batch()

    def write_batch(self):
        (self.inputs/'batch-00000-00023.json').write_text(json.dumps(self.batch),encoding='utf-8')

    def store(self):
        store=wb.WorkbenchStore(wb.WorkbenchConfig([
            wb.CorpusConfig('sample','Sample',self.source,candidate_index=self.index)],self.root/'labels'))
        self.addCleanup(store.close)
        return store

    def source_span(self, source, text):
        con = sqlite3.connect(self.db)
        con.execute("UPDATE paragraphs SET text=? WHERE edition_id='e1' AND id='p1'", (source,))
        con.commit()
        con.close()
        self.sha = fingerprint(self.db)
        self.row.update(start=0, end=len(text), text=text,
                        paragraph_sha256=hashlib.sha256(source.encode('utf-8')).hexdigest())
        self.batch['database_sha256'] = self.sha
        self.write_batch()

    def test_overlapping_spans_refuse_index_publication_without_source_changes(self):
        for source, text in [('aaaa', 'aaa'), ('哈哈哈哈', '哈哈哈')]:
            with self.subTest(source=source):
                self.source_span(source, text)
                before = self.db.read_bytes()
                with self.assertRaisesRegex(ValueError, '^candidate_span_mismatch$'):
                    ci.build(self.db, self.inputs, self.index)
                self.assertFalse(self.index.exists())
                self.assertFalse(list(self.root.glob('.candidate-index-*')))
                self.assertEqual(self.db.read_bytes(), before)

    def test_legacy_first_match_index_cannot_be_displayed_as_source_bound(self):
        for source, text in [('aaaa', 'aaa'), ('哈哈哈哈', '哈哈哈')]:
            with self.subTest(source=source):
                self.source_span(source, text)
                self.assertEqual(source.count(text), 1)  # The historical non-overlapping guard passed.
                # Construct a legacy index using its former first-match acceptance;
                # the workbench uses the real, unpatched validator afterwards.
                with patch.object(ci, 'unique_source_span_start', return_value=0):
                    ci.build(self.db, self.inputs, self.index)
                identity = ci.candidate_id('e1', self.row, self.sha)
                store = self.store()
                before = self.db.read_bytes()
                try:
                    with self.assertRaises(wb.WorkbenchError) as caught:
                        store.dialogue({'corpus':'sample', 'source':'candidates', 'id':'candidate:'+identity})
                    self.assertEqual(caught.exception.status, 409)
                    self.assertEqual(caught.exception.code, 'candidate_source_mismatch')
                    self.assertEqual(self.db.read_bytes(), before)
                finally:
                    store.close()
                self.index.unlink()

    def test_review_rejects_overlapping_artifact_before_output_or_model(self):
        from ltr.quotes import review
        for number, (source, text) in enumerate([('aaaa', 'aaa'), ('哈哈哈哈', '哈哈哈')]):
            with self.subTest(source=source):
                self.source_span(source, text)
                output = self.root / f'refused-review-{number}'
                before = self.db.read_bytes()
                with patch.object(review, 'request_model') as sender, \
                        patch('sys.stdout', new_callable=io.StringIO) as stdout, \
                        patch('sys.stderr', new_callable=io.StringIO) as stderr:
                    with self.assertRaises(SystemExit) as caught:
                        review.main(['--db',str(self.db), '--input',str(self.inputs), '--out',str(output),
                                     '--model','synthetic', '--max-calls','1', '--execute'])
                self.assertEqual(caught.exception.code, 1)
                sender.assert_not_called()
                self.assertFalse(output.exists())
                self.assertEqual(stdout.getvalue(), '')
                self.assertEqual(stderr.getvalue(), 'candidate review refused: changed_source_span\n')
                self.assertEqual(self.db.read_bytes(), before)

    def test_sidecar_does_not_change_source_and_binds_edition(self):
        before=self.db.read_bytes()
        result=ci.build(self.db,self.inputs,self.index)
        self.assertEqual(before,self.db.read_bytes())
        self.assertEqual(result['total'],1)
        self.assertTrue(result['source_unchanged'])
        with ci.read_index(self.index,self.sha) as con:
            self.assertEqual(con.execute('select edition_id,text from candidates').fetchone()[0],'e1')
            with self.assertRaises(sqlite3.OperationalError):con.execute('delete from candidates')

    def test_overlay_is_visible_without_changing_formal_counts(self):
        ci.build(self.db,self.inputs,self.index)
        store=self.store()
        overview=store.overview()['corpora'][0]
        self.assertEqual(overview['counts']['dialogues'],0)
        self.assertEqual(overview['pending_candidates']['total'],1)
        self.assertTrue(overview['capabilities']['candidates'])
        editions=store.editions('sample')['items']
        self.assertEqual(next(r for r in editions if r['id']=='e1')['candidate_count'],1)
        listing=store.dialogues({'corpus':'sample','source':'candidates'})
        self.assertEqual(len(listing['items']),1)
        identity=listing['items'][0]['id']
        detail=store.dialogue({'corpus':'sample','source':'candidates','id':identity})
        self.assertEqual(detail['context'][0]['text'],'Synthetic speech.')
        self.assertFalse(detail['item']['reference_eligible'])
        self.assertEqual(detail['item']['source'],'candidates')

    def test_overlay_review_is_separate_and_unaccepted(self):
        ci.build(self.db,self.inputs,self.index)
        store=self.store();before=self.db.read_bytes()
        identity=store.dialogues({'corpus':'sample','source':'candidates'})['items'][0]['id']
        saved=store.save_review({'corpus':'sample','source':'candidates','dialogue_id':identity,'verdict':'correct'})
        self.assertTrue(saved['saved']);self.assertFalse(saved['corpus_updated'])
        self.assertEqual(saved['review']['source'],'candidates')
        self.assertFalse(any(saved['review']['flags'].values()))
        self.assertEqual(before,self.db.read_bytes())
        self.assertEqual(store.review_export({'corpus':'sample'})['reviews'][0]['source'],'candidates')

    def test_bad_candidate_flags_are_rejected(self):
        self.row['reference_eligible']=1;self.write_batch()
        with self.assertRaises(ValueError):ci.build(self.db,self.inputs,self.index)
        self.assertFalse(self.index.exists())

    def test_review_small_batch_budget_leaves_remainder_unreviewed(self):
        from ltr.quotes.review import main
        self.batch['candidates']=[dict(self.row,start=start,end=end,text='Synthetic speech.'[start:end])
                                  for start,end in [(0,9),(10,16),(16,17)]]
        self.write_batch()
        before=self.db.read_bytes()
        def model(*args,**kwargs):
            items=args[3]['items']
            return {'payload':{'reviews':[{'candidate_id':r['candidate_id'],'decision':'uncertain',
                    'kind':'uncertain','basis':'insufficient_context'} for r in items]},'usage':{},'served_model':'test-model'}
        output=self.root/'small-review'
        with patch.dict(os.environ,ANTHROPIC_BASE_URL='http://example.invalid',ANTHROPIC_AUTH_TOKEN='synthetic'), patch('ltr.quotes.review.request_model',side_effect=model) as call, patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(main(['--db',str(self.db),'--input',str(self.inputs),'--out',str(output),
                                   '--model','test','--max-calls','1','--batch-size','2','--execute']),0)
        result=json.loads((output/'summary.json').read_text())
        self.assertEqual(result['candidates_total'],3)
        self.assertEqual(result['reviewed'],2)
        self.assertEqual(call.call_count,1)
        self.assertEqual(before,self.db.read_bytes())

    def test_export_marks_changed_candidate_payload_stale(self):
        ci.build(self.db,self.inputs,self.index)
        store=self.store()
        identity=store.dialogues({'corpus':'sample','source':'candidates'})['items'][0]['id']
        store.save_review({'corpus':'sample','source':'candidates','dialogue_id':identity,'verdict':'correct'})
        self.row['speaker']='B';self.write_batch()
        ci.build(self.db,self.inputs,self.index,refresh=True)
        exported=store.review_export({'corpus':'sample'})['reviews'][0]
        self.assertTrue(exported['stale'])
        self.assertFalse(any(exported['flags'].values()))

    def test_source_span_rewrite_is_rejected(self):
        self.row['text']='Rewritten speech';self.write_batch()
        with self.assertRaises(ValueError):ci.build(self.db,self.inputs,self.index)

    def test_stale_candidate_source_is_rejected(self):
        self.batch['database_sha256']='outdated';self.write_batch()
        with self.assertRaises(ValueError):ci.build(self.db,self.inputs,self.index)

    def test_source_directory_cannot_be_output(self):
        with self.assertRaises(ValueError):ci.build(self.db,self.inputs,self.source/'new.sqlite')

    def test_existing_unrelated_database_is_not_overwritten(self):
        self.index.write_bytes(b'keep')
        with self.assertRaises((ValueError,sqlite3.Error)):ci.build(self.db,self.inputs,self.index,refresh=True)
        self.assertEqual(self.index.read_bytes(),b'keep')

    def test_wal_appearing_during_index_build_refuses_publication(self):
        original=ci.load_candidates
        def concurrent_write(*args):
            rows=original(*args)
            Path(str(self.db)+'-wal').write_bytes(b'synthetic concurrent WAL')
            return rows
        with patch('ltr.quotes.index.load_candidates',side_effect=concurrent_write):
            with self.assertRaises(ValueError):ci.build(self.db,self.inputs,self.index)
        self.assertFalse(self.index.exists())

    def test_model_disagreement_is_preserved_as_conflict(self):
        identity=ci.candidate_id('e1',self.row,self.sha)
        for n,decision in enumerate(['keep','reject']):
            review={'schema':'unquoted-candidate-model-review-v1','database_sha256':self.sha,'model':f'reviewer{n}',
                    'reviews':[{'candidate_id':identity,'decision':decision,'kind':'spoken','basis':'direct_speech',**ci.FLAGS}]}
            (self.reviews/f'review-{n}.json').write_text(json.dumps(review))
        result=ci.build(self.db,self.inputs,self.index,self.reviews)
        self.assertEqual(result['review_status'],{'conflict':1})
        detail=self.store().dialogue({'corpus':'sample','source':'candidates','id':'candidate:'+identity})
        self.assertEqual(len(detail['candidate']['review_observations']),2)
        self.assertEqual(detail['candidate']['review_status'],'conflict')

    def test_invalid_source_enum_is_refused(self):
        with self.assertRaises(wb.WorkbenchError):self.store().dialogues({'corpus':'sample','source':'other'})

    def test_changed_source_makes_only_overlay_unavailable(self):
        ci.build(self.db,self.inputs,self.index)
        con=sqlite3.connect(self.db);con.execute("UPDATE paragraphs SET text='changed' WHERE edition_id='e1'");con.commit();con.close()
        store=self.store()
        overview=store.overview()['corpora'][0]
        self.assertTrue(overview['available'])
        self.assertFalse(overview['pending_candidates']['available'])
        with self.assertRaises(wb.WorkbenchError):store.dialogues({'corpus':'sample','source':'candidates'})


if __name__=='__main__':unittest.main()
