"""Synthetic source-first boundaries, immutable receipts and CLI integration."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import check_manifest
from ltr.project import context as ctx, ledger
from ltr.project.storage import create_project, read_project, write_json, project_lock
from ltr.library import glossary

REPO = Path(__file__).resolve().parents[3]
TARGET = '这是自编的当前译文用于检测连续二十个字符的泄漏边界。'
OLD = '这段自编旧译仅用于指纹探针绝对不能进入生成上下文正文。'


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'
        self.work = self.root / 'work'
        self.source = [{'id': 'p1', 'text': 'Synthetic first paragraph about a copper bell.'},
                       {'id': 'p2', 'text': 'Synthetic second paragraph about a quiet bridge.'}]
        create_project(self.project, dict(book_hash='synthetic', language='ja', items=[dict(paragraphs=self.source)]))
        project = read_project(self.project)
        project['units'][0]['target'] = TARGET
        project['units'][1]['target'] = '第二段自编现译。'
        write_json(self.project / 'project.json', project)
        self.ledger = self.root / 'ledger.json'
        ledger.init_ledger(self.ledger, self.project)
        self.add(decision='Keep the uncertain tone.')
        self.store = self.root / 'glossary.sqlite'
        glossary.init_store(self.store)
        entry = glossary.add(self.store, 'copper bell', '铜铃', 'name')
        glossary.confirm(self.store, entry['id'], '铜铃', '')
        glossary.lock(self.store, 'v1', '')
        self.evidence = self.root / 'evidence.json'
        self.relation = {'id': 'r1', 'level': 'verified', 'record': {
            'origin_kind': 'historical_translation', 'text': '自编历史证据短句。',
            'bindings': [{'paragraph_sha256': 'a' * 64}]}, 'references': []}
        self.save(self.evidence, dict(schema='ltr-evidence-v1', items=[self.relation]))
        self.candidates = self.root / 'candidates.json'
        self.save(self.candidates, [{'id': 'p1', 'target': '自编独立候选一。'}, {'id': 'p2', 'target': '自编独立候选二。'}])
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_bytes((REPO / 'assets/review-manifest.example.json').read_bytes())

    def save(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def add(self, **kwargs):
        return ledger.change_ledger(self.ledger, 'add', dimension='character_arcs', kind='fact',
                                    locator='p1', observation=kwargs.pop('observation', 'A synthetic observation.'), **kwargs)

    def build(self, **kwargs):
        return ctx.build(self.work, self.project, ['p1', 'p2'], **kwargs)

    def full_build(self):
        return self.build(ledger_path=self.ledger, evidence=[self.evidence], glossary_path=self.store, glossary_version='v1')

    def frozen(self):
        built = self.build()
        return built, ctx.freeze(self.work, built['context_id'], self.candidates)

    def cli(self, *args):
        proc = subprocess.run([sys.executable, '-X', 'utf8', '-B', 'scripts/ltr', 'context', *map(str, args)],
                              cwd=REPO, capture_output=True, encoding='utf-8', timeout=20)
        return proc

    def test_build_allowlist_and_no_target_or_manifest_body(self):
        result = self.full_build()
        folder = self.work / 'contexts' / result['context_id']
        context = ctx.read_json(folder / 'context.json')
        self.assertEqual(set(context), {'source', 'whole-book-understanding', 'verified-prior-work', 'confirmed-glossary'})
        manifest = (folder / 'manifest.json').read_text(encoding='utf-8')
        for text in (TARGET, self.source[0]['text'], 'A synthetic observation.', '自编历史证据短句。'):
            self.assertNotIn(text, manifest)
        self.assertNotIn(TARGET, (folder / 'context.json').read_text(encoding='utf-8'))
        self.assertEqual(ctx.summaries(self.work)['event_count'], 1)

    def test_current_target_leak_refuses_without_workspace(self):
        self.add(observation='prefix' + TARGET[2:24], decision='Keep tone.')
        with self.assertRaisesRegex(ctx.ContextError, 'current_target_leak'):
            self.build(ledger_path=self.ledger)
        self.assertFalse(self.work.exists())

    def test_old_probe_refuses_and_never_persists(self):
        probe = self.root / 'old.txt'
        probe.write_text(OLD, encoding='utf-8')
        self.add(observation=OLD, decision='Keep tone.')
        with self.assertRaisesRegex(ctx.ContextError, 'old_translation_leak'):
            self.build(ledger_path=self.ledger, probe_old=[probe])
        self.assertFalse(self.work.exists())
        result = self.build(probe_old=[probe])
        for path in self.work.rglob('*'):
            if path.is_file():
                self.assertNotIn(OLD.encode(), path.read_bytes())
        manifest = ctx.read_json(self.work / 'contexts' / result['context_id'] / 'manifest.json')
        self.assertEqual(manifest['probes'], [{'category': 'old-translation-excerpts', 'sha256': ctx.sha(probe.read_bytes())}])

    def test_candidate_and_clue_omitted_nested_verified_included(self):
        candidate = dict(self.relation, id='r2', level='candidate', record={'text': 'Candidate must not enter'})
        clue = dict(self.relation, id='r3', level='clue', record={'text': 'Clue must not enter'}, correspondences=[self.relation])
        self.save(self.evidence, dict(schema='ltr-evidence-v1', items=[candidate, clue]))
        result = self.build(evidence=[self.evidence])
        folder = self.work / 'contexts' / result['context_id']
        self.assertEqual(ctx.read_json(folder / 'manifest.json')['omitted_counts'], {'candidate': 1, 'clue': 1})
        body = (folder / 'context.json').read_text(encoding='utf-8')
        self.assertNotIn('must not enter', body)
        self.assertIn('自编历史证据短句', body)

    def test_target_bound_prior_relation_refused(self):
        for level in ('verified', 'candidate'):
            with self.subTest(level=level):
                relation = copy.deepcopy(self.relation)
                relation['level'] = level
                relation['record']['bindings'][0]['paragraph_sha256'] = ctx.sha(self.source[0]['text'].encode())
                self.save(self.evidence, dict(schema='ltr-evidence-v1', items=[relation]))
                with self.assertRaisesRegex(ctx.ContextError, 'prior_work_overlaps_target_range'):
                    self.build(evidence=[self.evidence])

    def test_unlocked_or_missing_glossary_version_refused(self):
        for version in ('missing', 'unlocked'):
            with self.assertRaisesRegex(glossary.GlossaryError, 'glossary_version_not_found'):
                self.build(glossary_path=self.store, glossary_version=version)
        with self.assertRaisesRegex(ctx.ContextError, 'context_glossary_version_required'):
            self.build(glossary_path=self.store)

    def test_glossary_sentence_rejected(self):
        for index, (source, target) in enumerate((('word', TARGET), (self.source[0]['text'], '短译'),
                                                (self.source[0]['text'][:-3], '另一短译'),
                                                ('prefix ' + self.source[0]['text'] + ' suffix', '额外短译'))):
            store = self.root / ('sentence' + str(index) + '.sqlite')
            glossary.init_store(store)
            entry = glossary.add(store, source, target, 'phrase')
            glossary.confirm(store, entry['id'], target, '')
            version = 'sentence' + str(index)
            glossary.lock(store, version, '')
            with self.assertRaisesRegex(ctx.ContextError, 'glossary_contains_target_sentence'):
                self.build(glossary_path=store, glossary_version=version)

    def test_ledger_binding_and_status_filter(self):
        self.add(observation='pending omitted')
        self.add(observation='withdrawn omitted', decision='Retain')
        ledger.change_ledger(self.ledger, 'remove', id='d3')
        result = self.build(ledger_path=self.ledger)
        rows = ctx.read_json(self.work / 'contexts' / result['context_id'] / 'context.json')['whole-book-understanding']
        self.assertEqual(len(rows), 1)
        data = ledger._load(self.ledger)
        data['project_sha256'] = 'b' * 64
        self.save(self.ledger, data)
        with self.assertRaisesRegex(ctx.ContextError, 'context_ledger_project_mismatch'):
            self.build(ledger_path=self.ledger)

    def test_compare_requires_freeze(self):
        with self.assertRaisesRegex(ctx.ContextError, 'context_not_frozen'):
            ctx.compare(self.work, 'frz-' + '0' * 16, self.project)

    def test_candidate_coverage_order_and_blank(self):
        result = self.build()
        original = ctx.read_json(self.candidates)
        for rows in (original[:1], original[::-1], [dict(original[0], target=' '), original[1]], original + original[:1]):
            self.save(self.candidates, rows)
            with self.assertRaisesRegex(ctx.ContextError, 'context_candidates_invalid'):
                ctx.freeze(self.work, result['context_id'], self.candidates)
        self.assertEqual(ctx.summaries(self.work)['event_count'], 1)

    def test_compare_supersede_export_preserves_original(self):
        built, frozen = self.frozen()
        path = self.work / 'frozen' / (frozen['freeze_id'] + '.json')
        original = path.read_bytes()
        rows = ctx.compare(self.work, frozen['freeze_id'], self.project)
        self.assertEqual(rows[0]['current_target'], TARGET)
        self.save(self.candidates, [dict(id='p1', target='修改后的候选。'), dict(id='p2', target='自编独立候选二。')])
        new = ctx.freeze(self.work, built['context_id'], self.candidates, frozen['freeze_id'], 'Synthetic revision reason')
        data = ctx.read_json(self.work / 'frozen' / (new['freeze_id'] + '.json'))
        self.assertFalse(data['before_first_comparison'])
        self.assertEqual(data['changed_ids'], ['p1'])
        self.assertEqual(data['reason'], 'Synthetic revision reason')
        self.assertEqual(path.read_bytes(), original)
        result = ctx.export(self.work, new['freeze_id'], self.manifest)
        self.assertEqual(result['frozen_before_comparison_ids'], ['p1', 'p2'])
        self.assertEqual(result['operator_exposure_status'], 'not-claimed')

    def test_repeat_freeze_requires_supersedes_and_reason(self):
        built, frozen = self.frozen()
        for parent, reason in ((None, None), (frozen['freeze_id'], ' ')):
            with self.assertRaisesRegex(ctx.ContextError, 'context_supersedes_required'):
                ctx.freeze(self.work, built['context_id'], self.candidates, parent, reason)

    def test_event_tamper_each_line_and_tail_removal(self):
        _, frozen = self.frozen()
        ctx.compare(self.work, frozen['freeze_id'], self.project)
        path = self.work / 'events.jsonl'
        original = path.read_bytes()
        lines = original.splitlines(keepends=True)
        for index in range(len(lines)):
            changed = list(lines)
            data = json.loads(changed[index])
            data['at'] = 'tampered'
            changed[index] = ctx.encoded(data)
            path.write_bytes(b''.join(changed))
            with self.assertRaisesRegex(ctx.ContextError, 'context_event_chain_broken'):
                ctx.summaries(self.work)
        path.write_bytes(original[:-1] + b' \n')
        with self.assertRaisesRegex(ctx.ContextError, 'context_event_chain_broken'):
            ctx.summaries(self.work)
        path.write_bytes(b''.join(lines[:1]))
        with self.assertRaisesRegex(ctx.ContextError, 'context_event_chain_broken'):
            ctx.summaries(self.work)
        path.write_bytes(original)

    def test_freeze_and_context_tamper(self):
        built, frozen = self.frozen()
        for path in (self.work / 'frozen' / (frozen['freeze_id'] + '.json'),
                     self.work / 'contexts' / built['context_id'] / 'context.json',
                     self.work / 'contexts' / built['context_id'] / 'manifest.json'):
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            code = 'context_not_frozen' if path.parent.name == 'frozen' else 'context_event_chain_broken'
            with self.assertRaisesRegex(ctx.ContextError, code):
                ctx.export(self.work, frozen['freeze_id'], self.manifest)
            path.write_bytes(original)

    def test_export_only_replaces_value_and_validates(self):
        result = self.full_build()
        frozen = ctx.freeze(self.work, result['context_id'], self.candidates)
        ctx.compare(self.work, frozen['freeze_id'], self.project)
        before = self.manifest.read_bytes()
        expected = ctx.export(self.work, frozen['freeze_id'], self.manifest)
        ctx.export(self.work, frozen['freeze_id'], self.manifest, write=True)
        after = self.manifest.read_bytes()
        self.assertEqual(after, ledger._replace_understanding(before, expected, 'source_first'))
        self.assertEqual(ledger._replace_understanding(before, {}, 'source_first'),
                         ledger._replace_understanding(after, {}, 'source_first'))
        errors = check_manifest.validate(json.loads(after))
        self.assertFalse([e for e in errors if e.startswith('source_first_')])

    def test_output_new_private_file_only_and_old_whole_attachment(self):
        _, frozen = self.frozen()
        output = self.root / 'compare.json'
        old = self.root / 'old.txt'
        old.write_text(OLD, encoding='utf-8')
        result = ctx.compare(self.work, frozen['freeze_id'], self.project, [old], output)
        self.assertEqual(result['count'], 2)
        self.assertEqual(ctx.read_json(output)['old_translations'], [OLD])
        original = output.read_bytes()
        with self.assertRaisesRegex(ctx.ContextError, 'context_output_exists_or_reserved'):
            ctx.compare(self.work, frozen['freeze_id'], self.project, output=output)
        self.assertEqual(output.read_bytes(), original)
        with self.assertRaises(ValueError):
            ctx.compare(self.work, frozen['freeze_id'], self.project, output=REPO / 'private.json')

    def test_git_workspace_and_corpus_refused(self):
        for folder, marker in (('git-work', '.git'), ('corpus', 'dialogue.sqlite')):
            root = self.root / folder
            root.mkdir()
            (root / marker).touch()
            with self.assertRaises(ValueError):
                ctx.build(root / 'context', self.project, ['p1', 'p2'])

    def test_static_cli_errors_and_help(self):
        for args in ((), ('build',), ('compare', '--workspace', self.work, '--frozen', TARGET, '--project', self.project)):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn(TARGET, result.stdout + result.stderr)
            self.assertNotIn(str(self.root), result.stdout + result.stderr)
        for command in ([], ['build'], ['freeze'], ['compare'], ['export'], ['list'], ['show']):
            result = self.cli(*command, '--help')
            self.assertEqual(result.returncode, 0)
            self.assertIn('not-claimed', result.stdout)

    def test_cards_horizon_and_leak(self):
        cards = self.root / 'cards.json'
        card = dict(id='card1', revision=1, claim='A source-grounded observation.', epistemic='explicit',
                    review_status='verified', evidence=[dict(paragraph_id='p1', start=0, end=9, book_hash='synthetic')],
                    available_from='p1')
        self.save(cards, [card])
        result = self.build(cards=cards, horizon='p1')
        self.assertEqual(len(result['inputs']), 2)
        card['claim'] = TARGET
        self.save(cards, [card])
        with self.assertRaisesRegex(ctx.ContextError, 'current_target_leak'):
            self.build(cards=cards, horizon='p1')
        card['claim'] = 'Short claim'
        card['available_from'] = 'p2'
        self.save(cards, [card])
        with self.assertRaisesRegex(ValueError, 'future_context'):
            self.build(cards=cards, horizon='p1')

    def test_disallowed_origin_even_in_omitted_record(self):
        self.relation['level'] = 'candidate'
        self.relation['record']['origin_kind'] = 'generated_draft'
        self.save(self.evidence, dict(schema='ltr-evidence-v1', items=[self.relation]))
        with self.assertRaisesRegex(ctx.ContextError, 'context_disallowed_origin'):
            self.build(evidence=[self.evidence])

    def test_ranges(self):
        for span in (['missing', 'p2'], ['p2', 'p1']):
            with self.assertRaises(ctx.ContextError):
                ctx.build(self.work, self.project, span)
        result = ctx.build(self.work, self.project, ['p2', 'p2'])
        self.assertEqual(result['inputs'][0]['count'], 1)

    def test_failed_project_read_still_records_comparison(self):
        built, frozen = self.frozen()
        with patch.object(ledger, '_project', side_effect=ValueError('synthetic failure')):
            with self.assertRaises(ValueError):
                ctx.compare(self.work, frozen['freeze_id'], self.project)
        new = ctx.freeze(self.work, built['context_id'], self.candidates, frozen['freeze_id'], 'Retry')
        self.assertFalse(new['before_first_comparison'])

    def test_writer_lock(self):
        built = self.build()
        with project_lock(self.work), self.assertRaisesRegex(ValueError, 'project_locked'):
            ctx.freeze(self.work, built['context_id'], self.candidates)

    def test_current_target_can_change_after_build(self):
        _, frozen = self.frozen()
        project = read_project(self.project)
        project['units'][0]['target'] = '当前译稿已修订。'
        project['revision'] += 1
        write_json(self.project / 'project.json', project)
        result = ctx.compare(self.work, frozen['freeze_id'], self.project)
        self.assertEqual(result[0]['current_target'], '当前译稿已修订。')

    def test_other_context_cannot_be_superseded(self):
        _, first = self.frozen()
        second = self.build()
        current = ctx.freeze(self.work, second['context_id'], self.candidates)
        with self.assertRaisesRegex(ctx.ContextError, 'context_supersedes_mismatch'):
            ctx.freeze(self.work, second['context_id'], self.candidates, first['freeze_id'], 'Reason')
        self.assertTrue(current['before_first_comparison'])

    def test_export_missing_field_preserves_other_bytes(self):
        _, frozen = self.frozen()
        raw = b'{ "schema" : "literary-review-manifest-v2", "other" : [ 1, 2 ] }\n'
        self.manifest.write_bytes(raw)
        ctx.export(self.work, frozen['freeze_id'], self.manifest, write=True)
        after = self.manifest.read_bytes()
        self.assertTrue(after.startswith(raw[:raw.rindex(b'}')]))
        self.assertEqual(json.loads(after)['other'], [1, 2])

    def test_empty_chain_with_orphan_refused(self):
        self.build()
        (self.work / 'events.jsonl').write_bytes(b'')
        with self.assertRaisesRegex(ctx.ContextError, 'context_event_chain_broken'):
            ctx.summaries(self.work)

    def test_threshold_is_unicode_characters(self):
        self.add(observation=TARGET[:19], decision='Retain')
        self.build(ledger_path=self.ledger)
        self.add(observation=TARGET[:20], decision='Retain')
        with self.assertRaisesRegex(ctx.ContextError, 'current_target_leak'):
            self.build(ledger_path=self.ledger)

    def test_hard_link_input_refused(self):
        import os
        linked = self.root / 'linked.json'
        os.link(self.evidence, linked)
        with self.assertRaisesRegex(glossary.GlossaryError, 'glossary_path_invalid'):
            self.build(evidence=[linked])

    def test_v2_bound_project(self):
        from ltr.project.binding import create_bound_project
        project = self.root / 'bound'
        imported = dict(book_hash='synthetic', language='ja', items=[dict(paragraphs=self.source)])
        create_bound_project(project, imported, project / 'corpus')
        result = ctx.build(self.work, project, ['p1', 'p2'])
        self.assertEqual(result['inputs'][0]['count'], 2)
        with self.assertRaises(ValueError):
            ctx.build(project / 'corpus' / 'contexts', project, ['p1', 'p2'])

    def test_cli_end_to_end_synthetic(self):
        def call(*args):
            proc = self.cli(*args)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)
        rejected = self.cli('compare', '--workspace', self.work, '--frozen', 'frz-' + '0' * 16, '--project', self.project)
        self.assertEqual(json.loads(rejected.stdout)['code'], 'context_not_frozen')
        built = call('build', '--workspace', self.work, '--project', self.project, '--range', 'p1', 'p2',
                     '--ledger', self.ledger, '--evidence', self.evidence, '--glossary', self.store, '--glossary-version', 'v1')
        frozen = call('freeze', '--workspace', self.work, '--context', built['context_id'], '--candidates', self.candidates)
        compared = call('compare', '--workspace', self.work, '--frozen', frozen['freeze_id'], '--project', self.project,
                        '--output', self.root / 'comparison.json')
        exported = call('export', '--workspace', self.work, '--frozen', frozen['freeze_id'], '--manifest', self.manifest, '--write')
        call('list', '--workspace', self.work)
        call('show', '--workspace', self.work, '--id', frozen['freeze_id'])
        probe = self.root / 'probe.txt'
        probe.write_text(OLD, encoding='utf-8')
        self.add(observation=OLD, decision='Keep tone')
        rejected = self.cli('build', '--workspace', self.root / 'rejected', '--project', self.project, '--range', 'p1', 'p2',
                            '--ledger', self.ledger, '--probe-old', probe)
        self.assertEqual(json.loads(rejected.stdout)['code'], 'old_translation_leak')
        self.assertFalse([e for e in check_manifest.validate(ctx.read_json(self.manifest)) if e.startswith('source_first_')])
        self.assertEqual((len(built['inputs']), frozen['count'], compared['count'], len(exported['candidate_ids'])), (4, 2, 2, 2))


if __name__ == '__main__':
    unittest.main()
