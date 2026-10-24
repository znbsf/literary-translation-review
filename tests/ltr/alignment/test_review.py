"""Source-free second-model zh->ja alignment review runtime, synthetic data only."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import urllib.error

from ltr.alignment.review import validate_review, load_source, run, SYSTEM, _reply_diagnostics
from ltr.quotes.batch import digest, ModelResponseError
from ltr.library.review_queue import fingerprint

SCHEMA = "haruhi-alignment-candidates-v1"
ZH1, ZH2, ZH3 = "PRIVATE_ZH_ONE", "PRIVATE_ZH_TWO", "PRIVATE_ZH_THREE"
JA1, JA2, JAO = "PRIVATE_JA_ONE", "PRIVATE_JA_TWO", "PRIVATE_JA_OTHER"
ZH_MAP = {"z1": ZH1, "z2": ZH2, "z3": ZH3}
JA_MAP = {"j1": JA1, "j2": JA2, "jx": JAO}


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


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


def make_row(cid, jids, status, **over):
    row = {"chinese_id": cid, "japanese_ids": list(jids), "status": status,
           "confidence": "medium", "requires_review": 1, "accepted": 0,
           "human_confirmed": 0, "semantic_verified": 0, "reference_eligible": 0,
           "ocr_verified": 0, "chinese_text_sha256": sha(ZH_MAP[cid]),
           "japanese": [{"id": j, "text_sha256": sha(JA_MAP[j])} for j in jids]}
    row.update(over)
    return row


def write_batch(directory: Path, name, db_sha, rows, **over):
    batch = {"schema": SCHEMA, "database_sha256": db_sha, "zh_edition_id": "zh1",
             "ja_edition_id": "ja1", "book_key": "bk", "requested_model": "producer-model",
             "served_model": "producer-served", "input_sha256": "x", "session_id": "s",
             "rows": rows}
    batch.update(over)
    (directory / name).write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")


class ReviewContractTests(unittest.TestCase):
    def test_exact_coverage_enums_and_extra_field(self):
        row = {"review_id": "r1", "decision": "keep", "basis": "semantic_match"}
        self.assertEqual(validate_review({"reviews": [row]}, ["r1"]), [row])
        with self.assertRaises(ValueError):
            validate_review({"reviews": [row]}, ["r1", "r2"])
        with self.assertRaises(ValueError):
            validate_review({"reviews": [row, dict(row)]}, ["r1"])
        for mutate in ({"decision": "accept"}, {"basis": "looks_ok"}, {"note": "x"}):
            with self.assertRaises(ValueError):
                validate_review({"reviews": [dict(row, **mutate)]}, ["r1"])
        with self.assertRaises(ValueError):
            validate_review({"reviews": [], "extra": 1}, [])

    def test_duplicate_proposal_id_rejected(self):
        dup = {"review_id": "r1", "decision": "keep", "basis": "semantic_match"}
        with self.assertRaises(ValueError):
            validate_review({"reviews": [dup, dup]}, ["r1", "r2"])


class ReplyDiagnosticTests(unittest.TestCase):
    def check_safe(self, shape):
        allowed_types = {"null", "object", "array", "string", "boolean", "integer",
                         "number", "other", "missing"}
        self.assertIs(type(shape), dict)
        self.assertEqual(set(shape), {"root_type", "has_reviews", "root_key_count", "reviews_type",
            "returned_rows", "inspected_rows", "rows_truncated", "expected_rows", "string_id_rows",
            "invalid_id_rows", "unique_ids", "duplicate_id_rows", "matched_ids", "unexpected_ids", "missing_ids"})
        for key, value in shape.items():
            if key in {"root_type", "reviews_type"}:
                self.assertIn(value, allowed_types)
            elif key in {"has_reviews", "rows_truncated"}:
                self.assertIs(type(value), bool)
            else:
                self.assertIs(type(value), int)
                self.assertGreaterEqual(value, 0)

    def test_exact_missing_extra_duplicate_and_unexpected_id_counts(self):
        for ids, expected_counts in [(["r1", "r2"], (2, 0, 2, 0, 0)),
                                     (["r1"], (1, 0, 1, 0, 1)),
                                     (["r1", "r2", "extra"], (3, 0, 2, 1, 0)),
                                     (["r1", "r1"], (1, 1, 1, 0, 1)),
                                     (["wrong1", "wrong2"], (2, 0, 0, 2, 2))]:
            with self.subTest(count=len(ids), expected_counts=expected_counts):
                shape = _reply_diagnostics({"reviews": [{"review_id": value} for value in ids]}, ["r1", "r2"])
                self.check_safe(shape)
                self.assertEqual((shape["unique_ids"], shape["duplicate_id_rows"], shape["matched_ids"],
                                  shape["unexpected_ids"], shape["missing_ids"]), expected_counts)
                self.assertEqual(shape["string_id_rows"], len(ids))
                self.assertEqual(shape["invalid_id_rows"], 0)
                self.assertEqual(shape["returned_rows"], len(ids))
                self.assertEqual(shape["inspected_rows"], len(ids))
                self.assertFalse(shape["rows_truncated"])

    def test_bad_roots_and_nested_shapes_emit_only_static_types_and_counts(self):
        marker = 'SYNTHETIC_PRIVATE_REPLY C:\\private\\source.txt'
        for payload, root_type, has_reviews, reviews_type, row_count, invalid in [
                (None, "null", False, "missing", 0, 0),
                ([], "array", False, "missing", 0, 0),
                (marker, "string", False, "missing", 0, 0),
                (True, "boolean", False, "missing", 0, 0),
                (7, "integer", False, "missing", 0, 0),
                (1.5, "number", False, "missing", 0, 0),
                ({marker: marker}, "object", False, "missing", 0, 0),
                ({"reviews": None}, "object", True, "null", 0, 0),
                ({"reviews": {marker: marker}}, "object", True, "object", 0, 0),
                ({"reviews": [None, [], {}, {"review_id": None}, {"review_id": [marker]},
                              {"review_id": {marker: marker}}, {"review_id": 7}]},
                 "object", True, "array", 7, 7)]:
            with self.subTest(root_type=root_type, reviews_type=reviews_type, row_count=row_count):
                shape = _reply_diagnostics(payload, ["r1", "r2"])
                self.check_safe(shape)
                self.assertEqual(shape["root_type"], root_type)
                self.assertEqual(shape["has_reviews"], has_reviews)
                self.assertEqual(shape["reviews_type"], reviews_type)
                self.assertEqual(shape["returned_rows"], row_count)
                self.assertEqual(shape["invalid_id_rows"], invalid)
                self.assertEqual(shape["missing_ids"], 2)
                self.assertEqual(shape["root_key_count"], len(payload) if type(payload) is dict else 0)
                self.assertNotIn(marker, json.dumps(shape))

    def test_malicious_strings_in_keys_ids_and_enums_are_never_returned(self):
        marker = "SYNTHETIC_SECRET_ID_AND_ENUM"
        payload = {"reviews": [{"review_id": marker, "decision": {marker: [marker]},
                                "basis": marker, marker: marker}], marker: marker}
        shape = _reply_diagnostics(payload, [marker, "missing"])
        self.check_safe(shape)
        self.assertEqual(shape["root_key_count"], 2)
        self.assertEqual(shape["matched_ids"], 1)
        self.assertEqual(shape["missing_ids"], 1)
        self.assertNotIn(marker, json.dumps(shape))

    def test_row_inspection_is_capped_at_1000_and_reports_prefix_coverage(self):
        class PoisonKey:
            def __hash__(self):
                return hash("review_id")

            def __eq__(self, other):
                raise ValueError("SYNTHETIC_SECRET_EXCEPTION")

        rows = [{"review_id": f"r{i}"} for i in range(1000)] + [{PoisonKey(): None}]
        shape = _reply_diagnostics({"reviews": rows}, [f"r{i}" for i in range(1001)])
        self.check_safe(shape)
        self.assertEqual(shape["returned_rows"], 1001)
        self.assertEqual(shape["inspected_rows"], 1000)
        self.assertTrue(shape["rows_truncated"])
        self.assertEqual(shape["expected_rows"], 1001)
        self.assertEqual(shape["matched_ids"], 1000)
        self.assertEqual(shape["missing_ids"], 1)
        self.assertEqual(shape["invalid_id_rows"], 0)
        # If diagnostic inspection itself encounters an unknown failure, omit it.
        self.assertIsNone(_reply_diagnostics({"reviews": [rows[-1]]}, ["r1"]))
        self.assertIsNone(_reply_diagnostics({}, [None]))


class AlignmentReviewRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.db = self.source / "dialogue.sqlite"
        make_db(self.db)
        self.baseline = fingerprint(self.db)
        self.inp = self.root / "input"
        self.inp.mkdir()
        self.out = self.root / "out"

    def args(self, **over):
        values = dict(db=self.db, input=self.inp, out=self.out, model="reviewer-model",
                      max_calls=5, batch_size=6, start=0, execute=True)
        values.update(over)
        return SimpleNamespace(**values)

    def env(self):
        return patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "http://x",
                                       "ANTHROPIC_AUTH_TOKEN": "t"})

    def fake(self, calls, decision="keep", basis="semantic_match"):
        def request(endpoint, token, model, payload, *, system=None, history=None, session_id=None):
            calls.append({"payload": json.loads(json.dumps(payload)),
                          "history": list(history or []), "session_id": session_id,
                          "system": system})
            reviews = [{"review_id": it["review_id"], "decision": decision, "basis": basis}
                       for it in payload["items"]]
            if history is not None:
                history.extend([{"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                                {"role": "assistant", "content": json.dumps({"reviews": reviews})}])
            usage = {"cache_read_input_tokens": 30 if len(calls) > 1 else 0}
            return {"payload": {"reviews": reviews}, "usage": usage, "served_model": model + "-served"}
        return request

    def out_text(self):
        return "".join(p.read_text(encoding="utf-8") for p in self.out.rglob("*.json"))

    def test_exact_source_bound_results_and_zero_flags(self):
        write_batch(self.inp, "align-00000-00001.json", self.baseline,
                    [make_row("z1", ["j1", "j2"], "candidate"),
                     make_row("z2", [], "unmatched")])
        calls = []
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=self.fake(calls)):
            summary = run(self.args())
        self.assertEqual(summary["candidate_rows_total"], 1)
        self.assertEqual(summary["skipped_unmatched_rows"], 1)
        self.assertEqual(summary["keep"], 1)
        self.assertEqual(summary["valid_calls"], 1)
        self.assertEqual(summary["unattempted_rows"], 0)
        review = json.loads((self.out / "review-000.json").read_text(encoding="utf-8"))
        self.assertEqual(review["schema"], "haruhi-alignment-model-review-v1")
        self.assertEqual(review["database_sha256"], self.baseline)
        self.assertEqual(review["producer_requested_model"], "producer-model")
        self.assertEqual(review["reviewer_served_model"], "reviewer-model-served")
        row = review["rows"][0]
        expected = digest({"source_sha256": self.baseline, "zh_edition_id": "zh1",
                           "ja_edition_id": "ja1", "chinese_id": "z1",
                           "japanese_ids": ["j1", "j2"], "chinese_text_sha256": sha(ZH1),
                           "japanese_hashes": [sha(JA1), sha(JA2)]})[:16]
        self.assertEqual(row["review_id"], expected)
        self.assertEqual(len(row["review_id"]), 16)
        self.assertEqual(row["chinese_id"], "z1")
        self.assertEqual(row["japanese_ids"], ["j1", "j2"])
        self.assertEqual(row["chinese_text_sha256"], sha(ZH1))
        self.assertEqual(row["japanese_hashes"], [sha(JA1), sha(JA2)])
        self.assertEqual(row["requires_review"], 1)
        for flag in ("accepted", "human_confirmed", "semantic_verified",
                     "reference_eligible", "ocr_verified"):
            self.assertEqual(row[flag], 0)
        self.assertEqual(review["session_id"], summary["session_id"])

    def test_payload_exact_text_and_bounded_context(self):
        write_batch(self.inp, "align-00000-00000.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate")])
        calls = []
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=self.fake(calls)):
            run(self.args())
        item = calls[0]["payload"]["items"][0]
        self.assertEqual(item["chinese"]["text"], ZH1)
        self.assertEqual([c["text"] for c in item["chinese"]["context"]], [ZH2])
        self.assertEqual(item['japanese_ids'],['j1'])
        reference=calls[0]['payload']['japanese_reference'][0]
        self.assertEqual(reference['text'],JA1)
        self.assertEqual(reference['ocr_status'],'unverified')
        self.assertEqual([c['text'] for c in reference['context']],[JA2])

    def test_stale_hash_wrong_edition_and_book_refused(self):
        write_batch(self.inp, "align-00000-00000.json", "deadbeef",
                    [make_row("z1", ["j1"], "candidate")])
        with self.assertRaises(ValueError):
            run(self.args())
        self.assertFalse(self.out.exists())
        for over in ({"ja_edition_id": "ja_other"}, {"ja_edition_id": "ja_zh_lang"},
                     {"ja_edition_id": "dup"}, {"ja_edition_id": "zh1"},
                     {"book_key": "other"}):
            write_batch(self.inp, "align-00000-00000.json", self.baseline,
                        [make_row("z1", ["j1"], "candidate")], **over)
            with self.assertRaises(ValueError):
                run(self.args())
        self.assertFalse(self.out.exists())

    def test_duplicate_global_id_and_changed_text_refused(self):
        write_batch(self.inp, "align-00000-00000.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate")])
        write_batch(self.inp, "align-00001-00001.json", self.baseline,
                    [make_row("z1", ["j2"], "candidate")])
        with self.assertRaises(ValueError):
            run(self.args())
        write_batch(self.inp, "align-00001-00001.json", self.baseline,
                    [make_row("z2", ["j1"], "candidate", chinese_text_sha256=sha("tampered"))])
        with self.assertRaises(ValueError):
            run(self.args())
        self.assertFalse(self.out.exists())

    def test_promotion_flags_and_extra_field_refused(self):
        for over in ({"accepted": 1}, {"requires_review": 0}, {"ocr_verified": 1},
                     {"semantic_verified": True}):
            write_batch(self.inp, "align-00000-00000.json", self.baseline,
                        [make_row("z1", ["j1"], "candidate", **over)])
            with self.assertRaises(ValueError):
                run(self.args())
        write_batch(self.inp, "align-00000-00000.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate", promoted=True)])
        with self.assertRaises(ValueError):
            run(self.args())
        write_batch(self.inp, "align-00000-00000.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate")], unexpected="x")
        with self.assertRaises(ValueError):
            run(self.args())
        self.assertFalse(self.out.exists())

    def test_same_producer_model_refused_before_output(self):
        write_batch(self.inp, "align-00000-00000.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate")])
        with self.assertRaises(ValueError):
            run(self.args(model="producer-model"))
        self.assertFalse(self.out.exists())

    def test_serial_history_session_and_cache(self):
        write_batch(self.inp, "align-00000-00002.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate"), make_row("z2", ["j1"], "candidate"),
                     make_row("z3", ["j1"], "candidate")])
        calls = []
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=self.fake(calls)):
            summary = run(self.args(batch_size=1))
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["session_id"], calls[2]["session_id"])
        self.assertEqual(len(calls[0]["history"]), 0)
        self.assertEqual(len(calls[1]["history"]), 2)
        self.assertEqual(calls[1]["history"][1]["role"], "assistant")
        self.assertEqual(calls[0]["system"], calls[2]["system"])
        self.assertEqual(summary["cache_read_input_tokens"], 60)
        self.assertEqual(summary["keep"], 3)
        self.assertEqual(len(calls[0]['payload']['japanese_reference']),1)
        self.assertNotIn('japanese_reference',calls[1]['payload'])
        self.assertNotIn('japanese_reference',calls[2]['payload'])

    def test_bounded_calls_and_unmatched_skip(self):
        write_batch(self.inp, "align-00000-00002.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate"), make_row("z2", [], "unmatched"),
                     make_row("z3", ["j1"], "candidate")])
        calls = []
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=self.fake(calls)):
            summary = run(self.args(batch_size=1, max_calls=1))
        self.assertEqual(len(calls), 1)
        self.assertEqual(summary["candidate_rows_total"], 2)
        self.assertEqual(summary["skipped_unmatched_rows"], 1)
        self.assertEqual(summary["attempted_calls"], 1)
        self.assertEqual(summary["valid_calls"], 1)
        self.assertEqual(summary["unattempted_rows"], 1)
        self.assertEqual(summary["remaining"], 1)

    def test_start_prefix_recorded(self):
        write_batch(self.inp, "align-00000-00002.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate"), make_row("z2", ["j1"], "candidate"),
                     make_row("z3", ["j1"], "candidate")])
        calls = []
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=self.fake(calls)):
            summary = run(self.args(start=1, batch_size=6))
        self.assertEqual(summary["skipped_prefix_rows"], 1)
        self.assertEqual(summary["attempted_rows"], 2)
        self.assertEqual(summary["remaining"], 1)
        self.assertEqual(len(calls[0]["payload"]["items"]), 2)

    def test_malformed_model_output_not_counted_valid(self):
        write_batch(self.inp, "align-00000-00000.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate")])
        calls = []
        def bad(*a, **k):
            calls.append(1)
            return {"payload": {"reviews": []}, "usage": {}, "served_model": "m"}
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=bad):
            summary = run(self.args())
        self.assertEqual(summary["valid_calls"], 0)
        self.assertEqual(summary["failed_calls"], 1)
        self.assertFalse(list(self.out.glob("review-*.json")))
        ledger = json.loads((self.out / "progress.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["calls"][0]["status"], "failed")
        self.assertEqual(ledger["calls"][0]["error_class"], "ValueError")

    def test_wal_during_model_call_refuses_result(self):
        write_batch(self.inp, "align-00000-00000.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate")])
        request = self.fake([])
        def concurrent(*a, **k):
            result = request(*a, **k)
            Path(str(self.db) + "-wal").write_bytes(b"synthetic concurrent WAL")
            return result
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=concurrent):
            summary = run(self.args())
        self.assertFalse(summary["source_unchanged"])
        self.assertFalse(list(self.out.glob("review-*.json")))

    def test_http_429_stops_without_retry(self):
        write_batch(self.inp, "align-00000-00002.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate"), make_row("z2", ["j1"], "candidate"),
                     make_row("z3", ["j1"], "candidate")])
        calls = []
        def boom(*a, **k):
            calls.append(1)
            raise urllib.error.HTTPError("http://x", 429, "limited", {}, None)
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=boom):
            summary = run(self.args(batch_size=1, max_calls=5))
        self.assertEqual(len(calls), 1)
        self.assertEqual(summary["failed_calls"], 1)
        self.assertEqual(summary["valid_calls"], 0)
        ledger = json.loads((self.out / "progress.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["calls"][0]["status"], "failed")
        self.assertEqual(ledger["calls"][0]["http_status"], 429)

    def test_no_source_prose_in_any_output(self):
        before = self.db.read_bytes()
        write_batch(self.inp, "align-00000-00001.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate"), make_row("z2", [], "unmatched")])
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=self.fake([])):
            run(self.args())
        exported = self.out_text()
        self.assertNotIn("PRIVATE_ZH", exported)
        self.assertNotIn("PRIVATE_JA", exported)
        self.assertEqual(before, self.db.read_bytes())

    def test_invalid_input_refused_before_output(self):
        write_batch(self.inp, "align-00000-00000.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate")], schema="wrong-schema")
        with self.assertRaises(ValueError):
            run(self.args())
        self.assertFalse(self.out.exists())
        (self.inp / "align-00000-00000.json").unlink()
        with self.assertRaises(ValueError):
            run(self.args())
        self.assertFalse(self.out.exists())

    def test_output_preexisting_inside_source_and_bounds_refused(self):
        self.out.mkdir()
        with self.assertRaises(ValueError):
            run(self.args())
        self.out.rmdir()
        with self.assertRaises(ValueError):
            run(self.args(out=self.source / "inside"))
        for values in ({"batch_size": 0}, {"batch_size": 13}, {"max_calls": 101},
                       {"max_calls": 0}, {"start": -1}, {"execute": False}):
            with self.assertRaises(ValueError):
                run(self.args(**values))
        self.assertFalse(self.out.exists())

    def test_load_source_helper_returns_bindings(self):
        write_batch(self.inp, "align-00000-00001.json", self.baseline,
                    [make_row("z1", ["j1", "j2"], "candidate"), make_row("z2", [], "unmatched")])
        con = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)
        try:
            con.execute("PRAGMA query_only=ON")
            loaded = load_source(self.inp, con, self.baseline)
        finally:
            con.close()
        self.assertEqual(loaded["pair"], ("zh1", "ja1"))
        self.assertEqual(loaded["skipped"], 1)
        self.assertEqual(len(loaded["candidates"]), 1)
        self.assertEqual(loaded["producer_requested"], "producer-model")

    def test_persistent_review_failure_stops_with_unattempted_rows(self):
        con=sqlite3.connect(self.db)
        con.executemany('INSERT INTO dialogues VALUES (?,?,?,?)',[(f'z{n}','zh1',n,'Synthetic extra') for n in (4,5)])
        con.commit();con.close();baseline=fingerprint(self.db)
        rows=[make_row(f'z{n}',['j1'],'candidate') for n in (1,2,3)]
        rows.extend(dict(rows[0],chinese_id=f'z{n}',chinese_text_sha256=sha('Synthetic extra')) for n in (4,5))
        write_batch(self.inp,'align-00000-00004.json',baseline,rows)
        with self.env(),patch('ltr.alignment.review.request_model',side_effect=ValueError('synthetic invalid response')) as call:
            result=run(self.args(batch_size=1,max_calls=100))
        self.assertEqual(call.call_count,3)
        self.assertEqual(result['stop_reason'],'consecutive_invalid_responses')
        self.assertEqual(result['unattempted_rows'],2)

    def test_review_payload_rejection_stops_without_retry(self):
        write_batch(self.inp,'align-00000-00000.json',self.baseline,[make_row('z1',['j1'],'candidate')])
        for status in (400,413):
            error=urllib.error.HTTPError('http://example.invalid',status,'synthetic',{},None)
            with self.env(),patch('ltr.alignment.review.request_model',side_effect=error) as call:
                result=run(self.args(out=self.root/f'out-{status}',max_calls=100))
            self.assertEqual(call.call_count,1)
            self.assertEqual(result['stop_reason'],'request_rejected')

    def test_recovery_excludes_verified_rows_inside_frozen_range(self):
        write_batch(self.inp,'align-00000-00002.json',self.baseline,
                    [make_row(f'z{n}',['j1'],'candidate') for n in (1,2,3)])
        first=self.root/'first';calls=[]
        with self.env(),patch('ltr.alignment.review.request_model',side_effect=self.fake(calls)):
            run(self.args(out=first,batch_size=1,max_calls=1))
            result=run(self.args(out=self.root/'recovery',batch_size=1,max_calls=100,start=0,end=2,exclude_reviewed=first))
        self.assertEqual(calls[1]['payload']['items'][0]['chinese']['id'],'z2')
        self.assertEqual(len(calls),2)
        self.assertEqual(result['excluded_reviewed_rows'],1)
        self.assertEqual(result['selected_range_rows'],2)
        self.assertEqual(result['valid_rows'],1)
        self.assertEqual(result['skipped_suffix_rows'],1)
        self.assertEqual(result['remaining'],1)

    def test_recovery_does_not_trust_promoted_review_flags(self):
        write_batch(self.inp,'align-00000-00000.json',self.baseline,[make_row('z1',['j1'],'candidate')])
        first=self.root/'first'
        with self.env(),patch('ltr.alignment.review.request_model',side_effect=self.fake([])):
            run(self.args(out=first))
        path=first/'review-000.json';data=json.loads(path.read_text());data['rows'][0]['accepted']=1;path.write_text(json.dumps(data))
        with self.env(),self.assertRaises(ValueError):run(self.args(out=self.root/'recovery',exclude_reviewed=first))
        self.assertFalse((self.root/'recovery').exists())


class ReplyDiagnosticRuntimeTests(unittest.TestCase):
    setUp = AlignmentReviewRuntimeTests.setUp
    args = AlignmentReviewRuntimeTests.args
    env = AlignmentReviewRuntimeTests.env
    fake = AlignmentReviewRuntimeTests.fake

    def batches(self):
        write_batch(self.inp, "align-000.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate"), make_row("z2", ["j2"], "candidate")])

    def test_rejected_run_records_only_bounded_reply_shape_without_source(self):
        self.batches()
        marker = "SYNTHETIC_PRIVATE_REPLY C:\\private\\reply.txt"
        for variant, code, row_count, matched, missing in [
                ("missing", "inexact_review_coverage", 1, 1, 1),
                ("extra", "inexact_review_coverage", 3, 2, 0),
                ("duplicate", "inexact_review_coverage", 2, 1, 1),
                ("unexpected", "inexact_review_coverage", 2, 1, 1),
                ("extra_key", "invalid_review_contract", 2, 2, 0),
                ("bad_enum", "invalid_review_enum", 2, 2, 0),
                ("bad_root", "invalid_review_contract", 0, 0, 2),
                ("bad_nested", "invalid_review_contract", 2, 0, 2),
                ("oversized", "inexact_review_coverage", 1001, 1, 1)]:
            with self.subTest(variant=variant):
                out = self.root / variant

                def request(_endpoint, _token, _model, payload, **_kwargs):
                    rows = [{"review_id": item["review_id"], "decision": "keep", "basis": "semantic_match"}
                            for item in payload["items"]]
                    response_payload = {"reviews": rows}
                    if variant == "missing": rows.pop()
                    elif variant == "extra": rows.append(dict(rows[0], review_id=marker))
                    elif variant == "duplicate": rows[1] = dict(rows[0])
                    elif variant == "unexpected": rows[1]["review_id"] = marker
                    elif variant == "extra_key": response_payload[marker] = marker
                    elif variant == "bad_enum": rows[0]["decision"] = marker
                    elif variant == "bad_root": response_payload = [marker]
                    elif variant == "bad_nested": response_payload = {"reviews": [None, {"review_id": {marker: marker}}]}
                    elif variant == "oversized": response_payload = {"reviews": [rows[0]] * 1001}
                    return {"payload": response_payload, "usage": {"cache_read_input_tokens": 9}}

                with self.env(), patch("ltr.alignment.review.request_model", side_effect=request) as caller:
                    result = run(self.args(out=out, max_calls=1))
                self.assertEqual(caller.call_count, 1)
                self.assertEqual((result["attempted_calls"], result["failed_calls"], result["valid_calls"]), (1, 1, 0))
                self.assertEqual((result["failed_rows"], result["valid_rows"]), (2, 0))
                self.assertEqual(result["cache_read_input_tokens"], 9)
                self.assertEqual(result["stop_reason"], "range_or_budget_complete")
                self.assertFalse(list(out.glob("review-*.json")))
                ledger = json.loads((out / "progress.json").read_text(encoding="utf-8"))
                record = ledger["calls"][0]
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["error_code"], code)
                shape = record["reply_shape"]
                self.assertEqual(shape["returned_rows"], row_count)
                self.assertEqual(shape["inspected_rows"], min(row_count, 1000))
                self.assertEqual(shape["rows_truncated"], row_count > 1000)
                self.assertEqual((shape["matched_ids"], shape["missing_ids"]), (matched, missing))
                self.assertEqual(shape["expected_rows"], 2)
                exported = "".join(path.read_text(encoding="utf-8") for path in out.glob("*.json"))
                for secret in (marker, ZH1, ZH2, JA1, JA2, str(self.root)):
                    self.assertNotIn(secret, exported)

    def test_diagnostic_failure_is_omitted_without_changing_rejection(self):
        self.batches()
        for name, diagnostic in (("none", None), ("exception", ValueError("SYNTHETIC_PRIVATE_EXCEPTION"))):
            with self.subTest(name=name):
                out = self.root / name
                options = {"side_effect": diagnostic} if isinstance(diagnostic, Exception) else {"return_value": diagnostic}
                with self.env(), patch("ltr.alignment.review.request_model", return_value={"payload": {"reviews": []}}), \
                        patch("ltr.alignment.review._reply_diagnostics", **options):
                    result = run(self.args(out=out, max_calls=1))
                self.assertEqual(result["failed_calls"], 1)
                ledger = json.loads((out / "progress.json").read_text(encoding="utf-8"))
                self.assertEqual(ledger["calls"][0]["error_code"], "inexact_review_coverage")
                self.assertNotIn("reply_shape", ledger["calls"][0])
                self.assertNotIn("SYNTHETIC_PRIVATE_EXCEPTION", json.dumps(ledger))

    def test_valid_and_unparseable_responses_do_not_run_diagnostics(self):
        self.batches()
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=self.fake([])), \
                patch("ltr.alignment.review._reply_diagnostics") as diagnostic:
            result = run(self.args(max_calls=1))
        diagnostic.assert_not_called()
        self.assertEqual(result["valid_rows"], 2)
        ledger = json.loads((self.out / "progress.json").read_text(encoding="utf-8"))
        self.assertNotIn("reply_shape", ledger["calls"][0])
        out = self.root / "unparseable"
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=ModelResponseError("invalid_model_json")), \
                patch("ltr.alignment.review._reply_diagnostics") as diagnostic:
            result = run(self.args(out=out, max_calls=1))
        diagnostic.assert_not_called()
        self.assertEqual(result["failed_calls"], 1)
        ledger = json.loads((out / "progress.json").read_text(encoding="utf-8"))
        self.assertNotIn("reply_shape", ledger["calls"][0])


class AlignmentReviewSessionTests(unittest.TestCase):
    """Optional in-memory session reuse for finite ranges/editions, synthetic only."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.db = self.source / "dialogue.sqlite"
        make_db(self.db)
        self.baseline = fingerprint(self.db)
        self.inp = self.root / "input"
        self.inp.mkdir()
        self.out = self.root / "out"

    def args(self, **over):
        values = dict(db=self.db, input=self.inp, out=self.out, model="reviewer-model",
                      max_calls=5, batch_size=6, start=0, execute=True)
        values.update(over)
        return SimpleNamespace(**values)

    def env(self):
        return patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "http://x",
                                       "ANTHROPIC_AUTH_TOKEN": "t"})

    def fake(self, calls, decision="keep", basis="semantic_match"):
        def request(endpoint, token, model, payload, *, system=None, history=None, session_id=None):
            calls.append({"payload": json.loads(json.dumps(payload)),
                          "history": list(history or []), "session_id": session_id})
            reviews = [{"review_id": it["review_id"], "decision": decision, "basis": basis}
                       for it in payload["items"]]
            if history is not None:
                history.extend([{"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                                {"role": "assistant", "content": json.dumps({"reviews": reviews})}])
            return {"payload": {"reviews": reviews}, "usage": {}, "served_model": model + "-served"}
        return request

    def exported(self, *directories):
        return "".join(p.read_text(encoding="utf-8")
                       for directory in directories for p in Path(directory).rglob("*.json"))

    def test_two_finite_ranges_share_uuid_and_grow_history(self):
        write_batch(self.inp, "align-00000-00001.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate"), make_row("z2", ["j1"], "candidate")])
        calls = []
        session = {}
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=self.fake(calls)):
            first = run(self.args(batch_size=1, max_calls=1, start=0), session=session)
            second = run(self.args(out=self.root / "out2", batch_size=1, max_calls=1, start=1),
                         session=session)
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(session["session_id"], first["session_id"])
        self.assertEqual([len(call["history"]) for call in calls], [0, 2])
        self.assertEqual(len(session["history"]), 4)
        self.assertEqual(session["model"], "reviewer-model")
        self.assertEqual(session["database_sha256"], self.baseline)
        self.assertEqual(session["system_sha256"], digest(SYSTEM))

    def test_later_range_new_reference_appended_and_seen_reference_omitted(self):
        write_batch(self.inp, "align-00000-00001.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate"), make_row("z2", ["j2"], "candidate")])
        calls = []
        session = {}
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=self.fake(calls)):
            run(self.args(batch_size=1, max_calls=1, start=0), session=session)
            run(self.args(out=self.root / "out2", batch_size=1, max_calls=1, start=1), session=session)
        self.assertEqual([r["id"] for r in calls[0]["payload"]["japanese_reference"]], ["j1"])
        self.assertEqual([r["id"] for r in calls[1]["payload"]["japanese_reference"]], ["j2"])
        self.assertEqual(calls[1]["payload"]["japanese_reference"][0]["text"], JA2)
        progress = json.loads((self.root / "out2" / "progress.json").read_text(encoding="utf-8"))
        self.assertEqual(progress["calls"][0]["session_id"], session["session_id"])
        self.assertNotEqual(progress["calls"][0]["history_sha256"], digest([]))

    def test_later_range_reusing_seen_reference_omits_reference_list(self):
        write_batch(self.inp, "align-00000-00001.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate"), make_row("z2", ["j1"], "candidate")])
        calls = []
        session = {}
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=self.fake(calls)):
            run(self.args(batch_size=1, max_calls=1, start=0), session=session)
            run(self.args(out=self.root / "out2", batch_size=1, max_calls=1, start=1), session=session)
        self.assertIn("japanese_reference", calls[0]["payload"])
        self.assertNotIn("japanese_reference", calls[1]["payload"])
        self.assertEqual(len(session["history"]), 4)

    def test_later_edition_pair_extends_same_session_with_new_reference(self):
        con=sqlite3.connect(self.db)
        con.executemany('INSERT INTO editions VALUES (?,?,?,?)',
                        [('zh2','Other synthetic','zh','bk2'),('ja2','Other synthetic','ja','bk2')])
        con.executemany('INSERT INTO dialogues VALUES (?,?,?,?)',
                        [('z4','zh2',0,'PRIVATE_ZH_EXTRA'),('j4','ja2',0,'PRIVATE_JA_EXTRA')])
        con.commit();con.close();self.baseline=fingerprint(self.db)
        write_batch(self.inp,'align-000.json',self.baseline,[make_row('z1',['j1'],'candidate')])
        other=self.root/'other';other.mkdir()
        row=dict(make_row('z1',['j1'],'candidate'),chinese_id='z4',
                 chinese_text_sha256=sha('PRIVATE_ZH_EXTRA'),japanese_ids=['j4'],
                 japanese=[{'id':'j4','text_sha256':sha('PRIVATE_JA_EXTRA')}])
        write_batch(other,'align-000.json',self.baseline,[row],
                    zh_edition_id='zh2',ja_edition_id='ja2',book_key='bk2')
        calls=[];state={}
        with self.env(),patch('ltr.alignment.review.request_model',side_effect=self.fake(calls)):
            first=run(self.args(),session=state)
            second=run(self.args(input=other,out=self.root/'second'),session=state)
        self.assertEqual(first['session_id'],second['session_id'])
        self.assertEqual([len(c['history']) for c in calls],[0,2])
        self.assertEqual([r['id'] for r in calls[1]['payload']['japanese_reference']],['j4'])
        self.assertEqual(second['valid_rows'],1)
        self.assertEqual(len(state["history"]), 4)

    def test_default_run_creates_fresh_state_every_call(self):
        write_batch(self.inp, "align-00000-00000.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate")])
        calls = []
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=self.fake(calls)):
            first = run(self.args())
            second = run(self.args(out=self.root / "out2"))
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertEqual(calls[0]["history"], [])
        self.assertEqual(calls[1]["history"], [])
        self.assertIn("japanese_reference", calls[1]["payload"])

    def test_binding_mismatch_refused_before_output_or_call(self):
        write_batch(self.inp, "align-00000-00000.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate")])
        good = {"model": "reviewer-model", "database_sha256": self.baseline,
                "system_sha256": digest(SYSTEM), "session_id": "s", "history": []}
        bad_states = [
            [],                                                    # non-dict
            dict(good, model="other"),                             # changed model
            dict(good, database_sha256="deadbeef"),                # changed source
            dict(good, system_sha256="deadbeef"),                  # changed system
            {"model": "reviewer-model"},                           # malformed / missing keys
            dict(good, history="not-a-list"),                      # malformed history
            {"model": "reviewer-model", "database_sha256": self.baseline,
             "system_sha256": digest(SYSTEM), "history": []},      # missing session_id
        ]
        for index, state in enumerate(bad_states):
            out = self.root / f"refuse-{index}"
            calls = []
            with self.env(), patch("ltr.alignment.review.request_model", side_effect=self.fake(calls)):
                with self.assertRaises(ValueError) as raised:
                    run(self.args(out=out), session=state)
            self.assertEqual(str(raised.exception), "invalid_session")
            self.assertFalse(out.exists())
            self.assertEqual(calls, [])

    def test_conflicting_reference_identity_refused_before_output(self):
        write_batch(self.inp, "align-00000-00000.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate")])
        session = {}
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=self.fake([])):
            run(self.args(), session=session)
        message = session["history"][0]
        payload = json.loads(message["content"])
        payload["japanese_reference"][0]["text"] = "tampered-reference"
        message["content"] = json.dumps(payload, ensure_ascii=False)
        out = self.root / "conflict"
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=self.fake([])) as call:
            with self.assertRaises(ValueError) as raised:
                run(self.args(out=out), session=session)
        self.assertEqual(str(raised.exception), "conflicting_japanese_reference")
        self.assertFalse(out.exists())
        self.assertEqual(call.call_count, 0)

    def test_malformed_nested_history_is_refused_before_dispatch(self):
        write_batch(self.inp, 'align-000.json', self.baseline,
                    [make_row('z1',['j1'],'candidate')])
        for number,history in enumerate([
                ['bad'],[{'role':'system','content':'bad'}],
                [{'role':'user','content':'not json'}],
                [{'role':'user','content':json.dumps({'japanese_reference':7})}],
                [{'role':'user','content':json.dumps({'japanese_reference':[{}]})}]]):
            state={'model':'reviewer-model','database_sha256':self.baseline,
                   'system_sha256':digest(SYSTEM),'session_id':'s','history':history}
            out=self.root/f'nested-{number}'
            with self.env(),patch('ltr.alignment.review.request_model') as call:
                with self.assertRaisesRegex(ValueError,'^invalid_session$'):
                    run(self.args(out=out),session=state)
            self.assertFalse(out.exists());call.assert_not_called()

    def test_conflicting_reference_cannot_be_hidden_by_later_duplicate(self):
        write_batch(self.inp,'align-000.json',self.baseline,
                    [make_row('z1',['j1'],'candidate')])
        state={}
        with self.env(),patch('ltr.alignment.review.request_model',side_effect=self.fake([])):
            run(self.args(),session=state)
        original=dict(state['history'][0]);changed=json.loads(original['content'])
        changed['japanese_reference'][0]['text']='CONFLICTING SYNTHETIC TEXT'
        state['history'][0]={'role':'user','content':json.dumps(changed)}
        state['history'].append(original)
        out=self.root/'hidden-conflict'
        with self.env(),patch('ltr.alignment.review.request_model') as call:
            with self.assertRaisesRegex(ValueError,'^conflicting_japanese_reference$'):
                run(self.args(out=out),session=state)
        self.assertFalse(out.exists());call.assert_not_called()

    def test_source_change_during_session_call_refuses_result(self):
        write_batch(self.inp, "align-00000-00000.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate")])
        session = {}
        request = self.fake([])
        def concurrent(*a, **k):
            result = request(*a, **k)
            Path(str(self.db) + "-wal").write_bytes(b"synthetic concurrent WAL")
            return result
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=concurrent):
            summary = run(self.args(), session=session)
        self.assertFalse(summary["source_unchanged"])
        self.assertFalse(list(self.out.glob("review-*.json")))
        self.assertEqual(set(session), {"model", "database_sha256", "system_sha256",
                                        "session_id", "reference_delivery", "request_byte_budget", "output_token_budget", "history_mode", "history"})

    def test_session_metadata_never_serializes_history_or_source(self):
        write_batch(self.inp, "align-00000-00000.json", self.baseline,
                    [make_row("z1", ["j1"], "candidate")])
        session = {}
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=self.fake([])):
            run(self.args(), session=session)
            run(self.args(out=self.root / "out2"), session=session)
        exported = self.exported(self.out, self.root / "out2")
        self.assertNotIn("PRIVATE_ZH", exported)
        self.assertNotIn("PRIVATE_JA", exported)
        self.assertNotIn('"history"', exported)
        summary = json.loads((self.root / "out2" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["session_id"], session["session_id"])

    def test_per_run_budget_counters_stay_per_run(self):
        write_batch(self.inp, "align-00000-00002.json", self.baseline,
                    [make_row(f"z{n}", ["j1"], "candidate") for n in (1, 2, 3)])
        calls = []
        session = {}
        with self.env(), patch("ltr.alignment.review.request_model", side_effect=self.fake(calls)):
            first = run(self.args(batch_size=1, max_calls=1, start=0), session=session)
            second = run(self.args(out=self.root / "out2", batch_size=1, max_calls=1, start=1),
                         session=session)
        self.assertEqual(first["attempted_calls"], 1)
        self.assertEqual(first["valid_calls"], 1)
        self.assertEqual(second["attempted_calls"], 1)
        self.assertEqual(second["valid_calls"], 1)
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual([len(call["history"]) for call in calls], [0, 2])

    def test_cache_usage_counts_completed_invalid_outputs_once(self):
        write_batch(self.inp, 'align-000.json', self.baseline,
                    [make_row(f'z{n}', ['j1'], 'candidate') for n in (1, 2, 3)])
        attempts = []
        success = self.fake([])
        def request(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                return {'payload': {'reviews': []}, 'usage': {'cache_read_input_tokens': 11,
                        'private_extra': 'SYNTHETIC_PRIVATE_USAGE'}}
            if len(attempts) == 2:
                raise ModelResponseError('invalid_model_json', usage={'cache_read_input_tokens': 13})
            result = success(*args, **kwargs)
            result['usage'] = {'cache_read_input_tokens': 17}
            return result
        with self.env(), patch('ltr.alignment.review.request_model', side_effect=request):
            result = run(self.args(batch_size=1, max_calls=3))
        self.assertEqual(result['cache_read_input_tokens'], 41)
        self.assertEqual(result['failed_calls'], 2)
        self.assertEqual(result['valid_rows'], 1)
        ledger = json.loads((self.out / 'progress.json').read_text())
        self.assertEqual([r.get('error_code') for r in ledger['calls'][:2]],
                         ['inexact_review_coverage', 'invalid_model_json'])
        self.assertNotIn('SYNTHETIC_PRIVATE_USAGE', self.exported(self.out))

    def test_invalid_enum_type_has_static_diagnostic_and_no_fake_usage(self):
        write_batch(self.inp, 'align-000.json', self.baseline, [make_row('z1', ['j1'], 'candidate')])
        def request(_endpoint, _token, _model, payload, **_kwargs):
            return {'payload': {'reviews': [{'review_id': payload['items'][0]['review_id'],
                    'decision': [], 'basis': 'semantic_match'}]},
                    'usage': {'cache_read_input_tokens': True, 'input_tokens': -1, 'output_tokens': '7'}}
        with self.env(), patch('ltr.alignment.review.request_model', side_effect=request):
            result = run(self.args(max_calls=1))
        self.assertEqual(result['cache_read_input_tokens'], 0)
        ledger = json.loads((self.out / 'progress.json').read_text())
        self.assertEqual(ledger['calls'][0]['error_code'], 'invalid_review_enum')
        self.assertEqual(ledger['calls'][0]['usage'], {})
        self.assertFalse(list(self.out.glob('review-*.json')))


class AlignmentSourceOrderTests(unittest.TestCase):
    setUp = AlignmentReviewRuntimeTests.setUp
    args = AlignmentReviewRuntimeTests.args
    env = AlignmentReviewRuntimeTests.env
    fake = AlignmentReviewRuntimeTests.fake

    def batches(self):
        write_batch(self.inp, 'align-000.json', self.baseline,
                    [make_row('z2', ['j1'], 'candidate'), make_row('z3', ['j1'], 'candidate')])
        write_batch(self.inp, 'align-001.json', self.baseline,
                    [make_row('z1', ['j1'], 'candidate')])

    def test_load_source_default_preserves_file_row_order_explicit_true_sorts(self):
        self.batches()
        con = sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True)
        try:
            con.execute('PRAGMA query_only=ON')
            default = load_source(self.inp, con, self.baseline)
            explicit_false = load_source(self.inp, con, self.baseline, source_order=False)
            canonical = load_source(self.inp, con, self.baseline, source_order=True)
        finally:
            con.close()
        self.assertEqual([r['chinese_id'] for r in default['candidates']], ['z2', 'z3', 'z1'])
        self.assertEqual(default, explicit_false)
        self.assertEqual([r['chinese_id'] for r in canonical['candidates']], ['z1', 'z2', 'z3'])
        self.assertEqual(default['artifacts'], canonical['artifacts'])
        self.assertEqual({r['review_id'] for r in default['candidates']},
                         {r['review_id'] for r in canonical['candidates']})

    def test_run_ranges_use_requested_order_and_record_safe_boolean(self):
        self.batches()
        for label, overrides, expected in (
            ('default', {}, 'z3'), ('false', {'source_order': False}, 'z3'),
            ('true', {'source_order': True}, 'z2')
        ):
            with self.subTest(mode=label):
                calls = []
                output = self.root / ('out-' + label)
                with self.env(), patch('ltr.alignment.review.request_model', side_effect=self.fake(calls)):
                    result = run(self.args(out=output, start=1, end=2, **overrides))
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]['payload']['items'][0]['chinese']['id'], expected)
                mode = overrides.get('source_order', False)
                self.assertIs(result['source_order'], mode)
                ledger = json.loads((output / 'progress.json').read_text(encoding='utf-8'))
                saved = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
                self.assertIs(ledger['source_order'], mode)
                self.assertIs(saved['source_order'], mode)

    def test_nonboolean_order_mode_refused_before_load_output_or_dispatch(self):
        self.batches()
        con = sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True)
        try:
            con.execute('PRAGMA query_only=ON')
            for mode in (None, 0, 1, 'true', [], {}):
                with self.subTest(mode=mode):
                    with self.assertRaisesRegex(ValueError, '^invalid_source_order$'):
                        load_source(self.inp, con, self.baseline, source_order=mode)
                    with patch('ltr.alignment.review.load_source') as loader, \
                            patch('ltr.alignment.review.request_model') as request:
                        with self.assertRaisesRegex(ValueError, '^invalid_source_order$'):
                            run(self.args(source_order=mode))
                        loader.assert_not_called()
                        request.assert_not_called()
                    self.assertFalse(self.out.exists())
        finally:
            con.close()

    def test_cli_source_order_is_explicit_opt_in(self):
        from contextlib import redirect_stdout
        import io
        from ltr.alignment import review as review_module

        arguments = ['--db', str(self.db), '--input', str(self.inp), '--out', str(self.out),
                     '--model', 'reviewer-model', '--max-calls', '1', '--execute']
        for flag, expected in (([], False), (['--source-order'], True)):
            with redirect_stdout(io.StringIO()), patch.object(review_module, 'run', return_value={'source_unchanged': True}) as runner:
                self.assertEqual(review_module.main(arguments + flag), 0)
            self.assertIs(runner.call_args.args[0].source_order, expected)


