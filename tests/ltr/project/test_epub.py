"""Only synthetic EPUB fixtures; no collection data or model calls."""
import hashlib
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from ltr.project import epub


CONTAINER = b'<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>'
OPF = b'''<package><metadata><title>Synthetic</title><language>ja</language></metadata>
<manifest><item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>
<item id="two" href="two.xhtml" media-type="application/xhtml+xml"/></manifest>
<spine><itemref idref="one"/><itemref idref="two"/></spine></package>'''
BODY = '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>A <ruby>猫<rp>(</rp><rt>ねこ</rt><rp>)</rp></ruby> B.</p><p>Second.</p></body></html>'.encode()


class EpubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "synthetic.epub"

    def make(self, replacements=None, extras=(), compression=zipfile.ZIP_STORED):
        entries = {"META-INF/container.xml": CONTAINER, "OPS/book.opf": OPF,
                   "OPS/one.xhtml": BODY, "OPS/two.xhtml": b'not XML; deliberately unreadable'}
        entries.update(replacements or {})
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.path, "w", compression=compression) as archive:
                for key, value in entries.items():
                    archive.writestr(key, value)
                for key, value in extras:
                    archive.writestr(key, value)
        return self.path

    def rejects(self, code, operation=None):
        with self.assertRaises(epub.EpubError) as caught:
            (operation or (lambda: epub.inspect_epub(self.path)))()
        self.assertEqual(str(caught.exception), code)

    def test_metadata_does_not_open_body_or_navigation(self):
        self.make(extras=[("OPS/nav.xhtml", b'<!DOCTYPE invalid>'), ("OPS/toc.ncx", b'bad')])
        original = epub.EpubBook.read
        opened = []
        def record(book, name):
            opened.append(name)
            return original(book, name)
        with patch.object(epub.EpubBook, "read", record):
            result = epub.inspect_epub(self.path)
        self.assertEqual(opened, ["META-INF/container.xml", "OPS/book.opf"])
        self.assertEqual(result["title"], "Synthetic")
        self.assertEqual(result["language"], "ja")
        self.assertEqual(result["book_hash"], hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_partial_selection_ruby_and_locations(self):
        self.make()
        result = epub.import_items(self.path, ["one"])
        self.assertEqual(len(result["items"]), 1)
        rows = result["items"][0]["paragraphs"]
        self.assertEqual([r["text"] for r in rows], ["A 猫 B.", "Second."])
        self.assertEqual(rows[0]["ruby"], [{"base": "猫", "reading": "ねこ"}])
        self.assertEqual(rows[0]["locator"], {"item_id": "one", "element_path": "/html[1]/body[1]/p[1]", "start": 0, "end": 6})
        self.assertEqual(rows[1]["id"], "one:p000002")
        self.assertEqual(rows[1]["locator"]["element_path"], "/html[1]/body[1]/p[2]")

    def test_selected_bad_body_rejected(self):
        self.make()
        self.rejects("EPUB_INVALID_XML", lambda: epub.import_items(self.path, ["two"]))

    def test_unknown_item_rejected_before_any_body_read(self):
        self.make()
        self.rejects("EPUB_UNKNOWN_ITEM", lambda: epub.import_items(self.path, ["one", "missing"]))

    def test_invalid_selections(self):
        self.make()
        for selection in (None, [], ["one", "one"], "one", [1], [[]]):
            with self.subTest(selection=repr(selection)):
                self.rejects("EPUB_INVALID_SELECTION", lambda: epub.import_items(self.path, selection))

    def test_zip_traversal_rejected(self):
        for name in ("../escape", "/absolute", "OPS/../escape", "OPS\\escape", "C:/escape"):
            with self.subTest(name=name):
                self.make(extras=[(name, b"x")])
                if "\\" in name:
                    # Python's Windows ZIP writer normalizes separators itself.
                    self.path.write_bytes(self.path.read_bytes().replace(b"OPS/escape", b"OPS\\escape"))
                self.rejects("EPUB_UNSAFE_PATH")

    def test_encoded_manifest_traversal_rejected(self):
        self.make({"OPS/book.opf": OPF.replace(b"one.xhtml", b"%2e%2e/escape")})
        self.rejects("EPUB_UNSAFE_PATH")

    def test_remote_manifest_rejected(self):
        self.make({"OPS/book.opf": OPF.replace(b"one.xhtml", b"https://example.invalid/a")})
        self.rejects("EPUB_UNSAFE_PATH")

    def test_nonspine_same_media_alias_allowed(self):
        aliases = (b'<item id="cover" href="cover.jpg" media-type="image/jpeg"/>'
                   b'<item id="cover-alias" href="cover.jpg" media-type="image/jpeg"/>')
        self.make({"OPS/book.opf": OPF.replace(b"</manifest>", aliases + b"</manifest>")},
                  extras=[("OPS/cover.jpg", b"synthetic image placeholder")])
        self.assertEqual(len(epub.inspect_epub(self.path)["spine"]), 2)
        self.assertEqual(len(epub.import_items(self.path, ["one"])["items"]), 1)

    def test_nonspine_conflicting_media_alias_rejected(self):
        aliases = (b'<item id="cover" href="cover.jpg" media-type="image/jpeg"/>'
                   b'<item id="cover-alias" href="cover.jpg" media-type="image/png"/>')
        self.make({"OPS/book.opf": OPF.replace(b"</manifest>", aliases + b"</manifest>")},
                  extras=[("OPS/cover.jpg", b"synthetic image placeholder")])
        self.rejects("EPUB_INVALID_MANIFEST")

    def test_alias_touching_spine_rejected(self):
        alias = b'<item id="alias" href="one.xhtml" media-type="application/xhtml+xml"/>'
        self.make({"OPS/book.opf": OPF.replace(b"</manifest>", alias + b"</manifest>")})
        self.rejects("EPUB_INVALID_MANIFEST")

    def test_duplicate_entry_rejected(self):
        self.make(extras=[("OPS/one.xhtml", BODY)])
        self.rejects("EPUB_DUPLICATE_ENTRY")

    def test_symlink_rejected(self):
        link = zipfile.ZipInfo("OPS/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.make(extras=[(link, b"target")])
        self.rejects("EPUB_SYMLINK")

    def test_drm_rejected(self):
        self.make(extras=[("META-INF/encryption.xml", b"unused")])
        self.rejects("EPUB_ENCRYPTED")

    def test_encrypted_flag_rejected(self):
        self.make()
        blob = bytearray(self.path.read_bytes())
        pos = blob.index(b"PK\x01\x02")
        blob[pos + 8] |= 1
        self.path.write_bytes(blob)
        self.rejects("EPUB_ENCRYPTED")

    def test_expansion_ratio_rejected(self):
        self.make(extras=[("OPS/large", b"a" * 200_000)], compression=zipfile.ZIP_DEFLATED)
        self.rejects("EPUB_EXPANSION_LIMIT")

    def test_archive_size_limit(self):
        self.make()
        with patch.object(epub, "MAX_ARCHIVE_BYTES", 20):
            self.rejects("EPUB_ARCHIVE_LIMIT")

    def test_entry_count_limit(self):
        self.make()
        with patch.object(epub, "MAX_ENTRIES", 2):
            self.rejects("EPUB_ENTRY_LIMIT")

    def test_total_size_limit(self):
        self.make()
        with patch.object(epub, "MAX_TOTAL_BYTES", 20):
            self.rejects("EPUB_EXPANSION_LIMIT")

    def test_doctype_and_entity_rejected(self):
        for prefix in (b'<!DOCTYPE html>', b'<!ENTITY x SYSTEM "file:///private">'):
            with self.subTest(prefix=prefix):
                self.make({"OPS/one.xhtml": prefix + BODY})
                self.rejects("EPUB_UNSAFE_XML", lambda: epub.import_items(self.path, ["one"]))

    def test_utf16_declaration_bypass_rejected(self):
        self.make({"OPS/one.xhtml": '<!DOCTYPE html><html/>'.encode("utf-16")})
        self.rejects("EPUB_UNSAFE_XML", lambda: epub.import_items(self.path, ["one"]))

    def test_xml_depth_limit(self):
        self.make({"OPS/one.xhtml": b"<html><body>" + b"<div>" * 130 + b"x" + b"</div>" * 130 + b"</body></html>"})
        self.rejects("EPUB_XML_LIMIT", lambda: epub.import_items(self.path, ["one"]))

    def test_unknown_spine_reference_rejected(self):
        self.make({"OPS/book.opf": OPF.replace(b'idref="two"', b'idref="missing"')})
        self.rejects("EPUB_INVALID_SPINE")

    def test_duplicate_spine_reference_rejected(self):
        self.make({"OPS/book.opf": OPF.replace(b'idref="two"', b'idref="one"')})
        self.rejects("EPUB_INVALID_SPINE")

    def test_inline_and_nested_block_content_preserved_without_scripts(self):
        self.make({"OPS/one.xhtml": b'<html><body><div>Before<p>A <em>B</em><br/>C<script>SECRET</script>.</p>After</div></body></html>'})
        rows = epub.import_items(self.path, ["one"])["items"][0]["paragraphs"]
        self.assertEqual([r["text"] for r in rows], ["Before", "A B\nC.", "After"])

    def test_significant_br_and_unicode_spaces_preserved(self):
        source = '<html><body><p>　A\n   B<br/>\n C<br/><br/>　D　</p></body></html>'.encode()
        self.make({"OPS/one.xhtml": source})
        row = epub.import_items(self.path, ["one"])["items"][0]["paragraphs"][0]
        self.assertEqual(row["text"], "　A B\nC\n\n　D　")
        self.assertEqual(row["locator"]["end"], len(row["text"]))

    def test_pre_whitespace_preserved_and_mode_restored(self):
        source = b'<html><body><pre>  A\n\t<span>B</span>  \n</pre><p>  C\n D  </p></body></html>'
        self.make({"OPS/one.xhtml": source})
        rows = epub.import_items(self.path, ["one"])["items"][0]["paragraphs"]
        self.assertEqual([r["text"] for r in rows], ["  A\n\tB  \n", "C D"])

    def test_private_xhtml_snapshot_exact_and_not_paragraph_content(self):
        source = b'\xef\xbb\xbf<html><body><p>Synthetic.</p><script>doNotExecute()</script></body></html>'
        self.make({"OPS/one.xhtml": source})
        item = epub.import_items(self.path, ["one"])["items"][0]
        self.assertEqual(item["source_xhtml"].encode(item["source_encoding"]), source)
        self.assertEqual(item["source_sha256"], hashlib.sha256(source).hexdigest())
        self.assertEqual([r["text"] for r in item["paragraphs"]], ["Synthetic."])

    def test_non_utf8_snapshot_fails_closed(self):
        source = '<?xml version="1.0" encoding="iso-8859-1"?><html><body><p>é</p></body></html>'.encode("iso-8859-1")
        self.make({"OPS/one.xhtml": source})
        self.rejects("EPUB_UNSUPPORTED_ENCODING", lambda: epub.import_items(self.path, ["one"]))

    def test_snapshot_hash_and_content_survive_file_replacement(self):
        self.make()
        expected_hash = hashlib.sha256(self.path.read_bytes()).hexdigest()
        original = epub.EpubBook.read
        def replace_after_snapshot(book, name):
            self.path.write_bytes(b"replaced file")
            return original(book, name)
        with patch.object(epub.EpubBook, "read", replace_after_snapshot):
            result = epub.import_items(self.path, ["one"])
        self.assertEqual(result["book_hash"], expected_hash)
        self.assertEqual(result["items"][0]["paragraphs"][0]["text"], "A 猫 B.")

    def test_io_error_does_not_expose_path(self):
        self.rejects("EPUB_INVALID_ARCHIVE")

    def test_source_symlink_rejected_before_open(self):
        self.make()
        with patch.object(Path, "is_symlink", return_value=True), patch.object(Path, "open") as opened:
            self.rejects("EPUB_SOURCE_LINK")
            opened.assert_not_called()

    def test_source_parent_link_rejected_before_open(self):
        self.make()
        with patch.object(Path, "is_symlink", side_effect=lambda: False):
            with patch.object(Path, "is_junction", side_effect=[False, True], create=True), patch.object(Path, "open") as opened:
                self.rejects("EPUB_SOURCE_LINK")
                opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
