import json
from pathlib import Path
import tempfile
import unittest

from ltr.project.learning_export import export_learning, _completed
from ltr.project.storage import digest, write_json


class LearningExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'learning'
        self.plan = {'jobs': ['J01', 'J02'], 'problems': [],
            'files': {'F01': {'kind': 'source', 'source_hash': 'a'*64, 'source_paragraphs': 1, 'jobs': ['J01']},
                      'F02': {'kind': 'translation', 'source_hash': 'b'*64, 'source_paragraphs': 1, 'jobs': ['J02']}},
            'books': [{'book_id': 'B01', 'title': 'Synthetic Volume', 'source_file': 'F01',
                       'reference_files': ['F02'], 'publication_order': 1}]}
        write_json(self.root/'learning-plan.json', self.plan)
        for number, kind in [(1, 'source'), (2, 'translation')]:
            job_id, file_id = f'J0{number}', f'F0{number}'
            payload = {'source': [{'id': 'P00000', 'text': 'PRIVATE SENTINEL DO NOT EXPORT'}]}
            job = {'file_id': file_id, 'kind': kind, 'source_hash': self.plan['files'][file_id]['source_hash'],
                   'source_ids': [f'original-{number}'], 'payload': payload}
            record = {'id': f'{job_id}:R0000', 'category': 'characters', 'name': f'Person {number}',
                      'description': f'Synthetic analysis {number}', 'aliases': ['Shared alias'],
                      'certainty': 'inferred', 'scope': 'book', 'evidence_ids': ['P00000']}
            self.save('jobs', job_id, job, [record])

    def read(self, path):
        return json.loads((self.root/path).read_text(encoding='utf-8'))

    def save(self, folder, identifier, job, records):
        result = {'records': records, 'input_digest': digest(job['payload']), 'status': 'model_proposed'}
        claim = {'status': 'completed', 'payload_digest': digest(job['payload']), 'result_digest': digest(result)}
        for suffix, value in [('input', job), ('result', result), ('claim', claim)]:
            write_json(self.root/folder/f'{identifier}.{suffix}.json', value)

    def merge(self):
        records, origins = [], {}
        for number in (1, 2):
            item = self.read(f'jobs/J0{number}.result.json')['records'][0]
            key = f'K{number:05d}'
            origins[key] = item['id']
            records.append({**item, 'id': key, 'origin_kind': 'source' if number == 1 else 'translation'})
        job = {'payload': {'records': records}, 'origin_map': origins}
        output = {'id': 'B01:R0000', 'category': 'characters', 'name': 'Merged Person',
                  'description': 'Keep distinct author and translator observations.', 'aliases': [],
                  'certainty': 'uncertain', 'scope': 'book', 'evidence_ids': list(origins)}
        self.save('merges', 'B01', job, [output])

    def test_partial_coverage_and_no_source_copy(self):
        (self.root/'jobs/J02.claim.json').unlink()
        result = export_learning(self.root)
        self.assertEqual((result['completed_jobs'], result['planned_jobs']), (1, 2))
        index = self.read('知识档案/knowledge-index.json')
        self.assertEqual(index['books'][0]['coverage']['status'], 'partial')
        for path in (self.root/'知识档案').glob('*'):
            self.assertNotIn('PRIVATE SENTINEL', path.read_text(encoding='utf-8'))
        self.assertEqual(len(list((self.root/'知识档案').glob('*.md'))), 7)

    def test_tampered_result_and_noncompleted_claim_rejected(self):
        value = self.read('jobs/J01.result.json')
        value['records'][0]['description'] = 'tampered'
        write_json(self.root/'jobs/J01.result.json', value)
        claim = self.read('jobs/J02.claim.json')
        claim['status'] = 'reserved'
        write_json(self.root/'jobs/J02.claim.json', claim)
        self.assertEqual(export_learning(self.root)['records'], 0)

    def test_merge_preferred_with_full_source_kind_evidence_chain(self):
        self.merge()
        result = export_learning(self.root)
        self.assertEqual((result['merged_books'], result['records']), (1, 1))
        item = self.read('知识档案/knowledge-index.json')['records'][0]
        self.assertEqual(set(item['source_kind']), {'source', 'translation'})
        self.assertEqual(item['evidence_trace'][0]['merge_evidence_id'], 'K00001')
        self.assertEqual(item['evidence_trace'][0]['original_source_ids'], ['original-1'])
        self.assertEqual(item['evidence_trace'][1]['source_hash'], 'b'*64)

    def test_stale_merge_falls_back_to_valid_leaves(self):
        self.merge()
        job = self.read('jobs/J01.input.json')
        record = self.read('jobs/J01.result.json')['records'][0]
        record['description'] = 'A new validated analysis.'
        self.save('jobs', 'J01', job, [record])
        result = export_learning(self.root)
        self.assertEqual((result['merged_books'], result['records']), (0, 2))

    def test_alias_conflicts_do_not_merge_character_groups(self):
        export_learning(self.root)
        index = self.read('知识档案/knowledge-index.json')
        self.assertEqual(len(index['groups']), 2)
        self.assertEqual(index['alias_conflicts'][0]['canonical_candidates'], ['Person 1', 'Person 2'])
        self.assertEqual(index['alias_conflicts'][0]['status'], 'unresolved_not_merged')

    def test_registry_proposed_and_repeat_export_is_idempotent(self):
        self.merge()
        registry = Path(self.temp.name)/'registry'
        first = export_learning(self.root, registry)
        second = export_learning(self.root, registry)
        self.assertEqual(first['registry']['background_revisions_created'], 1)
        self.assertEqual(second['registry']['background_revisions_created'], 0)
        value = json.loads((registry/'backgrounds/learning_B01/revision-1.json').read_text(encoding='utf-8'))
        self.assertEqual(value['status'], 'proposed')
        self.assertEqual(value['content']['records'][0]['source_kind'], ['source', 'translation'])

    def test_missing_publication_order_not_invented(self):
        self.plan['books'][0].pop('publication_order')
        write_json(self.root/'learning-plan.json', self.plan)
        result = export_learning(self.root, Path(self.temp.name)/'registry')
        self.assertEqual(result['registry']['books_missing_order'], 1)

    def test_corpus_order_registers_without_claiming_publication_order(self):
        self.plan['books'][0].pop('publication_order')
        self.plan['books'][0]['corpus_order'] = 1
        write_json(self.root/'learning-plan.json', self.plan)
        registry = Path(self.temp.name)/'registry'
        result = export_learning(self.root, registry)
        self.assertEqual(result['registry']['books_registered'], 1)
        state = json.loads((registry/'series.json').read_text(encoding='utf-8'))
        self.assertEqual(state['books']['B01']['order_basis'], 'corpus_traversal')
        self.assertIn('不能据此推断真实出版顺序', (self.root/'知识档案/总览.md').read_text(encoding='utf-8'))

    def test_invalid_evidence_mapping_rejected_even_with_valid_digest(self):
        job = self.read('jobs/J01.input.json')
        record = self.read('jobs/J01.result.json')['records'][0]
        record['evidence_ids'] = ['P99999']
        self.save('jobs', 'J01', job, [record])
        result = export_learning(self.root)
        self.assertEqual(result['completed_jobs'], 1)

    def test_bound_envelope_rejects_changed_source_ids_and_missing_binding(self):
        job = self.read('jobs/J01.input.json')
        claim = self.read('jobs/J01.claim.json')
        result = self.read('jobs/J01.result.json')
        for value in (claim, result):
            value.update(envelope_digest=digest(job), input_binding='job_envelope_v1')
        claim['result_digest'] = digest(result)
        write_json(self.root/'jobs/J01.claim.json', claim)
        write_json(self.root/'jobs/J01.result.json', result)
        self.assertIsNotNone(_completed(self.root, 'jobs', 'J01'))
        job['source_ids'] = ['nonexistent-source-id']
        write_json(self.root/'jobs/J01.input.json', job)
        self.assertIsNone(_completed(self.root, 'jobs', 'J01'))
        self.assertEqual(export_learning(self.root)['completed_jobs'], 1)
        job['source_ids'] = ['original-1']
        write_json(self.root/'jobs/J01.input.json', job)
        claim.pop('envelope_digest'); claim.pop('input_binding')
        write_json(self.root/'jobs/J01.claim.json', claim)
        self.assertIsNone(_completed(self.root, 'jobs', 'J01'))

    def test_legacy_payload_only_claim_remains_compatible_without_mutation(self):
        before = (self.root/'jobs/J01.claim.json').read_bytes()
        self.assertIsNotNone(_completed(self.root, 'jobs', 'J01'))
        self.assertEqual((self.root/'jobs/J01.claim.json').read_bytes(), before)
        self.assertNotIn('envelope_digest', self.read('jobs/J01.claim.json'))


if __name__ == '__main__':
    unittest.main()
