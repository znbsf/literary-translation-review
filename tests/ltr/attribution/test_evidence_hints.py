"""Synthetic-only tests for source-exact attribution evidence hints."""
import hashlib
import unittest

from ltr.attribution.evidence_hints import (
    MAX_HINTS_PER_PARAGRAPH,
    MAX_HINTS_PER_TARGET,
    build_evidence_hints,
)


def target(dialogue_id, edition_id, paragraphs):
    return {'dialogue_id': dialogue_id, 'edition_id': edition_id, 'context': paragraphs}


def paragraph(paragraph_id, edition_id, text):
    return {'id': paragraph_id, 'edition_id': edition_id, 'text': text, 'ordinal': 1}


class AttributionEvidenceHintTests(unittest.TestCase):
    def test_overlapping_repeated_substrings_expand_to_unique_exact_span(self):
        source = 'aaaXaaa'
        result = build_evidence_hints([target('d1', 'e1', [paragraph('p1', 'e1', source)])], [])
        span = next(item for item in result[0]['spans'] if item['start'] == 0)
        self.assertEqual((span['start'], span['end'], span['text']), (0, len(source), source))
        self.assertEqual(source.count(span['text']), 1)
        self.assertEqual(span['paragraph_sha256'], hashlib.sha256(source.encode('utf-8')).hexdigest())
        overlap = build_evidence_hints([target('d2', 'e1', [paragraph('p2', 'e1', 'aaaa')])], [])
        self.assertTrue(overlap[0]['spans'])
        self.assertTrue(all(item['text'] != 'aaa' for item in overlap[0]['spans']))
        for item in overlap[0]['spans']:
            occurrences = sum('aaaa'.startswith(item['text'], offset)
                              for offset in range(len('aaaa') - len(item['text']) + 1))
            self.assertEqual(occurrences, 1)

    def test_repeated_content_longer_than_maximum_span_has_no_hint(self):
        source = 'a' * 205
        result = build_evidence_hints([target('d1', 'e1', [paragraph('p1', 'e1', source)])], [])
        self.assertEqual(result, [{'dialogue_id': 'd1', 'edition_id': 'e1', 'spans': []}])

    def test_long_repeated_first_paragraph_does_not_exhaust_target_budget(self):
        result = build_evidence_hints([target('d1', 'e1', [
            paragraph('repeated', 'e1', 'a' * 205),
            paragraph('useful', 'e1', 'a distinct later source phrase.'),
        ])], [])
        self.assertTrue(result[0]['spans'])
        self.assertTrue(all(span['paragraph_id'] == 'useful' for span in result[0]['spans']))

    def test_199_repeated_codepoints_form_one_unique_whole_paragraph_hint(self):
        source = 'a' * 199
        result = build_evidence_hints([target('d1', 'e1', [paragraph('p1', 'e1', source)])], [])
        self.assertTrue(any((span['start'], span['end'], span['text']) == (0, 199, source)
                            for span in result[0]['spans']))

    def test_middle_vocabulary_anchor_is_prioritized_and_keeps_context(self):
        source = ('a' * 80) + ' Alias spoke ' + ('b' * 80)
        result = build_evidence_hints([target('d1', 'e1', [paragraph('p1', 'e1', source)])], ['Alias'])
        self.assertTrue(any(span['start'] <= source.index('Alias')
                            and span['end'] >= source.index('Alias') + len('Alias')
                            and span['text'].count('Alias') == 1
                            and len(span['text']) > len('Alias')
                            for span in result[0]['spans'][:3]))

    def test_foreign_edition_with_same_paragraph_id_is_ignored(self):
        result = build_evidence_hints([target('d1', 'edition-a', [
            paragraph('shared', 'edition-b', 'foreign edition must never leak'),
            paragraph('own', 'edition-a', 'own edition source'),
        ])], [])
        self.assertTrue(all(span['paragraph_id'] == 'own' for span in result[0]['spans']))
        self.assertTrue(all('foreign' not in span['text'] for span in result[0]['spans']))

    def test_targets_keep_their_own_context(self):
        result = build_evidence_hints([
            target('d1', 'e1', [paragraph('p1', 'e1', 'first target phrase.')]),
            target('d2', 'e1', [paragraph('p2', 'e1', 'second target phrase.')]),
        ], [])
        self.assertEqual([row['dialogue_id'] for row in result], ['d1', 'd2'])
        self.assertTrue(result[0]['spans'] and result[1]['spans'])
        self.assertTrue(all(span['paragraph_id'] == 'p1' for span in result[0]['spans']))
        self.assertTrue(all(span['paragraph_id'] == 'p2' for span in result[1]['spans']))

    def test_result_is_deterministic_and_obeys_per_target_and_paragraph_limits(self):
        context = [paragraph(f'p{i}', 'e1', f'Unique fixture token{i} appears here. Next sentence!')
                   for i in range(12)]
        item = target('d1', 'e1', context)
        first = build_evidence_hints([item], [f'token{i}' for i in range(12)])
        second = build_evidence_hints([item], [f'token{i}' for i in range(12)])
        self.assertEqual(first, second)
        self.assertLessEqual(len(first[0]['spans']), MAX_HINTS_PER_TARGET)
        per_paragraph = {}
        for span in first[0]['spans']:
            per_paragraph[span['paragraph_id']] = per_paragraph.get(span['paragraph_id'], 0) + 1
        self.assertTrue(all(count <= MAX_HINTS_PER_PARAGRAPH for count in per_paragraph.values()))

    def test_unicode_and_newline_offsets_are_codepoint_offsets(self):
        source = '猫\n猫\n猫尾。'
        result = build_evidence_hints([target('d1', 'e1', [paragraph('p1', 'e1', source)])], [])
        span = next(item for item in result[0]['spans'] if item['start'] == 0)
        self.assertEqual((span['start'], span['end'], span['text']), (0, len(source), source))
        self.assertEqual(source[span['start']:span['end']], span['text'])
        self.assertEqual(span['paragraph_sha256'], hashlib.sha256(source.encode('utf-8')).hexdigest())

    def test_vocabulary_is_only_an_anchor_and_output_has_no_identity_fields(self):
        result = build_evidence_hints([target('d1', 'e1', [
            paragraph('p1', 'e1', 'Alias called, then stopped.'),
        ])], ['Alias'])
        self.assertTrue(any('Alias' in span['text'] for span in result[0]['spans']))
        self.assertEqual(set(result[0]), {'dialogue_id', 'edition_id', 'spans'})
        self.assertTrue(all(set(span) == {'paragraph_id', 'start', 'end', 'text', 'paragraph_sha256'}
                            for span in result[0]['spans']))

    def test_invalid_shapes_raise_static_codes(self):
        with self.assertRaisesRegex(ValueError, '^invalid_evidence_hint_items$'):
            build_evidence_hints([None], [])
        with self.assertRaisesRegex(ValueError, '^duplicate_evidence_hint_target$'):
            build_evidence_hints([target('d1', 'e1', []), target('d1', 'e2', [])], [])
        with self.assertRaisesRegex(ValueError, '^invalid_evidence_hint_context$'):
            build_evidence_hints([target('d1', 'e1', [paragraph('p1', 'e1', 7)])], [])
        with self.assertRaisesRegex(ValueError, '^invalid_evidence_hint_vocabulary$'):
            build_evidence_hints([], [''])


if __name__ == '__main__':
    unittest.main()
