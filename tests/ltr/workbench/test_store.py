"""Standard-library unittest suite for ``ltr.workbench``.

Every test builds a temporary, synthetic corpus database and review directory
under ``tempfile.mkdtemp``; no real corpus data is ever opened.
"""

from __future__ import annotations

import http.client
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from ltr import workbench as wb  # noqa: E402

FULL_SCHEMA = """
CREATE TABLE editions (id TEXT PRIMARY KEY, book_key TEXT, title TEXT,
                       language TEXT, origin_kind TEXT);
CREATE TABLE dialogues (id TEXT PRIMARY KEY, edition_id TEXT, ordinal INTEGER,
                        text TEXT, speaker TEXT, interlocutor TEXT,
                        attribution_status TEXT, start_para INTEGER,
                        end_para INTEGER, context_hash TEXT);
CREATE TABLE paragraphs (id TEXT PRIMARY KEY, edition_id TEXT, ordinal INTEGER,
                         text TEXT, locator_json TEXT);
CREATE TABLE entries (id TEXT PRIMARY KEY, reference_eligible INTEGER,
                      attribution_status TEXT, alignment_status TEXT);
CREATE TABLE alignments (chinese_id TEXT, japanese_ids_json TEXT, status TEXT,
                         method TEXT, confidence REAL);
CREATE TABLE attribution_candidates (dialogue_id TEXT, speaker_canonical TEXT,
                        speaker_surface TEXT, addressee_canonical TEXT,
                        addressee_surface TEXT, human_confirmed INTEGER,
                        semantic_verified INTEGER, basis_detector TEXT,
                        created_at TEXT);
CREATE TABLE scan_pages (id TEXT PRIMARY KEY, ordinal INTEGER,
                         ocr_status TEXT, image_path TEXT);
"""

DIALOGUE_ROWS = [
    ("d1", "ed1", 1, "needle here", "Speaker A", "Speaker B", "guess", 1, 1, "ch1"),
    ("d2", "ed1", 2, "100% sure", "Speaker A", "Speaker B", "guess", 2, 2, "ch2"),
    ("d3", "ed1", 3, "under_score", "Speaker A", None, None, 3, 3, "ch3"),
    ("d4", "ed1", 4, "undersXcore", None, None, None, 4, 4, "ch4"),
    ("d5", "ed1", 5, "plain text", "OldSpeaker", "OldOther", "auto", 5, 5, "ch5"),
]

SCAN_PAGES = [
    ("p1", 1, "completed", "vol1/page.png"),
    ("p2", 2, "completed", "../escape.png"),
    ("p3", 3, "completed", "/abs/page.png"),
    ("p4", 4, "completed", "notes.txt"),
]


def seed_full(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO editions VALUES (?,?,?,?,?)",
        ("ed1", "bk1", "Title One", "zh", "manuscript"),
    )
    conn.executemany("INSERT INTO dialogues VALUES (?,?,?,?,?,?,?,?,?,?)", DIALOGUE_ROWS)
    conn.execute(
        "INSERT INTO alignments VALUES (?,?,?,?,?)",
        ("e1", json.dumps(["d1"]), "aligned", "manual", 0.9),
    )
    conn.execute("INSERT INTO entries VALUES (?,1,'ok','aligned')", ("e1",))
    # Candidate for d5 with an *unknown* speaker; base speaker is OldSpeaker.
    conn.execute(
        "INSERT INTO attribution_candidates VALUES (?,?,?,?,?,?,?,?,?)",
        ("d5", "", "", "", "", 0, 0, "det-v1", "2024-01-01T00:00:00Z"),
    )
    conn.executemany("INSERT INTO scan_pages VALUES (?,?,?,?)", SCAN_PAGES)
    for i in range(1, 6):
        locator = '{"page_id": "p1"}' if i == 5 else None
        conn.execute(
            "INSERT INTO paragraphs VALUES (?,?,?,?,?)",
            (f"par{i}", "ed1", i, f"paragraph {i}", locator),
        )


