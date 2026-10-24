import unittest
import hashlib
import io
import json
import urllib.error
from unittest.mock import patch
from ltr.quotes.batch import validate, request_model, request_byte_count, encode_request, parse_model_json, ModelResponseError, failure_metadata
from ltr.quotes.review import validate_review


class PrivateCandidateContractTests(unittest.TestCase):
    def setUp(self):
        self.targets = [{'id':'p1','ordinal':0,'text':'Narration. Hello, friend. More narration.'}]
        self.row = {'paragraph_id':'p1','text':'Hello, friend.','speaker':None,'addressee':None,'kind':'spoken'}

    def test_exact_span_bound_and_never_accepted(self):
        rows = validate({'candidates':[self.row]}, self.targets)
        row = rows[0]
        self.assertEqual(self.targets[0]['text'][row['start']:row['end']], self.row['text'])
        self.assertEqual(row['requires_review'], 1)
        for flag in ['human_confirmed','semantic_verified','accepted','reference_eligible']:
            self.assertEqual(row[flag], 0)

    def test_rewritten_text_rejected(self):
        self.row['text'] = 'Hello friend'
        with self.assertRaises(ValueError):
            validate({'candidates':[self.row]}, self.targets)

    def test_ambiguous_repeated_span_rejected(self):
        self.targets[0]['text'] = 'Hello, friend. Hello, friend.'
        with self.assertRaises(ValueError):
            validate({'candidates':[self.row]}, self.targets)

    def test_overlapping_occurrences_are_ambiguous_in_latin_and_cjk(self):
        for source, text in [('aaaa', 'aaa'), ('哈哈哈哈', '哈哈哈')]:
            with self.subTest(source=source):
                self.targets[0]['text'] = source
                self.row['text'] = text
                with self.assertRaisesRegex(ValueError, '^non_unique_or_changed_source_span$'):
                    validate({'candidates':[self.row]}, self.targets)

    def test_unique_multichar_span_keeps_exact_offsets_and_source_hash(self):
        for source, text, start, end in [('ZabcdeY', 'abc', 1, 4), ('甲乙丙丁戊', '乙丙丁', 1, 4)]:
            with self.subTest(source=source):
                self.targets[0]['text'] = source
                self.row['text'] = text
                result = validate({'candidates':[self.row]}, self.targets)[0]
                self.assertEqual((result['start'], result['end']), (start, end))
                self.assertEqual(result['paragraph_sha256'], hashlib.sha256(source.encode('utf-8')).hexdigest())
                self.assertEqual(result['text'], text)

    def test_other_paragraph_rejected(self):
        self.row['paragraph_id'] = 'not-a-target'
        with self.assertRaises(ValueError):
            validate({'candidates':[self.row]}, self.targets)

    def test_duplicate_span_rejected(self):
        with self.assertRaises(ValueError):
            validate({'candidates':[self.row,self.row]}, self.targets)

    def test_empty_is_valid_and_never_invented(self):
        self.assertEqual(validate({'candidates':[]},self.targets), [])

    def test_model_cannot_set_acceptance_flags(self):
        self.row['reference_eligible'] = 1
        with self.assertRaises(ValueError):
            validate({'candidates':[self.row]},self.targets)

    def test_continuous_worker_preserves_prefix_and_session(self):
        sent=[]
        class Opener:
            def open(self, request, timeout):
                sent.append(request)
                return io.BytesIO(json.dumps({'stop_reason':'end_turn','model':'synthetic',
                    'content':[{'type':'text','text':'{"candidates":[]}'}],
                    'usage':{'cache_read_input_tokens':100 if len(sent)>1 else 0}}).encode())
        history=[]
        with patch('ltr.quotes.batch.urllib.request.build_opener',return_value=Opener()):
            request_model('http://127.0.0.1:1','synthetic','example',{'batch':1},history=history,session_id='worker-one')
            response=request_model('http://127.0.0.1:1','synthetic','example',{'batch':2},history=history,session_id='worker-one')
        first=json.loads(sent[0].data);second=json.loads(sent[1].data)
        self.assertEqual(first['system'],second['system'])
        self.assertEqual(first['messages'],second['messages'][:1])
        self.assertEqual(second['messages'][1]['role'],'assistant')
        self.assertEqual(first['metadata'],second['metadata'])
        self.assertEqual(len(history),4)
        self.assertEqual(response['usage']['cache_read_input_tokens'],100)

    def test_second_model_review_must_cover_exact_candidate_ids(self):
        row={'candidate_id':'C1','decision':'uncertain','kind':'uncertain','basis':'insufficient_context'}
        self.assertEqual(validate_review({'reviews':[row]},['C1']),[row])
        with self.assertRaises(ValueError):
            validate_review({'reviews':[row]},['C1','C2'])

    def test_exact_json_envelope_only_and_no_partial_recovery(self):
        for value in ['{"x":1}', '```json\n{"x":1}\n```', '```\n{"x":1}\n```']:
            self.assertEqual(parse_model_json(value),{'x':1})
        for value in ['Explanation\n```json\n{"x":1}\n```', '```json\n{"x":1}\n```\nExtra', '{"x":']:
            with self.assertRaises(json.JSONDecodeError):parse_model_json(value)

    def test_transport_and_model_json_errors_are_distinct_without_text(self):
        class Opener:
            def __init__(self,raw):self.raw=raw
            def open(self,*args,**kwargs):return io.BytesIO(self.raw)
        history=[]
        for raw,code in [(b'invalid transport','invalid_transport_json'),
                         (json.dumps({'stop_reason':'end_turn','content':[{'type':'text','text':'PRIVATE RESPONSE'}],
                                      'usage':{'cache_read_input_tokens':42,'other':'PRIVATE'}}).encode(),'invalid_model_json')]:
            with patch('ltr.quotes.batch.urllib.request.build_opener',return_value=Opener(raw)):
                with self.assertRaises(ModelResponseError) as raised:request_model('http://example.invalid','synthetic','model',{},history=history)
            metadata=failure_metadata(raised.exception)
            self.assertEqual(metadata['error_code'],code)
            self.assertNotIn('PRIVATE',json.dumps(metadata))
            if code=='invalid_model_json':self.assertEqual(metadata['usage']['cache_read_input_tokens'],42)
        self.assertEqual(len(history),2)

    def test_http_refusal_diagnostics_are_static_and_do_not_echo_body(self):
        cases=[('maximum context length exceeded PRIVATE BODY','context_limit'),
               ('model is not supported PRIVATE BODY','model_unavailable'),
               ('quota exhausted PRIVATE BODY','quota'),
               ('rate limit PRIVATE BODY','rate_limit'),
               ('max_tokens exceeds maximum PRIVATE BODY','output_budget'),
               ('PRIVATE CREDENTIAL AND SOURCE','unspecified')]
        for message,expected in cases:
            with self.subTest(expected=expected):
                error=urllib.error.HTTPError('http://example.invalid',400,'Bad request',{},
                    io.BytesIO(json.dumps({'error':{'message':message,'code':'PRIVATE CODE'}}).encode()))
                class Opener:
                    def open(self,*args,**kwargs):raise error
                history=[{'role':'assistant','content':'PRIVATE HISTORY'}]
                with patch('ltr.quotes.batch.urllib.request.build_opener',return_value=Opener()):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        request_model('http://example.invalid','PRIVATE AUTH','model',{'text':'PRIVATE INPUT'},history=history)
                self.assertIs(raised.exception,error)
                metadata=failure_metadata(error)
                self.assertEqual(metadata['provider_error_hint'],expected)
                self.assertEqual(metadata['request_metrics']['history_messages'],1)
                self.assertEqual(metadata['request_metrics']['input_messages'],2)
                self.assertGreater(metadata['request_metrics']['request_bytes'],0)
                self.assertNotIn('PRIVATE',json.dumps(metadata))
                self.assertEqual(len(history),1)

    def test_oversized_error_body_is_bounded_and_not_copied(self):
        body=io.BytesIO(b'PRIVATE'+b'x'*20000)
        error=urllib.error.HTTPError('http://example.invalid',413,'Request refused',{},body)
        class Opener:
            def open(self,*args,**kwargs):raise error
        with patch('ltr.quotes.batch.urllib.request.build_opener',return_value=Opener()):
            with self.assertRaises(urllib.error.HTTPError):
                request_model('http://example.invalid','synthetic','model',{})
        self.assertEqual(body.tell(),8193)
        self.assertEqual(failure_metadata(error)['provider_error_hint'],'unspecified')
        self.assertNotIn('PRIVATE',json.dumps(failure_metadata(error)))
        nested=urllib.error.HTTPError('http://example.invalid',400,'Bad request',{},
                                     io.BytesIO(b'['*2000+b']'*2000))
        error=nested
        with patch('ltr.quotes.batch.urllib.request.build_opener',return_value=Opener()):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                request_model('http://example.invalid','synthetic','model',{})
        self.assertIs(raised.exception,nested)
        self.assertEqual(failure_metadata(nested)['provider_error_hint'],'unspecified')

    def test_network_error_keeps_type_and_only_records_numeric_request_shape(self):
        error=TimeoutError('PRIVATE NETWORK DETAIL')
        class Opener:
            def open(self,*args,**kwargs):raise error
        with patch('ltr.quotes.batch.urllib.request.build_opener',return_value=Opener()):
            with self.assertRaises(TimeoutError) as raised:
                request_model('http://example.invalid','synthetic','model',{})
        self.assertIs(raised.exception,error)
        metadata=failure_metadata(error)
        self.assertEqual(metadata['error_class'],'TimeoutError')
        self.assertEqual(metadata['request_metrics']['input_messages'],1)
        self.assertNotIn('provider_error_hint',metadata)
        self.assertNotIn('PRIVATE',json.dumps(metadata))


