"""Synthetic URL-policy checks for the publisher host allowlist."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from ltr.library import bibliography as bib  # noqa: E402


class GroupKadokawaHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="publisher-url-preview-")
        self.addCleanup(self.tmp.cleanup)

    def entry(self, publisher_url, field_sources):
        return {
            "work_id": "synthetic-work-1",
            "title_ja": "Synthetic title",
            "category": None,
            "edition_variant": None,
            "release_date": None,
            "isbn13": None,
            "publisher_url": publisher_url,
            "field_sources": field_sources,
            "uncertainties": [],
        }

    def overview(self, entry):
        path = Path(self.tmp.name) / "catalog.json"
        path.write_text(json.dumps({
            "schema": bib.SCHEMA,
            "verified_at_utc": "2026-10-22T00:00:00Z",
            "entries": [entry],
        }), encoding="utf-8")
        return bib.overview(path)

    def test_exact_group_host_accepts_https_catalog_and_field_sources(self):
        url = "https://group.kadokawa.co.jp/company/"
        sources = {key: url for key in bib.FIELD_SOURCE_KEYS}
        result = self.overview(self.entry(url, sources))
        self.assertTrue(result["available"])
        self.assertEqual(result["entries"][0]["publisher_url"], url)
        self.assertEqual(result["entries"][0]["field_sources"], sources)
        self.assertFalse(result["corpus_updated"])
        self.assertFalse(result["local_edition_mapping_verified"])
        self.assertEqual(
            bib.ALLOWED_HOSTS,
            frozenset({
                "www.kadokawa.co.jp", "store.kadokawa.co.jp", "promo.kadokawa.co.jp",
                "group.kadokawa.co.jp", "www.kadokawa.com.tw", "www.kodansha.co.jp",
            }),
        )

    def test_group_host_rejects_suffix_userinfo_port_and_http_in_both_fields(self):
        invalid = [
            "https://group.kadokawa.co.jp.evil.example/path",
            "https://group.kadokawa.co.jp@evil.example/path",
            "https://user:pass@group.kadokawa.co.jp/path",
            "https://group.kadokawa.co.jp:8443/path",
            "https://group.kadokawa.co.jp:443/path",
            "http://group.kadokawa.co.jp/path",
            "https://sub.group.kadokawa.co.jp/path",
        ]
        for url in invalid:
            with self.subTest(url=url):
                rejected_catalog_url = self.overview(self.entry(url, {}))
                self.assertFalse(rejected_catalog_url["available"])
                self.assertEqual(rejected_catalog_url["error"], "bibliography_url_invalid")
                rejected_field_source = self.overview(self.entry(
                    "https://group.kadokawa.co.jp/book/",
                    {"title_ja": url},
                ))
                self.assertFalse(rejected_field_source["available"])
                self.assertEqual(rejected_field_source["error"], "bibliography_field_sources_invalid")


if __name__ == "__main__":
    unittest.main()