class WorkbenchTestCase(unittest.TestCase):
    """Shared synthetic environment: one corpus DB plus a review directory."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wb-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "corpus"
        self.root.mkdir()
        self.review_dir = self.tmp / "reviews"
        self.review_dir.mkdir()
        self._make_db(self.root / "dialogue.sqlite")
        self.config = wb.WorkbenchConfig(
            [wb.CorpusConfig("c1", "Corpus One", self.root)], self.review_dir
        )
        self.store = wb.WorkbenchStore(self.config)
        self.addCleanup(self.store.close)

    def _make_db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.executescript(FULL_SCHEMA)
        seed_full(conn)
        conn.commit()
        conn.close()

    def _save(self, dialogue_id: str = "d1", verdict: str = "correct", **extra):
        body = {
            "corpus": "c1",
            "dialogue_id": dialogue_id,
            "verdict": verdict,
            "speaker": "",
            "addressee": "",
            "note": "",
        }
        body.update(extra)
        return self.store.save_review(body)

    def _document(self, fingerprint: str) -> dict:
        return {
            "schema": wb.ReviewStore.SCHEMA,
            "corpus": "c1",
            "dialogue_id": "d1",
            "corpus_fingerprint": fingerprint,
            "context_hash": "ch1",
            "verdict": "correct",
            "speaker": "",
            "addressee": "",
            "note": "",
            "created_at": "2024-01-01T00:00:00Z",
        }


class ReadOnlyDatabaseTests(WorkbenchTestCase):
    def test_database_connection_is_read_only(self) -> None:
        _, db = self.store.database("c1")
        conn = db.connect()
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE evil(x)")
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO dialogues VALUES ('x',NULL,1,'t',NULL,NULL,NULL,1,1,'c')"
            )

    def test_fingerprint_stable_after_reads_and_review_save(self) -> None:
        _, db = self.store.database("c1")
        before = db.fingerprint()
        self.assertEqual(len(before), 64)
        self.store.overview()
        self.store.editions("c1")
        self.store.dialogues({"corpus": "c1"})
        self.store.dialogue({"corpus": "c1", "id": "d1"})
        self._save()
        self.assertEqual(db.fingerprint(), before)

    def test_missing_database_is_never_created(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        cfg = wb.WorkbenchConfig([wb.CorpusConfig("c2", "Empty", empty)], self.review_dir)
        store = wb.WorkbenchStore(cfg)
        self.addCleanup(store.close)
        with self.assertRaises(wb.WorkbenchError) as ctx:
            store.database("c2")
        self.assertEqual(ctx.exception.code, "database_missing")
        overview = store.overview()["corpora"][0]
        self.assertEqual(
            overview["counts"],
            {"editions": 0, "paragraphs": 0, "dialogues": 0, "entries": 0, "alignments": 0},
        )
        self.assertFalse(overview["capabilities"]["attribution"])
        self.assertFalse((empty / "dialogue.sqlite").exists())

    def test_unknown_or_invalid_corpus_rejected(self) -> None:
        with self.assertRaises(wb.WorkbenchError) as ctx:
            self.store.database("nope")
        self.assertEqual(ctx.exception.code, "unknown_corpus")
        with self.assertRaises(wb.WorkbenchError):
            self.store.dialogues({"corpus": "../evil"})

    def test_config_load_rejects_invalid_json(self) -> None:
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with self.assertRaises(wb.WorkbenchError) as ctx:
            wb.WorkbenchConfig.load(bad)
        self.assertEqual(ctx.exception.code, "config_invalid")


class SchemaAndSearchTests(WorkbenchTestCase):
    def test_schema_with_optional_tables_missing(self) -> None:
        mini = self.tmp / "mini"
        mini.mkdir()
        conn = sqlite3.connect(mini / "dialogue.sqlite")
        conn.executescript(
            "CREATE TABLE dialogues (id TEXT PRIMARY KEY, edition_id TEXT,"
            " ordinal INTEGER, text TEXT, speaker TEXT, interlocutor TEXT,"
            " attribution_status TEXT, start_para INTEGER, end_para INTEGER,"
            " context_hash TEXT);"
            "INSERT INTO dialogues VALUES ('d1',NULL,1,'only table','S','T','guess',1,1,'ch');"
        )
        conn.commit()
        conn.close()
        cfg = wb.WorkbenchConfig([wb.CorpusConfig("m1", "Mini", mini)], None)
        store = wb.WorkbenchStore(cfg)
        self.addCleanup(store.close)
        listing = store.dialogues({"corpus": "m1"})
        self.assertEqual([item["id"] for item in listing["items"]], ["d1"])
        detail = store.dialogue({"corpus": "m1", "id": "d1"})
        self.assertEqual(detail["item"]["speaker"], "S")
        self.assertIsNone(detail["alignment"])
        self.assertIsNone(detail["entry"])
        self.assertEqual(detail["scan_pages"], [])
        self.assertEqual(detail["context"], [])
        self.assertFalse(store.overview()["corpora"][0]["capabilities"]["attribution"])

    def test_search_parameterisation_escapes_like_wildcards(self) -> None:
        def ids_for(q: str) -> list[str]:
            return [i["id"] for i in self.store.dialogues({"corpus": "c1", "q": q})["items"]]

        self.assertEqual(ids_for("needle"), ["d1"])
        self.assertEqual(ids_for("%"), ["d2"])  # literal percent, not a wildcard
        self.assertEqual(ids_for("under_score"), ["d3"])  # underscore literal
        self.assertEqual(ids_for("undersXcore"), ["d4"])


class AttributionTests(WorkbenchTestCase):
    def test_unknown_candidate_speaker_does_not_fall_back(self) -> None:
        detail = self.store.dialogue({"corpus": "c1", "id": "d5"})
        item = detail["item"]
        self.assertEqual(item["speaker"], "")
        self.assertEqual(item["addressee"], "")
        self.assertEqual(item["attribution_status"], "candidate")
        # The legacy base attribution is preserved only as provenance.
        self.assertEqual(detail["attribution"]["base"]["speaker"], "OldSpeaker")
        listing = self.store.dialogues({"corpus": "c1"})
        by_id = {i["id"]: i for i in listing["items"]}
        self.assertEqual(by_id["d5"]["speaker"], "")


class ScanTests(WorkbenchTestCase):
    def test_scan_image_containment(self) -> None:
        scans = self.root / "private" / "scans"
        (scans / "vol1").mkdir(parents=True)
        (scans / "vol1" / "page.png").write_bytes(b"png-bytes")
        (self.root / "escape.png").write_bytes(b"escaped")  # exists, outside scans
        data, content_type = self.store.scan_image({"corpus": "c1", "page": "p1"})
        self.assertEqual((data, content_type), (b"png-bytes", "image/png"))
        for page in ("p2", "p3", "p4", "missing"):
            with self.assertRaises(wb.WorkbenchError) as ctx:
                self.store.scan_image({"corpus": "c1", "page": page})
            self.assertEqual(ctx.exception.code, "scan_unavailable")


class ReviewTests(WorkbenchTestCase):
    def test_review_sanitiser_never_exposes_accepted_labels(self) -> None:
        result = self._save(verdict="incorrect", note="n")
        review = result["review"]
        self.assertEqual(review["verdict"], "incorrect")
        self.assertEqual(review["version"], 1)
        self.assertFalse(any(review["flags"].values()))
        for key in ("accepted", "labels_accepted", "human_confirmed", "reference_eligible"):
            self.assertNotIn(key, review)
        stored = self.store.reviews.get("c1", "d1")
        self.assertIsNotNone(stored)
        self.assertFalse(stored["labels_accepted"])

    def test_reviews_disabled_without_review_dir(self) -> None:
        cfg = wb.WorkbenchConfig([wb.CorpusConfig("c1", "Corpus One", self.root)], None)
        store = wb.WorkbenchStore(cfg)
        self.addCleanup(store.close)
        with self.assertRaises(wb.WorkbenchError) as ctx:
            store.save_review({"corpus": "c1", "dialogue_id": "d1", "verdict": "correct"})
        self.assertEqual(ctx.exception.code, "reviews_disabled")
        self.assertEqual(store.review_export({"corpus": "c1"})["reviews"], [])

    def test_review_store_rejects_stale_fingerprint(self) -> None:
        reviews = wb.ReviewStore(self.review_dir)
        reviews.save(self._document("fp-a"))
        with self.assertRaises(wb.WorkbenchError) as ctx:
            reviews.save(self._document("fp-b"))
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "stale_review"))

    def test_review_store_rebind_records_previous_fingerprint(self) -> None:
        reviews = wb.ReviewStore(self.review_dir)
        first = reviews.save(self._document("fp-a"))
        rebound = reviews.save(self._document("fp-b"), rebind=True)
        self.assertEqual(rebound["version"], 2)
        self.assertEqual(rebound["rebound_from_fingerprint"], "fp-a")
        self.assertEqual(rebound["created_at"], first["created_at"])

    def test_stale_review_rejected_after_database_change(self) -> None:
        self._save()
        conn = sqlite3.connect(self.root / "dialogue.sqlite")
        conn.execute(
            "INSERT INTO dialogues VALUES ('d9','ed1',9,?,'S','T','guess',9,9,'ch9')",
            ("x" * 2000,),
        )
        conn.commit()
        conn.close()
        with self.assertRaises(wb.WorkbenchError) as ctx:
            self._save()
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "stale_review"))


class ServerConstructionTests(WorkbenchTestCase):
    def test_build_server_validates_port_and_host(self) -> None:
        for port in (70000, -1, True, "80"):
            with self.assertRaises(wb.WorkbenchError):
                wb.build_server(self.config, port)  # type: ignore[arg-type]
        server = wb.build_server(self.config, 0)
        self.addCleanup(server.server_close)
        self.assertIn(server.server_address[0], ("127.0.0.1", "localhost"))
        with self.assertRaises(wb.WorkbenchError):
            wb.WorkbenchServer(0, self.store, host="0.0.0.0")


class HttpServerTests(WorkbenchTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = wb.build_server(self.config, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)
        self.port = int(self.server.server_address[1])

    def _request(self, method: str, path: str, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response.status, payload

    def test_http_rejects_foreign_host_header(self) -> None:
        status, payload = self._request("GET", "/api/overview", {"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertIn(b"host_not_allowed", payload)
        status, _ = self._request("GET", "/api/overview", {"Host": "192.168.0.1:8080"})
        self.assertEqual(status, 403)
        status, _ = self._request("GET", "/api/overview")
        self.assertEqual(status, 200)

    def test_http_rejects_cross_origin_and_cross_site(self) -> None:
        ok_origin = f"http://127.0.0.1:{self.port}"
        status, _ = self._request("GET", "/api/overview", {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self._request("GET", "/api/overview", {"Origin": "null"})
        self.assertEqual(status, 403)
        status, payload = self._request(
            "GET", "/api/overview",
            {"Origin": ok_origin, "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(status, 403)
        self.assertIn(b"cross_site_blocked", payload)
        status, _ = self._request("GET", "/api/overview", {"Origin": ok_origin})
        self.assertEqual(status, 200)

    def test_http_post_review_same_origin_only(self) -> None:
        origin = f"http://127.0.0.1:{self.port}"
        body = json.dumps(
            {"corpus": "c1", "dialogue_id": "d1", "verdict": "correct"}
        ).encode("utf-8")
        headers = {"Origin": origin, "Content-Type": "application/json"}
        status, payload = self._request("POST", "/api/reviews", headers, body)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(payload)["saved"])
        # Missing Origin (or wrong type) refuses the mutating endpoint.
        status, _ = self._request(
            "POST", "/api/reviews",
            {"Content-Type": "application/json"}, body,
        )
        self.assertEqual(status, 403)
        status, _ = self._request(
            "POST", "/api/reviews",
            {"Origin": origin, "Content-Type": "text/plain"}, body,
        )
        self.assertEqual(status, 415)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
