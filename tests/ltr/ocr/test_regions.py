"""Region OCR: candidate contract, external store, bounded runner and workbench endpoints (synthetic data)."""
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from ltr.ocr import regions
from ltr.ocr import regions_run as runner
from ltr import workbench as wb
from ltr.core import OCR_FLAGS, canonical_sha256

H = "a" * 64
SESSION = str(uuid.uuid4())


def payload(page_id="P:p0001", count=3):
    page = {"page_id": page_id, "source_sha256": H, "image_sha256": "b" * 64, "receipt_sha256": "c" * 64,
            "geometry_candidate_sha256": "d" * 64, "width": 100, "height": 200}
    rows = [{"region_id": "region-%06d" % (i + 1), "box": [0, i * 10, 50, i * 10 + 9], "word_refs": [[i, 0]]}
            for i in range(count)]
    return regions.build_payload(page, rows)


def rows(statuses):
    return [{"region_id": "region-%06d" % (i + 1), "status": s, **({"text": "字%d" % i} if s == "transcribed" else {})}
            for i, s in enumerate(statuses)]


def candidate(page_id="P:p0001", statuses=("transcribed", "unreadable", "transcribed")):
    return regions.build_candidate(payload(page_id, len(statuses)), rows(statuses), model="m-1", session_id=SESSION)


class ContractTest(unittest.TestCase):
    def test_candidate_counts_flags_and_round_trip(self):
        c = candidate()
        self.assertEqual((c["region_count"], c["transcribed_count"], c["unreadable_count"]), (3, 2, 1))
        self.assertTrue(all(c[k] == v for k, v in OCR_FLAGS.items()))
        self.assertEqual(regions.validate_candidate(json.loads(json.dumps(c))), c)

    def test_response_must_list_every_region_in_order(self):
        p = payload()
        good = rows(["transcribed"] * 3)
        for bad, code in [(good[:2], "ocr_region_response_invalid"),
                          ([good[1], good[0], good[2]], "ocr_region_order_invalid"),
                          ([dict(good[0], status="unreadable"), *good[1:]], "ocr_region_response_invalid"),
                          ([dict(good[0], text=" "), *good[1:]], "ocr_region_text_invalid"),
                          ([dict(good[0], box=[0, 0, 1, 1]), *good[1:]], "ocr_region_response_invalid")]:
            with self.subTest(code=code), self.assertRaises(regions.RegionError) as caught:
                regions.validate_response({"regions": bad}, p)
            self.assertEqual(caught.exception.code, code)

    def test_parse_rejects_duplicate_keys_and_fences(self):
        p = payload(count=1)
        with self.assertRaises(regions.RegionError):
            regions.parse_response('{"regions":[],"regions":[]}', p)
        with self.assertRaises(regions.RegionError):
            regions.parse_response('```json\n{"regions":[]}\n```', p)

    def test_tampering_is_rejected(self):
        for change in ({"accepted": 1}, {"unreadable_count": 0}, {"width": 10}, {"extra": True}):
            with self.subTest(change=change), self.assertRaises(regions.RegionError):
                regions.validate_candidate({**candidate(), **change})
        tampered = candidate()
        tampered["regions"][1]["text"] = "guess"
        with self.assertRaises(regions.RegionError):
            regions.validate_candidate(tampered)

    def test_geometry_must_fit_page_and_refs_be_unique(self):
        page = {k: v for k, v in payload().items() if k not in ("schema", "regions")}
        for bad in ([{"region_id": "r1", "box": [0, 0, 101, 5], "word_refs": [[0, 0]]}],
                    [{"region_id": "r1", "box": [0, 0, 5, 5], "word_refs": [[0, 0]]},
                     {"region_id": "r2", "box": [0, 5, 5, 9], "word_refs": [[0, 0]]}],
                    [{"region_id": "r1", "box": [0, 0, 5, 5], "word_refs": [[True, 0]]}]):
            with self.subTest(bad=bad), self.assertRaises(regions.RegionError):
                regions.build_payload(page, bad)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "corpus"
        self.root.mkdir()
        self.path = Path(self.tmp.name) / "regions.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_store_must_be_outside_corpus(self):
        with self.assertRaises(regions.RegionError):
            regions.RegionStore.create(self.root / "regions.sqlite", corpus_root=self.root)

    def test_write_overview_list_read(self):
        with regions.RegionStore.create(self.path, corpus_root=self.root) as store:
            store.add_batch("b1", origin="test", created_at="2026-01-01T00:00:00Z", model="m-1")
            digests = [store.add_candidate("b1", candidate("P:p%04d" % i)) for i in range(3)]
            store.add_candidate("b1", candidate("P:p9999", ("transcribed",) * 3))
        with regions.RegionStore(self.path) as store:
            overview = store.overview()
            self.assertEqual((overview["candidates"], overview["fully_transcribed"], overview["partial"],
                              overview["unreadable_regions"]), (4, 1, 3, 3))
            page = store.list(offset=1, limit=2)
            self.assertEqual((page["total"], len(page["items"])), (4, 2))
            self.assertEqual(store.list(page_id="P:p0001")["items"][0]["candidate_sha256"], digests[1])
            read = store.read(digests[0])
            self.assertEqual(canonical_sha256(read["candidate"]), digests[0])
            with self.assertRaises(sqlite3.Error):
                store.connection.execute("DELETE FROM candidates")

    def test_database_constraints_and_tampered_bodies(self):
        with regions.RegionStore.create(self.path, corpus_root=self.root) as store:
            store.add_batch("b1", origin="test", created_at="t")
            digest = store.add_candidate("b1", candidate())
            with self.assertRaises(sqlite3.IntegrityError):
                store.add_candidate("b1", candidate())  # same page twice in one batch
            body = json.loads(store.connection.execute("SELECT body FROM candidates").fetchone()[0])
            body["regions"][0]["text"] = "changed"
            store.connection.execute("UPDATE candidates SET body=?", [json.dumps(body)])
            store.connection.commit()
        with regions.RegionStore(self.path) as store, self.assertRaises(regions.RegionError) as caught:
            store.read(digest)
        self.assertEqual(caught.exception.code, "ocr_region_candidate_changed")


