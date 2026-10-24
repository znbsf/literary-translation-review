"""Complete-volume import tests use exclusively synthetic fixtures."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ltr.project.corpus_import import import_book, import_text, partition_learning
from ltr.project.epub import EpubError
from tests.ltr.project.test_epub import CONTAINER, OPF


class CorpusImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "synthetic.epub"

    def make(self, one="<p>First</p>", two="<p>Second</p>", declaration=""):
        with zipfile.ZipFile(self.path, "w") as archive:
            archive.writestr("META-INF/container.xml", CONTAINER)
            archive.writestr("OPS/book.opf", OPF)
            for name, body in (("one", one), ("two", two)):
                archive.writestr(f"OPS/{name}.xhtml", declaration + f"<html><body>{body}</body></html>")

    def test_spine_order_unique_ids_and_hash(self):
        self.make("<p>First</p><p>Second</p>", "<p>Third</p>")
        source = import_book(self.path)
        self.assertEqual([r["text"] for r in source["paragraphs"]], ["First", "Second", "Third"])
        self.assertEqual(len({r["id"] for r in source["paragraphs"]}), 3)
        self.assertEqual(source["book_hash"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(source["scope"]["spine_ids"], ["one", "two"])
        self.assertEqual(source["metadata"]["body_language"], "unverified")
        for item in source["items"]:
            self.assertEqual(hashlib.sha256(item["source_xhtml"].encode()).hexdigest(), item["source_sha256"])
        self.assertEqual(source["paragraphs"][-1]["locator"]["spine_index"], 1)

    def test_blank_layout_and_original_whitespace_preserved(self):
        body = "<p>A  B</p><p> \t </p><p><br/></p><p>　</p><pre> x  y\n z </pre>"
        self.make(body)
        source = import_book(self.path)
        self.assertEqual([r["text"] for r in source["paragraphs"]], ["A B", " x  y\n z ", "Second"])
        self.assertEqual(len(source["layout"]), 3)
        self.assertIn(" \t ", [r["text"] for r in source["layout"]])
        self.assertIn(body, source["items"][0]["source_xhtml"])

    def test_image_only_spine_is_recorded_but_not_learning_text(self):
        self.make('<div><img src="cover.png"/></div>')
        source = import_book(self.path)
        self.assertTrue(source["items"][0]["textless"])
        self.assertEqual(source["items"][0]["image_elements"], 1)
        self.assertEqual(source["items"][0]["paragraph_ids"], [])
        self.assertFalse(source["scope"]["image_text_verified"])
        self.assertEqual(source["scope"]["parsed_spine_count"], 2)

    def test_safe_doctype_and_unsafe_declarations(self):
        self.make(declaration="<!DOCTYPE html>")
        self.assertEqual(len(import_book(self.path)["paragraphs"]), 2)
        for declaration in ('<!DOCTYPE html SYSTEM "https://invalid.test/file">',
                            '<!DOCTYPE html [<!ENTITY x "oops">]>', '<!ENTITY x "oops">'):
            self.make(declaration=declaration)
            with self.assertRaisesRegex(EpubError, "EPUB_UNSAFE_XML"):
                import_book(self.path)

    def test_later_invalid_spine_fails_instead_of_partial_success(self):
        self.make(two="<p>broken")
        with self.assertRaisesRegex(EpubError, "EPUB_INVALID_XML"):
            import_book(self.path)

    def test_exact_xhtml11_declaration_and_nbsp_are_inert_compatible(self):
        declaration = '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">'
        self.make("<p>A&nbsp;B</p>", declaration=declaration)
        source = import_book(self.path)
        self.assertEqual(source["paragraphs"][0]["text"], "A\u00a0B")
        self.assertIn(declaration, source["items"][0]["source_xhtml"])
        self.assertEqual(source["items"][0]["parser_compatibility"],
                         ["inert_xhtml11_declaration", "standard_nbsp_entity"])
        self.make(declaration=declaration.replace("http://www.w3.org/", "https://invalid.test/"))
        with self.assertRaisesRegex(EpubError, "EPUB_UNSAFE_XML"):
            import_book(self.path)
        self.make(declaration=declaration[:-1] + ' [<!ENTITY x "oops">]>')
        with self.assertRaisesRegex(EpubError, "EPUB_UNSAFE_XML"):
            import_book(self.path)

    def test_leading_textless_image_repair_retains_all_body_text_and_image(self):
        self.make("<p>A<ruby>B<rt>bee</rt></ruby>C</p>", declaration='<img src="cover.png" alt="cover"/>')
        source = import_book(self.path)
        self.assertEqual([r["text"] for r in source["paragraphs"]], ["ABC", "Second"])
        self.assertEqual(source["items"][0]["image_elements"], 1)
        self.assertEqual(source["paragraphs"][0]["ruby"], [{"base": "B", "reading": "bee"}])
        self.assertEqual(source["paragraphs"][0]["locator"]["element_path_basis"], "compatibility_dom")
        self.assertIn('<img src="cover.png" alt="cover"/>', source["items"][0]["source_xhtml"])
        self.make("<p>All body text</p>", declaration='<img src="cover.png"/><?xml version="1.0" encoding="utf-8"?>')
        self.assertEqual([r["text"] for r in import_book(self.path)["paragraphs"]], ["All body text", "Second"])

    def test_misplaced_nonimage_or_text_is_not_silently_removed(self):
        for prefix in ("<p>Hidden text</p>", '<img src="x"/>Hidden text', '<img src="x"/><img src="y"/>'):
            self.make(declaration=prefix)
            with self.assertRaisesRegex(EpubError, "EPUB_INVALID_XML"):
                import_book(self.path)

    def test_partition_complete_exact_order_and_long_paragraph_not_split(self):
        self.make("<p>AAA</p><p>BBBBBBBBBB</p><p>CCC</p>", "<p>DD</p>")
        source = import_book(self.path)
        result = partition_learning(source, 5)
        self.assertEqual([c["characters"] for c in result["chunks"]], [3, 10, 5])
        self.assertTrue(result["chunks"][1]["exceeds_target"])
        self.assertEqual([r["id"] for c in result["chunks"] for r in c["paragraphs"]],
                         [r["id"] for r in source["paragraphs"]])
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["coverage"]["source_count"], 4)
        self.assertTrue(all(c["book_hash"] == source["book_hash"] for c in result["chunks"]))

    def test_partition_rejects_invalid_rows_and_sizes(self):
        self.make()
        source = import_book(self.path)
        for size in (0, -1, True, 1.2, "5"):
            with self.assertRaisesRegex(EpubError, "CORPUS_INVALID_PARTITION_SIZE"):
                partition_learning(source, size)
        source["paragraphs"].append(source["paragraphs"][0])
        with self.assertRaisesRegex(EpubError, "CORPUS_INVALID_SOURCE"):
            partition_learning(source)

    def test_text_utf8_and_gb18030_explicit_decoding_hash_and_offsets(self):
        path = Path(self.tmp.name) / "synthetic.txt"
        text = "样例甲\r\n\r\n样例乙\n"
        for encoding in ("utf-8", "utf-8-sig", "gb18030"):
            raw = text.encode(encoding)
            path.write_bytes(raw)
            source = import_text(path, encoding=encoding)
            self.assertEqual(source["source_text"], text)
            self.assertEqual(source["book_hash"], hashlib.sha256(raw).hexdigest())
            self.assertEqual([r["text"] for r in source["paragraphs"]], ["样例甲\r\n", "样例乙\n"])
            self.assertEqual(len(source["layout"]), 1)
            for row in source["paragraphs"] + source["layout"]:
                loc = row["locator"]
                self.assertEqual(text[loc["start"]:loc["end"]], row["text"])
            self.assertEqual(partition_learning(source)["coverage"]["covered_count"], 2)

    def test_text_encoding_errors_are_fixed_and_no_fallback(self):
        path = Path(self.tmp.name) / "synthetic.txt"
        path.write_bytes("合成".encode("gb18030"))
        with self.assertRaisesRegex(EpubError, "^CORPUS_DECODE_FAILED$"):
            import_text(path, encoding="utf-8")
        with self.assertRaisesRegex(EpubError, "^CORPUS_UNSUPPORTED_ENCODING$"):
            import_text(path, encoding="auto")

    def test_utf16_requires_bom_and_supports_both_endiannesses(self):
        path = Path(self.tmp.name) / "synthetic.txt"
        text = "合成甲\r\n\r\n合成乙\n"
        for bom, encoding in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
            raw = bom + text.encode(encoding)
            path.write_bytes(raw)
            source = import_text(path, encoding="utf-16")
            self.assertEqual(source["source_text"], text)
            self.assertEqual(source["book_hash"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(source["source_encoding"], "utf-16")
            self.assertEqual(len(source["paragraphs"]), 2)
            self.assertEqual(len(source["layout"]), 1)
            path.write_bytes(text.encode(encoding))
            with self.assertRaisesRegex(EpubError, "^CORPUS_UTF16_BOM_REQUIRED$"):
                import_text(path, encoding="utf-16")

    def test_utf16_incomplete_code_unit_fails_strictly(self):
        path = Path(self.tmp.name) / "synthetic.txt"
        path.write_bytes(b"\xff\xfe\x41")
        with self.assertRaisesRegex(EpubError, "^CORPUS_DECODE_FAILED$"):
            import_text(path, encoding="utf-16")

    def test_text_limit_and_empty_source(self):
        path = Path(self.tmp.name) / "synthetic.txt"
        path.write_bytes(b"12345")
        with patch("ltr.project.corpus_import.MAX_TEXT_BYTES", 4):
            with self.assertRaisesRegex(EpubError, "CORPUS_TEXT_LIMIT"):
                import_text(path, encoding="utf-8")
        path.write_bytes(b" \n\t")
        with self.assertRaisesRegex(EpubError, "CORPUS_NO_TEXT"):
            import_text(path, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
