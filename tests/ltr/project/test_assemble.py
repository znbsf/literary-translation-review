"""Synthetic project assembly, reader content and publication boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from ltr.project.assemble import AssembleError, assemble_project, _write_new
from ltr.project.binding import create_bound_project
from ltr.project.storage import StorageError, read_project, write_json

REPO = Path(__file__).resolve().parents[3]
XHTML = '{http://www.w3.org/1999/xhtml}'


class AssembleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'
        self.out = self.root / 'out'
        imported = {'book_hash': 'synthetic-hash', 'language': 'en', 'items': [
            {'id': item, 'paragraphs': [
                {'id': f'{item}:p{n}', 'text': f'Synthetic source {item} {n}',
                 'locator': {'item_id': item, 'element_path': f'p[{n}]', 'start': 0, 'end': 1}}
                for n in range(1, count + 1)]}
            for item, count in [('section-a', 2), ('section-b', 1)]]}
        create_bound_project(self.project, imported, self.project / 'corpus')
        self.disk = json.loads((self.project / 'project.json').read_text(encoding='utf-8'))
        self.targets = ['  Synthetic first & <one>  ', 'Synthetic second\ncontinued', 'Synthetic last']
        for unit, text in zip(self.disk['units'], self.targets):
            unit.update(target=text, status='accepted')
        self.save()

    def save(self):
        write_json(self.project / 'project.json', self.disk)

    def cli(self, *args):
        return subprocess.run([sys.executable, '-X', 'utf8', '-B', 'scripts/ltr', 'assemble',
                               '--project', str(self.project), '--out', str(self.out), *args],
                              cwd=REPO, capture_output=True, text=True, encoding='utf-8', timeout=30)

    def test_cli_confirmed_epub_and_txt_order_navigation_and_privacy(self):
        result = self.cli('--format', 'both')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)['status'], 'assembled')
        txt = (self.out / 'translation.txt').read_text(encoding='utf-8')
        self.assertEqual(txt, '译文\n\n' + '\n'.join(self.targets[:2]) + '\n\n' + self.targets[2] + '\n')
        with zipfile.ZipFile(self.out / 'translation.epub') as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(z.infolist()[0].filename, 'mimetype')
            self.assertEqual(z.infolist()[0].compress_type, zipfile.ZIP_STORED)
            self.assertEqual(z.read('mimetype'), b'application/epub+zip')
            container = ET.fromstring(z.read('META-INF/container.xml'))
            self.assertEqual(next(container.iter('{urn:oasis:names:tc:opendocument:xmlns:container}rootfile')).get('full-path'), 'EPUB/package.opf')
            package = ET.fromstring(z.read('EPUB/package.opf'))
            ns = '{http://www.idpf.org/2007/opf}'
            manifest = {e.get('id'): e.get('href') for e in package.iter(ns + 'item')}
            spine = [manifest[e.get('idref')] for e in package.iter(ns + 'itemref')]
            self.assertEqual(spine, ['title.xhtml', 'chapter-0001.xhtml', 'chapter-0002.xhtml'])
            nav = ET.fromstring(z.read('EPUB/nav.xhtml'))
            self.assertEqual([e.get('href') for e in nav.iter(XHTML + 'a')], spine[1:])
            targets = []
            for name in spine[1:]:
                doc = ET.fromstring(z.read('EPUB/' + name))
                for p in doc.iter(XHTML + 'p'):
                    targets.append((p.text or '') + ''.join('\n' + (c.tail or '') for c in p))
            self.assertEqual(targets, self.targets)
            content = txt + ''.join(z.read(n).decode('utf-8') for n in z.namelist())
            for value in [str(self.root), 'Synthetic source', *[u['id'] for u in self.disk['units']]]:
                self.assertNotIn(value, content)

    def test_shuffled_units_follow_source_order(self):
        self.disk['units'].reverse()
        self.save()
        assemble_project(self.project, self.out, format='txt')
        text = (self.out / 'translation.txt').read_text(encoding='utf-8')
        self.assertLess(text.index(self.targets[0]), text.index(self.targets[1]))
        self.assertLess(text.index(self.targets[1]), text.index(self.targets[2]))

    def test_duplicate_and_foreign_units_are_not_draft_exceptions(self):
        original = list(self.disk['units'])
        for extra in (original[0], {**original[0], 'id': 'foreign'}):
            self.disk['units'] = original + [extra]
            self.save()
            with self.assertRaisesRegex(StorageError, 'coverage_mismatch'):
                assemble_project(self.project, self.out, allow_draft=True)
            self.assertFalse(self.out.exists())

    def test_pending_reports_all_ids_before_output(self):
        self.disk['units'][0]['status'] = 'draft'
        self.disk['units'][1]['status'] = 'needs_review'
        self.disk['units'][2].update(status='draft', target='')
        self.save()
        result = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['paragraph_ids'], [u['id'] for u in self.disk['units']])
        self.assertFalse(self.out.exists())

    def test_missing_unit_fails_with_id_and_draft_has_visible_placeholder(self):
        missing = self.disk['units'].pop(1)['id']
        self.save()
        with self.assertRaises(AssembleError) as caught:
            assemble_project(self.project, self.out)
        self.assertEqual(caught.exception.paragraph_ids, [missing])
        # Existing callers keep strict coverage validation.
        with self.assertRaisesRegex(StorageError, 'coverage_mismatch'):
            read_project(self.project)
        assemble_project(self.project, self.out, allow_draft=True)
        self.assertIn('〔缺失译文〕', (self.out / 'translation.txt').read_text(encoding='utf-8'))

    def test_draft_notice_on_txt_and_epub_first_page(self):
        self.disk['units'][0]['status'] = 'draft'
        self.save()
        result = self.cli('--allow-draft')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue((self.out / 'translation.txt').read_text(encoding='utf-8').startswith('译文\n含未确认草稿\n'))
        with zipfile.ZipFile(self.out / 'translation.epub') as z:
            self.assertIn('含未确认草稿', z.read('EPUB/title.xhtml').decode('utf-8'))

    def test_existing_identical_file_is_rejected_and_all_outputs_preflighted(self):
        assemble_project(self.project, self.out, format='epub')
        before = (self.out / 'translation.epub').read_bytes()
        with self.assertRaisesRegex(AssembleError, 'assemble_output_exists'):
            assemble_project(self.project, self.out)
        self.assertEqual((self.out / 'translation.epub').read_bytes(), before)
        self.assertFalse((self.out / 'translation.txt').exists())

    def test_output_inside_git_is_rejected(self):
        with self.assertRaisesRegex(StorageError, 'private_root_inside_git'):
            assemble_project(self.project, REPO / 'outputs')

    def test_individual_formats(self):
        for format in ('epub', 'txt'):
            out = self.root / format
            assemble_project(self.project, out, format=format)
            self.assertEqual([p.name for p in out.iterdir()], ['translation.' + format])

    def test_atomic_publication_failure_leaves_no_partial_or_temporary_file(self):
        self.out.mkdir()
        target = self.out / 'translation.txt'
        with patch('ltr.project.assemble.os.link', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                _write_new(target, b'synthetic')
        self.assertEqual(list(self.out.iterdir()), [])

    def test_publication_race_cannot_overwrite(self):
        self.out.mkdir()
        target = self.out / 'translation.txt'
        link = os.link
        def raced(source, destination):
            Path(destination).write_bytes(b'existing')
            return link(source, destination)
        with patch('ltr.project.assemble.os.link', side_effect=raced):
            with self.assertRaisesRegex(AssembleError, 'assemble_output_exists'):
                _write_new(target, b'synthetic')
        self.assertEqual(target.read_bytes(), b'existing')
        self.assertEqual(list(self.out.iterdir()), [target])

    def test_source_snapshot_tampering_is_rejected(self):
        (self.project / 'source-snapshot.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(StorageError, 'source_snapshot_changed'):
            assemble_project(self.project, self.out)
        self.assertFalse(self.out.exists())


if __name__ == '__main__':
    unittest.main()
