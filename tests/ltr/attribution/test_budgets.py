"""Synthetic-only exact request budgets for continuous attribution workers."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from tests.ltr.attribution import test_review as fixtures
from ltr.attribution import review
from ltr.quotes import batch


class Opener:
    def __init__(self, preserve=False):
        self.preserve, self.sent = preserve, []

    def open(self, request, timeout):
        self.sent.append(request.data)
        payload = json.loads(json.loads(request.data)['messages'][-1]['content'])
        rows = [{'dialogue_id': row['dialogue_id'], 'speaker':None, 'addressee':None,
                 'kind':'uncertain', 'basis':'insufficient_context', 'evidence':[]}
                for row in payload['targets']]
        content = [{'type':'text', 'text':json.dumps({'suggestions':rows})}]
        if self.preserve:
            content = [{'type':'thinking', 'thinking':'PRIVATE_SYNTHETIC_THOUGHT',
                        'signature':'PRIVATE_SYNTHETIC_SIGNATURE' * 2000},
                       {'type':'redacted_thinking', 'data':''}] + content
        return io.BytesIO(json.dumps({'stop_reason':'end_turn','content':content}).encode())


class AttributionBudgetTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.AttributionReviewTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.env = patch.dict(os.environ, ANTHROPIC_BASE_URL='http://synthetic.invalid',
                              ANTHROPIC_AUTH_TOKEN='dummy')
        self.env.start(); self.addCleanup(self.env.stop)
        self.ids = patch.object(review.uuid, 'uuid4', return_value='fixed-session')
        self.ids.start(); self.addCleanup(self.ids.stop)

    def first_payload(self):
        targets, vocabulary = self.case.items()
        return {'edition_id':'e1','selection_kind':'haruhi_unknown',
                'targets':[review._model_item(targets[0])], 'vocabulary':vocabulary}

    def first_size(self, **options):
        return batch.request_byte_count('m1',self.first_payload(),system=review.SYSTEM,
                                        history=[],session_id='fixed-session',**options)

    def run_worker(self, opener, session=None, **options):
        with patch.object(batch.urllib.request,'build_opener',return_value=opener):
            return review.run(self.case._args(batch_size=1,max_calls=1,**options),session=session)

    def test_exact_limit_dispatches_and_records_actual_encoded_size(self):
        size = self.first_size(max_output_tokens=1234)
        opener = Opener()
        result = self.run_worker(opener,max_request_bytes=size,max_output_tokens=1234)
        self.assertEqual(len(opener.sent),1)
        self.assertEqual(len(opener.sent[0]),size)
        self.assertEqual(json.loads(opener.sent[0])['max_tokens'],1234)
        self.assertEqual((result['attempted_rows'],result['valid_rows'],result['failed_rows']),(1,1,0))
        self.assertEqual((result['request_byte_budget'],result['output_token_budget'],result['next_request_bytes']),
                         (size,1234,None))
        progress = json.loads((self.case.root/'out'/'progress.json').read_bytes())
        self.assertEqual(progress['calls'][0]['request_bytes'],size)
        for key in ('request_byte_budget','output_token_budget','next_request_bytes','stop_reason'):
            self.assertEqual(progress[key],result[key])

    def test_one_byte_over_budget_is_unattempted_with_zero_requests(self):
        size = self.first_size()
        opener = Opener(); state = {}
        result = self.run_worker(opener,session=state,max_request_bytes=size-1)
        self.assertEqual(opener.sent,[]); self.assertEqual(state['history'],[])
        self.assertEqual(result['stop_reason'],'request_byte_budget_exhausted')
        self.assertEqual(result['next_request_bytes'],size)
        for key in ('calls','attempted_calls','attempted_rows','valid_rows','failed_calls','failed_rows'):
            self.assertEqual(result[key],0)
        self.assertEqual(result['unattempted_rows'],5)
        progress = json.loads((self.case.root/'out'/'progress.json').read_bytes())
        self.assertEqual(progress['calls'],[])
        self.assertEqual(progress['stop_reason'],result['stop_reason'])
        self.assertEqual(progress['next_request_bytes'],size)
        self.assertFalse(list((self.case.root/'out').glob('suggestion-*.json')))

    def test_full_preserved_history_is_counted_before_next_dispatch(self):
        for reference in (False,True):
            with self.subTest(reuse_context=reference):
                state = {}; opener = Opener(preserve=True)
                out = self.case.root/f'preserve-{reference}'
                with patch.object(batch.urllib.request,'build_opener',return_value=opener):
                    result = review.run(self.case._args(out=out,batch_size=1,max_calls=3,
                        reuse_context=reference,history_mode='preserve-thinking',
                        max_request_bytes=20000,max_output_tokens=16000),session=state)
                self.assertEqual(len(opener.sent),1)
                self.assertEqual((result['attempted_rows'],result['valid_rows'],result['failed_rows'],
                                  result['unattempted_rows']),(1,1,0,4))
                self.assertEqual(result['stop_reason'],'request_byte_budget_exhausted')
                targets,_ = self.case.items()
                if reference:
                    payload = {'edition_id':'e1','selection_kind':'haruhi_unknown',
                               'targets':[review._reference_item(targets[1])]}
                    refs = review._new_references([targets[1]],review._supplied_references(
                        state['history'],'preserve-thinking'))
                    if refs: payload['context_reference'] = refs
                else:
                    payload = {'edition_id':'e1','selection_kind':'haruhi_unknown',
                               'targets':[review._model_item(targets[1])]}
                expected = batch.request_byte_count('m1',payload,system=review.SYSTEM_REFERENCE if reference
                    else review.SYSTEM,history=state['history'],session_id='fixed-session',
                    history_mode='preserve-thinking',max_output_tokens=16000)
                self.assertEqual(result['next_request_bytes'],expected)
                self.assertGreater(expected,50000)
                self.assertEqual(state['history'][1]['content'][0]['signature'],
                                 'PRIVATE_SYNTHETIC_SIGNATURE'*2000)
                body = ''.join(path.read_text(encoding='utf-8') for path in out.glob('*.json'))
                self.assertNotIn('PRIVATE_SYNTHETIC',body)

    def test_budget_and_output_limit_are_frozen_across_children(self):
        state = {}; opener = Opener()
        first = self.run_worker(opener,session=state,out=self.case.root/'first',
                                max_request_bytes=1000000,max_output_tokens=1234)
        second = self.run_worker(opener,session=state,out=self.case.root/'second',start=1,
                                 max_request_bytes=1000000,max_output_tokens=1234)
        self.assertEqual(first['session_id'],second['session_id'])
        self.assertEqual(len(json.loads(opener.sent[1])['messages']),3)
        self.assertEqual(second['valid_rows'],1)
        before = copy.deepcopy(state)
        variants = [(None,1234),(999999,1234),(1000001,1234),(1000000,None),(1000000,1235)]
        for index,(request_limit,output_limit) in enumerate(variants):
            out = self.case.root/f'drift-{index}'
            with patch.object(review,'request_model') as sender:
                with self.assertRaisesRegex(ValueError,'^invalid_session$'):
                    review.run(self.case._args(out=out,start=2,max_request_bytes=request_limit,
                                               max_output_tokens=output_limit),session=state)
                sender.assert_not_called()
            self.assertFalse(out.exists()); self.assertEqual(state,before)

    def test_legacy_session_defaults_preserve_actual_wire(self):
        state = {'model':'m1','database_sha256':fixtures.fingerprint(self.case.db),
                 'system_sha256':review.digest(review.SYSTEM),'session_id':'legacy',
                 'history':[{'role':'user','content':'{}'},{'role':'assistant','content':'{}'}]}
        before = copy.deepcopy(state['history'])
        expected = batch.encode_request('m1',self.first_payload(),system=review.SYSTEM,
                                        history=before,session_id='legacy')
        opener = Opener(); result = self.run_worker(opener,session=state)
        self.assertEqual(opener.sent,[expected])
        self.assertIsNone(result['request_byte_budget'])
        self.assertEqual(result['output_token_budget'],8000)
        self.assertEqual(state['request_byte_budget'],None)
        self.assertEqual(state['output_token_budget'],8000)

    def test_legacy_session_cannot_add_budget_under_same_id(self):
        for options in ({'max_request_bytes':1000000},{'max_output_tokens':16000}):
            state = {'model':'m1','database_sha256':fixtures.fingerprint(self.case.db),
                     'system_sha256':review.digest(review.SYSTEM),'session_id':'legacy','history':[]}
            with patch.object(review,'request_model') as sender:
                with self.assertRaisesRegex(ValueError,'^invalid_session$'):
                    review.run(self.case._args(**options),session=state)
                sender.assert_not_called()
            self.assertFalse((self.case.root/'out').exists())

    def test_invalid_budgets_rejected_before_source_or_output(self):
        for field, values, code in [
                ('max_request_bytes',[True,0,-1,1.0,'100',review.MAX_REQUEST_BYTES+1],
                 'invalid_request_byte_budget'),
                ('max_output_tokens',[True,0,-1,1.0,'100',131073],'invalid_output_token_budget')]:
            for value in values:
                with patch.object(review,'fingerprint') as source, patch.object(review,'request_model') as sender:
                    with self.assertRaisesRegex(ValueError,'^'+code+'$'):
                        review.run(self.case._args(**{field:value}))
                    source.assert_not_called(); sender.assert_not_called()
                self.assertFalse((self.case.root/'out').exists())

    def test_corrupted_session_budget_types_are_refused(self):
        opener = Opener(); state = {}
        self.run_worker(opener,session=state,out=self.case.root/'initial')
        for field,value in [('request_byte_budget',True),('output_token_budget',8000.0)]:
            broken = copy.deepcopy(state); broken[field] = value
            with patch.object(review,'request_model') as sender:
                with self.assertRaisesRegex(ValueError,'^invalid_session$'):
                    review.run(self.case._args(out=self.case.root/'broken'),session=broken)
                sender.assert_not_called()
            self.assertFalse((self.case.root/'broken').exists())

    def test_cli_budgets_end_to_end_and_static_invalid_error(self):
        argv = ['--db',str(self.case.db),'--kind','haruhi_unknown','--edition','e1',
                '--out',str(self.case.root/'cli'),'--model','m1','--batch-size','1','--execute']
        opener = Opener()
        with patch.object(batch.urllib.request,'build_opener',return_value=opener), \
                patch('sys.stdout',new_callable=io.StringIO) as captured:
            self.assertEqual(review.main(argv+['--max-request-bytes','1','--max-output-tokens','16000']),0)
        result = json.loads(captured.getvalue())
        self.assertEqual((result['request_byte_budget'],result['output_token_budget']),(1,16000))
        self.assertEqual(result['stop_reason'],'request_byte_budget_exhausted')
        self.assertEqual(opener.sent,[])
        with patch('sys.stderr',new_callable=io.StringIO) as captured:
            with self.assertRaises(SystemExit): review.main(argv+['--max-output-tokens','0'])
        self.assertEqual(captured.getvalue(),'attribution review refused: invalid_output_token_budget\n')


if __name__ == '__main__': unittest.main()