class FakeDatabase:
    def __init__(self, sha=H):
        self.sha = sha

    def source_sha256(self, refresh=False):
        return self.sha


def reply(text):
    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": 5}}


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "corpus"
        self.root.mkdir()
        self.store = Path(self.tmp.name) / "regions.sqlite"
        self.corpus = mock.Mock(root=self.root)
        patches = [mock.patch.object(regions, "payload_from_reading_units",
                                     side_effect=lambda c, d, page_id, sha: payload(page_id, 2)),
                   mock.patch.object(runner, "_page_image", return_value=(b"img", "image/png"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def targets(self, n):
        return [{"id": "P:p%04d" % i, "candidate_sha256": "d" * 64} for i in range(n)]

    def run_with(self, responses, n=3, **kwargs):
        sent = []

        def transport(encoded, session):
            sent.append(json.loads(encoded))
            return responses.pop(0)
        result = runner.run(self.corpus, FakeDatabase(), self.targets(n), self.store, "m-1", transport=transport,
                            execute=True, **kwargs)
        return result, sent

    def test_requires_execute(self):
        with self.assertRaises(runner.RunError):
            runner.run(self.corpus, FakeDatabase(), self.targets(1), self.store, "m-1", transport=lambda *a: None)

    def test_valid_session_then_stop_on_first_invalid(self):
        ok = json.dumps({"regions": rows(["transcribed", "unreadable"])[:2]})
        result, sent = self.run_with([reply(ok), reply('{"regions": []}'), reply(ok)])
        self.assertEqual((result["valid"], result["failed"], result["unattempted"]), (1, 1, 1))
        self.assertEqual(result["stop_reason"], "ocr_region_response_invalid")
        self.assertEqual(len(sent[1]["messages"]), 3)  # second call carries the first exchange
        with regions.RegionStore(self.store) as store:
            overview = store.overview()
        self.assertEqual(overview["candidates"], 1)
        self.assertEqual(overview["batches"][0]["summary"]["stop_reason"], "ocr_region_response_invalid")
        self.assertNotIn("字", json.dumps(overview["batches"][0]["summary"], ensure_ascii=False))

    def test_transport_failure_is_outcome_unknown(self):
        def boom(encoded, session):
            raise ConnectionError
        result = runner.run(self.corpus, FakeDatabase(), self.targets(2), self.store, "m-1", transport=boom,
                            execute=True)
        self.assertEqual((result["unknown"], result["valid"]), (1, 0))
        self.assertEqual(result["stop_reason"], "transport_outcome_unknown")

    def test_source_change_stops_before_sending(self):
        database = FakeDatabase()
        calls = []

        def transport(encoded, session):
            calls.append(1)
            database.sha = "e" * 64
            return reply(json.dumps({"regions": rows(["transcribed", "transcribed"])}))
        result = runner.run(self.corpus, database, self.targets(2), self.store, "m-1", transport=transport,
                            execute=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["stop_reason"], "source_changed")
        self.assertFalse(result["source_unchanged"])


class WorkbenchTest(unittest.TestCase):
    def test_list_and_detail_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root = tmp / "corpus"
            root.mkdir()
            db = sqlite3.connect(root / "dialogue.sqlite")
            db.execute("CREATE TABLE scan_pages(id TEXT, image_path TEXT, image_sha256 TEXT, width INT, height INT)")
            db.execute("INSERT INTO scan_pages VALUES('P:p0001','x.png',?,100,200)", ["b" * 64])
            db.commit()
            db.close()
            c = candidate()
            with regions.RegionStore.create(tmp / "regions.sqlite", corpus_root=root) as store:
                store.add_batch("b1", origin="test", created_at="t")
                digest = store.add_candidate("b1", c)
            (tmp / "config.json").write_text(json.dumps({"corpora": [
                {"id": "x", "path": "corpus", "ocr_regions": "regions.sqlite"}]}), encoding="utf-8")
            workbench = wb.WorkbenchStore(wb.WorkbenchConfig.load(tmp / "config.json"))
            try:
                listing = workbench.ocr_regions({"corpus": "x"})
                self.assertEqual(listing["items"][0]["candidate_sha256"], digest)
                detail = workbench.ocr_region({"corpus": "x", "candidate_sha256": digest})
                self.assertTrue(detail["image_bound"])
                self.assertFalse(detail["source_bound"])  # synthetic source hash differs from the real file
                overview = workbench.overview()["corpora"][0]
                self.assertTrue(overview["capabilities"]["ocr_regions"])
                self.assertEqual(overview["ocr_regions"]["candidates"], 1)
                with self.assertRaises(wb.WorkbenchError):
                    workbench.ocr_region({"corpus": "x", "candidate_sha256": "f" * 64})
            finally:
                workbench.close()

    def test_retired_config_keys_fail_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "c").mkdir()
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"corpora": [{"id": "x", "path": "c", "ocr_region_status_candidates": "s"}]}))
            with self.assertRaises(wb.WorkbenchError) as caught:
                wb.WorkbenchConfig.load(path)
            self.assertEqual(caught.exception.code, "ocr_region_config_retired")


if __name__ == "__main__":
    unittest.main()
