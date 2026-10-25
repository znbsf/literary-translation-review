# -*- coding: utf-8 -*-
"""Synthetic-only regression tests for corpus overlap gates."""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ltr.tools import leak_scan  # noqa: E402
from ltr.tools.corpus_overlap import (candidate_windows, has_corpus_overlap,
                                       match_corpus_windows_to_files,
                                       matching_window_count)  # noqa: E402


CORPUS_TEXT = "这是一段用于测试的连续中文语料文本"
ALLOWED_INPUTS = (
    "corpus/private/attribution-run/blind-input.json",
    "corpus/private/attribution-review/sample.json",
    "corpus/private/attribution-review/review.html",
    "corpus/private/ocr-review-a/review.html",
    "corpus/private/ocr-review-b/review.html",
)


def make_database(path: Path, paragraphs: list[str] | None = None) -> None:
    con = sqlite3.connect(path)
    try:
        with con:
            con.execute("create table paragraphs (text text not null)")
            con.executemany("insert into paragraphs values (?)",
                            [(text,) for text in (paragraphs or [CORPUS_TEXT])])
    finally:
        con.close()








class ScannerGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        corpus_dir = self.root / "corpus"
        (corpus_dir / "private").mkdir(parents=True)
        make_database(corpus_dir / "dialogue.sqlite")
        (self.root / "docs").mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_scan(self) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            declared = self.root / "declared.json"
            declared.write_text(json.dumps({"declared_inputs": list(ALLOWED_INPUTS),
                                            "excluded_prefixes": {"corpus/private/ocr-review-a/dependencies": "runtime"}}),
                                encoding="utf-8")
            result = leak_scan.main(["--root", str(self.root), "--corpus", "corpus", "--declared", str(declared)])
        return result, output.getvalue()

    def test_docs_and_arbitrary_run_review_names_are_not_exempt(self) -> None:
        (self.root / "docs" / "review.html").write_text(CORPUS_TEXT, encoding="utf-8")
        (self.root / "corpus" / "private" / "arbitrary-run").mkdir()
        (self.root / "corpus" / "private" / "arbitrary-run" /
         "review.html").write_text(CORPUS_TEXT, encoding="utf-8")
        code, output = self.run_scan()
        self.assertEqual(code, 1)
        self.assertIn("docs/review.html", output)
        self.assertIn("arbitrary-run/review.html", output)
        self.assertNotIn(CORPUS_TEXT, output)

    def test_named_private_review_input_is_exempt(self) -> None:
        for relative in ALLOWED_INPUTS:
            path = self.root.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(CORPUS_TEXT, encoding="utf-8")
        code, output = self.run_scan()
        self.assertEqual(code, 0)
        self.assertIn("精确路径豁免复核输入 = 5 个", output)
        for relative in ALLOWED_INPUTS:
            self.assertIn(relative, output)

    def test_does_not_match_across_paragraph_or_punctuation_boundary(self) -> None:
        self.assertFalse(has_corpus_overlap(
            "甲乙丙丁戊己庚辛壬癸", "甲乙丙丁\n戊己庚辛壬癸", 8))
        self.assertFalse(has_corpus_overlap(
            "甲乙丙丁，戊己庚辛壬癸", "甲乙丙丁戊己庚辛壬癸", 8))
        self.assertTrue(has_corpus_overlap(
            "前置あいうえおかきく后置", "あいうえおかきく", 8))

    def test_single_corpus_pass_matches_naive_per_file_counts(self) -> None:
        corpus = "甲乙丙丁戊己庚辛壬癸abcdefgh\n新的段落甲乙丙丁戊己庚辛"
        outputs = {
            "a.md": "前缀甲乙丙丁戊己庚辛壬癸后缀",
            "b.md": "甲乙丙丁，戊己庚辛壬癸",
            "c.md": "新的段落甲乙丙丁戊己庚辛",
        }
        window_files: dict[str, list[str]] = {}
        for path, text in outputs.items():
            for window in set(candidate_windows(text, 8)):
                window_files.setdefault(window, []).append(path)
        actual = match_corpus_windows_to_files(window_files, corpus, 8)
        expected = {path: count for path, text in outputs.items()
                    if (count := matching_window_count(text, corpus, 8))}
        self.assertEqual(actual, expected)
        all_windows = {window for text in outputs.values()
                       for window in candidate_windows(text, 8)}
        self.assertEqual(set(window_files),
                         {window for window in all_windows if window not in corpus})

    def test_unicode_decode_failure_fails_with_source_free_diagnostic(self) -> None:
        (self.root / "docs" / "bad.txt").write_bytes(b"\xff\xfe")
        code, output = self.run_scan()
        self.assertEqual(code, 1)
        self.assertIn("docs/bad.txt (UnicodeDecodeError)", output)
        self.assertIn("读取失败文件数 = 1", output)
        self.assertNotIn(CORPUS_TEXT, output)

    def test_only_the_named_ocr_dependencies_directory_is_excluded(self) -> None:
        private = self.root / "corpus" / "private"
        excluded = private / "ocr-review-a" / "dependencies" / "runtime.pyd"
        excluded.parent.mkdir(parents=True)
        excluded.write_bytes(b"\xff\xfe")

        other_files = (
            self.root / "docs" / "dependencies" / "payload.pyd",
            private / "arbitrary-run" / "dependencies" / "payload.whl",
            private / "ocr-review-a" / "dependencies-unrelated" / "payload.dll",
        )
        for path in other_files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xff\xfe")

        code, output = self.run_scan()
        self.assertEqual(code, 1)
        self.assertIn("精确路径排除文件数 = 1", output)
        self.assertIn("corpus/private/ocr-review-a/dependencies（runtime）", output)
        self.assertIn("读取失败文件数 = 3", output)
        self.assertIn("docs/dependencies/payload.pyd (UnicodeDecodeError)", output)
        self.assertIn("private/arbitrary-run/dependencies/payload.whl (UnicodeDecodeError)", output)
        self.assertIn(
            "private/ocr-review-a/dependencies-unrelated/payload.dll (UnicodeDecodeError)",
            output)
        self.assertNotIn(CORPUS_TEXT, output)


if __name__ == "__main__":
    unittest.main()
