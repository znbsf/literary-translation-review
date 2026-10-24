"""Synthetic integration of published coverage and separate worker snapshots."""
import http.client
import json
import os
import sqlite3
import threading
from contextlib import closing

from tests.ltr.workbench import test_store as base
from tests.ltr.library.test_run_snapshot import document

from ltr import workbench as wb


class RunSnapshotWorkbenchTests(base.WorkbenchTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / 'snapshot.json'
        self.source = self.store.database('c1')[1].source_sha256()
        self.config.corpus('c1').run_snapshot = self.path

    def write(self, **updates):
        payload = document()
        payload['source_sha256'] = self.source
        payload.update(updates)
        self.path.write_text(json.dumps(payload), encoding='utf-8')

    def item(self):
        return self.store.overview()['corpora'][0]

    def test_valid_snapshot_does_not_change_published_coverage(self):
        before = self.item()
        self.write()
        after = self.item()
        snapshot = after['run_snapshot']
        self.assertTrue(snapshot['available'])
        self.assertEqual(snapshot['workers'][0]['valid_rows'], 3)
        self.assertEqual(snapshot['workers'][0]['outcome_unknown_rows'], 2)
        for name in ('counts', 'pending_candidates', 'pending_identities', 'pending_alignments'):
            self.assertEqual(before[name], after[name])

    def test_source_mismatch_degrades_only_snapshot(self):
        self.write(source_sha256='f' * 64)
        item = self.item()
        self.assertTrue(item['available'])
        self.assertEqual(item['run_snapshot'], {'available': False, 'error': 'run_snapshot_source_mismatch'})

    def test_same_size_and_mtime_source_change_cannot_reuse_cached_sha(self):
        self.write()
        path = self.root / 'dialogue.sqlite'
        before = path.stat()
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("UPDATE editions SET title='Title Two' WHERE id='ed1'")
        self.assertEqual(path.stat().st_size, before.st_size)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.item()['run_snapshot'], {'available': False, 'error': 'run_snapshot_source_mismatch'})

    def test_missing_or_unconfigured_snapshot(self):
        self.assertEqual(self.item()['run_snapshot'], {'available': False, 'error': 'run_snapshot_unavailable'})
        self.config.corpus('c1').run_snapshot = None
        self.assertEqual(self.item()['run_snapshot'], {'available': False})

    def test_registered_relative_path_and_all_source_roots(self):
        config_path = self.tmp / 'config.json'
        payload = {'corpora': [{'id': 'c1', 'path': str(self.root), 'run_snapshot': 'snapshot.json'}]}
        config_path.write_text(json.dumps(payload), encoding='utf-8')
        loaded = wb.WorkbenchConfig.load(config_path)
        self.assertEqual(loaded.corpus('c1').run_snapshot, self.path)
        for unsafe in (str(self.root / 'snapshot.json'), True, ''):
            with self.subTest(unsafe=unsafe):
                payload['corpora'][0]['run_snapshot'] = unsafe
                config_path.write_text(json.dumps(payload), encoding='utf-8')
                with self.assertRaises(wb.WorkbenchError):
                    wb.WorkbenchConfig.load(config_path)
        payload['corpora'][0]['run_snapshot'] = str(self.tmp / 'other-corpus' / 'snapshot.json')
        payload['corpora'].append({'id': 'c2', 'path': str(self.tmp / 'other-corpus')})
        config_path.write_text(json.dumps(payload), encoding='utf-8')
        with self.assertRaises(wb.WorkbenchError) as raised:
            wb.WorkbenchConfig.load(config_path)
        self.assertEqual(raised.exception.code, 'run_snapshot_must_be_external')

    def test_real_http_static_errors_never_echo_file_body_or_path(self):
        sentinel = 'PRIVATE_SNAPSHOT_BODY_SENTINEL'
        self.write(private_text=sentinel)
        server = wb.build_server(self.config, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)
        client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        self.addCleanup(client.close)
        client.request('GET', '/api/overview')
        response = client.getresponse()
        body = response.read().decode('utf-8')
        self.assertEqual(response.status, 200)
        self.assertNotIn(sentinel, body)
        self.assertNotIn(str(self.path), body)
        item = json.loads(body)['corpora'][0]
        self.assertTrue(item['available'])
        self.assertEqual(item['run_snapshot'], {'available': False, 'error': 'run_snapshot_invalid'})
