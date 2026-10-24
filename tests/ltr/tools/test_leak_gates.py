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

from ltr.tools import leak_scan, merge_identity_batches  # noqa: E402
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


def run_merge(run_dir: Path, database: Path) -> int:
    old_argv = sys.argv
    sys.argv = ["merge_identity_batches.py", str(run_dir), str(database), "ed1", "1"]
    try:
        return merge_identity_batches.main()
    finally:
        sys.argv = old_argv


def row(note: str) -> dict[str, object]:
    return {
        "dialogue_id": 1,
        "speaker_candidate": "Alice",
        "addressee_candidate": None,
        "confidence_uncalibrated": 0.75,
        "note": note,
    }


class MergeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.run = self.base / "run"
        self.run.mkdir()
        self.database = self.base / "synthetic.sqlite"
        con = sqlite3.connect(self.database)
        try:
            with con:
                con.execute("create table dialogues (id integer, edition_id text)")
                con.executemany("insert into dialogues values (?, 'ed1')", [(0,), (1,)])
                con.execute("create table paragraphs (text text, edition_id text)")
                con.execute("insert into paragraphs values (?, 'ed1')", (CORPUS_TEXT,))
        finally:
            con.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_batch(self, note: str, *, qid: int = 1, start: int | None = None) -> None:
        if start is None:
            start = qid
        data = {
            "schema": "haruhi-identity-candidates-v1",
            "edition_id": "ed1",
            "batch_start": start,
            "batch_end": start,
            "candidates": [dict(row(note), dialogue_id=qid)],
        }
        (self.run / "batch-001.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_literal_hit_fails_without_generating_outputs(self) -> None:
        self.write_batch(CORPUS_TEXT)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as result:
                run_merge(self.run, self.database)
        self.assertIn("CONTRACT VIOLATION", str(result.exception))
        self.assertIn("/1:", str(result.exception))
        self.assertNotIn(CORPUS_TEXT, str(result.exception) + stdout.getvalue())
        self.assertFalse((self.run / "merged-identity.json").exists())
        self.assertFalse((self.run / "identity-summary.json").exists())

    def test_chinese_prefix_and_suffix_do_not_hide_overlap(self) -> None:
        self.write_batch("前缀这是一段用于测试的连续中文语料文本后缀")
        with self.assertRaises(SystemExit) as result:
            run_merge(self.run, self.database)
        self.assertIn("1 item", str(result.exception))
        self.assertNotIn(CORPUS_TEXT, str(result.exception))
        self.assertFalse((self.run / "merged-identity.json").exists())

    def test_failed_merge_preserves_existing_legal_outputs(self) -> None:
        merged = self.run / "merged-identity.json"
        summary = self.run / "identity-summary.json"
        merged.write_bytes(b"prior-valid-merged-output")
        summary.write_bytes(b"prior-valid-summary")
        before = merged.read_bytes(), summary.read_bytes()
        self.write_batch("前缀" + CORPUS_TEXT + "后缀")
        with self.assertRaises(SystemExit):
            run_merge(self.run, self.database)
        self.assertEqual((merged.read_bytes(), summary.read_bytes()), before)

    def test_unmatched_note_succeeds_and_writes_outputs(self) -> None:
        self.write_batch("Model judgment based on surrounding turns.")
        self.assertEqual(run_merge(self.run, self.database), 0)
        self.assertTrue((self.run / "merged-identity.json").is_file())
        summary = json.loads((self.run / "identity-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["note_leaks_against_corpus"], 0)
        self.assertEqual(summary["contract_checks"], "all passed")

    def test_zero_based_first_batch_succeeds(self) -> None:
        self.write_batch("Model judgment based on surrounding turns.", qid=0)
        self.assertEqual(run_merge(self.run, self.database), 0)
        merged = json.loads((self.run / "merged-identity.json").read_text(encoding="utf-8"))
        self.assertEqual(merged[0]["dialogue_id"], 0)

    def test_negative_batch_start_is_rejected(self) -> None:
        self.write_batch("Model judgment based on surrounding turns.", qid=0, start=-1)
        with self.assertRaises(SystemExit) as result:
            run_merge(self.run, self.database)
        self.assertIn("invalid batch bounds", str(result.exception))
        self.assertFalse((self.run / "merged-identity.json").exists())


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
