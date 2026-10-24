"""Synthetic configuration and HTTP boundary for the geometry-only viewer."""
import hashlib
import http.client
import json
import threading
import unittest
from unittest.mock import patch

from ltr import workbench as wb
from ltr.ocr import reading_order_index
from tests.ltr.ocr import test_reading_order_index as fixtures


class GeometryWorkbenchTests(unittest.TestCase):
    _build = fixtures.GeometryIndexTests._build
    _resign_first = fixtures.GeometryIndexTests._resign_first

    def setUp(self):
        fixtures.GeometryIndexTests.setUp(self)
        self.corpus = wb.CorpusConfig('fixture', 'Synthetic', self.root,
            ocr_reading_order_candidates=self.phase)
        self.config = wb.WorkbenchConfig([self.corpus], None)
        self.store = wb.WorkbenchStore(self.config)
        self.addCleanup(self.store.close)
        self.item = self.store.ocr_reading_order_candidates({'corpus':'fixture'})['items'][0]
        self.params = {'corpus':'fixture', 'id':self.item['id'],
                       'candidate_sha256':self.item['candidate_sha256']}

    def test_config_is_external_to_every_corpus_and_keeps_relative_paths(self):
        path = self.phase.parent / 'config.json'
        data = {'corpora':[{'id':'fixture', 'path':str(self.root),
                           'ocr_reading_order_candidates':self.phase.name}]}
        path.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(wb.WorkbenchConfig.load(path).corpus('fixture').ocr_reading_order_candidates, self.phase)
        for raw in (True, '', str(self.root / 'geometry')):
            data['corpora'][0]['ocr_reading_order_candidates'] = raw
            path.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaises(wb.WorkbenchError):wb.WorkbenchConfig.load(path)
        data['corpora'][0]['ocr_reading_order_candidates'] = str(self.phase)
        data['corpora'].append({'id':'other', 'path':str(self.phase.parent)})
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(wb.WorkbenchError):wb.WorkbenchConfig.load(path)

    def test_detail_requires_current_hash_and_image_rechecks_after_read(self):
        with self.assertRaises(wb.WorkbenchError):
            self.store.ocr_reading_order_candidate({**self.params, 'candidate_sha256':'0'*64})
        with self.assertRaises(wb.WorkbenchError):
            self.store.ocr_reading_order_image({**self.params, 'image_sha256':'0'*64})
        def changed(_params):
            self._resign_first(lambda c:c.update(raw_backward_band_steps=1))
            return b'synthetic image', 'image/png'
        with patch.object(self.store, 'scan_image', side_effect=changed):
            with self.assertRaises(wb.WorkbenchError):
                self.store.ocr_reading_order_image({**self.params, 'image_sha256':'a'*64})

    def test_list_forwards_collection_and_page_bounds(self):
        expected = {'available': True, 'total': 3580, 'offset': 20, 'limit': 10,
                    'items': [{'id':'SYN:0021', 'page_id':'SYN:0021',
                               'candidate_sha256':'a'*64, 'collection_id':'SYN',
                               'book_key':'synthetic', 'scan_ordinal':21,
                               'status':'geometry_input_rejected',
                               'geometry_candidate':False, 'raw_line_count':None,
                               'raw_word_count':None, 'segment_count':None,
                               'error_code':'geometry_input_rejected'}]}
        with patch.object(reading_order_index, 'list_candidates', return_value=expected) as listing:
            result = self.store.ocr_reading_order_candidates({
                'corpus':'fixture', 'offset':'20', 'limit':'10', 'collection_id':'SYN'})
        self.assertEqual(result, expected)
        args, kwargs = listing.call_args
        self.assertIs(args[0], self.corpus)
        self.assertEqual(kwargs, {'offset':20, 'limit':10, 'collection_id':'SYN'})
        for params in ({'corpus':'fixture','offset':'-1'},
                       {'corpus':'fixture','limit':'0'},
                       {'corpus':'fixture','collection_id':''}):
            with self.assertRaises(wb.WorkbenchError):
                self.store.ocr_reading_order_candidates(params)

    def test_http_list_detail_conflict_and_readonly_source(self):
        before = hashlib.sha256(self.dbpath.read_bytes()).hexdigest()
        server = wb.build_server(self.config, 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join, 3)
        self.addCleanup(server.shutdown)
        client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        self.addCleanup(client.close)
        from urllib.parse import urlencode
        for suffix, params, status in [
            ('candidates', {'corpus':'fixture', 'offset':1, 'limit':2, 'collection_id':'FIX'}, 200),
            ('candidates', {'corpus':'fixture', 'offset':0, 'limit':2, 'collection_id':'NO_MATCH'}, 200),
            ('candidate', self.params, 200),
            ('candidate', {**self.params,'candidate_sha256':'0'*64}, 409),
            ('candidates', {'corpus':'fixture','limit':0}, 400),
            ('candidates', {'corpus':'fixture','collection_id':''}, 400)]:
            client.request('GET', '/api/ocr-reading-order-'+suffix+'?'+urlencode(params))
            response = client.getresponse(); body = response.read().decode('utf-8')
            self.assertEqual(response.status, status)
            self.assertNotIn(str(self.root), body)
            self.assertNotIn('"text"', body)
            if suffix == 'candidates' and status == 200:
                result = json.loads(body)
                if params.get('collection_id') == 'NO_MATCH':
                    self.assertEqual((result['total'],result['offset'],result['limit'],result['items']),(0,0,2,[]))
                else:
                    self.assertEqual(len(result['items']),2)
                    self.assertEqual((result['total'],result['offset'],result['limit']),(10,1,2))
        self.assertEqual(hashlib.sha256(self.dbpath.read_bytes()).hexdigest(), before)


if __name__ == '__main__':unittest.main()
