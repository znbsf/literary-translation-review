"""Only synthetic candidate, SQLite and image fixtures; no runtime corpus access."""
import copy
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import struct
import zlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from ltr.ocr import vision_candidate as contract
from ltr.ocr import vision_index as index
from ltr import workbench as wb


class OCRVisionIndexTests(unittest.TestCase):
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
        def chunk(kind, body):
            return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        self.image.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 100, 200, 8, 2, 0, 0, 0)) +
                               chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\xff\xff" * 100) * 200)) + chunk(b"IEND", b""))
        self.image_hash = hashlib.sha256(self.image.read_bytes()).hexdigest()
        with sqlite3.connect(self.root / "dialogue.sqlite") as conn:
            conn.execute("CREATE TABLE scan_pages(id TEXT PRIMARY KEY, image_path TEXT,image_sha256 TEXT,width INTEGER,height INTEGER,ordinal INTEGER)")
            conn.execute("INSERT INTO scan_pages VALUES (?,?,?,?,?,?)", ("JA_FIX:p0001", "page.png", self.image_hash, 100, 200, 1))
        conn.close()
        self.corpus = wb.CorpusConfig("fixture", "Synthetic", self.root, ocr_vision_candidates=self.directory)
        self.store = wb.WorkbenchStore(wb.WorkbenchConfig([self.corpus], None))
        self.addCleanup(self.store.close)
        _, self.db = self.store.database("fixture")
        self.manifest = dict(schema=contract.INPUT_SCHEMA, source_sha256=self.db.source_sha256(), page_id="JA_FIX:p0001", image_sha256=self.image_hash, width=100, height=200)
        self.candidate = contract.create_candidate(self.manifest, {"segments": [{"order": 1, "kind": "main", "text": "<script>synthetic OCR</script>", "bbox": [10, 20, 900, 990]}], "uncertain_regions": [{"bbox": [1, 2, 30, 40], "reason": "synthetic uncertainty"}]}, model="fixture-model", session_id="fixture-session")
        self.write()
        self.params = {"corpus": "fixture", "id": "candidate-one.json"}

    def write(self, candidate=None, name="candidate-one.json"):
        (self.directory / name).write_text(json.dumps(self.candidate if candidate is None else candidate), encoding="utf-8")

    def assert_error(self, code):
        with self.assertRaises(index.OCRVisionIndexError) as caught:
            index.read_candidates(self.corpus, self.db)
        self.assertEqual(str(caught.exception), code)
        self.assertNotIn(str(self.base), str(caught.exception))

    def test_overview_and_detail_are_separate_readonly_projections(self):
        before = self.db.path.read_bytes()
        overview = index.overview(self.corpus, self.db)
        self.assertEqual((overview["candidate_count"], overview["page_count"]), (1, 1))
        self.assertEqual(overview["reference_eligible"], 0)
        listing = self.store.ocr_vision_candidates({"corpus": "fixture"})
        self.assertNotIn("synthetic OCR", json.dumps(listing))
        self.assertNotIn("image_path", json.dumps(listing))
        detail = self.store.ocr_vision_candidate(self.params)
        self.assertEqual(detail["candidate"]["segments"], self.candidate["segments"])
        self.assertEqual(self.db.path.read_bytes(), before)
        self.assertEqual(self.db.connect().execute("PRAGMA query_only").fetchone()[0], 1)
        with self.assertRaises(sqlite3.OperationalError):
            self.db.connect().execute("DELETE FROM scan_pages")

    def test_unconfigured_is_not_measured_zero(self):
        self.corpus.ocr_vision_candidates = None
        self.assertEqual(index.overview(self.corpus, self.db), {"available": False, "error": None})
        self.assertEqual(self.store.ocr_vision_candidates({"corpus": "fixture"}), {"available": False, "error": None})

    def test_empty_directory_is_available_without_completion_claim(self):
        (self.directory / "candidate-one.json").unlink()
        result = index.overview(self.corpus, self.db)
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["human_verified"], 0)

    def test_binding_and_contract_fail_closed(self):
        for key, value, code in [("source_sha256", "0" * 64, "ocr_vision_source_mismatch"), ("page_id", "missing", "ocr_vision_page_mismatch"), ("image_sha256", "0" * 64, "ocr_vision_image_mismatch"), ("width", 101, "ocr_vision_candidate_invalid"), ("accepted", 1, "ocr_vision_candidate_invalid")]:
            with self.subTest(key=key):
                candidate = copy.deepcopy(self.candidate); candidate[key] = value
                self.write(candidate); self.assert_error(code)
                self.assertFalse(index.overview(self.corpus, self.db)["available"])

    def test_actual_image_hash_required(self):
        self.image.write_bytes(b"changed synthetic image")
        self.assert_error("ocr_vision_image_mismatch")

    def test_image_endpoint_requires_hash_and_revalidates_bytes(self):
        with self.assertRaises(wb.WorkbenchError) as caught:
            self.store.ocr_vision_image(self.params)
        self.assertEqual(caught.exception.code, "ocr_vision_image_hash_required")
        params = {**self.params, "image_sha256": self.image_hash}
        self.assertEqual(self.store.ocr_vision_image(params)[0], self.image.read_bytes())
        self.image.write_bytes(b"mutated")
        with self.assertRaises(wb.WorkbenchError) as caught:
            self.store.ocr_vision_image(params)
        self.assertEqual(caught.exception.code, "ocr_vision_image_mismatch")

    def test_changed_selected_candidate_refused(self):
        item = self.store.ocr_vision_candidate(self.params)
        self.candidate["segments"][0]["text"] = "new synthetic text"
        self.write()
        with self.assertRaises(wb.WorkbenchError) as caught:
            self.store.ocr_vision_candidate({**self.params, "candidate_sha256": item["candidate_sha256"]})
        self.assertEqual(caught.exception.code, "ocr_vision_candidates_changed")

    def test_candidate_id_is_filename_only(self):
        for name in ["../candidate-one.json", "/candidate-one.json", "candidate-one.json/..", "other.json"]:
            with self.subTest(name=name), self.assertRaises(wb.WorkbenchError):
                self.store.ocr_vision_candidate({**self.params, "id": name})

    def test_pagination_is_bounded_and_deterministic(self):
        for number in range(24): self.write(name="candidate-%02d.json" % number)
        listing = self.store.ocr_vision_candidates({"corpus": "fixture", "offset": "20", "limit": "3"})
        self.assertEqual(listing["total"], 25)
        self.assertEqual(len(listing["items"]), 3)
        self.assertEqual(listing["items"][0]["id"], "candidate-20.json")
        self.assertEqual(self.store.ocr_vision_candidates({"corpus": "fixture", "limit": "999"})["limit"], 50)
        for value in ["-1", "bad", True]:
            with self.assertRaises(wb.WorkbenchError): self.store.ocr_vision_candidates({"corpus": "fixture", "offset": value})

    def test_limits_reject_whole_directory(self):
        for constant, value in [("MAX_FILES", 0), ("MAX_FILE_BYTES", 10), ("MAX_TOTAL_BYTES", 10), ("MAX_DIRECTORY_ENTRIES", 0), ("MAX_TOTAL_IMAGE_BYTES", 1)]:
            with self.subTest(constant=constant), patch.object(index, constant, value):
                self.assert_error("ocr_vision_limits_exceeded")

    def test_nested_directory_and_externality(self):
        (self.directory / "nested").mkdir()
        self.assert_error("ocr_vision_path_unsafe")
        with self.assertRaises(index.OCRVisionIndexError): index.validate_directory_path(self.root / "candidates", self.root)

    def test_symlink_candidate_refused(self):
        link = self.directory / "candidate-link.json"
        try: link.symlink_to(self.directory / "candidate-one.json")
        except OSError: self.skipTest("platform does not permit symlink creation")
        self.assert_error("ocr_vision_path_unsafe")

    def test_image_traversal_refused(self):
        self.db.close()
        with sqlite3.connect(self.db.path) as conn: conn.execute("UPDATE scan_pages SET image_path='../outside.png'")
        conn.close()
        self.candidate["source_sha256"] = self.db.source_sha256(); self.write()
        self.assert_error("ocr_vision_image_unavailable")

    def test_candidate_change_during_read_refused(self):
        original = index._bytes
        changed = False
        def read(path, maximum):
            nonlocal changed
            raw = original(path, maximum)
            if path.name == "candidate-one.json" and not changed:
                changed = True
                self.candidate["segments"][0]["text"] = "changed during read"
                self.write()
            return raw
        with patch.object(index, "_bytes", side_effect=read): self.assert_error("ocr_vision_candidates_changed")

    def test_source_change_during_read_refused(self):
        baseline = self.db.source_sha256()
        with patch.object(self.db, "source_sha256", side_effect=[baseline, "0" * 64]): self.assert_error("ocr_vision_source_mismatch")

    def test_journal_guard(self):
        Path(str(self.db.path) + "-wal").write_bytes(b"synthetic")
        self.assert_error("ocr_vision_source_unavailable")

    def test_duplicate_json_keys_refused(self):
        (self.directory / "candidate-one.json").write_text('{"source_sha256":"' + self.manifest["source_sha256"] + '","source_sha256":"duplicate"}')
        self.assert_error("ocr_vision_candidate_invalid")

    def test_config_external_relative_directory_and_missing_directory(self):
        config = self.base / "workbench.json"
        config.write_text(json.dumps({"corpora": [{"id": "fixture", "name": "Synthetic", "path": "source", "ocr_vision_candidates": "external"}]}))
        loaded = wb.WorkbenchConfig.load(config)
        self.assertEqual(loaded.corpora[0].ocr_vision_candidates, self.directory)
        self.corpus.ocr_vision_candidates = self.base / "not-created"
        self.assertEqual(index.overview(self.corpus, self.db), {"available": False, "error": "ocr_vision_directory_unavailable"})
        self.assertFalse(self.corpus.ocr_vision_candidates.exists())
        for value in [True, "", "source/private/candidates"]:
            config.write_text(json.dumps({"corpora": [{"id": "fixture", "path": "source", "ocr_vision_candidates": value}]}))
            with self.assertRaises(wb.WorkbenchError): wb.WorkbenchConfig.load(config)

    def test_reparse_attribute_rejected_even_without_symlink_permission(self):
        original = Path.lstat
        def attribute(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == self.directory:
                class Reparse:
                    st_file_attributes = 0x400
                    st_mode = result.st_mode
                return Reparse()
            return result
        with patch.object(Path, "lstat", attribute): self.assert_error("ocr_vision_path_unsafe")

    def test_added_file_during_read_refused(self):
        original = index._bytes
        added = False
        def read(path, maximum):
            nonlocal added
            raw = original(path, maximum)
            if path.name == "candidate-one.json" and not added:
                added = True; self.write(name="candidate-added.json")
            return raw
        with patch.object(index, "_bytes", side_effect=read): self.assert_error("ocr_vision_candidates_changed")

    def test_image_change_during_read_refused(self):
        original = index._bytes
        changed = False
        def read(path, maximum):
            nonlocal changed
            raw = original(path, maximum)
            if path == self.image and not changed:
                changed = True; self.image.write_bytes(b"changed during image read")
            return raw
        with patch.object(index, "_bytes", side_effect=read): self.assert_error("ocr_vision_image_mismatch")

    def test_missing_scan_metadata_and_unsafe_text_fail_closed(self):
        with patch.object(self.db, "columns", return_value={"id"}):
            self.assert_error("ocr_vision_scan_metadata_unavailable")
        for value in ["\ud800", "unsafe\u202e", "x" * (contract.MAX_SEGMENT_TEXT_CHARS + 1)]:
            candidate = copy.deepcopy(self.candidate); candidate["segments"][0]["text"] = value
            self.write(candidate); self.assert_error("ocr_vision_candidate_invalid")

    def test_image_response_rechecks_source_after_read(self):
        original = self.store.scan_image
        def read(params):
            result = original(params)
            self.db.close()
            with sqlite3.connect(self.db.path) as conn:
                conn.execute("UPDATE scan_pages SET ordinal=2")
            conn.close()
            return result
        with patch.object(self.store, "scan_image", side_effect=read):
            with self.assertRaises(wb.WorkbenchError) as caught:
                self.store.ocr_vision_image({**self.params, "image_sha256": self.image_hash})
        self.assertEqual(caught.exception.code, "ocr_vision_source_mismatch")

    def test_http_routes_csp_and_no_mutation_endpoint(self):
        server = wb.build_server(self.store.config)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
        self.addCleanup(connection.close)
        connection.request("GET", "/")
        response = connection.getresponse(); response.read()
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
        for route in ["/api/ocr-vision-candidates?corpus=fixture", "/api/ocr-vision-candidate?corpus=fixture&id=candidate-one.json", "/api/ocr-vision-image?corpus=fixture&id=candidate-one.json&image_sha256=" + self.image_hash]:
            connection.request("GET", route)
            response = connection.getresponse(); data = response.read()
            self.assertEqual(response.status, 200)
            self.assertTrue(data)
        connection.request("POST", "/api/ocr-vision-candidate", body=b"{}", headers={"Content-Type": "application/json"})
        response = connection.getresponse(); response.read(); self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
