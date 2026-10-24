"""Synthetic HTTP checks for the opt-in, source-bound reading text view."""
import hashlib
import http.client
import json
import threading
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

from ltr import workbench as wb
from ltr.ocr import reading_units
from tests.ltr.ocr import test_reading_order_index as fixtures


class ReadingUnitsWorkbenchTests(unittest.TestCase):
    _build = fixtures.GeometryIndexTests._build

    def setUp(self):
        fixtures.GeometryIndexTests.setUp(self)
        self.corpus = wb.CorpusConfig('fixture', 'Synthetic', self.root,
            ocr_reading_order_candidates=self.phase)
        self.config = wb.WorkbenchConfig([self.corpus], None)
        self.store = wb.WorkbenchStore(self.config)
        self.addCleanup(self.store.close)
        item = self.store.ocr_reading_order_candidates({'corpus': 'fixture'})['items'][0]
        self.params = {'corpus': 'fixture', 'id': item['id'],
                       'candidate_sha256': item['candidate_sha256']}

    def test_store_loads_only_explicit_reading_view_and_keeps_source_unchanged(self):
        before = hashlib.sha256(self.dbpath.read_bytes()).hexdigest()
        plain = self.store.ocr_reading_order_candidate(self.params)
        self.assertNotIn('"text"', json.dumps(plain))
        result = self.store.ocr_reading_units(self.params)
        self.assertTrue(result['available'])
        self.assertEqual(result['schema'], 'corpus-reading-units-v1')
        self.assertEqual(result['units'][0]['words'][0]['text'], 'synthetic')
        self.assertEqual(result['boundary_status'], 'unknown')
        self.assertEqual(result['flags']['requires_review'], 1)
        self.assertFalse(result['flags']['reference_eligible'])
        self.assertEqual(hashlib.sha256(self.dbpath.read_bytes()).hexdigest(), before)

    def test_http_returns_text_only_for_valid_bound_explicit_request(self):
        before = hashlib.sha256(self.dbpath.read_bytes()).hexdigest()
        server = wb.build_server(self.config, 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join, 3)
        self.addCleanup(server.shutdown)
        client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        self.addCleanup(client.close)
        for params, expected in [
            (self.params, 200),
            ({**self.params, 'candidate_sha256': '0' * 64}, 409),
            ({**self.params, 'id': 'SYNTHETIC_PRIVATE_SENTINEL'}, 409),
            ({'corpus': 'fixture'}, 409),
        ]:
            client.request('GET', '/api/ocr-reading-units?' + urlencode(params))
            response = client.getresponse()
            body = response.read().decode('utf-8')
            self.assertEqual(response.status, expected)
            self.assertNotIn(str(self.root), body)
            self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL', body)
            result = json.loads(body)
            if expected == 200:
                self.assertTrue(result['available'])
                self.assertEqual(result['candidate_sha256'], self.params['candidate_sha256'])
            else:
                self.assertNotIn('"text"', body)
                self.assertNotIn('synthetic', body)
        self.assertEqual(hashlib.sha256(self.dbpath.read_bytes()).hexdigest(), before)

    def test_unavailable_projection_is_conflict_with_static_code(self):
        with patch.object(reading_units, 'read_reading_units',
                          return_value={'available': False, 'error': 'ocr_reading_units_text_invalid'}):
            with self.assertRaises(wb.WorkbenchError) as caught:
                self.store.ocr_reading_units(self.params)
        self.assertEqual(caught.exception.code, 'ocr_reading_units_text_invalid')


if __name__ == '__main__':
    unittest.main()
