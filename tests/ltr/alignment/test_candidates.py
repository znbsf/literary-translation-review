"""Source-free zh->ja alignment candidate runtime, synthetic databases only."""
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import urllib.error

from ltr.alignment.candidates import validate_alignment, run

ZH_TEXT = "PRIVATE_ZH_ONE"
JA_TEXT = "PRIVATE_JA_ONE"


def make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE editions(id TEXT,title TEXT,language TEXT,book_key TEXT);
        CREATE TABLE dialogues(id TEXT,edition_id TEXT,ordinal INTEGER,text TEXT);
        INSERT INTO editions VALUES
            ('zh1','中','zh','bk'),('ja1','日','ja','bk'),
            ('ja_other','他','ja','other'),('ja_zh_lang','伪','zh','bk'),
            ('dup','x','ja','bk'),('dup','x','ja','bk');
        INSERT INTO dialogues VALUES
            ('z1','zh1',0,'PRIVATE_ZH_ONE'),
            ('z2','zh1',1,'PRIVATE_ZH_TWO'),
            ('z3','zh1',2,'PRIVATE_ZH_THREE'),
            ('j1','ja1',0,'PRIVATE_JA_ONE'),
            ('j2','ja1',1,'PRIVATE_JA_TWO'),
            ('jx','ja_other',0,'PRIVATE_JA_OTHER');
    """)
    con.commit()
    con.close()


class AlignmentContractTests(unittest.TestCase):
    def test_exact_coverage_and_never_verified(self):
        payload = {"alignments": [
            {"chinese_id": "z1", "japanese_ids": ["j1", "j2"], "status": "candidate", "confidence": "high"},
            {"chinese_id": "z2", "japanese_ids": [], "status": "unmatched", "confidence": "low"}]}
        rows = validate_alignment(payload, ["z1", "z2"], ["j1", "j2"])
        self.assertEqual([r["chinese_id"] for r in rows], ["z1", "z2"])
        self.assertEqual(rows[0]["japanese_ids"], ["j1", "j2"])
        self.assertEqual(rows[1]["status"], "unmatched")
        for flag in ("requires_review",):
            self.assertEqual(rows[0][flag], 1)
        for flag in ("accepted", "human_confirmed", "semantic_verified",
                     "reference_eligible", "ocr_verified"):
            self.assertEqual(rows[0][flag], 0)

    def test_missing_chinese_coverage_rejected(self):
        payload = {"alignments": [
            {"chinese_id": "z1", "japanese_ids": ["j1"], "status": "candidate", "confidence": "low"}]}
        with self.assertRaises(ValueError):
            validate_alignment(payload, ["z1", "z2"], ["j1"])

    def test_duplicate_chinese_coverage_rejected(self):
        row = {"chinese_id": "z1", "japanese_ids": [], "status": "unmatched", "confidence": "low"}
        with self.assertRaises(ValueError):
            validate_alignment({"alignments": [row, dict(row)]}, ["z1"], ["j1"])

    def test_out_of_edition_and_too_many_japanese_ids_rejected(self):
        for jids in (["jx"], ["j1", "j2", "j9", "j8", "j7", "j6"], ["j1", "j1"]):
            payload = {"alignments": [
                {"chinese_id": "z1", "japanese_ids": jids, "status": "candidate", "confidence": "low"}]}
            with self.assertRaises(ValueError):
                validate_alignment(payload, ["z1"], ["j1", "j2"])

    def test_extra_field_enum_and_status_mismatch_rejected(self):
        base = {"chinese_id": "z1", "japanese_ids": ["j1"], "status": "candidate", "confidence": "low"}
        for mutate in ({"text": "x"}, {"confidence": "certain"}, {"status": "verified"}):
            payload = {"alignments": [dict(base, **mutate)]}
            with self.assertRaises(ValueError):
                validate_alignment(payload, ["z1"], ["j1"])
        empty_candidate = {"chinese_id": "z1", "japanese_ids": [], "status": "candidate", "confidence": "low"}
        with self.assertRaises(ValueError):
            validate_alignment({"alignments": [empty_candidate]}, ["z1"], ["j1"])
        nonempty_unmatched = {"chinese_id": "z1", "japanese_ids": ["j1"], "status": "unmatched", "confidence": "low"}
        with self.assertRaises(ValueError):
            validate_alignment({"alignments": [nonempty_unmatched]}, ["z1"], ["j1"])


class AlignmentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.db = self.source / "dialogue.sqlite"
        make_db(self.db)
        self.out = self.root / "out"

    def args(self, **over):
        values = dict(db=self.db, zh_edition="zh1", ja_edition="ja1", out=self.out,
                      model="synthetic", max_calls=5, batch_size=6, start=0, execute=True)
        values.update(over)
        return SimpleNamespace(**values)

    def fake(self, calls, unmatched=False):
        def request(endpoint, token, model, payload, *, system=None, history=None, session_id=None):
            calls.append({"payload": json.loads(json.dumps(payload)),
                          "history": list(history or []), "session_id": session_id,
                          "system": system})
            rows = []
            for item in payload["chinese"]:
                if unmatched:
                    rows.append({"chinese_id": item["id"], "japanese_ids": [],
                                 "status": "unmatched", "confidence": "low"})
                else:
                    rows.append({"chinese_id": item["id"], "japanese_ids": ["j1"],
                                 "status": "candidate", "confidence": "medium"})
            if history is not None:
                history.extend([{"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                                {"role": "assistant", "content": json.dumps({"alignments": rows})}])
            usage = {"cache_read_input_tokens": 25 if len(calls) > 1 else 0}
            return {"payload": {"alignments": rows}, "usage": usage, "served_model": model + "-served"}
        return request

    def env(self):
        return patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "http://x", "ANTHROPIC_AUTH_TOKEN": "t"})

    def out_text(self):
        return "".join(p.read_text(encoding="utf-8") for p in self.out.rglob("*.json"))

    def test_serial_history_session_reference_and_cache(self):
        calls = []
        with self.env(), patch("ltr.alignment.candidates.request_model", side_effect=self.fake(calls)):
            summary = run(self.args(batch_size=2))
        self.assertEqual(len(calls), 2)
        first, second = calls
        self.assertEqual(set(first["payload"]), {"reference", "chinese"})
        self.assertEqual(set(second["payload"]), {"chinese"})
        ref = first["payload"]["reference"][0]
        self.assertEqual(ref["id"], "j1")
        self.assertEqual(ref["text"], JA_TEXT)
        self.assertEqual(ref["ocr_status"], "unverified")
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(first["system"], second["system"])
        self.assertEqual(len(second["history"]), 2)
        self.assertEqual(second["history"][1]["role"], "assistant")
        self.assertEqual(summary["cache_read_tokens"], 25)
        self.assertEqual(summary["matched"], 3)
        self.assertEqual(summary["remaining"], 0)

    def test_source_unchanged_and_no_source_text_exported(self):
        before = self.db.read_bytes()
        calls = []
        with self.env(), patch("ltr.alignment.candidates.request_model", side_effect=self.fake(calls)):
            summary = run(self.args())
        self.assertEqual(before, self.db.read_bytes())
        self.assertTrue(summary["source_unchanged"])
        self.assertFalse(summary["corpus_updated"])
        exported = self.out_text()
        self.assertNotIn("PRIVATE_ZH", exported)
        self.assertNotIn("PRIVATE_JA", exported)
        batch = json.loads(next(self.out.glob("align-*.json")).read_text(encoding="utf-8"))
        self.assertEqual(batch["served_model"], "synthetic-served")
        self.assertEqual(batch["rows"][0]["japanese"][0]["id"], "j1")
        self.assertTrue(batch["rows"][0]["japanese"][0]["text_sha256"])
        self.assertEqual(batch['rows'][0]['chinese_text_sha256'],hashlib.sha256(ZH_TEXT.encode()).hexdigest())
        self.assertEqual(batch['rows'][0]['japanese'][0]['text_sha256'],hashlib.sha256(JA_TEXT.encode()).hexdigest())

    def test_bounded_budget(self):
        calls = []
        with self.env(), patch("ltr.alignment.candidates.request_model", side_effect=self.fake(calls)):
            summary = run(self.args(batch_size=1, max_calls=1))
        self.assertEqual(len(calls), 1)
        self.assertEqual(summary["attempted"], 1)
        self.assertEqual(summary["valid"], 1)
        self.assertEqual(summary["remaining"], 2)
        self.assertEqual(summary['attempted_rows'],1)
        self.assertEqual(summary['unattempted_rows'],2)

    def test_unmatched_preserved(self):
        calls = []
        with self.env(), patch("ltr.alignment.candidates.request_model", side_effect=self.fake(calls, unmatched=True)):
            summary = run(self.args())
        self.assertEqual(summary["unmatched"], 3)
        self.assertEqual(summary["matched"], 0)
        batch = json.loads(next(self.out.glob("align-*.json")).read_text(encoding="utf-8"))
        self.assertTrue(all(r["status"] == "unmatched" for r in batch["rows"]))

    def test_wrong_edition_binding_refused(self):
        for ja in ("ja_other", "ja_zh_lang"):
            with self.assertRaises(ValueError):
                run(self.args(ja_edition=ja))
        with self.assertRaises(ValueError):
            run(self.args(ja_edition="dup"))  # ambiguous id
        with self.assertRaises(ValueError):
            run(self.args(ja_edition="zh1"))  # not distinct
        self.assertFalse(self.out.exists())

    def test_output_and_sidecar_refusals(self):
        self.out.mkdir()
        with self.assertRaises(ValueError):
            run(self.args())
        self.out.rmdir()
        with self.assertRaises(ValueError):
            run(self.args(out=self.source / "inside"))
        Path(str(self.db) + "-wal").write_bytes(b"active")
        with self.assertRaises(ValueError):
            run(self.args())

    def test_stop_on_http_429_no_retry(self):
        calls = []
        def boom(*a, **k):
            calls.append(1)
            raise urllib.error.HTTPError("http://x", 429, "limited", {}, None)
        with self.env(), patch("ltr.alignment.candidates.request_model", side_effect=boom):
            summary = run(self.args(batch_size=1, max_calls=5))
        self.assertEqual(len(calls), 1)
        self.assertEqual(summary["failed"], 1)
        ledger = json.loads((self.out / "progress.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["calls"][0]["status"], "failed")
        self.assertEqual(ledger["calls"][0]["error_class"], "HTTPError")
        self.assertEqual(ledger["calls"][0]["http_status"], 429)

    def test_source_change_refused_no_partial_output(self):
        calls = []
        with self.env(), patch("ltr.alignment.candidates.request_model", side_effect=self.fake(calls)), \
                patch("ltr.alignment.candidates.fingerprint",
                      side_effect=["base", "base", "changed", "changed"]):
            summary = run(self.args())
        self.assertFalse(summary["source_unchanged"])
        self.assertFalse(list(self.out.glob("align-*.json")))
        ledger=json.loads((self.out/'progress.json').read_text())
        self.assertEqual(ledger['calls'][0]['status'],'failed')

    def test_programmatic_invalid_bounds_cannot_dispatch(self):
        for values in ({'batch_size':0},{'max_calls':101},{'start':-1},{'execute':False}):
            with self.assertRaises(ValueError):run(self.args(**values))
        self.assertFalse(self.out.exists())

    def test_dialogue_id_reused_in_other_edition_is_refused(self):
        con=sqlite3.connect(self.db)
        con.execute("INSERT INTO dialogues VALUES ('j1','ja_other',1,'Synthetic other source')")
        con.commit();con.close()
        with self.assertRaises(ValueError):run(self.args())
        self.assertFalse(self.out.exists())

    def test_wal_appearing_during_model_call_refuses_candidates(self):
        request=self.fake([])
        def concurrent_write(*args,**kwargs):
            result=request(*args,**kwargs)
            Path(str(self.db)+'-wal').write_bytes(b'synthetic concurrent WAL')
            return result
        with self.env(),patch('ltr.alignment.candidates.request_model',side_effect=concurrent_write):
            result=run(self.args())
        self.assertFalse(result['source_unchanged'])
        self.assertFalse(list(self.out.glob('align-*.json')))

    def test_persistent_failures_stop_before_dispatching_whole_range(self):
        con=sqlite3.connect(self.db)
        con.executemany('INSERT INTO dialogues VALUES (?,?,?,?)',[(f'z{n}','zh1',n,'Synthetic quote') for n in (4,5)])
        con.commit();con.close()
        with self.env(),patch('ltr.alignment.candidates.request_model',side_effect=ValueError('synthetic invalid response')) as call:
            result=run(self.args(batch_size=1,max_calls=100))
        self.assertEqual(call.call_count,3)
        self.assertEqual(result['stop_reason'],'consecutive_invalid_responses')
        self.assertEqual(result['unattempted_rows'],2)
        self.assertEqual(result['valid_rows'],0)

    def test_context_or_payload_rejection_stops_without_retry(self):
        for status in (400,413):
            error=urllib.error.HTTPError('http://example.invalid',status,'synthetic',{},None)
            with self.env(),patch('ltr.alignment.candidates.request_model',side_effect=error) as call:
                result=run(self.args(out=self.root/f'out-{status}',batch_size=1,max_calls=100))
            self.assertEqual(call.call_count,1)
            self.assertEqual(result['stop_reason'],'request_rejected')

    def test_valid_completed_rows_are_excluded_but_failed_holes_dispatch(self):
        from ltr.library.review_queue import fingerprint
        completed=self.root/'completed';completed.mkdir()
        source=fingerprint(self.db)
        row={'chinese_id':'z1','japanese_ids':['j1'],'status':'candidate','confidence':'low',
             'chinese_text_sha256':hashlib.sha256(ZH_TEXT.encode()).hexdigest(),
             'japanese':[{'id':'j1','text_sha256':hashlib.sha256(JA_TEXT.encode()).hexdigest()}],
             'requires_review':1,'accepted':0,'human_confirmed':0,'semantic_verified':0,'reference_eligible':0,'ocr_verified':0}
        batch={'schema':'haruhi-alignment-candidates-v1','database_sha256':source,'zh_edition_id':'zh1',
               'ja_edition_id':'ja1','book_key':'bk','requested_model':'previous','rows':[row]}
        (completed/'align-00000-00000.json').write_text(json.dumps(batch))
        calls=[]
        with self.env(),patch('ltr.alignment.candidates.request_model',side_effect=self.fake(calls)):
            result=run(self.args(batch_size=1,max_calls=2,exclude_completed=completed))
        self.assertEqual([r['payload']['chinese'][0]['id'] for r in calls],['z2','z3'])
        self.assertEqual(result['skipped_completed_rows'],1)
        self.assertEqual(result['valid_rows'],2)
        self.assertEqual(result['remaining'],0)
        self.assertEqual(result['unattempted_rows'],0)

    def test_completed_rows_with_wrong_source_are_not_trusted(self):
        completed=self.root/'completed';completed.mkdir()
        (completed/'align-00000-00000.json').write_text(json.dumps({'schema':'haruhi-alignment-candidates-v1','database_sha256':'wrong'}))
        with self.env(),self.assertRaises(ValueError):run(self.args(exclude_completed=completed))
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
