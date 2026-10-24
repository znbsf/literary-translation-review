"""Synthetic-only source-bound OCR reading units, without OCR or network."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.ltr.ocr import test_reading_order_index as fixtures
from ltr.ocr import reading_order_geometry as geometry
from ltr.ocr import reading_order_index as order
from ltr.ocr import reading_units as units


class ReadingUnitsTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.GeometryIndexTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def read(self, identity='FIX:0001', sha=None):
        if sha is None:
            sha = next(item['candidate_sha256'] for item in order.list_candidates(
                self.case.corpus,self.case.db)['items'] if item['id'] == identity)
        return units.read_reading_units(self.case.corpus,self.case.db,identity,sha)

    def assert_unavailable(self, result):
        self.assertEqual(set(result),{'available','error'})
        self.assertIs(result['available'],False)
        self.assertIn(result['error'],units.ERROR_CODES)

    def rewrite_receipt(self, mutate, *, geometry_again=False):
        path = self.case.root/'private/ocr/FIX_0001.json'
        doc = json.loads(path.read_bytes());mutate(doc)
        raw = json.dumps(doc,ensure_ascii=True,separators=(',',':')).encode()
        path.write_bytes(raw);receipt_sha = hashlib.sha256(raw).hexdigest()
        with sqlite3.connect(self.case.dbpath) as con:
            con.execute('UPDATE scan_pages SET ocr_sha256=? WHERE id=?',(receipt_sha,'FIX:0001'))
        self.case.rows[0] = ('FIX:0001',receipt_sha)
        self.case.db.close();self.case._build()
        if geometry_again:
            g = geometry.word_order(doc['lines'],100,200)
            def replace(candidate):
                for key in ('layout','raw_line_count','raw_word_count','word_order','segments','bands',
                            'raw_backward_band_steps','new_backward_band_steps','main','ruby','other'):
                    candidate[key] = g[key]
                candidate['geometry_candidate'] = g['layout'] != 'unclassified'
                candidate['status'] = ('geometry_candidate_unverified' if candidate['geometry_candidate'] else
                    'empty_geometry_unclassified' if g['raw_word_count'] == 0 else 'unclassified_geometry_only')
            self.case._resign_first(replace)

    def test_classified_projection_preserves_repeated_words_and_unknown_boundaries(self):
        result = self.read()
        self.assertIs(result['available'],True)
        self.assertEqual(set(result),{'available','schema','id','candidate_sha256','page_id','source_sha256',
            'receipt_sha256','image_sha256','geometry_algorithm_sha256','projection_algorithm_sha256',
            'width','height','status','units','source_lines','flags','text_status','boundary_status'})
        self.assertEqual(result['schema'],'corpus-reading-units-v1')
        self.assertEqual(result['projection_algorithm_sha256'],hashlib.sha256(Path(units.__file__).read_bytes()).hexdigest())
        self.assertEqual(result['flags'],order.FLAGS)
        self.assertEqual((result['text_status'],result['boundary_status']),('complete','unknown'))
        unit = result['units'][0]
        self.assertEqual(set(unit),{'index','role','band','column','source_line','box','word_refs','words','boundary_before'})
        self.assertEqual(unit['word_refs'],[[0,0],[0,1]])
        self.assertEqual([w['ref'] for w in unit['words']],unit['word_refs'])
        self.assertEqual([w['text'] for w in unit['words']],['synthetic','synthetic'])
        self.assertEqual(unit['words'][1]['box'],[20,15,25,20])
        self.assertEqual(unit['boundary_before'],'unknown')
        self.assertNotIn('text',unit)
        self.assertEqual(result['source_lines'][0]['words'],unit['words'])
        unit['words'][0]['ref'][0] = 99
        self.assertEqual(result['source_lines'][0]['words'][0]['ref'],[0,0])

    def test_unclassified_uses_source_line_order_and_unknown_roles(self):
        result = self.read('FIX:0002')
        self.assertTrue(result['available'])
        self.assertEqual(result['status'],'unclassified_geometry_only')
        unit = result['units'][0]
        self.assertEqual((unit['role'],unit['band'],unit['column']),('unknown',None,None))
        self.assertEqual(unit['word_refs'],[[0,0],[0,1]])
        self.assertEqual(unit['box'],[10,10,25,20])

    def test_empty_page_has_empty_projections_without_blank_page_claim(self):
        result = self.read('FIX:0003')
        self.assertTrue(result['available'])
        self.assertEqual((result['units'],result['source_lines'],result['text_status']),([],[],'empty'))
        self.assertEqual(result['boundary_status'],'unknown')
        self.assertEqual(result['flags']['ocr_verified'],0)

    def test_empty_word_and_unicode_are_preserved_without_normalization(self):
        text = 'e\u0301🙂\n'
        self.rewrite_receipt(lambda doc:(doc['lines'][0]['words'][0].update(text=''),
                                        doc['lines'][0]['words'][1].update(text=text)))
        result = self.read()
        self.assertTrue(result['available'])
        self.assertEqual(result['text_status'],'partial')
        self.assertEqual(result['units'][0]['words'][0]['text'],'')
        self.assertEqual(result['units'][0]['words'][1]['text'],text)

    def test_missing_nonstring_and_unencodable_text_fail_after_resigning_source(self):
        for label,value in [('missing',None),('null',None),('int',1),('list',[]),('surrogate','\ud800')]:
            with self.subTest(label=label):
                def mutate(doc):
                    if label == 'missing':doc['lines'][0]['words'][0].pop('text',None)
                    else:doc['lines'][0]['words'][0]['text'] = value
                self.rewrite_receipt(mutate)
                result = self.read()
                self.assert_unavailable(result)
                self.assertEqual(result['error'],'ocr_reading_units_text_invalid')

    def test_word_codepoint_limit_is_inclusive(self):
        self.rewrite_receipt(lambda doc:doc['lines'][0]['words'][0].update(text='x'*4096))
        self.assertTrue(self.read()['available'])
        self.rewrite_receipt(lambda doc:doc['lines'][0]['words'][0].update(text='x'*4097))
        result = self.read();self.assert_unavailable(result)
        self.assertEqual(result['error'],'ocr_reading_units_limits_exceeded')

    def test_total_codepoint_limit_is_inclusive_and_counts_source_once(self):
        def change(doc,total):
            remaining = total;words = []
            while remaining:
                length = min(4096,remaining);remaining -= length
                words.append({'x':10,'y':10,'w':5,'h':5,'text':'x'*length})
            # Retain four tall lines so the frozen legacy classified manifest
            # stays valid; this case must reach the text budget, not fail on it.
            groups = [words[index::4] for index in range(4)]
            for group in groups:
                for index,word in enumerate(group):
                    word['y'] = 10 if index % 2 == 0 else 60
            doc['lines'] = [{'words':group} for group in groups]
        self.rewrite_receipt(lambda doc:change(doc,units.MAX_TOTAL_TEXT_CODEPOINTS),geometry_again=True)
        result = self.read();self.assertTrue(result['available'])
        self.assertEqual(sum(len(w['text']) for line in result['source_lines'] for w in line['words']),1_000_000)
        self.rewrite_receipt(lambda doc:change(doc,units.MAX_TOTAL_TEXT_CODEPOINTS+1),geometry_again=True)
        result = self.read();self.assert_unavailable(result)
        self.assertEqual(result['error'],'ocr_reading_units_limits_exceeded')

    def test_resigned_bad_refs_boxes_and_flags_fail_closed(self):
        variants = [
            lambda c:(c.update(word_order=[[0,0],[0,0]]),c['segments'][0].update(word_refs=[[0,0],[0,0]])),
            lambda c:(c.update(word_order=[[0,0],[0,2]]),c['segments'][0].update(word_refs=[[0,0],[0,2]])),
            lambda c:c['segments'][0].update(box=[10,10,24,20]),
            lambda c:c.update(accepted=True),
            lambda c:c.update(ocr_verified=1),
        ]
        for mutate in variants:
            self.case._build()
            sha = self.case._resign_first(mutate)
            self.assert_unavailable(self.read(sha=sha))

    def test_catalog_is_selected_page_lazy_and_rejected_page_has_no_text(self):
        self.case._catalog(reject_last=True)
        # Its hash remains frozen in metadata. Selecting another page must not read it.
        (self.case.phase/'geometry-000002.json').write_bytes(b'invalid-unselected')
        seen = [];original = order.vision._bytes
        def read(path,maximum):
            seen.append(Path(path).name)
            return original(path,maximum)
        with patch.object(order.vision,'_bytes',side_effect=read):
            result = self.read()
        self.assertTrue(result['available'])
        self.assertFalse(any(name.startswith('FIX_') and name != 'FIX_0001.json' for name in seen))
        rejected = self.read('FIX:0010')
        self.assert_unavailable(rejected)
        self.assertEqual(rejected['error'],'ocr_reading_units_geometry_unavailable')

    def test_missing_configuration_and_invalid_selection_do_not_read_candidate(self):
        with patch.object(order,'read_candidate') as reader:
            result = units.read_reading_units(SimpleNamespace(),self.case.db,'FIX:0001','a'*64)
            self.assertEqual(result,{'available':False,'error':'ocr_reading_units_not_configured'})
            for identity,sha in [(None,'a'*64),('FIX:0001','invalid'),(True,'a'*64)]:
                self.assert_unavailable(units.read_reading_units(self.case.corpus,self.case.db,identity,sha))
            reader.assert_not_called()

    def test_wrong_candidate_hash_is_rejected_without_word_text(self):
        self.assert_unavailable(self.read(sha='0'*64))

    def test_complete_before_after_detail_must_match(self):
        original = order.read_candidate;calls = []
        def read(*args):
            value = original(*args);calls.append(1)
            if len(calls) == 2:value['unexpected_metadata_change'] = True
            return value
        with patch.object(order,'read_candidate',side_effect=read):
            result = self.read()
        self.assertEqual(len(calls),2)
        self.assertEqual(result,{'available':False,'error':'ocr_reading_units_candidate_changed'})

    def test_source_race_fails_without_returning_any_text(self):
        original = order.read_candidate;calls = []
        def read(*args):
            value = original(*args);calls.append(1)
            if len(calls) == 1:
                with sqlite3.connect(self.case.dbpath) as con:con.execute('CREATE TABLE race_marker(id INTEGER)')
            return value
        with patch.object(order,'read_candidate',side_effect=read):
            self.assert_unavailable(self.read())

    def test_receipt_race_fails_without_returning_any_text(self):
        original = order._receipt_geometry;calls = []
        def read(*args):
            value = original(*args);calls.append(1)
            if len(calls) == 2:
                (self.case.root/'private/ocr/FIX_0001.json').write_bytes(b'changed-receipt')
            return value
        with patch.object(order,'_receipt_geometry',side_effect=read):
            self.assert_unavailable(self.read())

    def test_candidate_race_fails_without_returning_any_text(self):
        original = order.read_candidate;calls = []
        def read(*args):
            value = original(*args);calls.append(1)
            if len(calls) == 1:(self.case.phase/'geometry-01.json').write_bytes(b'changed-candidate')
            return value
        with patch.object(order,'read_candidate',side_effect=read):
            self.assert_unavailable(self.read())

    def test_projection_code_race_and_unknown_exceptions_are_static(self):
        with patch.object(units,'_projection_sha',side_effect=['a'*64,'b'*64]):
            result = self.read()
        self.assertEqual(result,{'available':False,'error':'ocr_reading_units_projection_changed'})
        with patch.object(order,'read_candidate',side_effect=RuntimeError('PRIVATE_FIXTURE_ERROR_BODY')):
            result = self.read()
        self.assertEqual(result,{'available':False,'error':'ocr_reading_units_unavailable'})
        self.assertNotIn('PRIVATE_FIXTURE_ERROR_BODY',json.dumps(result))


if __name__ == '__main__':unittest.main()
