"""Source-free contracts for derived worker-record snapshots."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ltr.library import run_snapshot as snapshot

SOURCE = 'a' * 64


def worker(**values):
    return {'worker_id': 'synthetic-worker-1', 'kind': 'identity', 'terminal': False,
            'stop_reason': 'running', 'planned_rows': 10, 'valid_rows': 3, 'failed_rows': 1,
            'outcome_unknown_rows': 2, 'unattempted_rows': 4, 'attempted_calls': 2,
            'cache_read_input_tokens': 123, **values}


def document(workers=None):
    return {'schema': snapshot.SCHEMA, 'source_sha256': SOURCE, 'scope_sha256': 'b' * 64,
            'generated_at': '2026-01-02T03:04:05Z', 'workers': [worker()] if workers is None else workers}


class RunSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'snapshot.json'

    def write(self, payload=None):
        self.path.write_text(json.dumps(document() if payload is None else payload), encoding='utf-8')

    def refuse(self, payload, code='run_snapshot_invalid'):
        self.write(payload)
        before = self.path.read_bytes()
        with self.assertRaises(snapshot.RunSnapshotError) as caught:
            snapshot.read_run_snapshot(self.path, SOURCE)
        self.assertEqual(str(caught.exception), code)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.path.read_bytes(), before)

    def test_projection_binds_source_scope_and_exact_snapshot_without_mutation(self):
        data = document([worker(), worker(worker_id='second', kind='alignment', terminal=True,
                                          stop_reason='request_rejected')])
        self.write(data); raw = self.path.read_bytes()
        result = snapshot.read_run_snapshot(self.path, SOURCE)
        self.assertEqual(result, {'available': True, **data, 'snapshot_sha256': hashlib.sha256(raw).hexdigest()})
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertNotIn('completed_rows', result)
        self.assertNotIn('active', result)
        self.assertNotIn('published', result)

    def test_unconfigured_empty_and_unavailable_are_distinct(self):
        self.assertEqual(snapshot.read_run_snapshot(None, SOURCE), {'available': False})
        with self.assertRaisesRegex(snapshot.RunSnapshotError, '^run_snapshot_unavailable$'):
            snapshot.read_run_snapshot(self.path, SOURCE)
        self.write(document([]))
        self.assertEqual(snapshot.read_run_snapshot(self.path, SOURCE)['workers'], [])

    def test_duplicate_json_keys_and_extra_fields_refused_without_content_leak(self):
        self.write()
        raw = self.path.read_text().replace('"planned_rows": 10', '"planned_rows": 10, "planned_rows": 10')
        self.path.write_text(raw)
        with self.assertRaisesRegex(snapshot.RunSnapshotError, '^run_snapshot_invalid$'):
            snapshot.read_run_snapshot(self.path, SOURCE)
        for field in ['path', 'session_id', 'model', 'history', 'body', 'account']:
            for location in ['root', 'worker']:
                data = document()
                (data if location == 'root' else data['workers'][0])[field] = 'PRIVATE_SYNTHETIC_BODY_OR_PATH'
                self.refuse(data)

    def test_source_scope_schema_time_and_expected_hash_guards(self):
        for key, values in {'schema': ['unknown'], 'source_sha256': [True, 'a'*63, 'A'*64],
                            'scope_sha256': [None, '../synthetic', 'x'*64],
                            'generated_at': [True, 'yesterday', '2026-01-02T03:04:05',
                                             '2026-01-02T03:04:05+01:00', '2026-02-30T03:04:05Z']}.items():
            for value in values:
                data = document(); data[key] = value
                self.refuse(data)
        data = document(); data['source_sha256'] = 'c'*64
        self.refuse(data, 'run_snapshot_source_mismatch')
        for value in [True, None, 'a'*63, 'bad']:
            with self.assertRaisesRegex(snapshot.RunSnapshotError, '^run_snapshot_expected_source_invalid$'):
                snapshot.read_run_snapshot(self.root/'missing', value)

    def test_worker_ids_kinds_stop_reasons_and_terminal_contract(self):
        for key, values in {'worker_id': ['', '../escape', '名字', 'a'*65, 'bad\nname', True],
                            'kind': ['unknown', None], 'stop_reason': ['HTTP secret', None, 'finished'],
                            'terminal': [0, 1, None, 'false']}.items():
            for value in values:
                self.refuse(document([worker(**{key: value})]))
        self.refuse(document([worker(), worker()]))
        self.refuse(document([worker(terminal=True)]))
        self.refuse(document([worker(stop_reason='request_rejected')]))
        for reason in snapshot.STOP_REASONS - {'running'}:
            self.write(document([worker(terminal=True, stop_reason=reason)]))
            self.assertTrue(snapshot.read_run_snapshot(self.path, SOURCE)['workers'][0]['terminal'])

    def test_exact_bool_excluding_bounded_counts_and_conservation(self):
        for key in snapshot.COUNTS:
            for value in [True, -1, 1.0, '1', None, 10**13]:
                self.refuse(document([worker(**{key: value})]))
        self.refuse(document([worker(valid_rows=4)]))
        self.write(document([worker(planned_rows=0, valid_rows=0, failed_rows=0,
                                   outcome_unknown_rows=0, unattempted_rows=0, attempted_calls=0)]))
        self.assertEqual(snapshot.read_run_snapshot(self.path, SOURCE)['workers'][0]['planned_rows'], 0)

    def test_size_worker_limit_and_unsafe_file_shape(self):
        self.refuse(document([worker(worker_id=str(i)) for i in range(snapshot.MAX_WORKERS+1)]))
        self.write()
        with patch.object(snapshot, 'MAX_BYTES', self.path.stat().st_size-1):
            with self.assertRaisesRegex(snapshot.RunSnapshotError, '^run_snapshot_limits_exceeded$'):
                snapshot.read_run_snapshot(self.path, SOURCE)
        with self.assertRaisesRegex(snapshot.RunSnapshotError, '^run_snapshot_path_unsafe$'):
            snapshot.read_run_snapshot(self.root, SOURCE)

    def test_file_and_ancestor_symlinks_rejected(self):
        self.write()
        link = self.root/'alias.json'; ancestor = self.root/'alias-dir'
        try:
            link.symlink_to(self.path)
            ancestor.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation unavailable')
        for path in [link, ancestor/'snapshot.json']:
            with self.assertRaisesRegex(snapshot.RunSnapshotError, '^run_snapshot_path_unsafe$'):
                snapshot.read_run_snapshot(path, SOURCE)

    def test_windows_reparse_attribute_refused(self):
        self.write(); original = Path.lstat
        def reparse(path):
            info = original(path)
            if path == self.path:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with patch.object(Path, 'lstat', reparse):
            with self.assertRaisesRegex(snapshot.RunSnapshotError, '^run_snapshot_path_unsafe$'):
                snapshot.read_run_snapshot(self.path, SOURCE)

    def test_replacement_during_validation_rejected(self):
        self.write(); validate = snapshot._validate
        def replace(data, source):
            result = validate(data, source)
            newer = document(); newer['generated_at'] = '2026-01-02T03:04:06Z'
            self.write(newer)
            return result
        with patch.object(snapshot, '_validate', side_effect=replace):
            with self.assertRaisesRegex(snapshot.RunSnapshotError, '^run_snapshot_changed$'):
                snapshot.read_run_snapshot(self.path, SOURCE)

    def test_change_during_open_and_invalid_unicode_have_static_errors(self):
        self.write(); fstat = snapshot.os.fstat
        def changed(fd):
            info = fstat(fd)
            return SimpleNamespace(**{key: getattr(info, key) for key in
                ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_ctime_ns')}, st_mtime_ns=info.st_mtime_ns+1)
        with patch.object(snapshot.os, 'fstat', side_effect=changed):
            with self.assertRaisesRegex(snapshot.RunSnapshotError, '^run_snapshot_changed$'):
                snapshot.read_run_snapshot(self.path, SOURCE)
        for raw in [b'\xffPRIVATE_SYNTHETIC', b'[]', b'{broken PRIVATE_SYNTHETIC']:
            self.path.write_bytes(raw)
            with self.assertRaisesRegex(snapshot.RunSnapshotError, '^run_snapshot_invalid$'):
                snapshot.read_run_snapshot(self.path, SOURCE)


if __name__ == '__main__':
    unittest.main()
