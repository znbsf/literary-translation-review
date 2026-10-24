"""Synthetic-only tests for strict attribution suggestion projections."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ltr.attribution import index as ai
from ltr.attribution import projection
from ltr.attribution.review import load_targets
from ltr.library.review_queue import fingerprint


SCHEMA = """
CREATE TABLE editions(id TEXT,title TEXT,language TEXT,book_key TEXT);
CREATE TABLE paragraphs(id TEXT,edition_id TEXT,ordinal INTEGER,text TEXT);
CREATE TABLE dialogues(id TEXT,edition_id TEXT,ordinal INTEGER,text TEXT,start_para INTEGER,
    end_para INTEGER,speaker TEXT,interlocutor TEXT,attribution_status TEXT);
CREATE TABLE attribution_candidates(dialogue_id TEXT,edition_id TEXT,speaker_surface TEXT,
    speaker_canonical TEXT,addressee_surface TEXT,addressee_canonical TEXT);
"""


class AttributionProjectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source_dir = self.root / 'source'
        self.source_dir.mkdir()
        self.db = self.source_dir / 'dialogue.sqlite'
        con = sqlite3.connect(self.db)
        try:
            con.executescript(SCHEMA)
            con.execute("INSERT INTO editions VALUES ('e1','Synthetic','zh','test')")
            con.executemany('INSERT INTO paragraphs VALUES (?,?,?,?)', [
                ('p0', 'e1', 0, 'Opening.'),
                ('p1', 'e1', 1, 'Alice said hi. Alice said hi.'),
                ('p2', 'e1', 2, 'Bob answered clearly.'),
                ('p3', 'e1', 3, 'Closing.'),
            ])
            con.executemany('INSERT INTO dialogues VALUES (?,?,?,?,?,?,?,?,?)', [
                ('d1', 'e1', 0, 'SYNTHETIC_LINE_ONE', 1, 1, 'Alice', 'Bob', 'unknown'),
                ('d2', 'e1', 1, 'SYNTHETIC_LINE_TWO', 2, 2, 'Bob', 'Alice', 'unknown'),
            ])
            con.executemany('INSERT INTO attribution_candidates VALUES (?,?,?,?,?,?)', [
                ('d1', 'e1', 'Alice', 'unknown', 'Bob', ''),
                ('d2', 'e1', 'Bob', '', 'Alice', ''),
            ])
            con.commit()
        finally:
            con.close()
        self.source_sha = fingerprint(self.db)
        self.inputs = self.root / 'history'
        self.inputs.mkdir()
        self.out = self.root / 'projection'
        self.db_before = self.db.read_bytes()

    def _row(self, dialogue_id, paragraph_id, evidence_text, *, position=None):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            target = next(item for item in load_targets(con, 'haruhi_unknown', 'e1')
                          if item['dialogue_id'] == dialogue_id)
            text = con.execute('SELECT text FROM paragraphs WHERE id=?',
                               (paragraph_id,)).fetchone()[0]
        finally:
            con.close()
        start = text.index(evidence_text) if position is None else position
        locator = {'edition_id': 'e1', 'paragraph_id': paragraph_id,
                   'ordinal': int(paragraph_id[1:]), 'start': start,
                   'end': start + len(evidence_text),
                   'paragraph_sha256': hashlib.sha256(text.encode()).hexdigest()}
        return {'suggestion_id': ai.identity(self.source_sha, 'haruhi_unknown', 'e1',
                                               dialogue_id, target['context_sha256']),
                'dialogue_id': dialogue_id, 'edition_id': 'e1',
                'ordinal': target['ordinal'], 'context_sha256': target['context_sha256'],
                'context_window': target['context_window'],
                'dialogue_text_sha256': target['dialogue_text_sha256'],
                'source_candidate_sha256': target['source_candidate_sha256'],
                'speaker': 'Alice' if dialogue_id == 'd1' else 'Bob',
                'addressee': 'Bob' if dialogue_id == 'd1' else 'Alice',
                'kind': 'spoken', 'basis': 'explicit_name', 'evidence': [locator],
                **ai.FLAGS}

    def _doc(self, rows, **overrides):
        doc = {'schema': ai.SUGGESTIONS_SCHEMA, 'database_sha256': self.source_sha,
               'kind': 'haruhi_unknown', 'edition_id': 'e1', 'model': 'synthetic-model',
               'served_model': 'synthetic-model', 'input_sha256': 'synthetic-input',
               'session_id': 'synthetic-session', 'rows': rows}
        doc.update(overrides)
        return doc

    def _write(self, name, rows, **overrides):
        path = self.inputs / name
        path.write_text(json.dumps(self._doc(rows, **overrides), ensure_ascii=False),
                        encoding='utf-8')
        return path

    def _mixed_rows(self):
        return [self._row('d1', 'p1', 'Alice said hi'),
                self._row('d2', 'p2', 'Bob answered clearly')]

    def test_default_strict_mode_rejects_duplicate_span_without_output(self):
        self._write('suggestion-001.json', self._mixed_rows())
        with self.assertRaisesRegex(ValueError, '^non_unique_evidence_span$'):
            projection.project(self.db, [self.inputs], self.out)
        self.assertFalse(self.out.exists())
        self.assertEqual(self.db.read_bytes(), self.db_before)

    def test_explicit_quarantine_removes_only_validator_rejected_row(self):
        path = self._write('suggestion-001.json', self._mixed_rows())
        original = path.read_bytes()
        result = projection.project(self.db, [self.inputs], self.out,
                                    quarantine_non_unique_evidence=True)
        self.assertEqual(result['original_rows'], 2)
        self.assertEqual(result['output_rows'], 1)
        self.assertEqual(result['quarantined_rows'], 1)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.db.read_bytes(), self.db_before)
        data = json.loads((self.out / 'input-000' / path.name).read_text(encoding='utf-8'))
        self.assertEqual([row['dialogue_id'] for row in data['rows']], ['d2'])
        self.assertEqual(data['_attribution_projection']['source_sha256'], self.source_sha)
        excluded = data['_attribution_projection']['excluded_rows']
        self.assertEqual(excluded, [{'row_number': 1,
                                     'row_sha256': hashlib.sha256(json.dumps(
                                         self._mixed_rows()[0], ensure_ascii=False,
                                         separators=(',', ':')).encode()).hexdigest(),
                                     'code': projection.QUARANTINE_CODE}])
        con = sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        try:
            loaded, _ = ai.load_suggestions(con, self.out, self.source_sha)
        finally:
            con.close()
        self.assertEqual(set(record['dialogue_id'] for record in loaded.values()), {'d2'})

    def test_unique_unmodified_file_is_byte_identical(self):
        row = self._row('d2', 'p2', 'Bob answered clearly')
        path = self._write('suggestion-001.json', [row])
        original = path.read_bytes()
        projection.project(self.db, [self.inputs], self.out)
        copied = self.out / 'input-000' / path.name
        self.assertEqual(copied.read_bytes(), original)
        manifest = json.loads((self.out / 'projection-manifest.json').read_text())
        self.assertEqual(manifest['files'][0]['original_sha256'],
                         hashlib.sha256(original).hexdigest())
        self.assertEqual(manifest['files'][0]['output_sha256'],
                         hashlib.sha256(original).hexdigest())

    def test_overlapping_match_start_is_quarantined_without_changing_retained_row(self):
        con = sqlite3.connect(self.db)
        try:
            con.execute("UPDATE paragraphs SET text='aaaa' WHERE id='p1'")
            con.commit()
        finally:
            con.close()
        self.source_sha = fingerprint(self.db)
        self.db_before = self.db.read_bytes()
        bad = self._row('d1', 'p1', 'aaa')
        good = self._row('d2', 'p2', 'Bob answered clearly')
        path = self._write('suggestion-overlap.json', [bad, good])
        original = path.read_bytes()
        result = projection.project(self.db, [self.inputs], self.out,
                                    quarantine_non_unique_evidence=True)
        self.assertEqual((result['output_rows'], result['quarantined_rows']), (1, 1))
        projected = json.loads((self.out / 'input-000' / path.name).read_bytes())
        self.assertEqual(projected['rows'], [good])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.db.read_bytes(), self.db_before)

    def test_cli_parse_error_does_not_echo_unrecognized_private_argument(self):
        secret = str(self.root / 'PRIVATE_ARGUMENT_SENTINEL')
        result = subprocess.run([sys.executable, '-B', '-m', 'ltr.attribution.projection',
                                 '--db', str(self.db), '--input', str(self.inputs),
                                 '--out', str(self.out), secret], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[3] / 'scripts')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, 'attribution projection refused: invalid arguments\n')
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertFalse(self.out.exists())

    def test_non_unique_alone_can_be_quarantined_but_bad_marker_cannot(self):
        rows = self._mixed_rows()
        rows[1]['accepted'] = 1
        self._write('suggestion-001.json', rows)
        with self.assertRaisesRegex(ValueError, '^attribution_flags_invalid$'):
            projection.project(self.db, [self.inputs], self.out,
                               quarantine_non_unique_evidence=True)
        self.assertFalse(self.out.exists())

    def test_wrong_source_binding_is_not_quarantined(self):
        self._write('suggestion-001.json', self._mixed_rows(),
                    database_sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, '^attribution_source_mismatch$'):
            projection.project(self.db, [self.inputs], self.out,
                               quarantine_non_unique_evidence=True)
        self.assertFalse(self.out.exists())

    def test_source_change_during_validation_prevents_publication(self):
        self._write('suggestion-001.json', [self._row('d2', 'p2', 'Bob answered clearly')])
        original_loader = projection.load_suggestions
        changed = False

        def load_then_change(*args, **kwargs):
            nonlocal changed
            result = original_loader(*args, **kwargs)
            if not changed:
                con = sqlite3.connect(self.db)
                try:
                    con.execute("UPDATE editions SET title='Changed' WHERE id='e1'")
                    con.commit()
                finally:
                    con.close()
                changed = True
            return result

        with mock.patch.object(projection, 'load_suggestions', side_effect=load_then_change):
            with self.assertRaisesRegex(ValueError, '^projection_source_changed$'):
                projection.project(self.db, [self.inputs], self.out)
        self.assertFalse(self.out.exists())

    def test_input_change_during_validation_prevents_publication(self):
        path = self._write('suggestion-001.json',
                           [self._row('d2', 'p2', 'Bob answered clearly')])
        original_loader = projection.load_suggestions
        changed = False

        def load_then_change(*args, **kwargs):
            nonlocal changed
            result = original_loader(*args, **kwargs)
            if not changed:
                path.write_bytes(path.read_bytes() + b' ')
                changed = True
            return result

        with mock.patch.object(projection, 'load_suggestions', side_effect=load_then_change):
            with self.assertRaisesRegex(ValueError, '^projection_input_changed$'):
                projection.project(self.db, [self.inputs], self.out)
        self.assertFalse(self.out.exists())

    def test_new_input_file_during_validation_prevents_publication(self):
        self._write('suggestion-001.json',
                    [self._row('d2', 'p2', 'Bob answered clearly')])
        original_loader = projection.load_suggestions
        added = False

        def load_then_add(*args, **kwargs):
            nonlocal added
            result = original_loader(*args, **kwargs)
            if not added:
                (self.inputs / 'suggestion-002.json').write_bytes(
                    (self.inputs / 'suggestion-001.json').read_bytes())
                added = True
            return result

        with mock.patch.object(projection, 'load_suggestions', side_effect=load_then_add):
            with self.assertRaisesRegex(ValueError, '^projection_input_changed$'):
                projection.project(self.db, [self.inputs], self.out)
        self.assertFalse(self.out.exists())

    def test_output_overlap_and_existing_target_are_rejected(self):
        with self.assertRaisesRegex(ValueError, '^projection_output_input_overlap$'):
            projection.project(self.db, [self.inputs], self.inputs / 'nested')
        self.out.mkdir()
        with self.assertRaisesRegex(ValueError, '^projection_output_exists$'):
            projection.project(self.db, [self.inputs], self.out)

    def test_repository_output_is_rejected_before_creation(self):
        target = Path(projection.__file__).resolve().parents[1] / 'synthetic-projection-test'
        with self.assertRaisesRegex(ValueError, '^projection_output_scope_rejected$'):
            projection.project(self.db, [self.inputs], target)
        self.assertFalse(target.exists())

    def test_source_directory_sibling_output_is_rejected(self):
        target = self.source_dir / 'new-projection'
        with self.assertRaisesRegex(ValueError, '^projection_output_scope_rejected$'):
            projection.project(self.db, [self.inputs], target)
        self.assertFalse(target.exists())

    def test_source_sidecar_is_rejected(self):
        self._write('suggestion-001.json', [self._row('d2', 'p2', 'Bob answered clearly')])
        sidecar = Path(str(self.db) + '-wal')
        sidecar.write_bytes(b'synthetic')
        self.addCleanup(lambda: sidecar.unlink(missing_ok=True))
        with self.assertRaisesRegex(ValueError, '^projection_source_sidecar_present$'):
            projection.project(self.db, [self.inputs], self.out)

    def test_linked_output_ancestor_is_rejected_when_supported(self):
        self._write('suggestion-001.json', [self._row('d2', 'p2', 'Bob answered clearly')])
        link = self.root / 'linked-parent'
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('directory symlink creation unavailable')
        with self.assertRaisesRegex(ValueError, '^projection_link_path_rejected$'):
            projection.project(self.db, [self.inputs], link / 'output')

    def test_cli_failure_does_not_echo_private_sentinel_or_paths(self):
        sentinel = 'PRIVATE_SENTINEL_MUST_NOT_LEAK'
        bad = self._row('d2', 'p2', 'Bob answered clearly')
        bad['unexpected'] = sentinel
        self._write('suggestion-001.json', [bad])
        proc = subprocess.run([
            sys.executable, '-B', '-m', 'ltr.attribution.projection',
            '--db', str(self.db), '--input', str(self.inputs), '--out', str(self.out),
            '--quarantine-non-unique-evidence',
        ], cwd=Path(__file__).resolve().parents[3] / 'scripts', capture_output=True, text=True,
           env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn(sentinel, proc.stdout + proc.stderr)
        self.assertNotIn(str(self.root), proc.stdout + proc.stderr)
        self.assertIn('attribution projection refused:', proc.stderr)
        self.assertFalse(self.out.exists())


if __name__ == '__main__':
    unittest.main()
