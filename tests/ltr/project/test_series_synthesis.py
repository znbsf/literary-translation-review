from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ltr.project.dimension_cycle import CycleError
from ltr.project.learning_export import CATEGORIES, export_learning
from ltr.project.series_synthesis import prepare, execute, export, run
from ltr.project.storage import digest, write_json


class SeriesSynthesisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'learning'
        plan = {'jobs': [], 'files': {}, 'books': [], 'problems': []}
        for number in (1, 2):
            book_id = f'B{number}'
            plan['books'].append({'book_id': book_id, 'title': f'Synthetic {number}',
                'source_file': f'F{number}S', 'reference_files': [f'F{number}T'],
                'order_basis': 'corpus_traversal', 'reference_import_complete': True})
            merge_records, origins = [], {}
            for letter, kind in [('S', 'source'), ('T', 'translation')]:
                file_id, job_id = f'F{number}{letter}', f'J{number}{letter}'
                plan['jobs'].append(job_id)
                meta = {'source_hash': digest(file_id), 'kind': kind, 'source_paragraphs': 1, 'jobs': [job_id]}
                plan['files'][file_id] = meta
                payload = {'source': [{'id': 'P0', 'text': 'PRIVATE SOURCE SENTINEL'}]}
                job = {'payload': payload, 'file_id': file_id, 'source_hash': meta['source_hash'],
                       'kind': kind, 'source_ids': [f'original-{file_id}']}
                leaf = []
                for i, category in enumerate(CATEGORIES):
                    record = {'id': f'{job_id}:R{i:04d}', 'category': category,
                        'name': f'Synthetic {category}', 'description': f'Analysis {number} {kind}',
                        'aliases': [], 'scope': 'book', 'certainty': 'inferred', 'evidence_ids': ['P0']}
                    leaf.append(record)
                    key = f'K{len(origins):05d}'
                    origins[key] = record['id']
                    merge_records.append({**record, 'id': key, 'origin_kind': kind})
                self.save('jobs', job_id, job, leaf)
            merged = [{'id': f'{book_id}:R{i:04d}', 'category': category, 'name': f'Merged {category}',
                'description': 'Distinct author and translator observations.', 'aliases': [],
                'scope': 'book', 'certainty': 'uncertain', 'evidence_ids': [f'K{i:05d}', f'K{i+6:05d}']}
                for i, category in enumerate(CATEGORIES)]
            self.save('merges', book_id, {'payload': {'records': merge_records}, 'origin_map': origins}, merged)
        write_json(self.root/'learning-plan.json', plan)
        export_learning(self.root)

    def read(self, path):
        return json.loads((self.root/path).read_text(encoding='utf-8'))

    def save(self, folder, identifier, job, records):
        result = {'records': records, 'input_digest': digest(job['payload']),
                  'unresolved': ['Synthetic unresolved difference'], 'coverage_note': 'Synthetic only.'}
        claim = {'status': 'completed', 'payload_digest': digest(job['payload']), 'result_digest': digest(result)}
        for suffix, value in [('input', job), ('result', result), ('claim', claim)]:
            write_json(self.root/folder/f'{identifier}.{suffix}.json', value)

    def model(self, model, prompt, payload, timeout):
        self.assertEqual(timeout, 420)
        self.assertNotIn('PRIVATE SOURCE SENTINEL', json.dumps(payload))
        return {'records': [{'category': payload['category'], 'name': 'Synthetic synthesis',
            'description': 'Keep the disagreement visible.', 'aliases': ['Unconfirmed alias'],
            'certainty': 'uncertain', 'scope': 'series_candidate',
            'evidence_ids': [r['id'] for r in payload['records']]}],
            'unresolved': ['Still unresolved.'], 'coverage_note': 'Only the supplied books.'}, 0.1

    def test_complete_prepare_uses_short_ids_and_preserves_chain(self):
        coverage = prepare(self.root)
        self.assertEqual(coverage['merged_books'], 2)
        job = self.read('跨卷档案/characters.input.json')
        self.assertEqual(list(job['origin_map']), ['K00000', 'K00001'])
        origin = job['origin_map']['K00000']
        self.assertEqual(origin['source_kind'], ['source', 'translation'])
        self.assertEqual(origin['evidence_trace'][0]['original_source_ids'], ['original-F1S'])
        self.assertNotIn('PRIVATE SOURCE SENTINEL', json.dumps(job))
        notes = job['payload']['books'][0]['source_coverage_notes']
        self.assertEqual({n['job_id'] for n in notes}, {'J1S', 'J1T'})
        self.assertTrue(all(n['note'] == 'Synthetic only.' for n in notes))

    def test_incomplete_claim_blocks_default_but_explicit_partial_is_labeled(self):
        claim = self.read('merges/B2.claim.json'); claim['status'] = 'reserved'
        write_json(self.root/'merges/B2.claim.json', claim)
        with self.assertRaisesRegex(CycleError, 'incomplete_book_merges'):
            prepare(self.root)
        coverage = prepare(self.root, allow_partial=True)
        self.assertEqual((coverage['merged_books'], coverage['planned_books'], coverage['status']), (1, 2, 'partial'))

    def test_tampered_index_rejected_before_model_input(self):
        index = self.read('知识档案/knowledge-index.json')
        index['records'][0]['description'] = 'tampered'
        write_json(self.root/'知识档案/knowledge-index.json', index)
        with self.assertRaisesRegex(CycleError, 'stale_knowledge_index'):
            prepare(self.root)

    def test_six_calls_export_and_resume_without_extra_calls(self):
        with patch('ltr.project.series_synthesis.call_model', side_effect=self.model) as mocked:
            first = run(self.root, 'synthetic', workers=3)
            second = run(self.root, 'synthetic', workers=3)
        self.assertEqual(mocked.call_count, 6)
        self.assertEqual(first['completed_categories'], 6)
        self.assertEqual(second['records'], 6)
        index = self.read('跨卷档案/index.json')
        record = index['results']['characters']['records'][0]
        self.assertEqual(record['status'], 'model_proposed')
        self.assertEqual(record['alias_identity_status'], 'proposed_not_confirmed')
        self.assertEqual(record['source_kind'], ['source', 'translation'])
        self.assertEqual(len(list((self.root/'跨卷档案').glob('*.md'))), 6)

    def test_unknown_evidence_keeps_failed_candidate_and_never_retries(self):
        prepare(self.root)
        job = self.read('跨卷档案/characters.input.json')
        invalid, _ = self.model('synthetic', '', job['payload'], 420)
        invalid['records'][0]['evidence_ids'] = ['K99999']
        with patch('ltr.project.series_synthesis.call_model', return_value=(invalid, 0.1)) as mocked:
            self.assertEqual(execute(self.root, 'characters', 'synthetic')['status'], 'failed')
            self.assertEqual(execute(self.root, 'characters', 'synthetic')['status'], 'requires_resolution')
        self.assertEqual(mocked.call_count, 1)
        self.assertTrue((self.root/'跨卷档案/characters.candidate.json').exists())
        self.assertEqual(export(self.root)['completed_categories'], 0)

    def test_edited_result_not_reused_or_exported(self):
        prepare(self.root)
        with patch('ltr.project.series_synthesis.call_model', side_effect=self.model):
            execute(self.root, 'characters', 'synthetic')
        result = self.read('跨卷档案/characters.result.json')
        result['records'][0]['source_kind'] = ['source']
        write_json(self.root/'跨卷档案/characters.result.json', result)
        self.assertEqual(execute(self.root, 'characters', 'synthetic')['status'], 'requires_resolution')
        self.assertEqual(export(self.root)['completed_categories'], 0)

    def test_changed_input_prevents_resume(self):
        prepare(self.root)
        job = self.read('跨卷档案/characters.input.json')
        job['payload']['coverage']['status'] = 'tampered'
        write_json(self.root/'跨卷档案/characters.input.json', job)
        with self.assertRaisesRegex(CycleError, 'learning_input_changed'):
            prepare(self.root)


if __name__ == '__main__':
    unittest.main()
