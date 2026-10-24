from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ltr.project import batch_translate as batch
from ltr.project.dimension_cycle import CycleError
from ltr.project.storage import digest, write_json


class BatchTranslateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ids = []
        self.add_job('J01')

    def read(self, path):
        return json.loads((self.root/path).read_text(encoding='utf-8'))

    def add_job(self, identifier):
        payload = {'source': [{'id': 'P0', 'text': 'Synthetic source one.'},
                              {'id': 'P1', 'text': 'Synthetic source two.'}],
                   'context': {'profile': 'Synthetic narrative voice.'},
                   'adjacent_source_before': [{'id': 'B0', 'text': 'Previous synthetic paragraph.'}],
                   'adjacent_source_after': []}
        job = {'payload': payload, 'source_ids': ['original-one', 'original-two'],
               'source_digest': digest(payload['source']), 'context_digest': digest(payload['context'])}
        write_json(self.root/'jobs'/f'{identifier}.input.json', job)
        self.ids.append(identifier)
        write_json(self.root/'plan.json', {'jobs': self.ids, 'books': [{'book_id': 'B1', 'jobs': self.ids}]})
        return job

    def transport(self, model, prompt, payload, timeout):
        self.assertEqual(timeout, 600)
        self.assertEqual(prompt, batch.PROMPT)
        self.assertNotIn('source_ids', payload)
        return {'units': [{'id': row['id'], 'target': '合成译文。'} for row in payload['source']],
                'issues': [{'id': 'P0', 'kind': 'synthetic_uncertainty'}]}, 0.01

    def test_success_binds_input_prompt_model_and_preserves_candidate(self):
        with patch.object(batch, 'call_model', side_effect=self.transport) as model:
            outcome = batch.execute_job(self.root, 'J01', 'synthetic-model')
        self.assertEqual(outcome['status'], 'completed')
        claim = self.read('jobs/J01.claim.json'); result = self.read('jobs/J01.result.json')
        self.assertEqual(claim['input_digest'], digest(self.read('jobs/J01.input.json')))
        self.assertEqual(claim['prompt_digest'], digest(batch.PROMPT))
        self.assertEqual(claim['result_digest'], digest(result))
        self.assertEqual([u['id'] for u in result['units']], ['P0', 'P1'])
        self.assertEqual(result['source_ids'], ['original-one', 'original-two'])
        self.assertEqual(result['status'], 'draft_single_pass')
        self.assertTrue((self.root/'jobs/J01.candidate.json').exists())
        model.assert_called_once()

    def test_missing_duplicate_empty_and_adjacent_units_are_rejected(self):
        variants = [[{'id': 'P0', 'target': '合成'}],
                    [{'id': 'P0', 'target': '合成'}, {'id': 'P0', 'target': '合成'}],
                    [{'id': 'P0', 'target': '合成'}, {'id': 'P1', 'target': ' '}],
                    [{'id': 'B0', 'target': '合成'}, {'id': 'P0', 'target': '合成'}, {'id': 'P1', 'target': '合成'}]]
        for i, units in enumerate(variants):
            identifier = f'BAD{i}'; self.add_job(identifier)
            with self.subTest(i=i), patch.object(batch, 'call_model', return_value=({'units': units}, 0.01)):
                self.assertEqual(batch.execute_job(self.root, identifier, 'synthetic')['status'], 'failed')
                self.assertTrue((self.root/'jobs'/f'{identifier}.candidate.json').exists())
                self.assertFalse((self.root/'jobs'/f'{identifier}.result.json').exists())

    def test_per_job_lock_prevents_duplicate_calls(self):
        started, release = threading.Event(), threading.Event()
        def transport(*args, **kwargs):
            started.set(); self.assertTrue(release.wait(timeout=5))
            return self.transport(*args, **kwargs)
        with patch.object(batch, 'call_model', side_effect=transport) as model:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(batch.execute_job, self.root, 'J01', 'synthetic')
                try:
                    self.assertTrue(started.wait(timeout=5))
                    self.assertEqual(pool.submit(batch.execute_job, self.root, 'J01', 'synthetic').result(timeout=3)['status'], 'in_progress')
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=5)['status'], 'completed')
        model.assert_called_once()

    def test_input_mapping_and_model_changes_prevent_reuse(self):
        with patch.object(batch, 'call_model', side_effect=self.transport) as model:
            batch.execute_job(self.root, 'J01', 'synthetic')
            with self.assertRaisesRegex(CycleError, 'batch_model_change_rejected'):
                batch.execute_job(self.root, 'J01', 'other-model')
            job = self.read('jobs/J01.input.json'); job['source_ids'][0] = 'changed-origin'
            write_json(self.root/'jobs/J01.input.json', job)
            with self.assertRaisesRegex(CycleError, 'batch_input_or_prompt_changed'):
                batch.execute_job(self.root, 'J01', 'synthetic')
        model.assert_called_once()
        self.assertEqual(batch.status(self.root)['requires_resolution'], 1)

    def test_resume_uses_verified_results_and_status_ignores_unplanned_claims(self):
        self.add_job('J02')
        with patch.object(batch, 'call_model', side_effect=self.transport) as model, redirect_stdout(io.StringIO()):
            first = batch.run(self.root, 'synthetic', workers=2)
            second = batch.run(self.root, 'synthetic', workers=2)
        self.assertEqual(model.call_count, 2)
        self.assertEqual((first['verified_completed'], second['verified_completed']), (2, 2))
        write_json(self.root/'jobs/EXTRA.claim.json', {'status': 'completed'})
        self.assertEqual(batch.status(self.root)['planned_jobs'], 2)
        result = self.read('jobs/J01.result.json'); result['units'][0]['target'] = '被修改的合成文字'
        write_json(self.root/'jobs/J01.result.json', result)
        progress = batch.status(self.root)
        self.assertEqual((progress['verified_completed'], progress['requires_resolution']), (1, 1))

    def test_stop_before_run_and_between_jobs_prevents_new_dispatch(self):
        self.add_job('J02'); (self.root/'STOP').write_text('synthetic stop')
        with patch.object(batch, 'call_model') as model, redirect_stdout(io.StringIO()):
            self.assertEqual(batch.run(self.root, 'synthetic', workers=1)['undispatched'], 2)
        model.assert_not_called(); (self.root/'STOP').unlink()
        def transport(*args, **kwargs):
            (self.root/'STOP').write_text('synthetic stop')
            return self.transport(*args, **kwargs)
        with patch.object(batch, 'call_model', side_effect=transport) as model, redirect_stdout(io.StringIO()):
            progress = batch.run(self.root, 'synthetic', workers=1)
        model.assert_called_once()
        self.assertEqual((progress['verified_completed'], progress['undispatched']), (1, 1))

    def test_three_consecutive_failures_stop_new_dispatch_without_retry(self):
        for i in range(2, 6): self.add_job(f'J{i:02d}')
        with patch.object(batch, 'call_model', side_effect=CycleError('runtime_timeout')) as model, redirect_stdout(io.StringIO()):
            progress = batch.run(self.root, 'synthetic', workers=1)
        self.assertEqual(model.call_count, 3)
        self.assertEqual((progress['failed'], progress['undispatched']), (3, 2))
        self.assertFalse((self.root/'attempt-history').exists())

    def test_explicit_technical_retry_archives_evidence_and_is_allowed_once(self):
        with patch.object(batch, 'call_model', side_effect=CycleError('runtime_timeout')):
            batch.execute_job(self.root, 'J01', 'synthetic')
        # A pre-existing private candidate is retained exactly in attempt history.
        write_json(self.root/'jobs/J01.candidate.json', {'synthetic': 'failed candidate'})
        old_claim = (self.root/'jobs/J01.claim.json').read_bytes()
        old_candidate = (self.root/'jobs/J01.candidate.json').read_bytes()
        with patch.object(batch, 'call_model', side_effect=self.transport) as model:
            outcome = batch.retry(self.root, 'J01', 'synthetic')
            with self.assertRaisesRegex(CycleError, 'batch_retry_not_eligible'):
                batch.retry(self.root, 'J01', 'synthetic')
        model.assert_called_once(); self.assertEqual(outcome['status'], 'completed')
        history = self.root/'attempt-history/J01/attempt-1'
        self.assertEqual((history/'claim.json').read_bytes(), old_claim)
        self.assertEqual((history/'candidate.json').read_bytes(), old_candidate)
        self.assertEqual(self.read('jobs/J01.claim.json')['attempt'], 2)

    def test_validation_failure_is_not_eligible_for_technical_retry(self):
        with patch.object(batch, 'call_model', return_value=({'units': []}, 0.01)) as model:
            batch.execute_job(self.root, 'J01', 'synthetic')
            with self.assertRaisesRegex(CycleError, 'batch_retry_not_eligible'):
                batch.retry(self.root, 'J01', 'synthetic')
        model.assert_called_once()

    def test_invalid_payload_is_rejected_before_model_dispatch(self):
        job = self.read('jobs/J01.input.json'); job['payload']['source'][1]['id'] = 'P0'
        job['source_digest'] = digest(job['payload']['source'])
        write_json(self.root/'jobs/J01.input.json', job)
        with patch.object(batch, 'call_model') as model:
            with self.assertRaisesRegex(CycleError, 'batch_duplicate_source_id'):
                batch.execute_job(self.root, 'J01', 'synthetic')
        model.assert_not_called()
        self.assertFalse((self.root/'jobs/J01.claim.json').exists())

    def test_missing_only_repair_preserves_existing_strings_and_evidence(self):
        first = {'units': [{'id': 'P0', 'target': '  已有合成译文。\n保留换行。'}], 'issues': ['old issue']}
        with patch.object(batch, 'call_model', return_value=(deepcopy(first), 0.01)):
            self.assertEqual(batch.execute_job(self.root, 'J01', 'synthetic')['status'], 'failed')
        old_candidate = (self.root/'jobs/J01.candidate.json').read_bytes()
        def repair_transport(model, prompt, payload, timeout):
            self.assertEqual(timeout, 600)
            self.assertEqual(prompt, batch.REPAIR_PROMPT)
            self.assertEqual([r['id'] for r in payload['source']], ['P1'])
            self.assertEqual(payload['context'], self.read('jobs/J01.input.json')['payload']['context'])
            self.assertNotIn(first['units'][0]['target'], json.dumps(payload, ensure_ascii=False))
            return {'units': [{'id': 'P1', 'target': '补齐的合成译文。'}], 'issues': ['new issue']}, 0.01
        with patch.object(batch, 'call_model', side_effect=repair_transport) as model:
            self.assertEqual(batch.repair_missing(self.root, 'J01', 'synthetic')['status'], 'completed')
            with self.assertRaisesRegex(CycleError, 'batch_missing_repair_not_eligible'):
                batch.repair_missing(self.root, 'J01', 'synthetic')
        model.assert_called_once()
        result = self.read('jobs/J01.result.json')
        self.assertEqual(result['units'][0], first['units'][0])
        self.assertEqual(result['issues'], ['old issue', 'new issue'])
        self.assertEqual((self.root/'attempt-history/J01/attempt-1/candidate.json').read_bytes(), old_candidate)
        self.assertEqual((self.root/'jobs/J01.candidate.json').read_bytes(), old_candidate)
        self.assertEqual(batch.status(self.root)['verified_completed'], 1)
        repair_candidate = self.read('missing-repairs/J01/candidate.json')
        repair_candidate['units'][0]['target'] = 'tampered'
        write_json(self.root/'missing-repairs/J01/candidate.json', repair_candidate)
        self.assertEqual(batch.status(self.root)['requires_resolution'], 1)

    def test_missing_repair_refuses_reordered_duplicate_unknown_and_empty_units(self):
        variants = [[{'id': 'P1', 'target': '合成'}, {'id': 'P0', 'target': '合成'}],
                    [{'id': 'P0', 'target': '合成'}, {'id': 'P0', 'target': '合成'}],
                    [{'id': 'P0', 'target': '合成'}, {'id': 'UNKNOWN', 'target': '合成'}],
                    [{'id': 'P0', 'target': ''}]]
        for i, units in enumerate(variants):
            identifier = f'REPAIR_BAD{i}'; job = self.add_job(identifier)
            job['payload']['source'].append({'id': 'P2', 'text': 'Synthetic third paragraph.'})
            job['source_ids'].append('original-three'); job['source_digest'] = digest(job['payload']['source'])
            write_json(self.root/'jobs'/f'{identifier}.input.json', job)
            with self.subTest(i=i), patch.object(batch, 'call_model', return_value=({'units': units}, 0.01)) as model:
                batch.execute_job(self.root, identifier, 'synthetic')
                with self.assertRaisesRegex(CycleError, 'batch_invalid_repair_subsequence'):
                    batch.repair_missing(self.root, identifier, 'synthetic')
                model.assert_called_once()

    def test_failed_missing_repair_is_not_repeated(self):
        with patch.object(batch, 'call_model', return_value=({'units': [{'id': 'P0', 'target': '合成'}]}, 0.01)):
            batch.execute_job(self.root, 'J01', 'synthetic')
        with patch.object(batch, 'call_model', return_value=({'units': []}, 0.01)) as model:
            self.assertEqual(batch.repair_missing(self.root, 'J01', 'synthetic')['status'], 'failed')
            with self.assertRaisesRegex(CycleError, 'batch_missing_repair_not_eligible'):
                batch.repair_missing(self.root, 'J01', 'synthetic')
        model.assert_called_once()
        self.assertTrue((self.root/'missing-repairs/J01/candidate.json').exists())
        self.assertEqual(self.read('jobs/J01.claim.json')['repair_calls'], 1)


if __name__ == '__main__':
    unittest.main()
