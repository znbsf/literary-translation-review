"""Synthetic-only opt-in evidence hints; no provider or private corpus access."""
import copy
import hashlib
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
from ltr.attribution.evidence_hints import build_evidence_hints


class SyntheticOpener:
    def __init__(self, responder=None):
        self.sent = []
        self.blocks = []
        self.responder = responder

    def open(self, request, timeout):
        self.sent.append(request.data)
        payload = json.loads(json.loads(request.data)['messages'][-1]['content'])
        rows = [{'dialogue_id': t['dialogue_id'], 'speaker':None, 'addressee':None,
                 'kind':'uncertain', 'basis':'insufficient_context', 'evidence':[]}
                for t in payload['targets']]
        if self.responder:
            rows = self.responder(rows)
        blocks = [{'type':'thinking', 'thinking':'SYNTHETIC_HINT_OPAQUE', 'signature':''},
                  {'type':'redacted_thinking', 'data':''},
                  {'type':'text', 'text':json.dumps({'suggestions':rows})}]
        self.blocks.append(copy.deepcopy(blocks))
        return io.BytesIO(json.dumps({'stop_reason':'end_turn','content':blocks,
                                    'usage':{'cache_read_input_tokens':3}}).encode())


class EvidenceHintModeTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.AttributionReviewTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.env = patch.dict(os.environ, ANTHROPIC_BASE_URL='http://synthetic.invalid',
                              ANTHROPIC_AUTH_TOKEN='synthetic-token')
        self.env.start(); self.addCleanup(self.env.stop)
        self.ids = patch.object(review.uuid, 'uuid4', return_value='fixed-session')
        self.ids.start(); self.addCleanup(self.ids.stop)

    def run_worker(self, opener, session=None, **options):
        options.setdefault('batch_size',1)
        options.setdefault('history_mode','preserve-thinking')
        with patch.object(batch.urllib.request,'build_opener',return_value=opener):
            return review.run(self.case._args(**options),session=session)

    def first_payload(self, reuse=False, hints=False):
        targets,vocab = self.case.items()
        payload = {'edition_id':'e1','selection_kind':'haruhi_unknown',
                   'targets':[(review._reference_item if reuse else review._model_item)(targets[0])]}
        if reuse:
            payload['context_reference'] = review._new_references([targets[0]],{})
        payload['vocabulary'] = vocab
        if hints:
            payload['evidence_hints'] = build_evidence_hints([targets[0]],vocab)
        return payload

    def test_default_system_and_wire_and_artifact_keys_remain_exact(self):
        self.assertEqual(hashlib.sha256(review.SYSTEM.encode()).hexdigest(),
                         'b7aebadb37c51b2dfe4c1ee1495e1e42c4d7d64f6b65499f205bd290d84f67eb')
        self.assertEqual(hashlib.sha256(review.SYSTEM_REFERENCE.encode()).hexdigest(),
                         'b7d1dec9993248ebf680870d041d41adba06d3b49e46b824cb64fbb2440ea628')
        for reuse in (False,True):
            with self.subTest(reuse_context=reuse):
                documents = []
                for name,mode in [('absent',{}),('none',{'evidence_hints':'none'})]:
                    out = self.case.root/f'default-{reuse}-{name}'
                    state = {};opener = SyntheticOpener()
                    with patch('ltr.attribution.evidence_hints.build_evidence_hints') as builder:
                        summary = self.run_worker(opener,session=state,out=out,reuse_context=reuse,**mode)
                        builder.assert_not_called()
                    expected = batch.encode_request('m1',self.first_payload(reuse),
                        system=review.SYSTEM_REFERENCE if reuse else review.SYSTEM,
                        history=[],session_id='fixed-session',history_mode='preserve-thinking')
                    self.assertEqual(opener.sent,[expected])
                    data = {p.name:p.read_bytes() for p in out.glob('*.json')}
                    for value in [state,summary,*[json.loads(raw) for raw in data.values()]]:
                        self.assertNotIn('evidence_hints_mode',value)
                    self.assertNotIn('evidence_hints',json.loads(json.loads(opener.sent[0])['messages'][0]['content']))
                    documents.append(data)
                self.assertEqual(documents[0],documents[1])
                progress = json.loads(documents[0]['progress.json'])
                suggestion = json.loads(documents[0]['suggestion-000.json'])
                self.assertEqual(set(progress),{'schema','kind','edition_id','database_sha256','model',
                    'session_id','history_mode','request_byte_budget','output_token_budget',
                    'next_request_bytes','stop_reason','calls'})
                self.assertEqual(set(suggestion),{'schema','database_sha256','kind','edition_id','model',
                    'served_model','input_sha256','session_id','rows'})

    def test_enabled_payload_hints_and_static_artifact_marker(self):
        opener = SyntheticOpener();state = {}
        result = self.run_worker(opener,session=state,evidence_hints='unique-spans')
        envelope = json.loads(opener.sent[0]);payload = json.loads(envelope['messages'][0]['content'])
        self.assertEqual(payload,self.first_payload(hints=True))
        self.assertEqual(envelope['system'],review.SYSTEM+'\n\n'+review.EVIDENCE_HINT_RULES)
        self.assertEqual(state['system_sha256'],review.digest(envelope['system']))
        self.assertEqual(payload['evidence_hints'][0]['dialogue_id'],'d1')
        self.assertTrue(payload['evidence_hints'][0]['spans'])
        self.assertEqual(result['valid_rows'],1)  # Hints do not force a non-null identity.
        for data in [state,result,*[json.loads(p.read_bytes()) for p in (self.case.root/'out').glob('*.json')]]:
            self.assertEqual(data['evidence_hints_mode'],'unique-spans')
        for path in (self.case.root/'out').glob('*.json'):
            data = json.loads(path.read_bytes())
            self.assertNotIn('evidence_hints',data)
            self.assertNotIn('SYNTHETIC_HINT_OPAQUE',path.read_text())
        doc = json.loads((self.case.root/'out/suggestion-000.json').read_bytes())
        for flag,value in {'requires_review':1,'accepted':0,'human_confirmed':0,
                           'semantic_verified':0,'reference_eligible':0}.items():
            self.assertEqual(doc['rows'][0][flag],value)

    def test_reuse_two_calls_keep_full_blocks_and_current_target_hints(self):
        opener = SyntheticOpener();state = {};targets,vocab = self.case.items()
        summary = self.run_worker(opener,session=state,reuse_context=True,
                                  evidence_hints='unique-spans',max_calls=2)
        self.assertEqual((len(opener.sent),summary['valid_rows'],len(state['history'])),(2,2,4))
        first,second = map(json.loads,opener.sent)
        self.assertEqual(second['messages'][:2],state['history'][:2])
        self.assertEqual(second['messages'][1]['content'],opener.blocks[0])
        self.assertEqual(second['system'],review.SYSTEM_REFERENCE+'\n\n'+review.EVIDENCE_HINT_RULES)
        payload = json.loads(second['messages'][-1]['content'])
        self.assertNotIn('context_reference',payload)
        self.assertEqual(payload['evidence_hints'],build_evidence_hints([targets[1]],vocab))
        self.assertNotIn('text',payload['targets'][0]['context'][0])
        self.assertEqual(state['history'][-1]['content'],opener.blocks[1])

    def test_empty_hints_keep_context_and_partial_candidate_contract(self):
        targets,_ = self.case.items()
        original = copy.deepcopy(targets[0])
        hints = [{'dialogue_id':'d1','edition_id':'e1','spans':[]}]
        opener = SyntheticOpener()
        with patch('ltr.attribution.evidence_hints.build_evidence_hints',return_value=hints):
            result = self.run_worker(opener,evidence_hints='unique-spans',partial_batches=True)
        payload = json.loads(json.loads(opener.sent[0])['messages'][0]['content'])
        self.assertEqual(payload['evidence_hints'],hints)
        self.assertEqual(payload['targets'],[review._model_item(original)])
        self.assertEqual(self.case.items()[0][0],original)
        self.assertEqual(result['valid_rows'],1)
        doc = json.loads((self.case.root/'out/suggestion-000.json').read_bytes())
        self.assertEqual(doc['evidence_hints_mode'],'unique-spans')
        self.assertIs(doc['partial_batch'],True)
        self.assertEqual(doc['refused_rows'],[])
        self.assertIsNone(doc['rows'][0]['speaker'])

    def test_hint_bytes_alone_cross_budget_before_any_http_attempt(self):
        payload = self.first_payload(hints=True)
        system = review.SYSTEM+'\n\n'+review.EVIDENCE_HINT_RULES
        options = dict(system=system,history=[],session_id='fixed-session',history_mode='preserve-thinking')
        size = batch.request_byte_count('m1',payload,**options)
        plain = dict(payload);plain.pop('evidence_hints')
        limit = batch.request_byte_count('m1',plain,**options)
        self.assertGreater(size,limit)
        opener = SyntheticOpener();state = {}
        result = self.run_worker(opener,session=state,evidence_hints='unique-spans',max_request_bytes=limit)
        self.assertEqual(opener.sent,[]);self.assertEqual(state['history'],[])
        self.assertEqual(result['next_request_bytes'],size)
        self.assertEqual(result['stop_reason'],'request_byte_budget_exhausted')
        self.assertEqual((result['attempted_calls'],result['attempted_rows'],result['failed_rows']),(0,0,0))
        self.assertEqual(json.loads((self.case.root/'out/progress.json').read_bytes())['calls'],[])
        self.assertFalse(list((self.case.root/'out').glob('suggestion-*.json')))

    def test_existing_validator_rejects_nonunique_and_unsupported_identity(self):
        self.case.edit("UPDATE paragraphs SET text='Alice Alice' WHERE edition_id='e1' AND id='p1'")
        for name,identity,evidence,code in [
            ('nonunique','Alice','Alice','non_unique_evidence_span'),
            ('identity','Not Present','Alice Alice','unsupported_identity')]:
            with self.subTest(name=name):
                def respond(rows):
                    rows[0].update(speaker=identity,basis='explicit_name',
                        evidence=[{'paragraph_id':'p1','text':evidence}])
                    return rows
                opener = SyntheticOpener(respond)
                result = self.run_worker(opener,out=self.case.root/name,evidence_hints='unique-spans')
                self.assertEqual((result['valid_rows'],result['failed_rows']),(0,1))
                call = json.loads((self.case.root/name/'progress.json').read_bytes())['calls'][0]
                self.assertEqual(call['error_code'],code)
                self.assertFalse(list((self.case.root/name).glob('suggestion-*.json')))

    def test_mode_is_bound_across_child_runs_and_cannot_drift(self):
        for initial,other in [('none','unique-spans'),('unique-spans','none')]:
            with self.subTest(initial=initial):
                state = {};opener = SyntheticOpener()
                self.run_worker(opener,session=state,out=self.case.root/f'initial-{initial}',evidence_hints=initial)
                self.run_worker(opener,session=state,out=self.case.root/f'next-{initial}',start=1,evidence_hints=initial)
                self.assertEqual(len(json.loads(opener.sent[-1])['messages']),3)
                previous = copy.deepcopy(state);out = self.case.root/f'drift-{initial}'
                with patch.object(review,'request_model') as sender:
                    with self.assertRaisesRegex(ValueError,'^invalid_session$'):
                        review.run(self.case._args(out=out,evidence_hints=other,history_mode='preserve-thinking'),session=state)
                    sender.assert_not_called()
                self.assertFalse(out.exists());self.assertEqual(state,previous)
                # Even a marker changed independently of the bound SYSTEM is refused.
                corrupt = copy.deepcopy(state)
                corrupt['evidence_hints_mode'] = other
                with patch.object(review,'request_model') as sender:
                    with self.assertRaisesRegex(ValueError,'^invalid_session$'):
                        review.run(self.case._args(out=out,evidence_hints=initial,
                                                  history_mode='preserve-thinking'),session=corrupt)
                    sender.assert_not_called()
                self.assertFalse(out.exists())

    def test_invalid_mode_refused_before_source_auth_output_or_model(self):
        for value in (None,True,1,[],{},'other'):
            with self.subTest(type=type(value).__name__):
                with patch.object(review,'fingerprint') as source, patch.object(review.os.environ,'get') as auth, \
                        patch.object(review,'request_model') as sender:
                    with self.assertRaisesRegex(ValueError,'^invalid_evidence_hint_mode$'):
                        review.run(self.case._args(evidence_hints=value))
                    source.assert_not_called();auth.assert_not_called();sender.assert_not_called()
                self.assertFalse((self.case.root/'out').exists())

    def test_cli_enabled_and_invalid_mode_reports_static_error(self):
        out = self.case.root/'cli'
        argv = ['--db',str(self.case.db),'--kind','haruhi_unknown','--edition','e1',
                '--out',str(out),'--model','m1','--batch-size','1','--execute']
        opener = SyntheticOpener()
        with patch.object(batch.urllib.request,'build_opener',return_value=opener), \
                patch('sys.stdout',new_callable=io.StringIO) as stdout:
            self.assertEqual(review.main(argv+['--history-mode','preserve-thinking','--evidence-hints','unique-spans']),0)
        self.assertEqual(json.loads(stdout.getvalue())['evidence_hints_mode'],'unique-spans')
        bad='SYNTHETIC_PRIVATE_INVALID_MODE'
        with patch.object(review,'run') as runner,patch('sys.stderr',new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit):review.main(argv+['--evidence-hints',bad])
            runner.assert_not_called()
        self.assertIn('invalid_evidence_hint_mode',stderr.getvalue())
        self.assertNotIn(bad,stderr.getvalue())


if __name__ == '__main__':unittest.main()
