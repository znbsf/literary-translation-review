"""Synthetic contract tests for optional related-publication metadata."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PREVIEW = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PREVIEW))

from ltr.library import bibliography as bib  # noqa: E402


class RelatedPublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="related-publications-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "catalog.json"

    def entry(self, work_id="wk-1", **overrides):
        value = {
            "work_id": work_id,
            "title_ja": "Synthetic title",
            "category": None,
            "edition_variant": None,
            "release_date": None,
            "isbn13": None,
            "publisher_url": "https://www.kadokawa.co.jp/product/synthetic",
            "field_sources": {},
            "uncertainties": [],
        }
        value.update(overrides)
        return value

    def relation(self, **overrides):
        value = {
            "work_id": "wk-2",
            "relation": "reprints_from",
            "note": "Synthetic reprint relation",
            "source_url": "https://www.kadokawa.co.jp/product/relation",
        }
        value.update(overrides)
        return value

    def load(self, entries):
        self.path.write_text(json.dumps({
            "schema": bib.SCHEMA,
            "verified_at_utc": "2026-01-01T00:00:00Z",
            "entries": entries,
        }, ensure_ascii=False), encoding="utf-8")
        return bib.load(self.path)

    def assert_rejected(self, entries):
        with self.assertRaises(bib.BibliographyError) as caught:
            self.load(entries)
        self.assertEqual(caught.exception.code, "bibliography_related_publications_invalid")

    def test_legacy_entry_keeps_its_normalized_shape(self):
        original = self.entry()
        normalized = self.load([original])["entries"][0]
        self.assertEqual(set(normalized), set(original))
        self.assertNotIn("related_publications", normalized)

    def test_valid_forward_reference_is_preserved(self):
        source = self.entry(related_publications=[self.relation()])
        target = self.entry("wk-2", title_ja="Synthetic target")
        result = self.load([source, target])
        self.assertEqual(result["entries"][0]["related_publications"], [self.relation()])
        self.assertNotIn("related_publications", result["entries"][1])

    def test_present_empty_array_is_preserved(self):
        normalized = self.load([self.entry(related_publications=[])])["entries"][0]
        self.assertEqual(normalized["related_publications"], [])

    def test_unknown_self_and_closed_object_violations_refuse(self):
        self.assert_rejected([self.entry(related_publications=[self.relation()])])
        self.assert_rejected([
            self.entry(related_publications=[self.relation(work_id="wk-1")])
        ])
        self.assert_rejected([
            self.entry(related_publications=[dict(self.relation(), extra="x")]),
            self.entry("wk-2"),
        ])
        self.assert_rejected([
            self.entry(related_publications=[{k: v for k, v in self.relation().items()
                                               if k != "source_url"}]),
            self.entry("wk-2"),
        ])

    def test_relation_note_host_and_duplicate_contract(self):
        for change in (
            {"relation": "unknown"},
            {"note": "x" * 161},
            {"note": "<b>markup</b>"},
            {"note": "https://example.invalid"},
            {"source_url": "https://evil.example/x"},
            {"source_url": "http://www.kadokawa.co.jp/x"},
        ):
            with self.subTest(change=change):
                self.assert_rejected([
                    self.entry(related_publications=[self.relation(**change)]),
                    self.entry("wk-2"),
                ])
        duplicate = self.relation()
        self.assert_rejected([
            self.entry(related_publications=[duplicate, duplicate]),
            self.entry("wk-2"),
        ])

    def test_note_limit_counts_unicode_code_points(self):
        note = "𠮷" * 160
        result = self.load([
            self.entry(related_publications=[self.relation(note=note)]),
            self.entry("wk-2"),
        ])
        self.assertEqual(result["entries"][0]["related_publications"][0]["note"], note)
        self.assert_rejected([
            self.entry(related_publications=[self.relation(note=note + "𠮷")]),
            self.entry("wk-2"),
        ])

    def test_limit_is_per_entry(self):
        relations = [
            self.relation(note=f"Synthetic relation {index}")
            for index in range(21)
        ]
        self.assert_rejected([
            self.entry(related_publications=relations),
            self.entry("wk-2"),
        ])


if __name__ == "__main__":
    unittest.main(verbosity=2)
