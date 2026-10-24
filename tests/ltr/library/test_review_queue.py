"""Source-free queue generation, using temporary synthetic databases only."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ltr.library.review_queue import build, fingerprint


class ReviewQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.corpus = self.root / "source"
        self.corpus.mkdir()
        self.db = self.corpus / "dialogue.sqlite"
        con = sqlite3.connect(self.db)
        con.executescript("""
            CREATE TABLE editions(id TEXT,language TEXT);
            CREATE TABLE paragraphs(edition_id TEXT,text TEXT);
            CREATE TABLE dialogues(id TEXT,edition_id TEXT);
            CREATE TABLE corpus_completion_quote(quote_id TEXT,status TEXT,requires_review INTEGER);
            INSERT INTO editions VALUES ('unquoted','zh'),('quoted','zh');
            INSERT INTO paragraphs VALUES ('unquoted','PRIVATE_SENTINEL_DO_NOT_EXPORT'),('quoted','other');
            INSERT INTO dialogues VALUES ('q1','quoted');
            INSERT INTO corpus_completion_quote VALUES ('q1','pending',1),('q2','candidate',0);
        """)
        con.close()
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"corpora": [{"id": "example", "path": "source"}]}), encoding="utf-8")
        self.out = self.root / "queues"

    def test_source_hash_unchanged_and_no_prose_exported(self):
        before = self.db.read_bytes()
        summary = build(self.config, self.out)
        self.assertEqual(before, self.db.read_bytes())
        rows = [json.loads(s) for s in (self.out / "example.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({r['kind'] for r in rows}, {'quote_pending', 'unquoted_edition_review'})
        self.assertNotIn('PRIVATE_SENTINEL', (self.out / "example.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(summary['corpora'][0]['corpus_sha256'], hashlib.sha256(before).hexdigest())
        self.assertFalse(summary['corpus_updated'])

    def test_missing_database_never_created(self):
        self.db.unlink()
        with self.assertRaises(FileNotFoundError):
            build(self.config, self.out)
        self.assertFalse(self.db.exists())
        self.assertFalse(self.out.exists())

    def test_output_inside_source_refused(self):
        with self.assertRaises(ValueError):
            build(self.config, self.corpus / 'queues')
        self.assertFalse((self.corpus / 'queues').exists())

    def test_existing_output_preserved(self):
        self.out.mkdir()
        keep = self.out / 'keep'
        keep.write_text('original')
        with self.assertRaises(ValueError):
            build(self.config, self.out)
        self.assertEqual(keep.read_text(), 'original')

    def test_active_wal_refused(self):
        Path(str(self.db) + '-wal').write_bytes(b'active')
        with self.assertRaises(ValueError):
            build(self.config, self.out)
        self.assertFalse(self.out.exists())

    def test_changed_source_discards_partial_queue(self):
        with patch('ltr.library.review_queue.fingerprint', side_effect=['before', 'after']):
            with self.assertRaises(ValueError):
                build(self.config, self.out)
        self.assertFalse(self.out.exists())
        self.assertFalse(list(self.root.glob('.review-queue-*')))

    def test_invalid_identifier_refused(self):
        self.config.write_text(json.dumps({'corpora': [{'id': '../escape', 'path': 'source'}]}))
        with self.assertRaises(ValueError):
            build(self.config, self.out)
        self.assertFalse(self.out.exists())

    def test_late_sidecar_discards_queue_even_when_database_hash_matches(self):
        for suffix in ('-wal', '-journal'):
            with self.subTest(suffix=suffix):
                calls = []
                sidecar = Path(str(self.db) + suffix)
                def snapshot(path):
                    calls.append(1)
                    result = fingerprint(path)
                    if len(calls) == 2:
                        sidecar.write_bytes(b'late synthetic sidecar')
                    return result
                try:
                    with patch('ltr.library.review_queue.fingerprint', side_effect=snapshot):
                        with self.assertRaises(ValueError):
                            build(self.config, self.out)
                    self.assertFalse(self.out.exists())
                    self.assertFalse(list(self.root.glob('.review-queue-*')))
                finally:
                    sidecar.unlink(missing_ok=True)

    def test_final_set_guard_rechecks_previously_read_source(self):
        calls = []
        def snapshot(path):
            calls.append(1)
            result = fingerprint(path)
            if len(calls) == 3:
                return 'changed-before-publication'
            return result
        with patch('ltr.library.review_queue.fingerprint', side_effect=snapshot):
            with self.assertRaises(ValueError):
                build(self.config, self.out)
        self.assertEqual(len(calls), 3)
        self.assertFalse(self.out.exists())
        self.assertFalse(list(self.root.glob('.review-queue-*')))


if __name__ == '__main__':
    unittest.main()