class AlignmentIncrementalReferenceTests(unittest.TestCase):
    setUp = AlignmentReviewRuntimeTests.setUp
    args = AlignmentReviewRuntimeTests.args
    env = AlignmentReviewRuntimeTests.env
    fake = AlignmentReviewRuntimeTests.fake

    def batches(self):
        write_batch(self.inp, 'align-000.json', self.baseline,
                    [make_row('z1', ['j1'], 'candidate'), make_row('z2', ['j2'], 'candidate'),
                     make_row('z3', ['j1', 'j2'], 'candidate')])

    def test_incremental_batches_append_only_needed_new_references(self):
        self.batches()
        calls, state = [], {}
        with self.env(), patch('ltr.alignment.review.request_model', side_effect=self.fake(calls)):
            result = run(self.args(batch_size=1, incremental_references=True), session=state)
        self.assertEqual([r['id'] for r in calls[0]['payload']['japanese_reference']], ['j1'])
        self.assertEqual([r['id'] for r in calls[1]['payload']['japanese_reference']], ['j2'])
        self.assertNotIn('japanese_reference', calls[2]['payload'])
        self.assertEqual([len(call['history']) for call in calls], [0, 2, 4])
        self.assertEqual(len(state['history']), 6)
        self.assertEqual(result['reference_delivery'], 'incremental')
        self.assertEqual(state['reference_delivery'], 'incremental')
        self.assertEqual(json.loads((self.out / 'progress.json').read_bytes())['reference_delivery'], 'incremental')

    def test_default_prefetch_includes_later_references_unchanged(self):
        self.batches()
        calls = []
        with self.env(), patch('ltr.alignment.review.request_model', side_effect=self.fake(calls)):
            result = run(self.args(batch_size=1))
        self.assertEqual([r['id'] for r in calls[0]['payload']['japanese_reference']], ['j1', 'j2'])
        self.assertNotIn('japanese_reference', calls[1]['payload'])
        self.assertEqual(result['reference_delivery'], 'prefetch')

    def test_first_incremental_payload_excludes_unrelated_large_later_reference(self):
        large = 'SYNTHETIC_LARGE_LATER_REFERENCE_' * 5000
        con = sqlite3.connect(self.db)
        con.executemany('INSERT INTO dialogues VALUES (?,?,?,?)',
                        [('j-middle', 'ja1', 10, 'Synthetic separating row'), ('j-large', 'ja1', 20, large)])
        con.commit()
        con.close()
        self.baseline = fingerprint(self.db)
        later = make_row('z2', ['j1'], 'candidate', japanese_ids=['j-large'],
                         japanese=[{'id': 'j-large', 'text_sha256': sha(large)}])
        write_batch(self.inp, 'align-000.json', self.baseline, [make_row('z1', ['j1'], 'candidate'), later])
        calls = []
        with self.env(), patch('ltr.alignment.review.request_model', side_effect=self.fake(calls)):
            run(self.args(batch_size=1, incremental_references=True))
        self.assertNotIn(large, json.dumps(calls[0]['payload']))
        self.assertEqual([r['id'] for r in calls[1]['payload']['japanese_reference']], ['j-large'])

    def test_failed_transport_without_history_does_not_mark_reference_supplied(self):
        write_batch(self.inp, 'align-000.json', self.baseline,
                    [make_row('z1', ['j1'], 'candidate'), make_row('z2', ['j1'], 'candidate')])
        calls = []
        success = self.fake([])

        def request(*args, **kwargs):
            calls.append(json.loads(json.dumps(args[3])))
            if len(calls) == 1:
                raise OSError('SYNTHETIC_PRIVATE_TRANSPORT_ERROR')
            return success(*args, **kwargs)

        state = {}
        with self.env(), patch('ltr.alignment.review.request_model', side_effect=request):
            result = run(self.args(batch_size=1, incremental_references=True), session=state)
        self.assertEqual([[r['id'] for r in call['japanese_reference']] for call in calls], [['j1'], ['j1']])
        self.assertEqual(result['attempted_calls'], 2)
        self.assertEqual(result['failed_rows'], 1)
        self.assertEqual(result['valid_rows'], 1)
        self.assertEqual(len(state['history']), 2)

    def test_mode_switch_is_refused_in_either_direction_before_output(self):
        self.batches()
        for initial in (False, True):
            state, calls = {}, []
            with self.env(), patch('ltr.alignment.review.request_model', side_effect=self.fake(calls)):
                run(self.args(out=self.root / f'first-{initial}', max_calls=1, batch_size=1,
                              incremental_references=initial), session=state)
                out = self.root / f'switch-{initial}'
                with self.assertRaisesRegex(ValueError, '^invalid_session$'):
                    run(self.args(out=out, incremental_references=not initial), session=state)
                self.assertEqual(len(calls), 1)
                self.assertFalse(out.exists())

    def test_strict_boolean_mode_refused_before_source_load_or_output(self):
        self.batches()
        for value in (None, 0, 1, 'true', [], {}):
            with patch('ltr.alignment.review.load_source') as loader:
                with self.assertRaisesRegex(ValueError, '^invalid_reference_delivery$'):
                    run(self.args(incremental_references=value))
                loader.assert_not_called()
            self.assertFalse(self.out.exists())

    def test_review_cli_incremental_reference_flag_is_explicit_opt_in(self):
        from contextlib import redirect_stdout
        import io
        from ltr.alignment import review as module
        argv = ['--db', str(self.db), '--input', str(self.inp), '--out', str(self.out),
                '--model', 'reviewer', '--max-calls', '1', '--execute']
        for flag, expected in (([], False), (['--incremental-references'], True)):
            with redirect_stdout(io.StringIO()), patch.object(module, 'run', return_value={'source_unchanged': True}) as runner:
                self.assertEqual(module.main(argv + flag), 0)
            self.assertIs(runner.call_args.args[0].incremental_references, expected)

    def test_conflict_in_later_loaded_reference_is_rejected_before_dispatch(self):
        self.batches()
        state = {}
        with self.env(), patch('ltr.alignment.review.request_model', side_effect=self.fake([])):
            run(self.args(max_calls=1, batch_size=1, incremental_references=True), session=state)
        payload = json.loads(state['history'][0]['content'])
        # Include a conflicting historical j2 even though the next selected
        # batch only needs j1; the whole loaded reference set must be checked.
        fake_j2 = dict(payload['japanese_reference'][0], id='j2')
        payload['japanese_reference'].append(fake_j2)
        state['history'][0]['content'] = json.dumps(payload)
        out = self.root / 'conflict'
        with self.env(), patch('ltr.alignment.review.request_model') as request:
            with self.assertRaisesRegex(ValueError, '^conflicting_japanese_reference$'):
                run(self.args(out=out, max_calls=1, batch_size=1, incremental_references=True), session=state)
            request.assert_not_called()
        self.assertFalse(out.exists())


