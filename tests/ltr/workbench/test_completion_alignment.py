"""Synthetic workbench integration for pending formal completion alignments."""
import http.client
import json
import threading
import unittest

from ltr import workbench as wb
from tests.ltr.alignment import test_completion as fixture_module


class CompletionAlignmentWorkbenchTests(unittest.TestCase):
    def setUp(self):
        fixture = fixture_module.CompletionAlignmentTests('test_overview_counts_pending_denominator_and_statuses')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture

    @property
    def store(self):
        return self.fixture.store

    def test_overview_exposes_stored_pending_counts_without_promoting_them(self):
        corpus = self.store.overview()['corpora'][0]
        self.assertTrue(corpus['capabilities']['completion_alignment_review'])
        self.assertEqual(corpus['completion_alignment_review']['pending_rows'], 1)
        self.assertEqual(corpus['completion_alignment_review']['mapped_pending_rows'], 1)
        self.assertEqual(corpus['counts']['alignments'], 0)

    def test_formal_filter_intersects_query_speaker_edition_and_pagination(self):
        self.fixture._mutate("INSERT INTO dialogues VALUES ('z2','zh1',2,'Other target','A',NULL,'x')")
        params = {'corpus': 'sample', 'pending_alignment': '1', 'q': 'Synthetic',
                  'speaker': 'A', 'edition': 'zh1', 'limit': '1'}
        result = self.store.dialogues(params)
        self.assertEqual([r['id'] for r in result['items']], ['z1'])
        self.assertFalse(result['has_more'])
        self.assertEqual(self.store.dialogues(dict(params, offset='1'))['items'], [])
        self.assertEqual(self.store.dialogues(dict(params, q='Other'))['items'], [])
        self.assertEqual(len(self.store.dialogues({'corpus': 'sample', 'edition': 'zh1'})['items']), 2)

    def test_unsupported_source_or_missing_schema_never_silently_ignores_filter(self):
        with self.assertRaises(wb.WorkbenchError) as caught:
            self.store.dialogues({'corpus': 'sample', 'source': 'candidates', 'pending_alignment': '1'})
        self.assertEqual(caught.exception.status, 400)
        self.fixture._mutate('DROP TABLE corpus_completion_alignment')
        corpus = self.store.overview()['corpora'][0]
        self.assertFalse(corpus['capabilities']['completion_alignment_review'])
        with self.assertRaises(wb.WorkbenchError) as caught:
            self.store.dialogues({'corpus': 'sample', 'pending_alignment': '1'})
        self.assertEqual(caught.exception.status, 400)
        self.assertIsNone(self.store.dialogue({'corpus': 'sample', 'id': 'z1'})['completion_alignment'])

    def test_formal_detail_adds_stored_source_without_changing_old_alignment(self):
        detail = self.store.dialogue({'corpus': 'sample', 'id': 'z1'})
        self.assertIsNone(detail['alignment'])
        completion = detail['completion_alignment']
        self.assertEqual(completion['source_quotes'][0]['id'], 'j1')
        self.assertFalse(completion['reference_eligible'])
        self.assertFalse(completion['historical_contract_revalidated'])

    def test_real_http_filter_and_source_mismatch_have_static_errors(self):
        server = wb.build_server(self.fixture.store.config, port=0)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: thread.join(timeout=5))
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        self.addCleanup(connection.close)
        connection.request('GET', '/api/dialogues?corpus=sample&pending_alignment=PRIVATE_SENTINEL')
        response = connection.getresponse()
        body = response.read().decode('utf-8')
        self.assertEqual(response.status, 400)
        self.assertNotIn('PRIVATE_SENTINEL', body)
        self.assertEqual(json.loads(body)['error'], 'invalid_pending_alignment_filter')
        connection.request('GET', '/api/dialogue?corpus=sample&id=z1')
        response = connection.getresponse()
        body = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(body['completion_alignment']['source_quotes'][0]['id'], 'j1')
        self.fixture._mutate('UPDATE corpus_completion_alignment SET binding_sha256=?',
                             ('PRIVATE_BINDING_SENTINEL',))
        connection.request('GET', '/api/dialogue?corpus=sample&id=z1')
        response = connection.getresponse()
        body = response.read().decode('utf-8')
        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(body)['error'], 'completion_alignment_source_mismatch')
        self.assertNotIn('PRIVATE_BINDING_SENTINEL', body)


if __name__ == '__main__':
    unittest.main()
