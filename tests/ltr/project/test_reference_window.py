"""Synthetic candidate-edition windows only; never accesses collection files."""
import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ltr.project.epub import EpubBook
from ltr.project.reference_window import extract_reference_window, ReferenceWindowError
from tests.ltr.project.test_epub import CONTAINER, OPF


class ReferenceWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def txt(self, value="目录\n\n合成正文甲\n合成正文乙\n后文", encoding="utf-8"):
        path = self.root / "synthetic.txt"
        path.write_bytes(value.encode(encoding))
        return path

    def epub(self):
        path = self.root / "synthetic.epub"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("META-INF/container.xml", CONTAINER)
            archive.writestr("OPS/book.opf", OPF)
            archive.writestr("OPS/one.xhtml", "<!DOCTYPE html><html><body><p>合成甲</p><p>合成乙</p></body></html>")
            archive.writestr("OPS/two.xhtml", "Invalid XML; must never read")
        return path

    def test_txt_explicit_lines_hash_and_offsets(self):
        path = self.txt()
        result = extract_reference_window(path, start_line=3, end_line=4)
        self.assertEqual([row["text"] for row in result["paragraphs"]], ["合成正文甲", "合成正文乙"])
        self.assertEqual([row["id"] for row in result["paragraphs"]], ["R0001", "R0002"])
        self.assertEqual(result["paragraphs"][0]["locator"]["decoded_char_start"], 4)
        self.assertEqual(result["source_hash"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(result["scope"]["alignment_status"], "unverified_candidate")

    def test_no_automatic_body_detection(self):
        with self.assertRaisesRegex(ReferenceWindowError, "REFERENCE_EXPLICIT_LINES_REQUIRED"):
            extract_reference_window(self.txt())
        with self.assertRaisesRegex(ReferenceWindowError, "REFERENCE_EXPLICIT_SPINE_REQUIRED"):
            extract_reference_window(self.epub())

    def test_limit_clips_row_and_locator(self):
        result = extract_reference_window(self.txt(), start_line=3, max_characters=7)
        self.assertEqual([row["text"] for row in result["paragraphs"]], ["合成正文甲", "合成"])
        self.assertEqual(result["paragraphs"][1]["locator"]["end"], 2)
        self.assertTrue(result["scope"]["truncated"])

    def test_hard_limit_all_characters(self):
        result = extract_reference_window(self.txt("文。" * 6000), start_line=1)
        self.assertEqual(sum(len(r["text"]) for r in result["paragraphs"]), 10000)
        with self.assertRaisesRegex(ReferenceWindowError, "REFERENCE_INVALID_LIMIT"):
            extract_reference_window(self.txt(), start_line=1, max_characters=10001)

    def test_epub_opens_only_explicit_spine(self):
        path = self.epub()
        original, opened = EpubBook.read, []
        def read(book, name):
            opened.append(name)
            return original(book, name)
        with patch.object(EpubBook, "read", read):
            result = extract_reference_window(path, spine_id="one", start_paragraph=1)
        self.assertNotIn("OPS/two.xhtml", opened)
        self.assertEqual(result["paragraphs"][0]["text"], "合成乙")
        self.assertEqual(result["paragraphs"][0]["locator"]["element_path"], "/html[1]/body[1]/p[2]")

    def test_unknown_spine_and_paragraph_rejected(self):
        path = self.epub()
        with self.assertRaisesRegex(ReferenceWindowError, "REFERENCE_UNKNOWN_SPINE"):
            extract_reference_window(path, spine_id="missing")
        with self.assertRaisesRegex(ReferenceWindowError, "REFERENCE_EMPTY_SELECTION"):
            extract_reference_window(path, spine_id="one", start_paragraph=2)

    def test_line_bounds_and_empty_rejected(self):
        path = self.txt()
        with self.assertRaisesRegex(ReferenceWindowError, "REFERENCE_LINE_OUT_OF_RANGE"):
            extract_reference_window(path, start_line=1, end_line=999)
        with self.assertRaisesRegex(ReferenceWindowError, "REFERENCE_EMPTY_SELECTION"):
            extract_reference_window(path, start_line=2, end_line=2)

    def test_explicit_legacy_encoding(self):
        path = self.txt("合成正文", "gb18030")
        result = extract_reference_window(path, start_line=1, encoding="gb18030")
        self.assertEqual(result["paragraphs"][0]["text"], "合成正文")
        with self.assertRaisesRegex(ReferenceWindowError, "REFERENCE_INVALID_INPUT"):
            extract_reference_window(path, start_line=1)

    def test_no_paths_in_error(self):
        with self.assertRaises(ReferenceWindowError) as caught:
            extract_reference_window(self.root / "sensitive-missing.txt", start_line=1)
        self.assertEqual(str(caught.exception), "REFERENCE_INVALID_INPUT")

    def test_unified_cli_saves_private_window_without_echo_or_overwrite(self):
        from ltr.project.__main__ import main

        source = self.txt()
        output = self.root / "window.json"
        args = ["reference-window", str(source), "--output", str(output),
                "--start-line", "3", "--end-line", "4"]
        captured = io.StringIO()
        with redirect_stdout(captured):
            self.assertEqual(main(args), 0)
            before = output.read_bytes()
            self.assertEqual(main(args), 2)
            self.assertEqual(output.read_bytes(), before)
        saved = json.loads(before)
        self.assertEqual([row["text"] for row in saved["paragraphs"]],
                         ["合成正文甲", "合成正文乙"])
        self.assertNotIn("合成正文", captured.getvalue())
        self.assertNotIn(str(self.root), captured.getvalue())
        repo_output = Path(__file__).resolve().parents[3] / "rejected-window.json"
        with patch("ltr.project.reference_window.extract_reference_window") as extract:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["reference-window", str(source),
                                       "--output", str(repo_output), "--start-line", "3"]), 2)
            extract.assert_not_called()
        self.assertFalse(repo_output.exists())


if __name__ == "__main__":
    unittest.main()
