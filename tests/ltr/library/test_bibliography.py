"""Synthetic unittest suite for the optional publisher bibliography catalog.

Every test builds temporary, synthetic JSON catalogs and (where needed) a small
synthetic corpus database under ``tempfile.mkdtemp``. No real corpus, catalog,
scan or configuration file is ever opened.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from ltr.library import bibliography as bib  # noqa: E402
from ltr import workbench as wb  # noqa: E402

FULL_SCHEMA = """
    CREATE TABLE editions (id TEXT PRIMARY KEY, book_key TEXT, title TEXT,
                           language TEXT, origin_kind TEXT);
    CREATE TABLE dialogues (id TEXT PRIMARY KEY, edition_id TEXT, ordinal INTEGER,
                            text TEXT, speaker TEXT, interlocutor TEXT,
                            attribution_status TEXT, start_para INTEGER,
                            end_para INTEGER, context_hash TEXT);
"""

def seed_full(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO editions VALUES (?,?,?,?,?)",
                 ("ed1", "bk1", "Title One", "zh", "manuscript"))
    conn.execute("INSERT INTO dialogues VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("d1", "ed1", 1, "text", "Speaker A", "Speaker B", "guess", 1, 1, "ch1"))


VALID_ISBN = "9780306406157"


class BibliographyCatalogTests(unittest.TestCase):
    """Direct validation of the external catalog projection."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bib-catalog-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def entry(self, **overrides):
        base = {
            "work_id": "wk-1",
            "title_ja": "作品タイトル",
            "category": "文庫",
            "edition_variant": "通常版",
            "release_date": "2020-01-02",
            "isbn13": VALID_ISBN,
            "publisher_url": "https://www.kadokawa.co.jp/product/1",
            "field_sources": {"title_ja": "https://www.kadokawa.co.jp/x"},
            "uncertainties": ["発売日は推定"],
        }
        base.update(overrides)
        return base

    def catalog(self, entries=None, verified="2024-05-06T07:08:09Z", schema=bib.SCHEMA):
        return {
            "schema": schema,
            "verified_at_utc": verified,
            "entries": [self.entry()] if entries is None else entries,
        }

    def write(self, payload, name="catalog.json") -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def assertRefused(self, payload, code):
        path = self.write(payload)
        result = bib.overview(path)
        self.assertFalse(result["available"])
        self.assertEqual(result["error"], code)
        # The sanitised error must never echo a machine path.
        self.assertNotIn(str(self.tmp), json.dumps(result))
        self.assertEqual(result["entries"], [])
        self.assertFalse(result["corpus_updated"])

    # -- availability --------------------------------------------------
    def test_unconfigured_is_unavailable(self):
        result = bib.overview(None)
        self.assertFalse(result["available"])
        self.assertNotIn("error", result)
        self.assertEqual(result["entries"], [])
        self.assertEqual(result["total"], 0)
        self.assertFalse(result["local_edition_mapping_verified"])

    def test_missing_file_is_not_empty_catalog(self):
        result = bib.overview(self.tmp / "absent.json")
        self.assertFalse(result["available"])
        self.assertEqual(result["error"], "bibliography_missing")

    def test_valid_catalog_with_nulls(self):
        entries = [
            self.entry(),
            self.entry(
                work_id="wk-2",
                title_ja=None,
                category=None,
                edition_variant=None,
                release_date=None,
                isbn13=None,
                publisher_url=None,
                field_sources={},
                uncertainties=[],
            ),
        ]
        result = bib.overview(self.write(self.catalog(entries=entries)))
        self.assertTrue(result["available"])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["verified_at_utc"], "2024-05-06T07:08:09Z")
        self.assertFalse(result["corpus_updated"])
        self.assertFalse(result["local_edition_mapping_verified"])
        first, second = result["entries"]
        self.assertEqual(set(first), set(bib.ENTRY_KEYS))
        self.assertEqual(first["work_id"], "wk-1")
        self.assertEqual(first["isbn13"], VALID_ISBN)
        self.assertEqual(first["field_sources"], {"title_ja": "https://www.kadokawa.co.jp/x"})
        self.assertIsNone(second["title_ja"])
        self.assertIsNone(second["release_date"])
        self.assertIsNone(second["isbn13"])
        self.assertIsNone(second["publisher_url"])
        self.assertEqual(second["field_sources"], {})
        self.assertEqual(second["uncertainties"], [])

    def test_field_source_null_allowed(self):
        result = bib.overview(self.write(self.catalog(
            entries=[self.entry(field_sources={"isbn13": None})]
        )))
        self.assertTrue(result["available"])
        self.assertIsNone(result["entries"][0]["field_sources"]["isbn13"])

    def test_exact_kodansha_host_accepted_for_catalog_and_field_links(self):
        publisher = 'https://www.kodansha.co.jp/book/products/synthetic-entry'
        sources = {key: 'https://www.kodansha.co.jp/titles/synthetic-title' for key in bib.FIELD_SOURCE_KEYS}
        result = bib.overview(self.write(self.catalog(entries=[self.entry(publisher_url=publisher, field_sources=sources)])))
        self.assertTrue(result['available'])
        self.assertEqual(result['entries'][0]['publisher_url'], publisher)
        self.assertEqual(result['entries'][0]['field_sources'], sources)
        self.assertFalse(result['corpus_updated'])
        self.assertFalse(result['local_edition_mapping_verified'])

    def test_kodansha_host_requires_exact_hostname_https_no_credentials_or_port(self):
        invalid = [
            'https://www.kodansha.co.jp.evil/synthetic',
            'https://kodansha.co.jp.evil/synthetic',
            'https://kodansha.co.jp/synthetic',
            'https://user:synthetic@www.kodansha.co.jp/synthetic',
            'https://www.kodansha.co.jp:8443/synthetic',
            'https://www.kodansha.co.jp:443/synthetic',
            'http://www.kodansha.co.jp/synthetic',
            'https://bookclub.kodansha.co.jp/synthetic',
            'https://cocreco.kodansha.co.jp/synthetic',
            'https://other.kodansha.co.jp/synthetic',
        ]
        for url in invalid:
            with self.subTest(url=url):
                self.assertRefused(self.catalog(entries=[self.entry(publisher_url=url)]), 'bibliography_url_invalid')
                self.assertRefused(self.catalog(entries=[self.entry(field_sources={'title_ja': url})]),
                                   'bibliography_field_sources_invalid')

    # -- rejections ----------------------------------------------------
    def test_rejections(self):
        e = self.entry
        cases = [
            (self.catalog(schema="corpus-publisher-bibliography-v2"),
             "bibliography_schema_mismatch"),
            ({"schema": bib.SCHEMA, "verified_at_utc": "2024-01-01T00:00:00Z",
              "entries": [], "extra": 1}, "bibliography_schema_mismatch"),
            ({"schema": bib.SCHEMA, "verified_at_utc": "2024-01-01T00:00:00Z"},
             "bibliography_schema_mismatch"),
            ({"schema": bib.SCHEMA, "verified_at_utc": "2024-01-01T00:00:00Z",
              "entries": {}}, "bibliography_entries_invalid"),
            (self.catalog(verified="2024-01-01T00:00:00"), "bibliography_timestamp_invalid"),
            (self.catalog(verified="not-a-time"), "bibliography_timestamp_invalid"),
            (self.catalog(verified=123), "bibliography_timestamp_invalid"),
            (self.catalog(entries=[e(work_id="")]), "bibliography_entry_invalid"),
            (self.catalog(entries=[e(work_id=7)]), "bibliography_entry_invalid"),
            (self.catalog(entries=[dict(e(), extra=1)]), "bibliography_entry_invalid"),
            (self.catalog(entries=[{k: v for k, v in e().items() if k != "isbn13"}]),
             "bibliography_entry_invalid"),
            (self.catalog(entries=[e(title_ja=5)]), "bibliography_entry_invalid"),
            (self.catalog(entries=[e(release_date="2024-02-30")]), "bibliography_date_invalid"),
            (self.catalog(entries=[e(release_date="2024-1-1")]), "bibliography_date_invalid"),
            (self.catalog(entries=[e(release_date=20240101)]), "bibliography_date_invalid"),
            (self.catalog(entries=[e(isbn13="9780306406158")]), "bibliography_isbn_invalid"),
            (self.catalog(entries=[e(isbn13="9780306406157".translate(str.maketrans('0123456789', '０１２３４５６７８９')))]),
             "bibliography_isbn_invalid"),
            (self.catalog(entries=[e(isbn13="978030640615")]), "bibliography_isbn_invalid"),
            (self.catalog(entries=[e(isbn13=9780306406157)]), "bibliography_isbn_invalid"),
            (self.catalog(entries=[e(), e(work_id="wk-1", isbn13=None)]),
             "bibliography_duplicate_work_id"),
            (self.catalog(entries=[e(), e(work_id="wk-2")]), "bibliography_duplicate_isbn"),
            (self.catalog(entries=[e(publisher_url="http://www.kadokawa.co.jp/x")]),
             "bibliography_url_invalid"),
            (self.catalog(entries=[e(publisher_url="https://evil.example.com/x")]),
             "bibliography_url_invalid"),
            (self.catalog(entries=[e(publisher_url="https://user:pass@www.kadokawa.co.jp/x")]),
             "bibliography_url_invalid"),
            (self.catalog(entries=[e(publisher_url="https://www.kadokawa.co.jp:8443/x")]),
             "bibliography_url_invalid"),
            (self.catalog(entries=[e(publisher_url="javascript:alert(1)")]),
             "bibliography_url_invalid"),
            (self.catalog(entries=[e(publisher_url=42)]), "bibliography_url_invalid"),
            (self.catalog(entries=[e(field_sources={"unknown": "https://www.kadokawa.co.jp/x"})]),
             "bibliography_field_sources_invalid"),
            (self.catalog(entries=[e(field_sources=[])]), "bibliography_field_sources_invalid"),
            (self.catalog(entries=[e(field_sources={"title_ja": "https://evil.example.com/x"})]),
             "bibliography_field_sources_invalid"),
            (self.catalog(entries=[e(uncertainties="oops")]),
             "bibliography_uncertainties_invalid"),
            (self.catalog(entries=[e(uncertainties=[1])]),
             "bibliography_uncertainties_invalid"),
            (self.catalog(entries=[e(uncertainties=["x"] * (bib.MAX_UNCERTAINTIES + 1))]),
             "bibliography_uncertainties_invalid"),
        ]
        for payload, code in cases:
            with self.subTest(code=code):
                self.assertRefused(payload, code)

    def test_oversize_rejected(self):
        path = self.tmp / "huge.json"
        path.write_bytes(b" " * (bib.MAX_BYTES + 1))
        result = bib.overview(path)
        self.assertFalse(result["available"])
        self.assertEqual(result["error"], "bibliography_too_large")

    def test_too_many_entries_rejected(self):
        entries = [
            self.entry(work_id="wk-%d" % index, isbn13=None)
            for index in range(bib.MAX_ENTRIES + 1)
        ]
        self.assertRefused(self.catalog(entries=entries), "bibliography_too_many_entries")


