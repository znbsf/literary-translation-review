"""Synthetic chapter-boundary tests; no private prose or model calls."""
import hashlib
import tempfile
from pathlib import Path
import unittest
import zipfile
from unittest.mock import patch

from ltr.project.epub import EpubError, EpubBook
from ltr.project.selected_chapter import extract_selected_chapter
from tests.ltr.project.test_epub import CONTAINER, OPF


class SelectedChapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "synthetic.epub"

    def make(self, one, two="<p>Next chapter</p>", declaration=""):
        with zipfile.ZipFile(self.path, "w") as archive:
            archive.writestr("META-INF/container.xml", CONTAINER)
            archive.writestr("OPS/book.opf", OPF)
            for key, body in (("one", one), ("two", two)):
                archive.writestr("OPS/" + key + ".xhtml", declaration + "<html><body>" + body + "</body></html>")

    def run_selection(self, start="OPS/one.xhtml#start", end="OPS/one.xhtml#end"):
        return extract_selected_chapter(self.path, {"match_count": 1,
            "book_hash": hashlib.sha256(self.path.read_bytes()).hexdigest(),
            "start_href": start, "end_href": end})

    def test_same_file_excludes_next_chapter_and_keeps_original_path(self):
        self.make('<p>Before</p><h2 id="start">Chosen</h2><p>Body</p><h2 id="end">Next</h2><p>After</p>')
        result = self.run_selection()
        self.assertEqual([p["text"] for p in result["paragraphs"]], ["Chosen", "Body"])
        self.assertEqual(result["paragraphs"][1]["locator"]["element_path"], "/html[1]/body[1]/p[2]")

    def test_inline_start_end_and_ancestor_text_tail(self):
        self.make('<div>Before<p>Prefix<a id="start"/>Chosen<b> bold</b> tail<a id="end"/>After</p>After ancestor</div>')
        self.assertEqual([p["text"] for p in self.run_selection()["paragraphs"]], ["Chosen bold tail"])

    def test_cross_file_fragment_end(self):
        self.make('<p>Before<a id="start"/>One</p>', '<p>Two<a id="end"/>After</p>')
        result = self.run_selection(end="OPS/two.xhtml#end")
        self.assertEqual([p["text"] for p in result["paragraphs"]], ["One", "Two"])

    def test_end_file_without_fragment_never_opened(self):
        self.make('<p id="start">One</p>')
        opened = []
        original = EpubBook.read
        def read(book, name):
            opened.append(name)
            return original(book, name)
        with patch.object(EpubBook, "read", read):
            self.run_selection(end="OPS/two.xhtml")
        self.assertNotIn("OPS/two.xhtml", opened)

    def test_unknown_or_duplicate_fragment_fails(self):
        for body in ('<p id="start">One</p>', '<p id="start">One</p><p id="end"/><p id="end"/>'):
            self.make(body)
            with self.assertRaisesRegex(EpubError, "CHAPTER_AMBIGUOUS_FRAGMENT"):
                self.run_selection()

    def test_reversed_fragments_fail(self):
        self.make('<p id="end">One</p><p id="start">Two</p>')
        with self.assertRaisesRegex(EpubError, "CHAPTER_REVERSED_BOUNDARY"):
            self.run_selection()

    def test_html5_declaration_accepted_but_external_dtd_rejected(self):
        self.make('<p id="start">One</p><p id="end"/>', declaration="<!DOCTYPE html>")
        self.assertEqual(len(self.run_selection()["paragraphs"]), 1)
        self.make('<p id="start">One</p><p id="end"/>', declaration='<!DOCTYPE html SYSTEM "https://invalid.test/x.dtd">')
        with self.assertRaisesRegex(EpubError, "EPUB_UNSAFE_XML"):
            self.run_selection()

    def test_missing_end_rejected(self):
        self.make('<p id="start">One</p>')
        with self.assertRaisesRegex(EpubError, "CHAPTER_INVALID_BOUNDARY"):
            self.run_selection(end=None)

    def test_ruby_boundary_is_ambiguous(self):
        self.make('<p><ruby id="start">A<rt>B</rt></ruby></p><p id="end"/>')
        with self.assertRaisesRegex(EpubError, "CHAPTER_AMBIGUOUS_FRAGMENT"):
            self.run_selection()

    def test_stale_catalogue_rejected(self):
        self.make('<p id="start">One</p><p id="end"/>')
        with self.assertRaisesRegex(EpubError, "CHAPTER_STALE_CATALOGUE"):
            extract_selected_chapter(self.path, {"match_count": 1, "book_hash": "stale",
                "start_href": "OPS/one.xhtml#start", "end_href": "OPS/one.xhtml#end"})


if __name__ == "__main__":
    unittest.main()