class RequestByteEncodingTests(unittest.TestCase):
    def test_default_output_budget_preserves_original_wire_body(self):
        payload = {'synthetic': '合成'}
        system, model, sid = 'Synthetic system', 'synthetic-model', 'synthetic-session'
        history = [{'role': 'user', 'content': 'Synthetic previous message'}]
        original = {'model': model, 'max_tokens': 8000, 'system': system,
                    'messages': [*history, {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
                    'metadata': {'user_id': json.dumps({'device_id': 'corpus-runtime', 'account_uuid': '', 'session_id': sid})}}
        expected = json.dumps(original, ensure_ascii=False).encode('utf-8')
        self.assertEqual(encode_request(model, payload, system=system, history=history, session_id=sid), expected)
        self.assertEqual(encode_request(model, payload, system=system, history=history, session_id=sid, max_output_tokens=8000), expected)

    def test_explicit_output_budget_exact_encoder_send_count_parity(self):
        for budget in (1, 16000, 131072):
            sent = []
            payload = {'synthetic': '合成\ntext'}
            history = [{'role': 'user', 'content': 'Synthetic previous message'}]
            kwargs = dict(system='Synthetic system', history=history, session_id='synthetic-session', max_output_tokens=budget)
            expected = encode_request('synthetic', payload, **kwargs)
            size = request_byte_count('synthetic', payload, **kwargs)

            class Opener:
                def open(self, request, timeout):
                    sent.append(request.data)
                    return io.BytesIO(json.dumps({'stop_reason': 'end_turn',
                        'content': [{'type': 'text', 'text': '{}'}]}).encode())

            with patch('urllib.request.build_opener', return_value=Opener()):
                request_model('http://synthetic.invalid', 'synthetic-token', 'synthetic', payload, **kwargs)
            self.assertEqual(sent, [expected])
            self.assertEqual(size, len(sent[0]))
            self.assertEqual(json.loads(sent[0])['max_tokens'], budget)

    def test_invalid_output_budget_never_opens_network(self):
        for budget in (True, False, 0, -1, 131073, 1.5, '16000', [], {}):
            with patch('urllib.request.build_opener') as opener:
                for function in (encode_request, request_byte_count):
                    with self.assertRaisesRegex(ValueError, '^invalid_output_token_budget$'):
                        function('synthetic', {}, max_output_tokens=budget)
                with self.assertRaisesRegex(ValueError, '^invalid_output_token_budget$'):
                    request_model('http://synthetic.invalid', 'synthetic-token', 'synthetic', {}, max_output_tokens=budget)
                opener.assert_not_called()

    def test_count_and_pure_encoder_equal_exact_sent_http_body(self):
        payload = {'items': [{'id': 'synthetic', 'text': '合成日本語\n"quoted" 😀'}]}
        system = 'Synthetic 系统\n规则'
        initial = [{'role': 'user', 'content': '合成历史'}, {'role': 'assistant', 'content': '{"synthetic":true}'}]
        for sid in (None, 'synthetic-session'):
            for previous in ([], initial):
                history = [dict(row) for row in previous]
                expected = encode_request('synthetic-model', payload, system=system, history=history, session_id=sid)
                count = request_byte_count('synthetic-model', payload, system=system, history=history, session_id=sid)
                self.assertEqual(history, previous)
                sent = []

                class Opener:
                    def open(self, request, timeout):
                        sent.append(request.data)
                        return io.BytesIO(json.dumps({'stop_reason': 'end_turn', 'model': 'synthetic-model',
                            'content': [{'type': 'text', 'text': '{"synthetic":true}'}]}).encode())

                with patch('urllib.request.build_opener', return_value=Opener()):
                    request_model('http://synthetic.invalid', 'synthetic-token', 'synthetic-model', payload,
                                  system=system, history=history, session_id=sid)
                self.assertEqual(sent, [expected])
                self.assertEqual(count, len(sent[0]))
                decoded = json.loads(sent[0])
                self.assertEqual(decoded['system'], system)
                self.assertEqual(len(decoded['messages']), len(previous) + 1)
                self.assertEqual('metadata' in decoded, sid is not None)


if __name__ == '__main__':
    unittest.main()
