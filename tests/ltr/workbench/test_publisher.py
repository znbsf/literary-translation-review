import json
import unittest
from pathlib import Path

from tests.ltr.library import test_publisher_identifiers as fixture
from ltr import workbench as wb


class PublisherWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.PublisherIdentifierTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.write_candidate(self.case.candidate_payload([self.case.observation()]))

    def store(self, **kwargs):
        c = self.case
        config = wb.WorkbenchConfig([wb.CorpusConfig('sample', 'Synthetic', c.source, **kwargs)], c.tmp / 'labels')
        store = wb.WorkbenchStore(config)
        self.addCleanup(store.close)
        return store

    def config_file(self, **fields):
        path = self.case.tmp / 'config.json'
        path.write_text(json.dumps({'corpora': [{'id': 'sample', 'path': str(self.case.source), **fields}]}), encoding='utf-8')
        return path

    def test_external_relative_config_and_overview(self):
        c = self.case
        path = self.config_file(bibliography='catalog.json', publisher_identifiers='candidates.json')
        config = wb.WorkbenchConfig.load(path)
        self.assertEqual(config.corpora[0].publisher_identifiers, c.candidate)
        before = c.db.read_bytes()
        store = wb.WorkbenchStore(config)
        self.addCleanup(store.close)
        data = store.overview()['corpora'][0]
        self.assertTrue(data['available'])
        self.assertTrue(data['publisher_identifiers']['available'])
        self.assertEqual(data['publisher_identifiers']['candidate_rows'], 1)
        self.assertFalse(data['publisher_identifiers']['corpus_updated'])
        self.assertEqual(data['counts']['alignments'], 0)
        self.assertEqual(c.db.read_bytes(), before)
        self.assertNotIn(fixture.P1, json.dumps(data))

    def test_invalid_file_degrades_without_hiding_corpus_or_catalog(self):
        c = self.case
        c.candidate.write_text('{}')
        data = self.store(bibliography=c.catalog, publisher_identifiers=c.candidate).overview()['corpora'][0]
        self.assertTrue(data['available'])
        self.assertTrue(data['publisher_bibliography']['available'])
        self.assertFalse(data['publisher_identifiers']['available'])
        self.assertNotIn(str(c.tmp), json.dumps(data))

    def test_unconfigured_does_not_claim_empty_scan(self):
        data = self.store().overview()['corpora'][0]['publisher_identifiers']
        self.assertEqual(data, {'available': False, 'error': None})

    def test_identifier_config_requires_external_catalog(self):
        with self.assertRaises(wb.WorkbenchError) as caught:
            wb.WorkbenchConfig.load(self.config_file(publisher_identifiers='candidates.json'))
        self.assertEqual(caught.exception.code, 'publisher_identifiers_require_bibliography')
        with self.assertRaises(wb.WorkbenchError) as caught:
            wb.WorkbenchConfig.load(self.config_file(bibliography='catalog.json', publisher_identifiers=str(self.case.source / 'inside.json')))
        self.assertEqual(caught.exception.code, 'publisher_identifiers_must_be_external')


if __name__ == '__main__':
    unittest.main()