class AlignmentRequestBudgetTests(unittest.TestCase):
    setUp = AlignmentReviewRuntimeTests.setUp
    args = AlignmentReviewRuntimeTests.args
    env = AlignmentReviewRuntimeTests.env
    fake = AlignmentReviewRuntimeTests.fake

    def batches(self):
        write_batch(self.inp, 'align-000.json', self.baseline,
                    [make_row(f'z{i}', ['j1'], 'candidate') for i in (1, 2, 3)])

    def state(self, budget):
        return dict(model='reviewer-model', database_sha256=self.baseline, system_sha256=digest(SYSTEM),
                    session_id='synthetic-byte-session', history=[], reference_delivery='prefetch', request_byte_budget=budget)

    def measured_first(self):
        from ltr.quotes.batch import request_byte_count
        calls = []
        with self.env(), patch('ltr.alignment.review.request_model', side_effect=self.fake(calls)):
            run(self.args(out=self.root / 'measure', batch_size=1, max_calls=1), session=self.state(None))
        return request_byte_count('reviewer-model', calls[0]['payload'], system=SYSTEM,
                                  history=calls[0]['history'], session_id='synthetic-byte-session')

    def test_first_request_over_budget_remains_unattempted_and_terminal_history_valid(self):
        from ltr.alignment import plan as alignment_plan
        self.batches()
        with self.env(), patch('ltr.alignment.review.request_model') as request:
            result = run(self.args(max_request_bytes=1, batch_size=1))
            request.assert_not_called()
        self.assertEqual(result['stop_reason'], 'request_byte_budget_exhausted')
        self.assertEqual(result['request_byte_budget'], 1)
        self.assertGreater(result['next_request_bytes'], 1)
        self.assertEqual((result['attempted_calls'], result['attempted_rows'], result['failed_rows'], result['valid_rows']), (0, 0, 0, 0))
        self.assertEqual(result['unattempted_rows'], 3)
        self.assertEqual(json.loads((self.out / 'progress.json').read_bytes())['calls'], [])
        statuses, _, _ = alignment_plan._history(self.out, self.baseline, ('zh1', 'ja1'))
        self.assertFalse(set().union(*statuses.values()))
        self.assertNotIn(ZH1, json.dumps(result))
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_history_growth_preserves_success_and_stops_before_next_attempt(self):
        self.batches()
        cap = self.measured_first()
        calls, state = [], self.state(cap)
        with self.env(), patch('ltr.alignment.review.request_model', side_effect=self.fake(calls)):
            result = run(self.args(max_request_bytes=cap, batch_size=1), session=state)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['valid_rows'], 1)
        self.assertEqual(result['failed_calls'], 0)
        self.assertEqual(result['attempted_rows'], 1)
        self.assertEqual(result['unattempted_rows'], 2)
        self.assertEqual(result['stop_reason'], 'request_byte_budget_exhausted')
        self.assertGreater(result['next_request_bytes'], cap)
        self.assertEqual(len(state['history']), 2)
        ledger = json.loads((self.out / 'progress.json').read_bytes())
        self.assertEqual(len(ledger['calls']), 1)
        self.assertEqual(ledger['calls'][0]['request_bytes'], cap)
        self.assertTrue((self.out / 'review-000.json').is_file())
        self.assertFalse((self.out / 'review-001.json').exists())

    def test_equal_request_size_budget_is_allowed(self):
        self.batches()
        cap = self.measured_first()
        calls = []
        with self.env(), patch('ltr.alignment.review.request_model', side_effect=self.fake(calls)):
            result = run(self.args(max_request_bytes=cap, batch_size=1, end=1), session=self.state(cap))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['valid_rows'], 1)
        self.assertEqual(result['stop_reason'], 'range_or_budget_complete')
        self.assertIsNone(result['next_request_bytes'])

    def test_invalid_budgets_refused_before_source_load(self):
        self.batches()
        for value in (True, False, 0, -1, 16 * 1024 * 1024 + 1, 1.5, '100', [], {}):
            with patch('ltr.alignment.review.load_source') as loader:
                with self.assertRaisesRegex(ValueError, '^invalid_request_byte_budget$'):
                    run(self.args(max_request_bytes=value))
                loader.assert_not_called()
            self.assertFalse(self.out.exists())

    def test_session_byte_budget_cannot_change_in_either_direction(self):
        self.batches()
        for n, (first, second) in enumerate(((None, 10000), (10000, None), (10000, 20000))):
            state, calls = {}, []
            with self.env(), patch('ltr.alignment.review.request_model', side_effect=self.fake(calls)):
                run(self.args(out=self.root / f'first-{n}', max_request_bytes=first, end=1), session=state)
                out = self.root / f'switch-{n}'
                with self.assertRaisesRegex(ValueError, '^invalid_session$'):
                    run(self.args(out=out, max_request_bytes=second, start=1), session=state)
            self.assertEqual(len(calls), 1)
            self.assertFalse(out.exists())

    def test_source_sidecar_after_sizing_cannot_become_valid_history(self):
        from ltr.alignment import plan as alignment_plan
        from ltr.alignment import review as module
        self.batches()
        original = module.request_byte_count

        def changed(*args, **kwargs):
            size = original(*args, **kwargs)
            Path(str(self.db) + '-journal').write_bytes(b'synthetic concurrent writer')
            return size

        with self.env(), patch.object(module, 'request_byte_count', side_effect=changed), patch.object(module, 'request_model') as request:
            result = run(self.args(max_request_bytes=1))
            request.assert_not_called()
        self.assertFalse(result['source_unchanged'])
        self.assertEqual(result['attempted_calls'], 0)
        with self.assertRaises(ValueError):
            alignment_plan._history(self.out, self.baseline, ('zh1', 'ja1'))

    def test_review_cli_byte_budget_is_optional(self):
        from contextlib import redirect_stdout
        import io
        from ltr.alignment import review as module
        argv = ['--db', str(self.db), '--input', str(self.inp), '--out', str(self.out),
                '--model', 'reviewer', '--max-calls', '1', '--execute']
        for flag, expected in (([], None), (['--max-request-bytes', '12345'], 12345)):
            with redirect_stdout(io.StringIO()), patch.object(module, 'run', return_value={'source_unchanged': True}) as runner:
                self.assertEqual(module.main(argv + flag), 0)
            self.assertEqual(runner.call_args.args[0].max_request_bytes, expected)


