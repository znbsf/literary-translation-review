"""Synthetic knowledge-learning lifecycle checks; no live transport or prose."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ltr.project import series_learning as learning
from ltr.project.storage import digest, write_json


class SeriesLearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def record(self, evidence='P00000'):
        return {'category': 'characters', 'name': '合成人物',
                'description': '合成记录：该人物明确表示不确定。', 'aliases': [],
                'certainty': 'explicit', 'scope': 'scene', 'evidence_ids': [evidence]}

    def job(self, job_id='J01', kind='source'):
        payload = {'edition_kind': kind, 'book_title': '合成测试书',
                   'source': [{'id': 'P00000', 'text': '合成测试段落。'}]}
        value = {'job_id': job_id, 'kind': kind, 'payload': payload,
                 'source_path': str(self.root / 'private-source.txt'),
                 'source_ids': ['private-locator'], 'source_hash': 'synthetic-hash'}
        write_json(self.root / 'jobs' / f'{job_id}.input.json', value)
        return value

    def result(self, job_id='J01', folder='jobs'):
        return learning.read(self.root / folder / f'{job_id}.result.json')

    def test_transport_gets_content_payload_without_local_envelope(self):
        job = self.job()
        with patch.object(learning, 'call_model', return_value=({'records': [self.record()]}, 0.01)) as model:
            self.assertEqual(learning.execute(self.root, 'J01', 'synthetic')['status'], 'completed')
        args = model.call_args.args
        self.assertEqual(args[2], job['payload'])
        dispatched = json.dumps(args, ensure_ascii=False)
        for private_value in (str(self.root), 'private-source.txt', 'private-locator', 'synthetic-hash'):
            self.assertNotIn(private_value, dispatched)
        self.assertEqual(model.call_args.kwargs, {'timeout': 420})

    def test_unknown_source_evidence_preserves_candidate_and_failed_claim_without_retry(self):
        self.job()
        invalid = {'records': [self.record('P99999')]}
        with patch.object(learning, 'call_model', return_value=(invalid, 0.01)) as model:
            self.assertEqual(learning.execute(self.root, 'J01', 'synthetic')['status'], 'failed')
            self.assertEqual(learning.execute(self.root, 'J01', 'synthetic')['status'], 'requires_resolution')
        model.assert_called_once()
        self.assertEqual(learning.read(self.root / 'jobs/J01.candidate.json'), invalid)
        claim = learning.read(self.root / 'jobs/J01.claim.json')
        self.assertEqual(claim['status'], 'failed')
        self.assertEqual(claim['code'], 'invalid_learning_evidence')
        self.assertFalse((self.root / 'jobs/J01.result.json').exists())

    def test_successful_resume_skips_model_and_detects_input_change(self):
        job = self.job()
        with patch.object(learning, 'call_model', return_value=({'records': [self.record()]}, 0.01)) as model:
            self.assertEqual(learning.execute(self.root, 'J01', 'synthetic')['status'], 'completed')
            self.assertEqual(learning.execute(self.root, 'J01', 'synthetic')['status'], 'reused')
            job['payload']['source'][0]['text'] = '改变后的合成测试段落。'
            write_json(self.root / 'jobs/J01.input.json', job)
            self.assertEqual(learning.execute(self.root, 'J01', 'synthetic')['status'], 'requires_resolution')
        model.assert_called_once()

    def test_changed_result_cannot_be_reused(self):
        self.job()
        with patch.object(learning, 'call_model', return_value=({'records': [self.record()]}, 0.01)) as model:
            learning.execute(self.root, 'J01', 'synthetic')
            result = self.result()
            result['records'][0]['description'] = '被改写的合成结论。'
            write_json(self.root / 'jobs/J01.result.json', result)
            self.assertEqual(learning.execute(self.root, 'J01', 'synthetic')['status'], 'requires_resolution')
        model.assert_called_once()

    def test_concurrent_job_dispatch_calls_model_once(self):
        self.job()
        started, release = threading.Event(), threading.Event()
        def transport(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(timeout=5))
            return {'records': [self.record()]}, 0.01
        with patch.object(learning, 'call_model', side_effect=transport) as model:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(learning.execute, self.root, 'J01', 'synthetic')
                try:
                    self.assertTrue(started.wait(timeout=5))
                    second = pool.submit(learning.execute, self.root, 'J01', 'synthetic')
                    self.assertEqual(second.result(timeout=3)['status'], 'in_progress')
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=5)['status'], 'completed')
            self.assertEqual(learning.execute(self.root, 'J01', 'synthetic')['status'], 'reused')
        model.assert_called_once()

    def test_source_mapping_change_invalidates_bound_claim(self):
        job = self.job()
        with patch.object(learning, 'call_model', return_value=({'records': [self.record()]}, 0.01)) as model:
            learning.execute(self.root, 'J01', 'synthetic')
            claim = learning.read(self.root / 'jobs/J01.claim.json')
            self.assertEqual(claim['envelope_digest'], digest(job))
            self.assertEqual(claim['input_binding'], 'job_envelope_v1')
            job['source_ids'] = ['different-source-paragraph']
            write_json(self.root / 'jobs/J01.input.json', job)
            self.assertEqual(learning.execute(self.root, 'J01', 'synthetic')['status'], 'requires_resolution')
        model.assert_called_once()

    def test_distinct_jobs_keep_independent_dispatch_locks(self):
        self.job('J01'); self.job('J02')
        together = threading.Barrier(2)
        def transport(*args, **kwargs):
            together.wait(timeout=5)
            return {'records': [self.record()]}, 0.01
        with patch.object(learning, 'call_model', side_effect=transport) as model:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(learning.execute, self.root, job_id, 'synthetic')
                           for job_id in ('J01', 'J02')]
                outcomes = [future.result(timeout=7)['status'] for future in futures]
        self.assertEqual(outcomes, ['completed', 'completed'])
        self.assertEqual(model.call_count, 2)

    def test_legacy_resume_is_not_upgraded_to_envelope_binding(self):
        self.job()
        with patch.object(learning, 'call_model', return_value=({'records': [self.record()]}, 0.01)):
            learning.execute(self.root, 'J01', 'synthetic')
        result = self.result()
        claim = learning.read(self.root / 'jobs/J01.claim.json')
        for value in (claim, result):
            value.pop('envelope_digest'); value.pop('input_binding')
        claim['result_digest'] = digest(result)
        write_json(self.root / 'jobs/J01.result.json', result)
        write_json(self.root / 'jobs/J01.claim.json', claim)
        with patch.object(learning, 'call_model') as model:
            outcome = learning.execute(self.root, 'J01', 'synthetic')
        self.assertEqual(outcome['input_binding'], 'legacy_payload_only')
        model.assert_not_called()
        self.assertNotIn('envelope_digest', learning.read(self.root / 'jobs/J01.claim.json'))

    def test_edition_kind_is_preserved_through_dispatch_and_record_basis(self):
        for kind in ('source', 'translation'):
            with self.subTest(kind=kind):
                job_id = 'J_' + kind
                self.job(job_id, kind)
                with patch.object(learning, 'call_model', return_value=({'records': [self.record()]}, 0.01)) as model:
                    learning.execute(self.root, job_id, 'synthetic')
                self.assertEqual(model.call_args.args[2]['edition_kind'], kind)
                self.assertEqual(self.result(job_id)['knowledge_basis'], kind)

    def manifest(self):
        return {'books': [{'book_id': 'B01', 'title': '合成测试书',
                           'source_file': 'FS', 'reference_files': ['FT'],
                           'pairing_status': 'synthetic_candidate'}],
                'files': {'FS': {'kind': 'source', 'jobs': ['JS0', 'JS1']},
                          'FT': {'kind': 'translation', 'jobs': ['JT0']}}}

    def finish_leaf(self, job_id, kind):
        self.job(job_id, kind)
        with patch.object(learning, 'call_model', return_value=({'records': [self.record()]}, 0.01)):
            self.assertEqual(learning.execute(self.root, job_id, 'synthetic')['status'], 'completed')

    def test_merge_waits_for_every_source_and_translation_leaf(self):
        manifest = self.manifest()
        self.finish_leaf('JS0', 'source')
        self.assertEqual(learning.prepare_merges(self.root, manifest), [])
        self.finish_leaf('JT0', 'translation')
        self.assertEqual(learning.prepare_merges(self.root, manifest), [])
        self.assertFalse((self.root / 'merges/B01.input.json').exists())
        self.finish_leaf('JS1', 'source')
        self.assertEqual(learning.prepare_merges(self.root, manifest), ['B01'])
        prepared = learning.read(self.root / 'merges/B01.input.json')
        self.assertEqual([r['origin_kind'] for r in prepared['payload']['records']],
                         ['source', 'source', 'translation'])
        self.assertEqual(set(prepared['origin_map'].values()),
                         {f'{job}:R0000' for job in ('JS0', 'JS1', 'JT0')})

    def test_unknown_merge_evidence_rejected_and_candidate_retained(self):
        write_json(self.root / 'merges/B01.input.json', {
            'payload': {'title': '合成测试书',
                        'records': [{**self.record(), 'id': 'K00000'}]},
            'origin_map': {'K00000': 'JS0:R0000'}})
        invalid = {'records': [self.record('K99999')]}
        with patch.object(learning, 'call_model', return_value=(deepcopy(invalid), 0.01)) as model:
            result = learning.execute(self.root, 'B01', 'synthetic', merge=True)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(model.call_args.args[1], learning.MERGE_PROMPT)
        self.assertEqual(learning.read(self.root / 'merges/B01.candidate.json'), invalid)
        self.assertEqual(learning.read(self.root / 'merges/B01.claim.json')['code'],
                         'invalid_learning_evidence')
        self.assertFalse((self.root / 'merges/B01.result.json').exists())


if __name__ == '__main__':
    unittest.main()