class BibliographyConfigTests(unittest.TestCase):
    """Config resolution and backwards compatibility."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bib-config-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_default_none(self):
        config = wb.CorpusConfig("c1", "n", Path("/tmp/x"))
        self.assertIsNone(config.bibliography)
        self.assertIsNone(config.attribution_index)

    def test_relative_path_resolved_against_config(self):
        (self.tmp / "data").mkdir()
        config_path = self.tmp / "config.json"
        config_path.write_text(json.dumps({
            "corpora": [{
                "id": "c1", "name": "N", "path": "data",
                "bibliography": "facts/catalog.json",
            }]
        }), encoding="utf-8")
        config = wb.WorkbenchConfig.load(config_path)
        self.assertEqual(
            config.corpora[0].bibliography,
            (self.tmp / "facts" / "catalog.json").resolve(),
        )

    def test_absolute_path_kept(self):
        (self.tmp / "data").mkdir()
        external = self.tmp / "external.json"
        config_path = self.tmp / "config.json"
        config_path.write_text(json.dumps({
            "corpora": [{
                "id": "c1", "name": "N", "path": "data",
                "bibliography": str(external),
            }]
        }), encoding="utf-8")
        config = wb.WorkbenchConfig.load(config_path)
        self.assertEqual(config.corpora[0].bibliography, external.resolve())

    def test_bibliography_must_be_external(self):
        (self.tmp / "data").mkdir()
        config_path = self.tmp / "config.json"
        config_path.write_text(json.dumps({
            "corpora": [{
                "id": "c1", "name": "N", "path": "data",
                "bibliography": "data/catalog.json",
            }]
        }), encoding="utf-8")
        with self.assertRaises(wb.WorkbenchError) as ctx:
            wb.WorkbenchConfig.load(config_path)
        self.assertEqual(ctx.exception.code, "bibliography_must_be_external")

    def test_blank_bibliography_invalid(self):
        (self.tmp / "data").mkdir()
        config_path = self.tmp / "config.json"
        config_path.write_text(json.dumps({
            "corpora": [{
                "id": "c1", "name": "N", "path": "data",
                "bibliography": "   ",
            }]
        }), encoding="utf-8")
        with self.assertRaises(wb.WorkbenchError) as ctx:
            wb.WorkbenchConfig.load(config_path)
        self.assertEqual(ctx.exception.code, "config_invalid")


class BibliographyOverviewTests(unittest.TestCase):
    """Overview integration and the read-only guarantee."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bib-wb-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "corpus"
        self.root.mkdir()
        conn = sqlite3.connect(self.root / "dialogue.sqlite")
        conn.executescript(FULL_SCHEMA)
        seed_full(conn)
        conn.commit()
        conn.close()

    def entry(self, **overrides):
        base = {
            "work_id": "wk-1",
            "title_ja": "作品タイトル",
            "category": "文庫",
            "edition_variant": "通常版",
            "release_date": "2020-01-02",
            "isbn13": VALID_ISBN,
            "publisher_url": "https://www.kadokawa.co.jp/product/1",
            "field_sources": {"title_ja": "https://www.kadokawa.co.jp/x"},
            "uncertainties": ["発売日は推定"],
        }
        base.update(overrides)
        return base

    def catalog_path(self, payload=None, name="catalog.json") -> Path:
        payload = payload if payload is not None else {
            "schema": bib.SCHEMA,
            "verified_at_utc": "2024-05-06T07:08:09Z",
            "entries": [self.entry()],
        }
        path = self.tmp / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def store_with(self, bibliography):
        config = wb.WorkbenchConfig(
            [wb.CorpusConfig("c1", "Corpus One", self.root, bibliography=bibliography)],
            None,
        )
        store = wb.WorkbenchStore(config)
        self.addCleanup(store.close)
        return store

    def corpus(self, store):
        overview = store.overview()
        return overview["corpora"][0]

    def test_overview_exposes_catalog(self):
        corpus = self.corpus(self.store_with(self.catalog_path()))
        pb = corpus["publisher_bibliography"]
        self.assertTrue(pb["available"])
        self.assertEqual(pb["total"], 1)
        self.assertEqual(pb["verified_at_utc"], "2024-05-06T07:08:09Z")
        self.assertFalse(pb["corpus_updated"])
        self.assertFalse(pb["local_edition_mapping_verified"])
        self.assertEqual(pb["entries"][0]["work_id"], "wk-1")
        self.assertGreaterEqual(corpus["counts"]["dialogues"], 1)

    def test_overview_not_configured(self):
        pb = self.corpus(self.store_with(None))["publisher_bibliography"]
        self.assertFalse(pb["available"])
        self.assertNotIn("error", pb)

    def test_invalid_catalog_keeps_corpus_working(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        corpus = self.corpus(self.store_with(bad))
        pb = corpus["publisher_bibliography"]
        self.assertFalse(pb["available"])
        self.assertEqual(pb["error"], "bibliography_invalid_json")
        # Every other corpus source still works.
        self.assertGreaterEqual(corpus["counts"]["dialogues"], 1)
        self.assertTrue(corpus["available"])

    def test_missing_catalog_not_total_zero_catalog(self):
        pb = self.corpus(self.store_with(self.tmp / "absent.json"))["publisher_bibliography"]
        self.assertFalse(pb["available"])
        self.assertEqual(pb["error"], "bibliography_missing")

    def test_no_source_mutation(self):
        catalog = self.catalog_path()
        db = self.root / "dialogue.sqlite"
        db_before = db.read_bytes()
        db_mtime = db.stat().st_mtime_ns
        catalog_before = catalog.read_bytes()
        store = self.store_with(catalog)
        store.overview()
        store.overview()
        self.assertEqual(db.read_bytes(), db_before)
        self.assertEqual(db.stat().st_mtime_ns, db_mtime)
        self.assertEqual(catalog.read_bytes(), catalog_before)
        # Nothing new was written into the corpus directory.
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["dialogue.sqlite"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
