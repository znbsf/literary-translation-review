"""Synthetic, source-free tests for the private attribution suggestion runtime."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from ltr.attribution.review import (
    load_targets, load_vocabulary, validate_suggestions, validate_partial_suggestions, run, main,
    SYSTEM, SYSTEM_REFERENCE, digest, _context_id, _model_item, _reference_item, _new_references,
)
from ltr.quotes.batch import ModelResponseError
from ltr.library.review_queue import fingerprint

SCHEMA = """
CREATE TABLE editions(id TEXT,title TEXT,language TEXT);
CREATE TABLE paragraphs(id TEXT,edition_id TEXT,ordinal INTEGER,text TEXT);
CREATE TABLE dialogues(id TEXT,edition_id TEXT,ordinal INTEGER,text TEXT,start_para INTEGER,
    end_para INTEGER,speaker TEXT,interlocutor TEXT,attribution_status TEXT);
CREATE TABLE attribution_candidates(dialogue_id TEXT,edition_id TEXT,speaker_surface TEXT,
    speaker_canonical TEXT,addressee_surface TEXT,addressee_canonical TEXT);
CREATE TABLE corpus_completion_quote(quote_id TEXT,edition_id TEXT,kind TEXT,speaker_id TEXT,
    addressee_id TEXT,speaker_status TEXT,addressee_status TEXT,status TEXT,requires_review INTEGER);
