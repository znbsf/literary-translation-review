"""Synthetic unittest suite for the external publisher ISBN candidates reader.

Every test builds a temporary, synthetic corpus database, a synthetic publisher
catalogue and a synthetic candidate document under ``tempfile.mkdtemp``. All
strings are invented; no real corpus, catalogue, configuration file or source
prose is ever opened.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from ltr.library import bibliography as bib  # noqa: E402
from ltr.library import publisher_identifiers as pi  # noqa: E402
from ltr.library.review_queue import fingerprint  # noqa: E402

# Invented ISBNs (checksums verified below): an ISBN-13 with its ISBN-10
# equivalent (no check letter) and one whose ISBN-10 ends in ``X``.
ISBN13_A = "9780123456786"
ISBN10_A = "0123456789"
ISBN13_B = "9781234567897"
ISBN10_B = "123456789X"

# Invented text. Offsets address the complete NFKC text, with no letters removed.
P1 = "978-0-123-45678-6"
P2 = "012-3456"
P3 = "789"
P4 = "123456789X"
E2P1 = "other555"                 # -> 555

DEFAULT_PARAGRAPHS = [
    ("e1", "p1", 0, P1, "{}"),
    ("e1", "p2", 1, P2, "{}"),
    ("e1", "p3", 2, P3, "{}"),
    ("e1", "p4", 3, P4, "{}"),
    ("e2", "p1", 0, E2P1, "{}"),
]
DEFAULT_EDITIONS = [
    ("e1", "Volume One", "zh", "bk1", "synthetic"),
    ("e2", "Volume Two", "zh", "bk2", "synthetic"),
]
CATALOG_ENTRIES = [
    {
        "work_id": "wk-a",
        "title_ja": "合成作品A",
        "category": "文庫",
        "edition_variant": "通常版",
        "release_date": "2020-01-02",
        "isbn13": ISBN13_A,
        "publisher_url": "https://www.kadokawa.co.jp/product/1",
        "field_sources": {},
        "uncertainties": [],
    },
    {
        "work_id": "wk-b",
        "title_ja": "合成作品B",
        "category": "文庫",
        "edition_variant": "通常版",
        "release_date": "2020-01-03",
        "isbn13": ISBN13_B,
        "publisher_url": "https://www.kadokawa.co.jp/product/2",
        "field_sources": {},
        "uncertainties": [],
    },
]


class PublisherIdentifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pubid-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.source = self.tmp / "corpus"
        self.source.mkdir()
        self.editions = list(DEFAULT_EDITIONS)
        self.paragraphs = list(DEFAULT_PARAGRAPHS)
        self.db = self.write_db()
        self.source_sha = fingerprint(self.db)
        self.catalog = self.tmp / "catalog.json"
        self.write_catalog(CATALOG_ENTRIES)
        self.catalog_sha = fingerprint(self.catalog)
        self.candidate = self.tmp / "candidates.json"

    # -- fixtures ------------------------------------------------------
    def write_db(self) -> Path:
        db = self.source / "dialogue.sqlite"
        if db.exists():
            db.unlink()
        con = sqlite3.connect(db)
        con.executescript(
            """
            CREATE TABLE editions (id TEXT PRIMARY KEY, title TEXT, language TEXT,
                                   book_key TEXT, origin_kind TEXT);
            CREATE TABLE paragraphs (edition_id TEXT, id TEXT, ordinal INTEGER,
                                     text TEXT, locator_json TEXT);
            """
        )
        con.executemany("INSERT INTO editions VALUES (?,?,?,?,?)", self.editions)
        con.executemany("INSERT INTO paragraphs VALUES (?,?,?,?,?)", self.paragraphs)
        con.commit()
        con.close()
        return db

    def write_catalog(self, entries) -> None:
        self.catalog.write_text(
            json.dumps(
                {
                    "schema": bib.SCHEMA,
                    "verified_at_utc": "2024-05-06T07:08:09Z",
                    "entries": entries,
                }
            ),
            encoding="utf-8",
        )

    def text_of(self, edition_id, paragraph_id) -> str:
        for edition, pid, _ordinal, text, _locator in self.paragraphs:
            if edition == edition_id and pid == paragraph_id:
                return text
        raise AssertionError("unknown synthetic paragraph")

    def ordinal_of(self, edition_id, paragraph_id) -> int:
        for edition, pid, ordinal, _text, _locator in self.paragraphs:
            if edition == edition_id and pid == paragraph_id:
                return ordinal
        raise AssertionError("unknown synthetic paragraph")

    def locator(self, edition_id, paragraph_id, start, end):
        text = self.text_of(edition_id, paragraph_id)
        return {
            "paragraph_id": paragraph_id,
            "ordinal": self.ordinal_of(edition_id, paragraph_id),
            "paragraph_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "normalized_start": start,
            "normalized_end": end,
        }

    def observation(self, **overrides):
        base = {
            "edition_id": "e1",
            "publisher_work_id": "wk-a",
            "isbn13": ISBN13_A,
            "observed_form": "isbn13",
            "identifier_digits": ISBN13_A,
            "normalization": "nfkc then literal digits/X with whitespace and dash separators only",
            "paragraph_locators": [],
            "isbn_label_nearby": True,
            "requires_review": 1,
            "local_edition_mapping_verified": 0,
            "accepted": 0,
            "human_confirmed": 0,
            "semantic_verified": 0,
            "reference_eligible": 0,
        }
        base.update(overrides)
        if 'paragraph_locators' not in overrides:
            base['paragraph_locators'] = [self.locator('e1', 'p1', 0, 17)]
        return base

    def candidate_payload(self, observations, **overrides):
        editions = {row["edition_id"] for row in observations}
        records = {row["publisher_work_id"] for row in observations}
        payload = {
            "schema": pi.SCHEMA,
            "created_at_utc": "2024-05-06T07:08:09Z",
            "source_sha256": self.source_sha,
            "publisher_catalog_sha256": self.catalog_sha,
            "isbn_rule_sources": ["https://www.isbn-international.org/manual"],
            "searched_editions": len(self.editions),
            "searched_paragraphs": len(self.paragraphs),
            "rolling_ocr_line_window": 16,
            "editions_with_candidates": len(editions),
            "distinct_publisher_records": len(records),
            "candidate_rows": len(observations),
            "observations": observations,
            "caveats": ["occurrence is not edition identity"],
            "corpus_updated": False,
        }
        payload.update(overrides)
        return payload

    def write_candidate(self, payload) -> Path:
        self.candidate.write_text(json.dumps(payload), encoding="utf-8")
        return self.candidate

    def read(self, payload=None):
        if payload is not None:
            self.write_candidate(payload)
        return pi.read_candidates(self.candidate, self.db, self.catalog)

    def assertRefused(self, payload, code):
        self.write_candidate(payload)
        with self.assertRaises(pi.PublisherIdentifierError) as ctx:
            pi.read_candidates(self.candidate, self.db, self.catalog)
        self.assertEqual(ctx.exception.code, code)
        return str(ctx.exception)

    # -- valid reads ---------------------------------------------------
    def test_valid_isbn13_observation(self):
        result = self.read(self.candidate_payload([self.observation()]))
        self.assertTrue(result["available"])
        self.assertEqual(result["candidate_rows"], 1)
        self.assertEqual(result["editions_with_candidates"], 1)
        self.assertEqual(result["distinct_publisher_records"], 1)
        self.assertEqual(result["searched_editions"], len(self.editions))
        self.assertEqual(result["searched_paragraphs"], len(self.paragraphs))
        self.assertFalse(result["corpus_updated"])
        self.assertFalse(result["local_edition_mapping_verified"])
        self.assertTrue(result["source_unchanged"])
        self.assertEqual(result["source_sha256"], self.source_sha)
        self.assertEqual(result["publisher_catalog_sha256"], self.catalog_sha)
        self.assertIn("advertisement", result["caution"])
        row = result["observations"][0]
        self.assertEqual(row["isbn13"], ISBN13_A)
        self.assertEqual(row["publisher_work_id"], "wk-a")
        self.assertEqual(row["requires_review"], 1)
        for flag in ("local_edition_mapping_verified", "accepted", "human_confirmed",
                     "semantic_verified", "reference_eligible"):
            self.assertEqual(row[flag], 0)
        locator = row["paragraph_locators"][0]
        self.assertEqual(set(locator), set(pi.LOCATOR_KEYS))
        self.assertEqual(locator["paragraph_id"], "p1")
        self.assertEqual(locator["normalized_start"], 0)
        self.assertEqual(locator["normalized_end"], 17)
        # No source prose and no producer free text is ever projected.
        dumped = json.dumps(result)
        self.assertNotIn(P1, dumped)
        self.assertNotIn("note978", dumped)
        self.assertNotIn("normalization", row)
        self.assertNotIn("caveats", result)
        self.assertNotIn("isbn_rule_sources", result)

    def test_kodansha_catalog_uses_shared_strict_bibliography_validation(self):
        entries = [dict(entry, publisher_url='https://www.kodansha.co.jp/book/products/synthetic-entry',
                        field_sources={'title_ja': 'https://www.kodansha.co.jp/titles/synthetic-title'})
                   for entry in CATALOG_ENTRIES]
        self.write_catalog(entries)
        self.catalog_sha = fingerprint(self.catalog)
        self.assertTrue(self.read(self.candidate_payload([self.observation()]))['available'])
        entries[0]['field_sources']['title_ja'] = 'https://www.kodansha.co.jp.evil/synthetic'
        self.write_catalog(entries)
        self.catalog_sha = fingerprint(self.catalog)
        self.assertRefused(self.candidate_payload([self.observation()]), 'publisher_identifiers_catalog_invalid')

    def test_split_isbn10_across_ocr_lines(self):
        observation = self.observation(
            observed_form="isbn10_equivalent",
            identifier_digits=ISBN10_A,
            paragraph_locators=[self.locator("e1", "p2", 0, 8), self.locator("e1", "p3", 0, 3)],
        )
        result = self.read(self.candidate_payload([observation]))
        self.assertTrue(result["available"])
        row = result["observations"][0]
        self.assertEqual(row["identifier_digits"], ISBN10_A)
        self.assertEqual([loc["paragraph_id"] for loc in row["paragraph_locators"]], ["p2", "p3"])

    def test_isbn10_x_check_digit(self):
        observation = self.observation(
            publisher_work_id="wk-b",
            isbn13=ISBN13_B,
            observed_form="isbn10_equivalent",
            identifier_digits=ISBN10_B,
            paragraph_locators=[self.locator("e1", "p4", 0, 10)],
        )
        result = self.read(self.candidate_payload([observation]))
        self.assertTrue(result["available"])
        self.assertEqual(result["observations"][0]["identifier_digits"], ISBN10_B)

    # -- binding and source integrity ----------------------------------
    def test_forged_checksum_rejected(self):
        payload = self.candidate_payload([self.observation(isbn13="9780123456787")])
        self.assertRefused(payload, "publisher_identifiers_isbn_invalid")

    def test_forged_offset_rejected(self):
        payload = self.candidate_payload(
            [self.observation(paragraph_locators=[self.locator("e1", "p1", 0, 999)])]
        )
        self.assertRefused(payload, "publisher_identifiers_span_mismatch")

    def test_nonempty_span_required(self):
        payload = self.candidate_payload(
            [self.observation(paragraph_locators=[self.locator("e1", "p1", 5, 5)])]
        )
        self.assertRefused(payload, "publisher_identifiers_span_mismatch")

    def test_forged_paragraph_hash_rejected(self):
        locator = self.locator("e1", "p1", 0, 17)
        locator["paragraph_sha256"] = "a" * 64
        payload = self.candidate_payload([self.observation(paragraph_locators=[locator])])
        self.assertRefused(payload, "publisher_identifiers_paragraph_mismatch")

    def test_forged_catalog_hash_rejected(self):
        payload = self.candidate_payload(
            [self.observation()], publisher_catalog_sha256="0" * 64
        )
        self.assertRefused(payload, "publisher_identifiers_catalog_sha_mismatch")

    def test_forged_source_hash_rejected(self):
        payload = self.candidate_payload([self.observation()], source_sha256="0" * 64)
        self.assertRefused(payload, "publisher_identifiers_source_mismatch")

    def test_different_edition_same_paragraph_id_rejected(self):
        # Edition ``e2`` also owns a paragraph ``p1``; reusing edition ``e1``'s
        # hash for the ``e2`` row must fail.
        locator = self.locator("e2", "p1", 0, 3)
        locator["paragraph_sha256"] = hashlib.sha256(
            self.text_of("e1", "p1").encode("utf-8")
        ).hexdigest()
        payload = self.candidate_payload(
            [self.observation(edition_id="e2", paragraph_locators=[locator])]
        )
        self.assertRefused(payload, "publisher_identifiers_paragraph_mismatch")

    def test_wal_appearance_rejected(self):
        wal = Path(str(self.db) + "-wal")
        wal.write_bytes(b"synthetic concurrent WAL")
        try:
            payload = self.candidate_payload([self.observation()])
            self.assertRefused(payload, "publisher_identifiers_unstable_source")
        finally:
            wal.unlink()

    def test_source_missing_rejected(self):
        self.db.unlink()
        payload = self.candidate_payload([self.observation()])
        self.assertRefused(payload, "publisher_identifiers_source_missing")

    # -- duplicates and bounds -----------------------------------------
    def test_duplicate_observation_rejected(self):
        payload = self.candidate_payload([self.observation(), self.observation()])
        self.assertRefused(payload, "publisher_identifiers_duplicate_observation")

    def test_too_many_observations_rejected(self):
        rows = [self.observation() for _ in range(pi.MAX_OBSERVATIONS + 1)]
        payload = self.candidate_payload(rows)
        self.assertRefused(payload, "publisher_identifiers_too_many_observations")

    def test_too_many_locators_rejected(self):
        locators = [self.locator("e1", "p1", 0, 17)] * (pi.MAX_LOCATORS + 1)
        payload = self.candidate_payload([self.observation(paragraph_locators=locators)])
        self.assertRefused(payload, "publisher_identifiers_locator_invalid")

    def test_no_locators_rejected(self):
        payload = self.candidate_payload([self.observation(paragraph_locators=[])])
        self.assertRefused(payload, "publisher_identifiers_locator_invalid")

    def test_duplicate_locator_rejected(self):
        locators = [self.locator("e1", "p2", 0, 8), self.locator("e1", "p2", 0, 8)]
        payload = self.candidate_payload(
            [self.observation(
                observed_form="isbn10_equivalent",
                identifier_digits=ISBN10_A,
                paragraph_locators=locators,
            )]
        )
        self.assertRefused(payload, "publisher_identifiers_duplicate_locator")

    def test_ordinal_order_rejected(self):
        locators = [self.locator("e1", "p3", 0, 3), self.locator("e1", "p2", 0, 8)]
        payload = self.candidate_payload(
            [self.observation(
                observed_form="isbn10_equivalent",
                identifier_digits=ISBN10_A,
                paragraph_locators=locators,
            )]
        )
        self.assertRefused(payload, "publisher_identifiers_locator_invalid")

    # -- flags and types -----------------------------------------------
    def test_bool_as_int_count_rejected(self):
        payload = self.candidate_payload([self.observation()], searched_editions=True)
        self.assertRefused(payload, "publisher_identifiers_counts_invalid")

    def test_bool_as_int_flag_rejected(self):
        payload = self.candidate_payload([self.observation(requires_review=True)])
        self.assertRefused(payload, "publisher_identifiers_flags_invalid")

    def test_bool_label_rejected(self):
        payload = self.candidate_payload([self.observation(isbn_label_nearby=1)])
        self.assertRefused(payload, "publisher_identifiers_flags_invalid")

    def test_acceptance_flag_rejected(self):
        payload = self.candidate_payload([self.observation(accepted=1)])
        self.assertRefused(payload, "publisher_identifiers_flags_invalid")

    def test_mapping_flag_rejected(self):
        payload = self.candidate_payload(
            [self.observation(local_edition_mapping_verified=1)]
        )
        self.assertRefused(payload, "publisher_identifiers_flags_invalid")

    # -- schema, counts, catalogue membership --------------------------
    def test_unknown_top_level_key_rejected(self):
        payload = self.candidate_payload([self.observation()], extra=1)
        self.assertRefused(payload, "publisher_identifiers_schema_mismatch")

    def test_unknown_observation_key_rejected(self):
        payload = self.candidate_payload([self.observation(extra=1)])
        self.assertRefused(payload, "publisher_identifiers_observation_invalid")

    def test_count_mismatch_rejected(self):
        payload = self.candidate_payload([self.observation()], candidate_rows=2)
        self.assertRefused(payload, "publisher_identifiers_counts_invalid")

    def test_search_counts_must_match_source(self):
        payload = self.candidate_payload([self.observation()], searched_paragraphs=1)
        self.assertRefused(payload, "publisher_identifiers_search_counts_mismatch")

    def test_catalog_membership_required(self):
        payload = self.candidate_payload([self.observation(publisher_work_id="wk-x")])
        self.assertRefused(payload, "publisher_identifiers_catalog_mismatch")

    def test_isbn_not_in_catalog_rejected(self):
        # A checksum-valid ISBN-13 that is absent from the catalogue.
        payload = self.candidate_payload(
            [self.observation(isbn13="9780306406157", identifier_digits="9780306406157")]
        )
        self.assertRefused(payload, "publisher_identifiers_catalog_mismatch")

    def test_contract_and_window_rejected(self):
        cases = [
            (self.candidate_payload([self.observation()], corpus_updated=True),
             "publisher_identifiers_contract_invalid"),
            (self.candidate_payload([self.observation()], rolling_ocr_line_window=8),
             "publisher_identifiers_counts_invalid"),
            (self.candidate_payload([self.observation()], created_at_utc="not-a-time"),
             "publisher_identifiers_timestamp_invalid"),
            (self.candidate_payload([self.observation()], source_sha256="xyz"),
             "publisher_identifiers_hash_invalid"),
            (self.candidate_payload([self.observation()], isbn_rule_sources=["http://x.example"]),
             "publisher_identifiers_url_invalid"),
            (self.candidate_payload([self.observation()], caveats="oops"),
             "publisher_identifiers_caveats_invalid"),
        ]
        for payload, code in cases:
            with self.subTest(code=code):
                self.assertRefused(payload, code)

    # -- external boundary, read-only guarantee ------------------------
    def test_candidate_file_must_be_external(self):
        inside = self.source / "candidates.json"
        inside.write_text(json.dumps(self.candidate_payload([self.observation()])),
                          encoding="utf-8")
        with self.assertRaises(pi.PublisherIdentifierError) as ctx:
            pi.read_candidates(inside, self.db, self.catalog)
        self.assertEqual(ctx.exception.code, "publisher_identifiers_candidate_not_external")

    def test_catalog_file_must_be_external(self):
        inside = self.source / "catalog.json"
        inside.write_bytes(self.catalog.read_bytes())
        with self.assertRaises(pi.PublisherIdentifierError) as ctx:
            pi.read_candidates(self.candidate, self.db, inside)
        self.assertEqual(ctx.exception.code, "publisher_identifiers_catalog_not_external")

    def test_source_unchanged_and_read_only(self):
        self.write_candidate(self.candidate_payload([self.observation()]))
        db_before = self.db.read_bytes()
        db_mtime = self.db.stat().st_mtime_ns
        listing_before = sorted(path.name for path in self.source.iterdir())
        pi.read_candidates(self.candidate, self.db, self.catalog)
        pi.read_candidates(self.candidate, self.db, self.catalog)
        self.assertEqual(self.db.read_bytes(), db_before)
        self.assertEqual(self.db.stat().st_mtime_ns, db_mtime)
        self.assertEqual(sorted(path.name for path in self.source.iterdir()), listing_before)

    def test_missing_and_oversize_candidate(self):
        with self.assertRaises(pi.PublisherIdentifierError) as ctx:
            pi.read_candidates(self.tmp / "absent.json", self.db, self.catalog)
        self.assertEqual(ctx.exception.code, "publisher_identifiers_missing")

        big = self.tmp / "big.json"
        big.write_bytes(b" " * (pi.MAX_BYTES + 1))
        with self.assertRaises(pi.PublisherIdentifierError) as ctx:
            pi.read_candidates(big, self.db, self.catalog)
        self.assertEqual(ctx.exception.code, "publisher_identifiers_too_large")

    def test_invalid_json_rejected(self):
        self.candidate.write_text("{not json", encoding="utf-8")
        with self.assertRaises(pi.PublisherIdentifierError) as ctx:
            pi.read_candidates(self.candidate, self.db, self.catalog)
        self.assertEqual(ctx.exception.code, "publisher_identifiers_invalid_json")

    # -- sanitised failures --------------------------------------------
    def test_safe_errors_do_not_leak_private_text(self):
        secret = "SECRETPROSE"
        self.paragraphs = [("e1", "p1", 0, secret + "978-0-123-45678-6", "{}")]
        self.editions = [("e1", "Volume One", "zh", "bk1", "synthetic")]
        self.db = self.write_db()
        self.source_sha = fingerprint(self.db)

        good = self.observation(paragraph_locators=[self.locator("e1", "p1", len(secret), len(secret) + 17)])
        result = self.read(self.candidate_payload([good]))
        self.assertNotIn(secret, json.dumps(result))

        forged = self.observation(paragraph_locators=[self.locator("e1", "p1", 0, 999)])
        message = self.assertRefused(
            self.candidate_payload([forged]), "publisher_identifiers_span_mismatch"
        )
        self.assertNotIn(secret, message)
        self.assertNotIn(str(self.tmp), message)

    def test_overview_is_safe_and_non_raising(self):
        self.candidate.write_text("{not json", encoding="utf-8")
        result = pi.overview(self.candidate, self.db, self.catalog)
        self.assertFalse(result["available"])
        self.assertEqual(result["error"], "publisher_identifiers_invalid_json")
        self.assertEqual(result["observations"], [])
        self.assertFalse(result["local_edition_mapping_verified"])
        self.assertNotIn(str(self.tmp), json.dumps(result))

    def reset_source(self, paragraphs):
        self.paragraphs = paragraphs
        self.db = self.write_db()
        self.source_sha = fingerprint(self.db)

    def test_normalization_does_not_remove_letters_inside_identifier(self):
        text = '978-0-12FORGED3-45678-6'
        self.reset_source([('e1', 'p1', 0, text, '{}')])
        row = self.observation(paragraph_locators=[self.locator('e1', 'p1', 0, len(text))])
        self.assertRefused(self.candidate_payload([row]), 'publisher_identifiers_span_mismatch')

    def test_offsets_address_complete_nfkc_text(self):
        prefix = 'Synthetic prefix: '
        identifier = '９７８－０－１２３－４５６７８－６'
        self.reset_source([('e1', 'p1', 0, prefix + identifier + ' end', '{}')])
        row = self.observation(paragraph_locators=[self.locator('e1', 'p1', len(prefix), len(prefix) + len(identifier))])
        result = self.read(self.candidate_payload([row]))
        self.assertTrue(result['available'])
        self.assertNotIn(prefix, json.dumps(result))

    def test_utc_offset_timestamp_is_accepted(self):
        result = self.read(self.candidate_payload([self.observation()], created_at_utc='2024-05-06T07:08:09+00:00'))
        self.assertTrue(result['available'])

    def test_split_cannot_skip_unrelated_paragraph(self):
        self.reset_source([('e1', 'p2', 1, P2, '{}'),
                           ('e1', 'gap', 2, 'unrelated content', '{}'),
                           ('e1', 'p3', 3, P3, '{}')])
        row = self.observation(observed_form='isbn10_equivalent', identifier_digits=ISBN10_A,
            paragraph_locators=[self.locator('e1', 'p2', 0, 8), self.locator('e1', 'p3', 0, 3)])
        self.assertRefused(self.candidate_payload([row]), 'publisher_identifiers_noncontiguous_span')

    def test_split_cannot_omit_tail_of_first_paragraph(self):
        self.reset_source([('e1', 'p2', 1, P2 + 'TAIL', '{}'), ('e1', 'p3', 2, P3, '{}')])
        row = self.observation(observed_form='isbn10_equivalent', identifier_digits=ISBN10_A,
            paragraph_locators=[self.locator('e1', 'p2', 0, 8), self.locator('e1', 'p3', 0, 3)])
        self.assertRefused(self.candidate_payload([row]), 'publisher_identifiers_span_mismatch')

    def test_split_preserves_separator_only_intermediate_paragraph(self):
        self.reset_source([('e1', 'p2', 1, P2, '{}'), ('e1', 'space', 2, ' ', '{}'), ('e1', 'p3', 3, P3, '{}')])
        row = self.observation(observed_form='isbn10_equivalent', identifier_digits=ISBN10_A,
            paragraph_locators=[self.locator('e1', 'p2', 0, 8), self.locator('e1', 'space', 0, 1), self.locator('e1', 'p3', 0, 3)])
        self.assertTrue(self.read(self.candidate_payload([row]))['available'])

    def test_identifier_cannot_be_substring_of_longer_digits(self):
        self.reset_source([('e1', 'p1', 0, '0' + ISBN13_A, '{}')])
        row = self.observation(paragraph_locators=[self.locator('e1', 'p1', 1, 14)])
        self.assertRefused(self.candidate_payload([row]), 'publisher_identifiers_span_mismatch')

    def test_malformed_form_and_isbn_fail_with_static_error(self):
        for overrides in [{'observed_form': []}, {'isbn13': ISBN13_A + '\n'}]:
            with self.subTest(overrides=overrides):
                self.assertRefused(self.candidate_payload([self.observation(**overrides)]), 'publisher_identifiers_isbn_invalid')


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
