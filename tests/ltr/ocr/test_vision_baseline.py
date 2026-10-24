"""Synthetic-only legacy OCR/page comparison; never reads a configured corpus."""
import copy
import hashlib
import http.client
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from urllib.parse import urlencode
from unittest.mock import patch

from ltr.ocr import vision_candidate as contract
from ltr.ocr import vision_index as index
from ltr import workbench as wb


class OCRVisionBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "source"
        self.scans = self.root / "private" / "scans"
        self.scans.mkdir(parents=True)
        self.directory = self.base / "external"
        self.directory.mkdir()
        self.image = self.scans / "page.png"
        # Binding checks inspect bytes and declared dimensions; no real image.
        self.image.write_bytes(b"synthetic image bytes only")
        self.image_hash = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.path = self.root / "dialogue.sqlite"
        with sqlite3.connect(self.path) as conn:
            conn.executescript("""
                CREATE TABLE scan_pages(id TEXT PRIMARY KEY, edition_id TEXT, image_path TEXT,
                    image_sha256 TEXT,width INTEGER,height INTEGER,ordinal INTEGER);
                CREATE TABLE paragraphs(id TEXT,edition_id TEXT,ordinal INTEGER,text TEXT,locator_json TEXT);
            """)
            conn.execute("INSERT INTO scan_pages VALUES (?,?,?,?,?,?,?)",
                         ("PAGE:one", "ED1", "page.png", self.image_hash, 100, 200, 7))
            conn.execute("INSERT INTO scan_pages VALUES (?,?,?,?,?,?,?)",
                         ("PAGE:two", "ED2", "page.png", self.image_hash, 100, 200, 8))
            conn.executemany("INSERT INTO paragraphs VALUES (?,?,?,?,?)", [
                ("1", "ED1", 20, "<script>synthetic older line</script>\nsecond", '{"page_id":"PAGE:one"}'),
                ("2", "ED1", 10, "synthetic earlier line", '{"nested":[{"image_id":"PAGE:one"}]}'),
                ("1", "ED2", 1, "MUST_NOT_EXPOSE_OTHER_EDITION", '{"page_id":"PAGE:two"}'),
                ("3", "ED1", 30, "MUST_NOT_EXPOSE_OTHER_PAGE", '{"page_id":"PAGE:two"}')])
        conn.close()
        self.corpus = wb.CorpusConfig("fixture", "Synthetic", self.root, ocr_vision_candidates=self.directory)
        self.reopen()
        manifest = dict(schema=contract.INPUT_SCHEMA, source_sha256=self.db.source_sha256(),
                        page_id="PAGE:one", image_sha256=self.image_hash, width=100, height=200)
        self.candidate = contract.create_candidate(manifest,
            {"segments": [{"order": 1, "kind": "main", "text": "synthetic new OCR", "bbox": [1, 1, 900, 900]}],
             "uncertain_regions": []}, model="fixture", session_id="synthetic")
        self.write_candidate()

    def reopen(self):
        self.store = wb.WorkbenchStore(wb.WorkbenchConfig([self.corpus], None))
        self.addCleanup(self.store.close)
        _, self.db = self.store.database("fixture")

    def write_candidate(self):
        raw = json.dumps(self.candidate).encode("utf-8")
        (self.directory / "candidate-one.json").write_bytes(raw)
        self.params = {"corpus": "fixture", "id": "candidate-one.json",
                       "candidate_sha256": hashlib.sha256(raw).hexdigest()}

    def change_db(self, sql, rows=None):
        self.store.close()
        with sqlite3.connect(self.path) as conn:
            if rows is None: conn.executescript(sql)
            else: conn.executemany(sql, rows)
        conn.close()
        self.reopen()
        self.candidate["source_sha256"] = self.db.source_sha256()
        self.write_candidate()

    def baseline(self, params=None):
        return self.store.ocr_vision_baseline(self.params if params is None else params)

    def assert_unavailable(self, code):
        result = self.baseline()
        self.assertFalse(result["available"])
        self.assertEqual(result["error"], code)
        self.assertIsNone(result["total_rows"])
        self.assertEqual(result["rows"], [])
        self.assertNotIn(str(self.base), json.dumps(result))
        return result

    def test_page_locator_local_keys_original_ordinal_exact_text_and_readonly(self):
        before = self.path.read_bytes()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = self.baseline()
        self.assertTrue(result["available"])
        self.assertEqual((result["total_rows"], result["matched_rows"], result["scanned_rows"]), (2, 2, 3))
        self.assertEqual(result["invalid_locator_count"], 0)
        self.assertEqual(result["page_id"], "PAGE:one")
        self.assertEqual(result["candidate_sha256"], self.params["candidate_sha256"])
        self.assertEqual(result["source_sha256"], self.candidate["source_sha256"])
        self.assertEqual(result["paragraph_scope"], "page_edition")
        self.assertEqual([(r["edition_id"], r["id"], r["ordinal"]) for r in result["rows"]], [("ED1", "2", 10), ("ED1", "1", 20)])
        self.assertEqual(result["rows"][1]["text"], "<script>synthetic older line</script>\nsecond")
        for row in result["rows"]:
            self.assertEqual(row["display_text_sha256"], hashlib.sha256(row["text"].encode()).hexdigest())
            self.assertFalse(row["text_truncated"])
        for secret in ("MUST_NOT_EXPOSE_OTHER_EDITION", "MUST_NOT_EXPOSE_OTHER_PAGE", str(self.base)):
            self.assertNotIn(secret, json.dumps(result))
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.db.connect().execute("PRAGMA query_only").fetchone()[0], 1)
        with self.assertRaises(sqlite3.OperationalError): self.db.connect().execute("DELETE FROM paragraphs")
        self.assertEqual(result["accepted"], 0)
        self.assertEqual(result["reference_eligible"], 0)

    def test_locator_only_scope_does_not_guess_from_paragraph_id(self):
        self.change_db("ALTER TABLE scan_pages DROP COLUMN edition_id")
        result = self.baseline()
        self.assertEqual(result["paragraph_scope"], "all_editions_locator")
        self.assertEqual(result["total_rows"], 2)
        self.assertEqual({row["edition_id"] for row in result["rows"]}, {"ED1"})

    def collection_schema(self):
        self.change_db("""
            ALTER TABLE scan_pages DROP COLUMN edition_id;
            ALTER TABLE scan_pages ADD COLUMN collection_id TEXT;
            UPDATE scan_pages SET collection_id='COL1' WHERE id='PAGE:one';
            UPDATE scan_pages SET collection_id='COL2' WHERE id='PAGE:two';
            CREATE TABLE editions(id TEXT,metadata_json TEXT);
            INSERT INTO editions VALUES ('ED1','{"collection_id":"COL1","derived_ocr":true}');
            INSERT INTO editions VALUES ('ED2','{"collection_id":"COL2","derived_ocr":true}');
            INSERT INTO editions VALUES ('ZH1','{}');
            INSERT INTO editions VALUES ('ZH2',NULL);
            INSERT INTO paragraphs VALUES ('1','ZH1',1,'PRIVATE_UNRELATED_CHINESE',NULL);
        """)

    def test_collection_metadata_maps_unique_edition_excludes_unrelated_bad_locators(self):
        self.collection_schema()
        before = self.path.read_bytes()
        result = self.baseline()
        self.assertTrue(result["available"])
        self.assertEqual(result["paragraph_scope"], "collection_edition")
        self.assertEqual(result["total_rows"], 2)
        self.assertEqual(result["scanned_rows"], 3)
        self.assertEqual(result["invalid_locator_count"], 0)
        self.assertTrue(result["locator_coverage_complete"])
        self.assertEqual({row["edition_id"] for row in result["rows"]}, {"ED1"})
        self.assertNotIn("PRIVATE_UNRELATED_CHINESE", json.dumps(result))
        self.assertEqual(self.path.read_bytes(), before)

    def test_collection_wrong_value_and_no_explicit_mapping_are_unavailable(self):
        self.collection_schema()
        self.change_db("UPDATE scan_pages SET collection_id='WRONG_COLLECTION' WHERE id='PAGE:one'")
        result = self.assert_unavailable("ocr_vision_baseline_edition_mapping_unavailable")
        self.assertIsNone(result["scanned_rows"])
        self.assertFalse(result["scan_complete"])
        self.change_db("UPDATE scan_pages SET collection_id='COL1' WHERE id='PAGE:one'; UPDATE editions SET metadata_json='{}'")
        self.assert_unavailable("ocr_vision_baseline_edition_mapping_unavailable")

    def test_collection_mapping_ambiguity_does_not_pick_arbitrary_edition(self):
        self.collection_schema()
        self.change_db("UPDATE editions SET metadata_json='{\"collection_id\":\"COL1\"}' WHERE id='ED2'")
        result = self.assert_unavailable("ocr_vision_baseline_edition_mapping_ambiguous")
        self.assertIsNone(result["matched_rows"])
        self.assertIsNone(result["invalid_locator_count"])
        self.change_db("UPDATE editions SET metadata_json='{\"collection_id\":\"COL2\"}' WHERE id='ED2';"
                       "INSERT INTO editions VALUES ('ED1','{\"collection_id\":\"COL_OTHER\"}')")
        self.assert_unavailable("ocr_vision_baseline_edition_mapping_ambiguous")

    def test_collection_metadata_is_strict_and_bounded_without_content_errors(self):
        self.collection_schema()
        for metadata in ("PRIVATE_MALFORMED_METADATA", "[]", '{"collection_id":false}',
                         '{"collection_id":"COL1","collection_id":"COL2"}',
                         '{"collection_id":"COL1","unknown":NaN}',
                         '{"collection_id":"COL1","unknown":Infinity}',
                         ' ' * (index.MAX_BASELINE_METADATA_CHARS + 1)):
            with self.subTest(length=len(metadata)):
                self.change_db("UPDATE editions SET metadata_json=? WHERE id='ED1'", [(metadata,)])
                result = self.assert_unavailable("ocr_vision_baseline_edition_mapping_invalid")
                self.assertIsNone(result["scanned_rows"])
                self.assertNotIn("PRIVATE_MALFORMED_METADATA", json.dumps(result))
        self.change_db("UPDATE editions SET metadata_json='{\"collection_id\":\"COL1\"}' WHERE id='ED1'")
        with patch.object(index, "MAX_BASELINE_EDITIONS", 2):
            self.assert_unavailable("ocr_vision_baseline_edition_mapping_limit_exceeded")

    def test_incomplete_collection_schema_retains_locator_only_fallback(self):
        self.collection_schema()
        self.change_db("DROP TABLE editions; DELETE FROM paragraphs WHERE edition_id='ZH1'")
        result = self.baseline()
        self.assertTrue(result["available"])
        self.assertEqual(result["paragraph_scope"], "all_editions_locator")
        self.assertEqual(result["total_rows"], 2)

    def test_candidate_source_page_image_and_required_hash_checked_before_scan(self):
        for changes, code in [({"candidate_sha256": None}, "ocr_vision_candidate_hash_required"),
                              ({"candidate_sha256": "0" * 64}, "ocr_vision_candidates_changed"),
                              ({"id": "../candidate-one.json"}, "ocr_vision_id_invalid")]:
            with self.subTest(code=code), patch.object(index, "read_baseline") as scanner:
                with self.assertRaises(wb.WorkbenchError) as caught: self.baseline({**self.params, **changes})
                self.assertEqual(caught.exception.code, code)
                scanner.assert_not_called()
        saved = copy.deepcopy(self.candidate)
        for field, value, code in [("source_sha256", "0" * 64, "ocr_vision_source_mismatch"),
                                   ("page_id", "missing", "ocr_vision_page_mismatch"),
                                   ("image_sha256", "0" * 64, "ocr_vision_image_mismatch")]:
            with self.subTest(field=field):
                self.candidate = dict(saved, **{field: value}); self.write_candidate()
                with patch.object(index, "read_baseline") as scanner, self.assertRaises(wb.WorkbenchError) as caught:
                    self.baseline()
                self.assertEqual(caught.exception.code, code)
                scanner.assert_not_called()

    def test_selected_candidate_is_rechecked_after_scan(self):
        original = index.read_baseline
        def changed(*args):
            result = original(*args)
            self.candidate["segments"][0]["text"] = "changed synthetic candidate"
            self.write_candidate()
            return result
        params = dict(self.params)
        with patch.object(index, "read_baseline", side_effect=changed), self.assertRaises(wb.WorkbenchError) as caught:
            self.baseline(params)
        self.assertEqual(caught.exception.code, "ocr_vision_candidates_changed")

    def test_source_is_rechecked_after_scan(self):
        original = index.read_baseline
        def changed(*args):
            result = original(*args)
            self.db.close()
            with sqlite3.connect(self.path) as conn: conn.execute("UPDATE paragraphs SET text='changed synthetic text'")
            conn.close()
            return result
        with patch.object(index, "read_baseline", side_effect=changed), self.assertRaises(wb.WorkbenchError) as caught:
            self.baseline()
        self.assertEqual(caught.exception.code, "ocr_vision_source_mismatch")

    def test_missing_paragraphs_and_missing_locator_have_unknown_totals(self):
        self.change_db("DROP TABLE paragraphs")
        self.assert_unavailable("ocr_vision_baseline_paragraphs_unavailable")
        self.change_db("CREATE TABLE paragraphs(id TEXT,edition_id TEXT,ordinal INTEGER,text TEXT)")
        self.assert_unavailable("ocr_vision_baseline_locator_unavailable")

    def test_invalid_empty_large_and_duplicate_key_locators_are_not_complete_zero(self):
        locators = [None, "not-json-PRIVATE_LOCATOR", "{}", "[]", "null",
                    "x" * (index.MAX_BASELINE_LOCATOR_CHARS + 1), '{"page_id":"PAGE:one","page_id":"PAGE:two"}']
        self.change_db("DELETE FROM paragraphs")
        self.change_db("INSERT INTO paragraphs VALUES (?,?,?,?,?)",
                       [(str(i), "ED1", i, "synthetic", locator) for i, locator in enumerate(locators)])
        result = self.assert_unavailable("ocr_vision_baseline_locator_unavailable")
        self.assertEqual(result["invalid_locator_count"], len(locators))
        self.assertTrue(result["scan_complete"])
        self.assertFalse(result["locator_coverage_complete"])
        self.assertNotIn("PRIVATE_LOCATOR", json.dumps(result))

    def test_mixed_bad_locator_reports_lower_bound_not_total(self):
        self.change_db("INSERT INTO paragraphs VALUES ('bad','ED1',0,'PRIVATE_BAD_LOCATOR_TEXT','not-json')")
        result = self.baseline()
        self.assertTrue(result["available"])
        self.assertEqual(result["matched_rows"], 2)
        self.assertIsNone(result["total_rows"])
        self.assertEqual(result["invalid_locator_count"], 1)
        self.assertFalse(result["locator_coverage_complete"])
        self.assertNotIn("PRIVATE_BAD_LOCATOR_TEXT", json.dumps(result))

    def test_scan_limit_prevents_text_scan_and_returns_unknown_count(self):
        with patch.object(index, "MAX_BASELINE_PARAGRAPHS", 2):
            result = self.assert_unavailable("ocr_vision_baseline_scan_limit_exceeded")
        self.assertIsNone(result["scanned_rows"])
        self.assertIsNone(result["matched_rows"])
        self.assertIsNone(result["invalid_locator_count"])
        self.assertFalse(result["scan_complete"])

    def test_row_and_character_limits_preserve_display_prefix_hash(self):
        self.change_db("DELETE FROM paragraphs")
        self.change_db("INSERT INTO paragraphs VALUES (?,?,?,?,?)", [
            (str(i), "ED1", i, "😀" * 1100, '{"page_id":"PAGE:one"}') for i in range(105)])
        result = self.baseline()
        self.assertEqual(result["total_rows"], 105)
        self.assertEqual(len(result["rows"]), 50)
        self.assertEqual(sum(len(r["text"]) for r in result["rows"]), 50000)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["text_hash_scope"], "displayed_text")
        for row in result["rows"]:
            self.assertEqual(len(row["text"]), 1000)
            self.assertEqual(row["original_text_chars"], 1100)
            self.assertTrue(row["text_truncated"])
            self.assertEqual(row["display_text_sha256"], hashlib.sha256(row["text"].encode()).hexdigest())
        self.change_db("UPDATE paragraphs SET text='short'")
        result = self.baseline()
        self.assertEqual(len(result["rows"]), 100)
        self.assertTrue(result["truncated"])

    def test_ambiguous_local_key_refused_and_errors_do_not_leak_sql(self):
        self.change_db("INSERT INTO paragraphs VALUES ('1','ED1',21,'PRIVATE_DUPLICATE','{\"page_id\":\"PAGE:two\"}')")
        self.assert_unavailable("ocr_vision_baseline_row_ambiguous")
        with patch.object(self.db, "connect", side_effect=sqlite3.OperationalError("PRIVATE_SQL /secret/path SELECT text")):
            result = index.read_baseline(self.db, "PAGE:one")
        self.assertEqual(result["error"], "ocr_vision_baseline_unavailable")
        for secret in ("PRIVATE_SQL", "/secret/path", "SELECT text"):
            self.assertNotIn(secret, json.dumps(result))

    def test_empty_scope_known_zero_and_other_page_has_no_text_leak(self):
        self.change_db("DELETE FROM paragraphs WHERE edition_id='ED1'")
        result = self.baseline()
        self.assertTrue(result["available"])
        self.assertEqual(result["total_rows"], 0)
        self.assertTrue(result["scan_complete"])
        self.assertFalse(result["truncated"])

    def test_http_get_is_bound_readonly_and_no_post_or_logs(self):
        before = self.path.read_bytes()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            server = wb.build_server(self.store.config)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
                try:
                    route = "/api/ocr-vision-baseline?" + urlencode(self.params)
                    connection.request("GET", route)
                    response = connection.getresponse(); data = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                    self.assertEqual(data["total_rows"], 2)
                    connection.request("POST", "/api/ocr-vision-baseline", body=b"{}", headers={"Content-Type": "application/json"})
                    response = connection.getresponse(); response.read()
                    self.assertEqual(response.status, 404)
                finally: connection.close()
            finally:
                server.shutdown(); server.server_close(); thread.join()
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