class AlignmentOutputBudgetTests(unittest.TestCase):
    setUp = AlignmentReviewRuntimeTests.setUp
    args = AlignmentReviewRuntimeTests.args
    env = AlignmentReviewRuntimeTests.env
    fake = AlignmentReviewRuntimeTests.fake

    def batches(self):
        write_batch(self.inp, 'align-000.json', self.baseline,
                    [make_row('z1', ['j1'], 'candidate'), make_row('z2', ['j1'], 'candidate')])

    def test_explicit_budget_forwarded_to_size_and_send_and_recorded_in_terminal_history(self):
        from ltr.alignment import review as module
        from ltr.alignment import plan as alignment_plan
        self.batches()
        calls, budgets, state = [], [], {}
        successful = self.fake(calls)

        def request(*args, **kwargs):
            budgets.append(kwargs.pop('max_output_tokens'))
            return successful(*args, **kwargs)

        with self.env(), patch.object(module, 'request_model', side_effect=request), \
                patch.object(module, 'request_byte_count', wraps=module.request_byte_count) as size:
            result = run(self.args(max_output_tokens=16000, batch_size=1), session=state)
        self.assertEqual(budgets, [16000, 16000])
        self.assertEqual([call.kwargs['max_output_tokens'] for call in size.call_args_list], [16000, 16000])
        self.assertEqual(state['output_token_budget'], 16000)
        self.assertEqual(len(state['history']), 4)
        self.assertEqual(result['output_token_budget'], 16000)
        self.assertEqual(json.loads((self.out / 'progress.json').read_bytes())['output_token_budget'], 16000)
        evidence, _, _ = alignment_plan._history(self.out, self.baseline, ('zh1', 'ja1'))
        self.assertEqual(len(evidence['valid_model_review']), 2)

    def test_default_omits_new_request_keyword_and_records_8000(self):
        self.batches()
        with self.env(), patch('ltr.alignment.review.request_model', side_effect=self.fake([])) as request:
            result = run(self.args())
        self.assertNotIn('max_output_tokens', request.call_args.kwargs)
        self.assertEqual(result['output_token_budget'], 8000)

    def test_invalid_output_budgets_fail_before_input_or_dispatch(self):
        for value in (True, False, 0, -1, 131073, 1.5, '16000', [], {}):
            with patch('ltr.alignment.review.load_source') as loader, patch('ltr.alignment.review.request_model') as request:
                with self.assertRaisesRegex(ValueError, '^invalid_output_token_budget$'):
                    run(self.args(max_output_tokens=value))
                loader.assert_not_called()
                request.assert_not_called()
            self.assertFalse(self.out.exists())

    def test_effective_budget_cannot_change_in_reused_session(self):
        self.batches()
        for n, (first, second) in enumerate(((None, 16000), (16000, None), (16000, 24000))):
            state, sent = {}, []
            original = self.fake(sent)

            def request(*args, **kwargs):
                kwargs.pop('max_output_tokens', None)
                return original(*args, **kwargs)

            with self.env(), patch('ltr.alignment.review.request_model', side_effect=request):
                run(self.args(out=self.root / f'first-output-{n}', end=1, max_output_tokens=first), session=state)
                out = self.root / f'changed-output-{n}'
                with self.assertRaisesRegex(ValueError, '^invalid_session$'):
                    run(self.args(out=out, start=1, max_output_tokens=second), session=state)
            self.assertEqual(len(sent), 1)
            self.assertFalse(out.exists())

    def test_explicit_8000_and_default_share_effective_budget_without_reset(self):
        self.batches()
        state, calls = {}, []
        original = self.fake(calls)

        def request(*args, **kwargs):
            kwargs.pop('max_output_tokens', None)
            return original(*args, **kwargs)

        with self.env(), patch('ltr.alignment.review.request_model', side_effect=request):
            first = run(self.args(end=1), session=state)
            second = run(self.args(out=self.root / 'second-output', start=1, max_output_tokens=8000), session=state)
        self.assertEqual(first['session_id'], second['session_id'])
        self.assertEqual([len(call['history']) for call in calls], [0, 2])
        self.assertEqual(state['output_token_budget'], 8000)

    def test_review_cli_output_budget_is_optional(self):
        from contextlib import redirect_stdout
        import io
        from ltr.alignment import review as module
        argv = ['--db', str(self.db), '--input', str(self.inp), '--out', str(self.out),
                '--model', 'reviewer', '--max-calls', '1', '--execute']
        for flag, expected in (([], None), (['--max-output-tokens', '16000'], 16000)):
            with redirect_stdout(io.StringIO()), patch.object(module, 'run', return_value={'source_unchanged': True}) as runner:
                self.assertEqual(module.main(argv + flag), 0)
            self.assertEqual(runner.call_args.args[0].max_output_tokens, expected)


if __name__ == "__main__":
    unittest.main()
