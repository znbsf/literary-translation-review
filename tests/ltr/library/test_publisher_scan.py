"""Synthetic-only producer tests; no local corpus or private files are read."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests.ltr.library import test_publisher_identifiers as fixtures
from ltr.library import publisher_identifiers as pi
from ltr.library import publisher_scan as scan


class PublisherScanTests(unittest.TestCase):
    # Reuse fixture construction without importing/inheriting its TestCase.
    setUp = fixtures.PublisherIdentifierTests.setUp
    write_db = fixtures.PublisherIdentifierTests.write_db
    write_catalog = fixtures.PublisherIdentifierTests.write_catalog

    def source_rows(self, rows, ja=True):
        self.editions = [("e1", "Synthetic one", "ja" if ja else "zh", "b1", "synthetic"),
                         ("e2", "Synthetic two", "ja" if ja else "zh", "b2", "synthetic")]
        self.paragraphs = rows
        self.db = self.write_db()

    def run_scan(self):
        result = scan.scan(self.db, self.catalog, self.candidate)
        payload = json.loads(self.candidate.read_bytes())
        checked = pi.read_candidates(self.candidate, self.db, self.catalog)
        self.assertEqual(checked["candidate_rows"], result["candidate_rows"])
        self.assertNotIn("observations", result)
        self.assertEqual(set(payload), pi.TOP_KEYS)
        self.assertFalse(result["corpus_updated"])
        self.assertFalse(result["local_edition_mapping_verified"])
        return result, payload

    def assert_clean(self):
        self.assertEqual(list(self.tmp.glob(".publisher-scan-*")), [])

    def test_whole_nfkc_prefix_offsets_and_source_unchanged(self):
        self.source_rows([("e1", "p", 0, "ﬁ ISBN: ９７８－０－１２３－４５６７８－６ tail", "{}")])
        before = self.db.read_bytes(), self.db.stat().st_mtime_ns
        result, payload = self.run_scan()
        self.assertEqual(result["candidate_rows"], 1)
        loc = payload["observations"][0]["paragraph_locators"][0]
        self.assertEqual((loc["normalized_start"], loc["normalized_end"]), (9, 26))
        self.assertTrue(payload["observations"][0]["isbn_label_nearby"])
        self.assertEqual(before, (self.db.read_bytes(), self.db.stat().st_mtime_ns))
        self.assert_clean()

    def test_split_isbn10_lowercase_x_deduplicates_windows(self):
        self.source_rows([("e1", "a", 0, "prefix 12345", "{}"),
                          ("e1", "b", 1, "6789x tail", "{}"),
                          ("e1", "c", 2, "synthetic end", "{}"),
                          ("e1", "d", 3, "synthetic more", "{}")])
        result, payload = self.run_scan()
        self.assertEqual(result["candidate_rows"], 1)
        row = payload["observations"][0]
        self.assertEqual(row["identifier_digits"], fixtures.ISBN10_B)
        self.assertEqual(row["observed_form"], "isbn10_equivalent")
        self.assertEqual([loc["paragraph_id"] for loc in row["paragraph_locators"]], ["a", "b"])

    def test_no_split_across_editions_or_non_ja(self):
        for ja in (True, False):
            with self.subTest(ja=ja):
                self.source_rows([("e1", "p", 0, "12345", "{}"),
                                  ("e2", "p", 0, "6789X", "{}")], ja=ja)
                result, _ = self.run_scan()
                self.assertEqual(result["candidate_rows"], 0)
                self.candidate.unlink()
        self.source_rows([("e1", "p", 0, "12345", "{}"),
                          ("e1", "q", 1, "6789X", "{}")], ja=False)
        result, _ = self.run_scan()
        self.assertEqual(result["candidate_rows"], 0)

    def test_no_letters_ocr_correction_or_longer_number_substrings(self):
        bad = ["978-0-12FORGED3-45678-6", "978O123456786",
               "0" + fixtures.ISBN13_A, fixtures.ISBN13_A + "0",
               "X" + fixtures.ISBN10_B, fixtures.ISBN10_B + "x"]
        self.source_rows([("e1", str(i), i, text, "{}") for i, text in enumerate(bad)])
        result, _ = self.run_scan()
        self.assertEqual(result["candidate_rows"], 0)

    def test_repeated_positions_and_namespaced_paragraph_ids(self):
        self.source_rows([("e1", "p", 0, fixtures.ISBN13_A + " / " + fixtures.ISBN13_A, "{}"),
                          ("e2", "p", 0, "prefix " + fixtures.ISBN13_A, "{}")])
        result, payload = self.run_scan()
        self.assertEqual(result["candidate_rows"], 3)
        locs = [row["paragraph_locators"][0] for row in payload["observations"]]
        self.assertEqual([loc["normalized_start"] for loc in locs], [0, 16, 7])

    def test_contained_isbn10_is_not_a_second_physical_occurrence(self):
        isbn13, isbn10 = '9780000000019', '0000000019'
        self.write_catalog([dict(fixtures.CATALOG_ENTRIES[0], isbn13=isbn13)])
        self.source_rows([('e1', 'p', 0, '978-' + isbn10 + ' / ' + isbn10, '{}')])
        result, payload = self.run_scan()
        self.assertEqual(result['candidate_rows'], 2)
        self.assertEqual(result['nested_equivalents_omitted'], 1)
        self.assertEqual([r['observed_form'] for r in payload['observations']],
                         ['isbn13', 'isbn10_equivalent'])

    def test_contained_split_equivalent_found_before_outer_is_removed(self):
        self.write_catalog([dict(fixtures.CATALOG_ENTRIES[0], isbn13='9780000000019')])
        self.source_rows([('e1', 'p', 0, '978-', '{}'), ('e1', 'q', 1, '0000000019', '{}')])
        result, payload = self.run_scan()
        self.assertEqual(result['candidate_rows'], 1)
        self.assertEqual(result['nested_equivalents_omitted'], 1)
        self.assertEqual(payload['observations'][0]['observed_form'], 'isbn13')

    def test_empty_intermediate_paragraph_skipped_and_counted_once(self):
        self.source_rows([("e1", "a", 0, "12345", "{}"), ("e1", "empty", 1, "", "{}"),
                          ("e1", "b", 2, "6789X", "{}"), ("e1", "c", 3, "end", "{}")])
        result, _ = self.run_scan()
        self.assertEqual(result["candidate_rows"], 0)
        self.assertEqual(result["skipped_empty_spans"], 1)

    def test_separator_only_intermediate_paragraph_preserved(self):
        self.source_rows([("e1", "a", 0, "12345", "{}"), ("e1", "space", 1, " — ", "{}"),
                          ("e1", "b", 2, "6789X", "{}")])
        _, payload = self.run_scan()
        self.assertEqual(len(payload["observations"][0]["paragraph_locators"]), 3)

    def test_window_limit_includes_physical_empty_rows(self):
        self.source_rows([("e1", "a", 0, "12345", "{}")]
                         + [("e1", str(i), i, " ", "{}") for i in range(1, 16)]
                         + [("e1", "b", 16, "6789X", "{}")])
        result, _ = self.run_scan()
        self.assertEqual(result["candidate_rows"], 0)

    def test_null_catalog_isbn_skipped(self):
        entries = [dict(entry, isbn13=None) for entry in fixtures.CATALOG_ENTRIES]
        self.write_catalog(entries)
        result, _ = self.run_scan()
        self.assertEqual(result["candidate_rows"], 0)

    def test_existing_output_and_publication_race_preserved(self):
        self.candidate.write_bytes(b"existing synthetic result")
        with self.assertRaises(scan.PublisherScanError):
            scan.scan(self.db, self.catalog, self.candidate)
        self.assertEqual(self.candidate.read_bytes(), b"existing synthetic result")
        self.candidate.unlink()
        original_link = scan.os.link

        def racing_link(source, target):
            self.candidate.write_bytes(b"racing synthetic result")
            original_link(source, target)

        with patch.object(scan.os, "link", side_effect=racing_link):
            with self.assertRaisesRegex(scan.PublisherScanError, "publisher_scan_output_exists"):
                scan.scan(self.db, self.catalog, self.candidate)
        self.assertEqual(self.candidate.read_bytes(), b"racing synthetic result")
        self.assert_clean()

    def test_dangling_output_symlink_preserved(self):
        target = self.tmp / "missing-target.json"
        try:
            self.candidate.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable on this platform")
        with self.assertRaisesRegex(scan.PublisherScanError, "publisher_scan_output_exists"):
            scan.scan(self.db, self.catalog, self.candidate)
        self.assertTrue(self.candidate.is_symlink())
        self.assertFalse(target.exists())
        self.assert_clean()

    def test_hardlink_unsupported_fails_without_fallback_overwrite(self):
        with patch.object(scan.os, "link", side_effect=OSError("FAKE_PRIVATE_PATH")):
            with self.assertRaisesRegex(scan.PublisherScanError, "^publisher_scan_invalid$"):
                scan.scan(self.db, self.catalog, self.candidate)
        self.assertFalse(self.candidate.exists())
        self.assert_clean()

    def test_sixteen_line_match_is_reader_valid(self):
        self.source_rows([("e1", "a", 0, "12345", "{}")]
                         + [("e1", str(i), i, " ", "{}") for i in range(1, 15)]
                         + [("e1", "b", 15, "6789X", "{}")])
        _, payload = self.run_scan()
        self.assertEqual(len(payload["observations"][0]["paragraph_locators"]), 16)

    def test_979_catalog_has_no_isbn10_equivalent(self):
        core = "979012345678"
        isbn = core + str((-sum(int(c) * (1 if i % 2 == 0 else 3)
                                for i, c in enumerate(core))) % 10)
        self.write_catalog([dict(fixtures.CATALOG_ENTRIES[0], isbn13=isbn)])
        self.source_rows([("e1", "p", 0, isbn + " / " + fixtures.ISBN10_A, "{}")])
        result, payload = self.run_scan()
        self.assertEqual(result["candidate_rows"], 1)
        self.assertEqual(payload["observations"][0]["observed_form"], "isbn13")

    def test_external_paths_and_input_protection(self):
        for output in (self.db, self.catalog, self.source / "new.json"):
            with self.subTest(output=output.name):
                with self.assertRaises(scan.PublisherScanError):
                    scan.scan(self.db, self.catalog, output)
        internal = self.source / "catalog.json"
        internal.write_bytes(self.catalog.read_bytes())
        with self.assertRaises(scan.PublisherScanError):
            scan.scan(self.db, internal, self.candidate)
        self.assertFalse(self.candidate.exists())

    def test_postvalidation_source_catalog_or_wal_mutation_refused(self):
        original = pi.read_candidates
        for kind in ("source", "catalog", "wal", "journal"):
            with self.subTest(kind=kind):
                db_before, catalog_before = self.db.read_bytes(), self.catalog.read_bytes()

                def mutate(*args):
                    result = original(*args)
                    if kind == "source":
                        with self.db.open("ab") as stream:
                            stream.write(b"synthetic change")
                    elif kind == "catalog":
                        with self.catalog.open("ab") as stream:
                            stream.write(b" ")
                    else:
                        Path(str(self.db) + "-" + kind).write_bytes(b"synthetic sidecar")
                    return result

                try:
                    with patch.object(pi, "read_candidates", side_effect=mutate):
                        with self.assertRaises(scan.PublisherScanError):
                            scan.scan(self.db, self.catalog, self.candidate)
                    self.assertFalse(self.candidate.exists())
                    self.assert_clean()
                finally:
                    self.db.write_bytes(db_before)
                    self.catalog.write_bytes(catalog_before)
                    for suffix in ("-wal", "-journal"):
                        Path(str(self.db) + suffix).unlink(missing_ok=True)

    def test_initial_wal_refused(self):
        Path(str(self.db) + "-wal").write_bytes(b"synthetic WAL")
        with self.assertRaises(scan.PublisherScanError):
            scan.scan(self.db, self.catalog, self.candidate)
        self.assertFalse(self.candidate.exists())

    def test_observation_and_serialized_size_bounds(self):
        self.source_rows([("e1", "p", 0, " / ".join([fixtures.ISBN13_A] * 3), "{}")])
        for name, value in (("MAX_OBSERVATIONS", 2), ("MAX_BYTES", 10)):
            with self.subTest(bound=name):
                with patch.object(scan, name, value):
                    with self.assertRaises(scan.PublisherScanError):
                        scan.scan(self.db, self.catalog, self.candidate)
                self.assertFalse(self.candidate.exists())
                self.assert_clean()

    def test_catalog_size_bound_and_checksum(self):
        self.catalog.write_bytes(b" " * (pi.MAX_BYTES + 1))
        with self.assertRaises(scan.PublisherScanError):
            scan.scan(self.db, self.catalog, self.candidate)
        self.write_catalog([dict(fixtures.CATALOG_ENTRIES[0], isbn13="9780123456787")])
        with self.assertRaises(scan.PublisherScanError):
            scan.scan(self.db, self.catalog, self.candidate)

    def test_sanitized_api_cli_and_private_text_absent(self):
        self.source_rows([("e1", "p", 0, "FAKE_PRIVATE_PROSE " + fixtures.ISBN13_A, "{}")])
        result, payload = self.run_scan()
        self.assertNotIn("FAKE_PRIVATE_PROSE", json.dumps(payload))
        self.assertNotIn(str(self.tmp), json.dumps(result))
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = scan.main(["--db", str(self.db), "--catalog", str(self.catalog),
                              "--out", str(self.candidate)])
        self.assertEqual(code, 1)
        self.assertNotIn(str(self.tmp), stdout.getvalue())
        self.assertNotIn("FAKE_PRIVATE_PROSE", stdout.getvalue())
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(scan.main(["--FAKE_PRIVATE_PROSE", str(self.tmp)]), 1)
        self.assertNotIn("FAKE_PRIVATE_PROSE", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