"""
PARAGRAPHS = [('p0', 'e1', 0, 'Narration zero.'), ('p1', 'e1', 1, 'Alice called out loudly.'),
              ('p2', 'e1', 2, 'The room was silent.'), ('p3', 'e1', 3, 'Bob answered softly.'),
              ('p4', 'e1', 4, 'End of scene.'),
              ('p0', 'e2', 0, 'e2 zero.'), ('p1', 'e2', 1, 'Someone else spoke.'),
              ('p2', 'e2', 2, 'e2 two.'), ('p3', 'e2', 3, 'e2 three.'), ('p4', 'e2', 4, 'e2 four.')]
DIALOGUES = [('d1', 'e1', 0, 'LINE_ONE_SENTINEL', 1, 2, 'Alice', 'Bob', 'unknown'),
             ('d2', 'e1', 1, 'LINE_TWO_SENTINEL', 2, 3, 'Bob', 'Alice', 'unknown'),
             ('d3', 'e1', 2, 'LINE_THREE_SENTINEL', 3, 4, 'Alice', 'Bob', 'unknown'),
             ('d4', 'e1', 3, 'LINE_FOUR_SENTINEL', 1, 1, 'Bob', 'Alice', 'unknown'),
             ('d5', 'e1', 4, 'LINE_FIVE_SENTINEL', 0, 4, 'Alice', 'Bob', 'unknown'),
             ('dx', 'e2', 0, 'OTHER_EDITION_SENTINEL', 1, 2, 'Carol', 'Dave', 'unknown')]
CANDIDATES = [('d1', 'e1', 'Alice', 'unknown', 'Bob', ''),
              ('d2', 'e1', 'Bob', None, 'Alice', None),
              ('d3', 'e1', 'Alice', '', None, ''),
              ('d4', 'e1', None, None, None, None),
              ('d5', 'e1', 'Alice', 'unknown', 'Bob', None)]
QUOTES = [('d1', 'e1', 'quote', 'Alice', 'Bob', 'unknown', 'unknown', 'pending', 1),
          ('d2', 'e1', 'quote', 'Bob', 'Alice', 'unknown', 'unknown', 'candidate', 0)]


class AttributionReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.db = self.source / 'dialogue.sqlite'
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        con.executemany("INSERT INTO editions VALUES (?,?,?)",
                        [('e1', 'One', 'zh'), ('e2', 'Two', 'zh')])
        con.executemany("INSERT INTO paragraphs VALUES (?,?,?,?)", PARAGRAPHS)
        con.executemany("INSERT INTO dialogues VALUES (?,?,?,?,?,?,?,?,?)", DIALOGUES)
        con.executemany("INSERT INTO attribution_candidates VALUES (?,?,?,?,?,?)", CANDIDATES)
        con.executemany("INSERT INTO corpus_completion_quote VALUES (?,?,?,?,?,?,?,?,?)", QUOTES)
        con.commit()
        con.close()

    def connect(self):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        return con

    def edit(self, sql, params=()):
        con = sqlite3.connect(self.db)
        con.execute(sql, params)
        con.commit()
        con.close()

    def items(self, kind='haruhi_unknown', edition='e1'):
        con = self.connect()
        try:
            return load_targets(con, kind, edition), load_vocabulary(con, edition)
        finally:
            con.close()

    def rows(self):
        def null(dialogue_id):
            return {'dialogue_id': dialogue_id, 'speaker': None, 'addressee': None,
                    'kind': 'uncertain', 'basis': 'insufficient_context', 'evidence': []}
        return [{'dialogue_id': 'd1', 'speaker': 'Alice', 'addressee': 'Bob', 'kind': 'spoken',
                 'basis': 'explicit_name', 'evidence': [{'paragraph_id': 'p1', 'text': 'Alice called out'}]},
                null('d2'), null('d3'), null('d4'), null('d5')]

    def test_haruhi_targets_bind_edition_scoped_context(self):
        targets, vocabulary = self.items()
        self.assertEqual([t['dialogue_id'] for t in targets], ['d1', 'd2', 'd3', 'd4', 'd5'])
        self.assertEqual(vocabulary, ['Alice', 'Bob'])

        target = targets[0]
        self.assertEqual(target['text'], 'LINE_ONE_SENTINEL')
        self.assertEqual([p['ordinal'] for p in target['context']], [0, 1, 2, 3, 4])
        self.assertEqual(target['dialogue_text_sha256'], hashlib.sha256(b'LINE_ONE_SENTINEL').hexdigest())
        self.assertEqual(len(target['context_sha256']), 64)
        paragraph = next(p for p in target['context'] if p['id'] == 'p1')
        self.assertEqual((paragraph['edition_id'], paragraph['text']), ('e1', 'Alice called out loudly.'))

    def test_single_target_lookup_retains_pending_selection_rules(self):
        con=self.connect()
        try:
            self.assertEqual([r['dialogue_id'] for r in load_targets(con,'haruhi_unknown','e1','d1')],['d1'])
            self.assertEqual(load_targets(con,'completion_quote','e1','d2'),[])
        finally:con.close()

    def test_completion_quote_selects_only_requires_review(self):
        targets, _ = self.items(kind='completion_quote')
        self.assertEqual([t['dialogue_id'] for t in targets], ['d1'])

    def test_same_paragraph_id_in_other_edition_never_resolves(self):
        targets, _ = self.items()
        for target in targets:
            for paragraph in target['context']:
                self.assertEqual(paragraph['edition_id'], 'e1')
                self.assertNotIn('Someone else spoke.', paragraph['text'])

    def test_missing_context_refused(self):
        self.edit("DELETE FROM paragraphs WHERE edition_id='e1' AND ordinal=2")
        with self.assertRaises(ValueError) as raised:
            self.items()
        self.assertEqual(str(raised.exception), 'missing_context')

    def test_unmapped_duplicate_and_ambiguous_ids_refused(self):
        for dialogue_id, expected in [('ghost', 'unmapped_source_id'),
                                      ('d1', 'duplicate_source_id'),
                                      ('dx', 'ambiguous_source_id')]:
            self.edit("INSERT INTO attribution_candidates VALUES (?,'e1',NULL,NULL,NULL,NULL)", (dialogue_id,))
            with self.assertRaises(ValueError) as raised:
                self.items()
            self.assertEqual(str(raised.exception), expected)
            self.edit("DELETE FROM attribution_candidates WHERE dialogue_id=? AND speaker_surface IS NULL",
                      (dialogue_id,))

    def test_global_duplicate_dialogue_id_refused(self):
        self.edit("INSERT INTO dialogues VALUES ('d1','e2',0,'dup',1,2,'Carol','Dave','unknown')")
        with self.assertRaises(ValueError) as raised:
            self.items()
        self.assertEqual(str(raised.exception), 'ambiguous_source_id')

    def _reject(self, mutate, expected):
        items, vocabulary = self.items()
        rows = self.rows()
        mutate(rows)
        with self.assertRaises(ValueError) as raised:
            validate_suggestions({'suggestions': rows}, items, vocabulary)
        self.assertEqual(str(raised.exception), expected)

    def test_evidence_locators_bind_offsets_and_hash_without_text(self):
        items, vocabulary = self.items()
        result = validate_suggestions({'suggestions': self.rows()}, items, vocabulary)
        self.assertEqual([r['dialogue_id'] for r in result], ['d1', 'd2', 'd3', 'd4', 'd5'])
        evidence = result[0]['evidence'][0]
        self.assertEqual((evidence['edition_id'], evidence['paragraph_id'], evidence['ordinal']), ('e1', 'p1', 1))
        self.assertEqual((evidence['start'], evidence['end']), (0, len('Alice called out')))
        self.assertEqual(evidence['paragraph_sha256'], hashlib.sha256('Alice called out loudly.'.encode()).hexdigest())
        self.assertNotIn('text', evidence)
        self.assertEqual(result[0]['source_candidate_sha256'], hashlib.sha256(
            json.dumps({'dialogue_id': 'd1', 'edition_id': 'e1', 'speaker_surface': 'Alice',
                        'speaker_canonical': 'unknown', 'addressee_surface': 'Bob', 'addressee_canonical': ''},
                       ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest())

    def test_identity_substring_of_context_accepted(self):
        items, vocabulary = self.items()
        rows = self.rows()
        rows[0] = {'dialogue_id': 'd1', 'speaker': 'silent', 'addressee': None, 'kind': 'spoken',
                   'basis': 'context_inference', 'evidence': [{'paragraph_id': 'p2', 'text': 'silent'}]}
        result = validate_suggestions({'suggestions': rows}, items, vocabulary)
        self.assertEqual(result[0]['speaker'], 'silent')

    def test_invalid_enum_and_coverage_rejected(self):
        self._reject(lambda rows: rows[0].update(kind='bogus'), 'invalid_enum')
        self._reject(lambda rows: rows[0].update(basis='bogus'), 'invalid_enum')
        self._reject(lambda rows: rows.pop(), 'invalid_suggestion_contract')
        self._reject(lambda rows: rows.__setitem__(4, dict(rows[3])), 'inexact_target_coverage')

    def test_invalid_and_unsupported_identity_rejected(self):
        self._reject(lambda rows: rows[0].update(speaker='unknown'), 'invalid_identity')
        self._reject(lambda rows: rows[0].update(speaker='A' * 81), 'invalid_identity')
        self._reject(lambda rows: rows[0].update(speaker='Zed'), 'unsupported_identity')

    def test_evidence_binding_rejected(self):
        self._reject(lambda rows: rows[0].update(evidence=[{'paragraph_id': 'p1', 'text': 'zzz'}]),
                     'non_unique_evidence_span')
        self._reject(lambda rows: rows[0].update(evidence=[{'paragraph_id': 'p9', 'text': 'Alice called out'}]),
                     'unbound_evidence')
        self._reject(lambda rows: rows[0].update(evidence=[{'paragraph_id': 'p1', 'text': 'Someone else spoke.'}]),
                     'non_unique_evidence_span')
        self._reject(lambda rows: rows[0].update(evidence=[{'paragraph_id': 'p1', 'text': 'Alice called out'},
                                                           {'paragraph_id': 'p1', 'text': 'Alice called out'}]),
                     'duplicate_evidence_span')
        self._reject(lambda rows: rows[0].update(evidence=[{'paragraph_id': 'p1', 'text': 'Alice called out', 'x': 1}]),
                     'invalid_evidence')

    def test_non_null_identity_requires_evidence(self):
        self._reject(lambda rows: rows[0].update(evidence=[]), 'missing_identity_evidence')

    def test_not_dialogue_and_null_rules(self):
        items, vocabulary = self.items()
        rows = self.rows()
        rows[0] = {'dialogue_id': 'd1', 'speaker': None, 'addressee': None, 'kind': 'not_dialogue',
                   'basis': 'source_not_dialogue', 'evidence': []}
        result = validate_suggestions({'suggestions': rows}, items, vocabulary)
        self.assertEqual(result[0]['kind'], 'not_dialogue')
        self._reject(lambda rows: rows[0].update(kind='not_dialogue', basis='insufficient_context'),
                     'invalid_not_dialogue')
        self._reject(lambda rows: rows[0].update(kind='not_dialogue', speaker='Alice',
                                                 basis='source_not_dialogue',
                                                 evidence=[{'paragraph_id': 'p1', 'text': 'Alice called out'}]),
                     'invalid_not_dialogue')
        self._reject(lambda rows: rows[0].update(speaker=None, addressee=None, kind='spoken',
                                                 basis='direct_address', evidence=[]),
                     'invalid_null_identity')

    def test_model_cannot_supply_flags_or_extra_fields(self):
        self._reject(lambda rows: rows[0].update(reference_eligible=1), 'invalid_suggestion_contract')
        self._reject(lambda rows: rows[0].update(accepted=1), 'invalid_suggestion_contract')

    def test_null_uncertain_remains_unresolved(self):
        items, vocabulary = self.items()
        result = validate_suggestions({'suggestions': self.rows()}, items, vocabulary)
        for row in result[1:]:
            self.assertIsNone(row['speaker'])
            self.assertIsNone(row['addressee'])
            self.assertEqual(row['basis'], 'insufficient_context')

    @staticmethod
    def valid_responder(item):
        if item['dialogue_id'] == 'd1':
            return {'dialogue_id': 'd1', 'speaker': 'Alice', 'addressee': 'Bob', 'kind': 'spoken',
                    'basis': 'explicit_name', 'evidence': [{'paragraph_id': 'p1', 'text': 'Alice called out'}]}
        return {'dialogue_id': item['dialogue_id'], 'speaker': None, 'addressee': None,
                'kind': 'uncertain', 'basis': 'insufficient_context', 'evidence': []}

    @staticmethod
    def _fake(responder, calls=None):
        def fake(endpoint, token, model, payload, *, system=None, history=None, session_id=None):
            if calls is not None:
                calls.append({'payload': payload, 'system': system, 'session_id': session_id, 'model': model,
                              'history_len': len(history or [])})
            rows = [responder(item) for item in payload['targets']]
            if history is not None:
                history.extend([{'role': 'user', 'content': 'u'}, {'role': 'assistant', 'content': 'a'}])
            return {'payload': {'suggestions': rows}, 'usage': {'cache_read_input_tokens': 7},
                    'served_model': 'served-' + model}
        return fake

    def _args(self, out=None, **overrides):
        values = dict(db=self.db, kind='haruhi_unknown', edition='e1', out=out or (self.root / 'out'),
                      model='m1', max_calls=1, batch_size=6, start=0, execute=True)
        values.update(overrides)
        return argparse.Namespace(**values)

    def _run(self, responder, **overrides):
        calls = []
        out = self.root / 'out'
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://x', 'ANTHROPIC_AUTH_TOKEN': 't'}):
            with patch('ltr.attribution.review.request_model', side_effect=self._fake(responder, calls)):
                summary = run(self._args(out=out, **overrides))
        return summary, out, calls

    def test_run_haruhi_end_to_end_without_prose_outputs(self):
        summary, out, calls = self._run(self.valid_responder)
        data = json.loads((out / 'suggestion-000.json').read_text(encoding='utf-8'))
        self.assertEqual(data['schema'], 'corpus-attribution-suggestions-v1')
        self.assertEqual(data['kind'], 'haruhi_unknown')
        self.assertEqual(len(data['rows']), 5)
        row = data['rows'][0]
        self.assertEqual(len(row['suggestion_id']), 16)
        self.assertEqual(row['requires_review'], 1)
        for flag in ('accepted', 'human_confirmed', 'semantic_verified', 'reference_eligible'):
            self.assertEqual(row[flag], 0)
        blob = (out / 'suggestion-000.json').read_text(encoding='utf-8')
        for secret in ('LINE_ONE_SENTINEL', 'called out', 'Narration zero.', 'End of scene.'):
            self.assertNotIn(secret, blob)
        self.assertEqual(summary['valid'], 5)
        self.assertEqual(summary['resolved_speaker'], 1)
        self.assertEqual(summary['calls'], 1)
        self.assertTrue(summary['source_unchanged'])
        self.assertFalse(summary['corpus_updated'])
        self.assertEqual(summary['cache_read_input_tokens'], 7)

    def test_run_completion_quote_end_to_end(self):
        summary, out, calls = self._run(self.valid_responder, kind='completion_quote')
        data = json.loads((out / 'suggestion-000.json').read_text(encoding='utf-8'))
        self.assertEqual([r['dialogue_id'] for r in data['rows']], ['d1'])
        self.assertEqual(summary['valid'], 1)

    def test_prefix_history_and_session_stable_per_worker(self):
        summary, out, calls = self._run(self.valid_responder, batch_size=1, max_calls=5)
        self.assertEqual(calls[0]['payload']['vocabulary'], ['Alice', 'Bob'])
        for record in calls[1:]:
            self.assertNotIn('vocabulary', record['payload'])
        self.assertEqual({record['session_id'] for record in calls}, {calls[0]['session_id']})
        self.assertEqual({record['system'] for record in calls}, {calls[0]['system']})
        self.assertEqual([record['history_len'] for record in calls], [0, 2, 4, 6, 8])

    def test_call_budget_stops(self):
        summary, out, calls = self._run(self.valid_responder, batch_size=1, max_calls=2)
        self.assertEqual(summary['calls'], 2)
        self.assertEqual(summary['valid'], 2)
        self.assertEqual(summary['unattempted'], 3)
        self.assertEqual(summary['selected'], 5)

    def test_persistent_failures_stop_without_retry(self):
        def boom(item):
            raise urllib.error.HTTPError('http://x', 500, 'boom', None, io.BytesIO(b''))
        summary, out, calls = self._run(boom, batch_size=1, max_calls=10)
        self.assertEqual(summary['calls'], 3)
        self.assertEqual(summary['failed'], 3)
        self.assertEqual(summary['unattempted'], 2)
        self.assertEqual(summary['valid'], 0)

    def test_stop_status_halts_immediately(self):
        def boom(item):
            raise urllib.error.HTTPError('http://x', 429, 'slow', None, io.BytesIO(b''))
        summary, out, calls = self._run(boom, batch_size=1, max_calls=10)
        self.assertEqual(summary['calls'], 1)
        self.assertEqual(summary['failed'], 1)

    def test_progress_is_metadata_only(self):
        summary, out, calls = self._run(self.valid_responder)
        blob = (out / 'progress.json').read_text(encoding='utf-8')
        self.assertEqual(json.loads(blob)['schema'], 'corpus-attribution-progress-v1')
        for secret in ('LINE_ONE_SENTINEL', 'called out', 'Alice'):
            self.assertNotIn(secret, blob)

    def test_no_source_mutation(self):
        before = self.db.read_bytes()
        self._run(self.valid_responder)
        self.assertEqual(before, self.db.read_bytes())

    def test_source_hash_change_refused_before_save(self):
        out = self.root / 'out'
        with patch('ltr.attribution.review.fingerprint', side_effect=['a', 'a', 'b']):
            with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://x', 'ANTHROPIC_AUTH_TOKEN': 't'}):
                with patch('ltr.attribution.review.request_model',
                           side_effect=self._fake(self.valid_responder)):
                    with self.assertRaises(ValueError) as raised:
                        run(self._args(out=out))
        self.assertEqual(str(raised.exception), 'source_database_changed')
        self.assertFalse((out / 'suggestion-000.json').exists())

    def test_wal_sidecar_refused(self):
        Path(str(self.db) + '-wal').write_bytes(b'active')
        with self.assertRaises(ValueError) as raised:
            run(self._args())
        self.assertEqual(str(raised.exception), 'unstable_database_snapshot')
        self.assertFalse((self.root / 'out').exists())

    def test_wal_detected_before_save_refused(self):
        def responder(item):
            Path(str(self.db) + '-wal').write_bytes(b'late')
            return self.valid_responder(item)
        out = self.root / 'out'
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://x', 'ANTHROPIC_AUTH_TOKEN': 't'}):
            with patch('ltr.attribution.review.request_model', side_effect=self._fake(responder)):
                with self.assertRaises(ValueError) as raised:
                    run(self._args(out=out))
        self.assertEqual(str(raised.exception), 'unstable_database_snapshot')
        self.assertFalse((out / 'suggestion-000.json').exists())

    def test_missing_database_refused(self):
        with self.assertRaises(FileNotFoundError):
            run(self._args(db=self.root / 'missing.sqlite'))

    def test_output_refusals(self):
        (self.root / 'out').mkdir()
        with self.assertRaises(ValueError) as raised:
            run(self._args())
        self.assertEqual(str(raised.exception), 'output_already_exists')
        with self.assertRaises(ValueError) as raised:
            run(self._args(out=self.source / 'out'))
        self.assertEqual(str(raised.exception), 'output_inside_source')

    def test_budget_and_kind_validated_programmatically(self):
        for overrides, expected in [({'kind': 'bogus'}, 'invalid_kind'),
                                    ({'max_calls': 0}, 'invalid_budget'),
                                    ({'batch_size': 13}, 'invalid_budget'),
                                    ({'start': -1}, 'invalid_budget'),
                                    ({'execute': False}, 'invalid_budget'),
                                    ({'batch_size': True}, 'invalid_budget')]:
            with self.assertRaises(ValueError) as raised:
                run(self._args(**overrides))
            self.assertEqual(str(raised.exception), expected)

    def test_long_quote_context_keeps_both_endpoints_without_fake_continuity(self):
        self.edit("INSERT INTO paragraphs VALUES ('p40','e1',40,'Synthetic endpoint paragraph')")
        self.edit("UPDATE dialogues SET end_para=40 WHERE id='d1'")
        targets,_=self.items()
        target=next(r for r in targets if r['dialogue_id']=='d1')
        self.assertEqual(target['context_window'],'endpoint_windows')
        self.assertTrue({1,40}<={r['ordinal'] for r in target['context']})
        self.assertLessEqual(len(target['context']),10)

    def test_session_can_continue_between_finite_edition_jobs(self):
        calls=[];session={}
        with patch.dict(os.environ,ANTHROPIC_BASE_URL='http://example.invalid',ANTHROPIC_AUTH_TOKEN='synthetic'),patch('ltr.attribution.review.request_model',side_effect=self._fake(self.valid_responder,calls)):
            first=run(self._args(out=self.root/'first',batch_size=1),session=session)
            second=run(self._args(out=self.root/'second',batch_size=1,start=1),session=session)
            with self.assertRaises(ValueError):run(self._args(out=self.root/'wrong',model='different'),session=session)
        self.assertEqual(first['session_id'],second['session_id'])
        self.assertEqual([r['history_len'] for r in calls],[0,2])
        self.assertIn('vocabulary',calls[1]['payload'])
        self.assertEqual(session['history'][:2],[{'role':'user','content':'u'},{'role':'assistant','content':'a'}])
        self.assertFalse((self.root/'wrong').exists())

    def test_skipped_prefix_is_not_counted_as_completed(self):
        result,_,_=self._run(self.valid_responder,batch_size=1,start=4)
        self.assertEqual(result['valid_rows'],1)
        self.assertEqual(result['remaining'],4)
        self.assertEqual(result['skipped_prefix_rows'],4)

    def test_target_order_follows_source_dialogue_sequence(self):
        self.edit("UPDATE dialogues SET ordinal=99 WHERE id='d1'")
        targets,_=self.items()
        self.assertEqual(targets[-1]['dialogue_id'],'d1')

    def test_recovery_skips_only_valid_source_bound_results(self):
        calls=[];first=self.root/'first'
        with patch.dict(os.environ,ANTHROPIC_BASE_URL='http://example.invalid',ANTHROPIC_AUTH_TOKEN='synthetic'),patch('ltr.attribution.review.request_model',side_effect=self._fake(self.valid_responder,calls)):
            run(self._args(out=first,batch_size=1))
            result=run(self._args(out=self.root/'recovery',batch_size=1,max_calls=4,exclude_completed=[first]))
        self.assertEqual([r['payload']['targets'][0]['dialogue_id'] for r in calls],['d1','d2','d3','d4','d5'])
        self.assertEqual(result['excluded_completed_rows'],1)
        self.assertEqual(result['valid_rows'],4)
        self.assertEqual(result['remaining'],0)

    def test_recovery_rejects_changed_approval_flags(self):
        first=self.root/'first'
        with patch.dict(os.environ,ANTHROPIC_BASE_URL='http://example.invalid',ANTHROPIC_AUTH_TOKEN='synthetic'),patch('ltr.attribution.review.request_model',side_effect=self._fake(self.valid_responder)):
            run(self._args(out=first,batch_size=1))
        path=first/'suggestion-000.json';document=json.loads(path.read_text());document['rows'][0]['accepted']=1;path.write_text(json.dumps(document))
        with self.assertRaises(ValueError):run(self._args(out=self.root/'recovery',exclude_completed=[first]))
        self.assertFalse((self.root/'recovery').exists())

    def test_cli_end_to_end_and_sanitized_refusal(self):
        out = self.root / 'cli-out'
        argv = ['--db', str(self.db), '--kind', 'haruhi_unknown', '--edition', 'e1', '--out', str(out),
                '--model', 'm1', '--max-calls', '1', '--batch-size', '6', '--start', '0', '--execute']
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://x', 'ANTHROPIC_AUTH_TOKEN': 't'}):
            with patch('ltr.attribution.review.request_model', side_effect=self._fake(self.valid_responder)):
                with patch('sys.stdout', new_callable=io.StringIO) as captured:
                    self.assertEqual(main(argv), 0)
        printed = json.loads(captured.getvalue())
        self.assertEqual(printed['valid'], 5)
        self.assertNotIn('LINE_ONE_SENTINEL', captured.getvalue())
        with self.assertRaises(SystemExit) as raised:
            main(['--db', str(self.db), '--kind', 'haruhi_unknown', '--edition', 'e1',
                  '--out', str(self.root / 'cli-2'), '--model', 'm1'])
        self.assertNotEqual(raised.exception.code, 0)
        self.assertFalse((self.root / 'cli-2').exists())
        with patch('ltr.attribution.review.run', side_effect=ValueError('output_already_exists')):
            with patch('sys.stderr', new_callable=io.StringIO) as error:
                with self.assertRaises(SystemExit):
                    main(argv)
        self.assertIn('output_already_exists', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())


    @staticmethod
    def partial_responder(bad_ids):
        """Answer every target; the given ids get an unsupported identity."""
        def responder(item):
            if item['dialogue_id'] in bad_ids:
                return {'dialogue_id': item['dialogue_id'], 'speaker': 'Zed', 'addressee': None,
                        'kind': 'spoken', 'basis': 'explicit_name',
                        'evidence': [{'paragraph_id': 'p1', 'text': 'Alice called out'}]}
            return AttributionReviewTests.valid_responder(item)
        return responder

    def _partial_run(self, responder, out, **overrides):
        calls = []
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://x', 'ANTHROPIC_AUTH_TOKEN': 't'}):
            with patch('ltr.attribution.review.request_model', side_effect=self._fake(responder, calls)):
                summary = run(self._args(out=out, **overrides))
        return summary, calls

    def test_partial_retains_valid_row_alongside_unsupported_identity(self):
        out = self.root / 'partial'
        summary, _ = self._partial_run(self.partial_responder({'d2'}), out,
                                       batch_size=2, max_calls=1, partial_batches=True)
        data = json.loads((out / 'suggestion-000.json').read_text(encoding='utf-8'))
        self.assertEqual([row['dialogue_id'] for row in data['rows']], ['d1'])
        self.assertTrue(data['partial_batch'])
        self.assertEqual(data['refused_rows'], [{'dialogue_id': 'd2', 'error_code': 'unsupported_identity'}])
        self.assertEqual(summary['valid_rows'], 1)
        self.assertEqual(summary['failed_rows'], 1)
        self.assertEqual(summary['partial_calls'], 1)
        self.assertEqual(summary['valid_calls'], 0)
        self.assertEqual(summary['failed_calls'], 0)
        self.assertEqual(summary['attempted_calls'], 1)

    def test_partial_invalid_model_fields_never_saved(self):
        def responder(item):
            row = self.valid_responder(item)
            if item['dialogue_id'] == 'd2':
                row['accepted'] = 1
            return row
        out = self.root / 'partial-fields'
        summary, _ = self._partial_run(responder, out, batch_size=2, max_calls=1, partial_batches=True)
        data = json.loads((out / 'suggestion-000.json').read_text(encoding='utf-8'))
        self.assertEqual([row['dialogue_id'] for row in data['rows']], ['d1'])
        self.assertEqual(data['rows'][0]['accepted'], 0)
        self.assertEqual(data['refused_rows'],
                         [{'dialogue_id': 'd2', 'error_code': 'invalid_suggestion_contract'}])
        blob = (out / 'suggestion-000.json').read_text(encoding='utf-8')
        self.assertNotIn('"accepted": 1', blob)

    def test_partial_global_contract_rejected_before_salvage(self):
        items, vocabulary = self.items()
        good = self.rows()[0]
        unsupported = {'dialogue_id': 'd2', 'speaker': 'Zed', 'addressee': None, 'kind': 'spoken',
                       'basis': 'explicit_name', 'evidence': [{'paragraph_id': 'p1', 'text': 'Alice called out'}]}
        ghost = {'dialogue_id': 'ghost', 'speaker': None, 'addressee': None, 'kind': 'uncertain',
                 'basis': 'insufficient_context', 'evidence': []}
        for payload, expected in [
                ({'suggestions': [good, dict(good)]}, 'inexact_target_coverage'),
                ({'suggestions': [good, ghost]}, 'inexact_target_coverage'),
                ({'suggestions': [good]}, 'invalid_suggestion_contract'),
                ({'suggestions': [good, unsupported], 'extra': 1}, 'invalid_suggestion_contract'),
                ({'suggestions': 'not-a-list'}, 'invalid_suggestion_contract')]:
            with self.assertRaises(ValueError) as raised:
                validate_partial_suggestions(payload, items[:2], vocabulary)
            self.assertEqual(str(raised.exception), expected)
        valid, refused = validate_partial_suggestions(
            {'suggestions': [good, unsupported]}, items[:2], vocabulary)
        self.assertEqual([row['dialogue_id'] for row in valid], ['d1'])
        self.assertEqual(refused, [{'dialogue_id': 'd2', 'error_code': 'unsupported_identity'}])

    def test_partial_global_coverage_failure_refuses_whole_call(self):
        def responder(item):
            row = self.valid_responder(item)
            if item['dialogue_id'] == 'd2':
                row['dialogue_id'] = 'ghost'
            return row
        out = self.root / 'partial-coverage'
        summary, _ = self._partial_run(responder, out, batch_size=2, max_calls=1, partial_batches=True)
        self.assertEqual(summary['valid_rows'], 0)
        self.assertEqual(summary['failed_rows'], 2)
        self.assertEqual(summary['failed_calls'], 1)
        self.assertFalse((out / 'suggestion-000.json').exists())

    def test_strict_default_rejects_partial_response_wholesale(self):
        out = self.root / 'strict'
        summary, _ = self._partial_run(self.partial_responder({'d2'}), out, batch_size=2, max_calls=1)
        self.assertEqual(summary['valid_rows'], 0)
        self.assertEqual(summary['failed_rows'], 2)
        self.assertEqual(summary['partial_calls'], 0)
        self.assertFalse((out / 'suggestion-000.json').exists())

    def test_partial_counts_distinguish_calls_and_rows(self):
        out = self.root / 'counts'
        summary, _ = self._partial_run(self.partial_responder({'d2', 'd5'}), out,
                                       batch_size=2, max_calls=4, partial_batches=True)
        self.assertEqual(summary['attempted_calls'], 3)
        self.assertEqual(summary['valid_calls'], 1)
        self.assertEqual(summary['partial_calls'], 1)
        self.assertEqual(summary['failed_calls'], 1)
        self.assertEqual(summary['attempted_calls'],
                         summary['valid_calls'] + summary['partial_calls'] + summary['failed_calls'])
        self.assertEqual(summary['valid_rows'], 3)
        self.assertEqual(summary['failed_rows'], 2)
        self.assertEqual(summary['remaining'], 2)
        self.assertEqual(summary['unattempted_rows'], 0)
        self.assertTrue((out / 'suggestion-000.json').exists())
        self.assertTrue((out / 'suggestion-001.json').exists())
        self.assertFalse((out / 'suggestion-002.json').exists())

    def test_partial_recovery_skips_saved_valid_rows_and_dispatches_holes(self):
        first = self.root / 'first'
        first_calls = []
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://x', 'ANTHROPIC_AUTH_TOKEN': 't'}):
            with patch('ltr.attribution.review.request_model',
                       side_effect=self._fake(self.partial_responder({'d2', 'd5'}), first_calls)):
                run(self._args(out=first, batch_size=2, max_calls=4, partial_batches=True))
        self.assertEqual(sorted(p.name for p in first.glob('suggestion-*.json')),
                         ['suggestion-000.json', 'suggestion-001.json'])
        second_calls = []
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://x', 'ANTHROPIC_AUTH_TOKEN': 't'}):
            with patch('ltr.attribution.review.request_model',
                       side_effect=self._fake(self.valid_responder, second_calls)):
                result = run(self._args(out=self.root / 'recovery', batch_size=6, max_calls=4,
                                        partial_batches=True, exclude_completed=[first]))
        dispatched = [target['dialogue_id'] for call in second_calls for target in call['payload']['targets']]
        self.assertEqual(sorted(dispatched), ['d2', 'd5'])
        self.assertEqual(result['excluded_completed_rows'], 3)
        self.assertEqual(result['remaining'], 0)

    def test_partial_source_change_refuses_save(self):
        out = self.root / 'changed'
        with patch('ltr.attribution.review.fingerprint', side_effect=['a', 'a', 'b']):
            with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://x', 'ANTHROPIC_AUTH_TOKEN': 't'}):
                with patch('ltr.attribution.review.request_model',
                           side_effect=self._fake(self.partial_responder({'d2'}))):
                    with self.assertRaises(ValueError) as raised:
                        run(self._args(out=out, batch_size=2, max_calls=1, partial_batches=True))
        self.assertEqual(str(raised.exception), 'source_database_changed')
        self.assertFalse((out / 'suggestion-000.json').exists())

    def test_partial_refused_metadata_has_no_source_fragments(self):
        out = self.root / 'meta'
        self._partial_run(self.partial_responder({'d2'}), out, batch_size=2, max_calls=1, partial_batches=True)
        blob = (out / 'suggestion-000.json').read_text(encoding='utf-8')
        data = json.loads(blob)
        for refused in data['refused_rows']:
            self.assertEqual(set(refused), {'dialogue_id', 'error_code'})
        for secret in ('Zed', 'Alice called out', 'LINE_ONE_SENTINEL', 'LINE_TWO_SENTINEL', 'Narration zero.'):
            self.assertNotIn(secret, blob)

    def test_partial_three_all_invalid_calls_stop(self):
        def responder(item):
            return {'dialogue_id': item['dialogue_id'], 'speaker': 'Zed', 'addressee': None,
                    'kind': 'spoken', 'basis': 'explicit_name',
                    'evidence': [{'paragraph_id': 'p1', 'text': 'Alice called out'}]}
        out = self.root / 'stop'
        summary, calls = self._partial_run(responder, out, batch_size=1, max_calls=10, partial_batches=True)
        self.assertEqual(len(calls), 3)
        self.assertEqual(summary['calls'], 3)
        self.assertEqual(summary['valid_rows'], 0)
        self.assertEqual(summary['failed_rows'], 3)
        self.assertEqual(summary['failed_calls'], 3)
        self.assertEqual(summary['unattempted_rows'], 2)
        self.assertEqual(summary['stop_reason'], 'consecutive_invalid_responses')
        self.assertFalse((out / 'suggestion-000.json').exists())

    def test_partial_then_valid_continues_same_session(self):
        calls = []
        session = {}
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://x', 'ANTHROPIC_AUTH_TOKEN': 't'}):
            with patch('ltr.attribution.review.request_model',
                       side_effect=self._fake(self.partial_responder({'d2'}), calls)):
                first = run(self._args(out=self.root / 'p1', batch_size=2, max_calls=1,
                                       partial_batches=True), session=session)
                second = run(self._args(out=self.root / 'p2', batch_size=2, max_calls=1, start=2,
                                        partial_batches=True), session=session)
        self.assertEqual(first['session_id'], second['session_id'])
        self.assertEqual([record['history_len'] for record in calls], [0, 2])
        self.assertEqual(first['partial_calls'], 1)
        self.assertEqual(second['valid_calls'], 1)
        self.assertIn('vocabulary', calls[1]['payload'])

    def test_cli_partial_batches_flag(self):
        out = self.root / 'cli-partial'
        argv = ['--db', str(self.db), '--kind', 'haruhi_unknown', '--edition', 'e1', '--out', str(out),
                '--model', 'm1', '--max-calls', '1', '--batch-size', '2', '--start', '0',
                '--partial-batches', '--execute']
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://x', 'ANTHROPIC_AUTH_TOKEN': 't'}):
            with patch('ltr.attribution.review.request_model',
                       side_effect=self._fake(self.partial_responder({'d2'}))):
                with patch('sys.stdout', new_callable=io.StringIO) as captured:
                    self.assertEqual(main(argv), 0)
        printed = json.loads(captured.getvalue())
        self.assertEqual(printed['partial_calls'], 1)
        self.assertEqual(printed['valid_rows'], 1)
        self.assertEqual(printed['failed_rows'], 1)

    def test_final_progress_records_partial_and_all_refused_outcomes(self):
        out = self.root / 'progress-out'
        summary, _ = self._partial_run(self.partial_responder({'d2', 'd5'}), out,
                                       batch_size=2, max_calls=3, partial_batches=True)
        progress = json.loads((out / 'progress.json').read_text())
        self.assertEqual([r['status'] for r in progress['calls']],
                         ['valid_partial', 'valid', 'failed'])
        self.assertEqual(progress['calls'][-1]['refused_rows'],
                         [{'dialogue_id': 'd5', 'error_code': 'unsupported_identity'}])
        self.assertEqual(summary['attempted_rows'], summary['valid_rows'] + summary['failed_rows'])

    def test_validation_feedback_is_source_free_and_does_not_retry_old_rows(self):
        out = self.root / 'feedback'
        summary, calls = self._partial_run(self.partial_responder({'d2'}), out,
                                           batch_size=2, max_calls=3, partial_batches=True)
        self.assertNotIn('previous_batch_validation', calls[0]['payload'])
        feedback = calls[1]['payload']['previous_batch_validation']
        self.assertEqual(feedback['refused_rows'],
                         [{'dialogue_id': 'd2', 'error_code': 'unsupported_identity'}])
        self.assertNotIn('Zed', json.dumps(feedback))
        self.assertNotIn('LINE_TWO_SENTINEL', json.dumps(feedback))
        self.assertEqual([r['dialogue_id'] for r in calls[1]['payload']['targets']], ['d3', 'd4'])
        self.assertNotIn('previous_batch_validation', calls[2]['payload'])
        self.assertEqual(summary['failed_rows'], 1)
        self.assertEqual(summary['valid_rows'], 4)

    # --- optional context-reference reuse (reuse-context) -------------------

    @staticmethod
    def _ref_fake(responder, calls=None, append=True):
        """Mimic request_model: append the real user+assistant turn only when completed."""
        def fake(endpoint, token, model, payload, *, system=None, history=None, session_id=None):
            if calls is not None:
                calls.append({'payload': payload, 'system': system, 'session_id': session_id,
                              'model': model, 'history_len': len(history or [])})
            rows = [responder(item) for item in payload['targets']]
            if append and history is not None:
                history.extend([{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
                                {'role': 'assistant',
                                 'content': json.dumps({'suggestions': rows}, ensure_ascii=False)}])
            return {'payload': {'suggestions': rows}, 'usage': {'cache_read_input_tokens': 7},
                    'served_model': 'served-' + model}
        return fake

    def _ref_run(self, responder, out=None, calls=None, append=True, session=None, **overrides):
        out = out or (self.root / 'refout')
        overrides.setdefault('reuse_context', True)
        calls = [] if calls is None else calls
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://x', 'ANTHROPIC_AUTH_TOKEN': 't'}):
            with patch('ltr.attribution.review.request_model',
                       side_effect=self._ref_fake(responder, calls, append)):
                summary = run(self._args(out=out, **overrides), session=session)
        return summary, out, calls

    def test_reuse_context_default_is_inline_and_untouched(self):
        summary, out, calls = self._run(self.valid_responder)
        payload = calls[0]['payload']
        self.assertEqual(calls[0]['system'], SYSTEM)
        self.assertNotIn('context_reference', payload)
        locator = payload['targets'][0]['context'][0]
        self.assertIn('text', locator)
        self.assertNotIn('context_id', locator)

    def test_reference_mode_dedups_context_and_omits_supplied(self):
        summary, out, calls = self._ref_run(self.valid_responder, batch_size=3, max_calls=2)
        first = calls[0]['payload']
        ids = [r['context_id'] for r in first['context_reference']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {_context_id('e1', f'p{i}') for i in range(5)})
        self.assertEqual(set(first['targets'][0]['context'][0]),
                         {'context_id', 'paragraph_id', 'ordinal'})
        self.assertEqual(calls[0]['system'], SYSTEM_REFERENCE)
        self.assertNotIn('context_reference', calls[1]['payload'])
        self.assertEqual(summary['valid_rows'], 5)

    def test_reference_mode_later_edition_appends_new_references(self):
        self.edit("INSERT INTO attribution_candidates VALUES ('dx','e2','Carol','unknown','Dave','')")
        calls, session = [], {}
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://x', 'ANTHROPIC_AUTH_TOKEN': 't'}):
            with patch('ltr.attribution.review.request_model',
                       side_effect=self._ref_fake(self.valid_responder, calls)):
                run(self._args(out=self.root / 'ref-e1', reuse_context=True), session=session)
                run(self._args(out=self.root / 'ref-e2', edition='e2', reuse_context=True), session=session)
        first = {r['context_id'] for r in calls[0]['payload']['context_reference']}
        second = calls[1]['payload']['context_reference']
        self.assertTrue(second)
        self.assertTrue(all(r['edition_id'] == 'e2' for r in second))
        self.assertFalse(first & {r['context_id'] for r in second})
        self.assertNotEqual(_context_id('e1', 'p1'), _context_id('e2', 'p1'))

    def test_reference_mode_evidence_locators_unchanged(self):
        summary, out, calls = self._ref_run(self.valid_responder)
        data = json.loads((out / 'suggestion-000.json').read_text(encoding='utf-8'))
        evidence = data['rows'][0]['evidence'][0]
        self.assertEqual((evidence['edition_id'], evidence['paragraph_id'], evidence['ordinal']),
                         ('e1', 'p1', 1))
        self.assertEqual((evidence['start'], evidence['end']), (0, len('Alice called out')))
        self.assertNotIn('text', evidence)

    def test_reference_mode_completed_invalid_json_does_not_resend(self):
        calls, state = [], {'first': True}
        def fake(endpoint, token, model, payload, *, system=None, history=None, session_id=None):
            calls.append(payload)
            rows = [self.valid_responder(item) for item in payload['targets']]
            if history is not None:
                history.extend([{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
                                {'role': 'assistant', 'content': 'not json'}])
            if state['first']:
                state['first'] = False
                raise ModelResponseError('invalid_model_json')
            return {'payload': {'suggestions': rows}, 'usage': {}, 'served_model': model}
        out = self.root / 'ref-invalid'
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://x', 'ANTHROPIC_AUTH_TOKEN': 't'}):
            with patch('ltr.attribution.review.request_model', side_effect=fake):
                summary = run(self._args(out=out, reuse_context=True, batch_size=3, max_calls=2))
        self.assertIn('context_reference', calls[0])
        self.assertNotIn('context_reference', calls[1])
        self.assertEqual(summary['valid_rows'], 2)

    def test_reference_mode_transport_failure_keeps_refs_for_resend(self):
        calls, state = [], {'first': True}
        def fake(endpoint, token, model, payload, *, system=None, history=None, session_id=None):
            calls.append(payload)
            if state['first']:
                state['first'] = False
                raise urllib.error.URLError('boom')
            rows = [self.valid_responder(item) for item in payload['targets']]
            if history is not None:
                history.extend([{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
                                {'role': 'assistant',
                                 'content': json.dumps({'suggestions': rows}, ensure_ascii=False)}])
            return {'payload': {'suggestions': rows}, 'usage': {}, 'served_model': model}
        out = self.root / 'ref-transport'
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://x', 'ANTHROPIC_AUTH_TOKEN': 't'}):
            with patch('ltr.attribution.review.request_model', side_effect=fake):
                summary = run(self._args(out=out, reuse_context=True, batch_size=3, max_calls=2))
        self.assertIn('context_reference', calls[1])
        self.assertEqual({r['context_id'] for r in calls[1]['context_reference']},
                         {_context_id('e1', f'p{i}') for i in range(5)})
        self.assertEqual(summary['valid_rows'], 2)

    def _reference_session(self, history):
        return {'model': 'm1', 'database_sha256': fingerprint(self.db),
                'system_sha256': digest(SYSTEM_REFERENCE), 'session_id': 's', 'history': history}

    def test_reference_mode_conflicting_history_refused_before_call(self):
        entry = lambda text: {'context_id': _context_id('e1', 'p1'), 'edition_id': 'e1',
                              'paragraph_id': 'p1', 'ordinal': 1, 'text': text}
        session = self._reference_session([
            {'role': 'user', 'content': json.dumps({'context_reference': [entry('AAA')]})},
            {'role': 'user', 'content': json.dumps({'context_reference': [entry('BBB')]})}])
        calls = []
        out = self.root / 'ref-conflict'
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://x', 'ANTHROPIC_AUTH_TOKEN': 't'}):
            with patch('ltr.attribution.review.request_model',
                       side_effect=self._ref_fake(self.valid_responder, calls)):
                with self.assertRaises(ValueError) as raised:
                    run(self._args(out=out, reuse_context=True), session=session)
        self.assertEqual(str(raised.exception), 'conflicting_context_reference')
        self.assertEqual(calls, [])
        self.assertFalse(out.exists())

    def test_reference_mode_malformed_history_refused(self):
        good = {'context_id': _context_id('e1', 'p1'), 'edition_id': 'e1',
                'paragraph_id': 'p1', 'ordinal': 1, 'text': 't'}
        cases = [({'context_reference': None}, 'invalid_context_reference'),
                 ({'context_reference': 'not-a-list'}, 'invalid_context_reference'),
                 ({'context_reference': ['nope']}, 'invalid_context_reference'),
                 ({'context_reference': [{'context_id': 'x'}]}, 'invalid_context_reference'),
                 ({'context_reference': [dict(good, paragraph_id='p2')]}, 'invalid_context_reference'),
                 ({'context_reference': [dict(good, text_sha256='00')]}, 'conflicting_context_reference')]
        for index, (body, expected) in enumerate(cases):
            out = self.root / f'ref-bad-{index}'
            session = self._reference_session([{'role': 'user', 'content': json.dumps(body)}])
            with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://x', 'ANTHROPIC_AUTH_TOKEN': 't'}):
                with patch('ltr.attribution.review.request_model',
                           side_effect=self._ref_fake(self.valid_responder)):
                    with self.assertRaises(ValueError) as raised:
                        run(self._args(out=out, reuse_context=True), session=session)
            self.assertEqual(str(raised.exception), expected)
            self.assertFalse(out.exists())

    def test_reference_namespace_and_ordinal_are_not_ambiguous(self):
        self.assertNotEqual(_context_id('a\x1fb','c'),_context_id('a','b\x1fc'))
        state={}
        with patch.dict(os.environ,ANTHROPIC_BASE_URL='http://x',ANTHROPIC_AUTH_TOKEN='t'):
            with patch('ltr.attribution.review.request_model',side_effect=self._ref_fake(self.valid_responder)):
                run(self._args(out=self.root/'initial-ref',reuse_context=True),session=state)
        message=state['history'][0];payload=json.loads(message['content'])
        payload['context_reference'][0]['ordinal']+=1
        message['content']=json.dumps(payload)
        out=self.root/'bad-ordinal'
        with patch.dict(os.environ,ANTHROPIC_BASE_URL='http://x',ANTHROPIC_AUTH_TOKEN='t'):
            with patch('ltr.attribution.review.request_model') as call:
                with self.assertRaisesRegex(ValueError,'^conflicting_context_reference$'):
                    run(self._args(out=out,reuse_context=True),session=state)
        self.assertFalse(out.exists());call.assert_not_called()

    def test_reference_mode_cannot_switch_within_session(self):
        env = {'ANTHROPIC_BASE_URL': 'http://x', 'ANTHROPIC_AUTH_TOKEN': 't'}
        ref_session = {}
        with patch.dict(os.environ, env):
            with patch('ltr.attribution.review.request_model',
                       side_effect=self._ref_fake(self.valid_responder)):
                run(self._args(out=self.root / 'switch-ref', reuse_context=True), session=ref_session)
                with self.assertRaises(ValueError) as raised:
                    run(self._args(out=self.root / 'switch-inline', reuse_context=False),
                        session=ref_session)
        self.assertEqual(str(raised.exception), 'invalid_session')
        self.assertFalse((self.root / 'switch-inline').exists())
        inline_session = {}
        with patch.dict(os.environ, env):
            with patch('ltr.attribution.review.request_model',
                       side_effect=self._fake(self.valid_responder)):
                run(self._args(out=self.root / 'switch-i1'), session=inline_session)
                with self.assertRaises(ValueError) as raised:
                    run(self._args(out=self.root / 'switch-r2', reuse_context=True),
                        session=inline_session)
        self.assertEqual(str(raised.exception), 'invalid_session')
        self.assertFalse((self.root / 'switch-r2').exists())

    def test_reference_mode_outputs_never_serialize_source(self):
        summary, out, calls = self._ref_run(self.valid_responder)
        blob = ''.join((out / name).read_text(encoding='utf-8')
                       for name in ('progress.json', 'summary.json', 'suggestion-000.json'))
        for secret in ('LINE_ONE_SENTINEL', 'Alice called out', 'Narration zero.', 'End of scene.'):
            self.assertNotIn(secret, blob)
        self.assertIn('Alice called out', json.dumps(calls[0]['payload']))

    def test_reference_payload_reduces_size_without_losing_context(self):
        paragraphs = [{'id': f'p{i}', 'edition_id': 'e1', 'ordinal': i,
                       'text': f'PARA{i}_' + 'x' * 500} for i in range(5)]
        targets = [{'dialogue_id': f'd{i}', 'ordinal': i, 'text': f'LINE{i}',
                    'context_window': 'continuous_window', 'context': paragraphs} for i in range(3)]
        inline = json.dumps({'targets': [_model_item(t) for t in targets]}, ensure_ascii=False)
        refs = _new_references(targets, {})
        reference = json.dumps({'targets': [_reference_item(t) for t in targets],
                                'context_reference': refs}, ensure_ascii=False)
        self.assertEqual(len(refs), 5)
        self.assertLess(len(reference), len(inline))
        for paragraph in paragraphs:
            self.assertEqual(reference.count(paragraph['text']), 1)
            self.assertEqual(inline.count(paragraph['text']), 3)
        self.assertNotIn('text', _reference_item(targets[0])['context'][0])

    def test_cli_reuse_context_flag_end_to_end(self):
        out = self.root / 'cli-ref'
        calls = []
        argv = ['--db', str(self.db), '--kind', 'haruhi_unknown', '--edition', 'e1', '--out', str(out),
                '--model', 'm1', '--max-calls', '1', '--batch-size', '6', '--start', '0',
                '--reuse-context', '--execute']
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://x', 'ANTHROPIC_AUTH_TOKEN': 't'}):
            with patch('ltr.attribution.review.request_model',
                       side_effect=self._ref_fake(self.valid_responder, calls)):
                with patch('sys.stdout', new_callable=io.StringIO) as captured:
                    self.assertEqual(main(argv), 0)
        printed = json.loads(captured.getvalue())
        self.assertEqual(printed['valid'], 5)
        self.assertNotIn('LINE_ONE_SENTINEL', captured.getvalue())
        self.assertEqual(calls[0]['system'], SYSTEM_REFERENCE)
        self.assertIn('context_reference', calls[0]['payload'])


if __name__ == '__main__':
    unittest.main()
