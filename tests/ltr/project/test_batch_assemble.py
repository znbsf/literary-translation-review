"""Synthetic complete-book publishing and provenance checks."""
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET

from ltr.project.batch_assemble import assemble_book, AssembleError
from ltr.project.batch_translate import PROMPT
from ltr.project.corpus_import import import_book
from ltr.project.storage import write_json, digest
from tests.ltr.project.test_epub import CONTAINER, OPF


class BatchAssembleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root, self.destination = base / "project", base / "delivery"
        epub = base / "source.epub"
        with zipfile.ZipFile(epub, "w") as archive:
            archive.writestr("META-INF/container.xml", CONTAINER)
            archive.writestr("OPS/book.opf", OPF)
            archive.writestr("OPS/one.xhtml", '<html><body><p>PRIVATE SOURCE A</p><p> </p><p>PRIVATE SOURCE B</p></body></html>')
            archive.writestr("OPS/two.xhtml", '<html><body><p>PRIVATE SOURCE C</p></body></html>')
        self.source = import_book(epub)
        write_json(self.root / "imports/F01.json", self.source)
        self.plan = {"jobs": ["J01", "J02"], "books": [{"book_id": "E04", "title": "合成卷",
                     "source_file": "F01", "source_snapshot_digest": digest(self.source), "jobs": ["J01", "J02"]}]}
        write_json(self.root / "plan.json", self.plan)
        for number, rows in [(1, self.source["paragraphs"][:1]), (2, self.source["paragraphs"][1:])]:
            job_id = f"J0{number}"
            payload = {"source": [{"id": f"P{i:03d}", "text": row["text"]} for i, row in enumerate(rows)],
                       "context": {}, "adjacent_source_before": [], "adjacent_source_after": []}
            job = {"book_id": "E04", "source_hash": self.source["book_hash"],
                   "source_ids": [row["id"] for row in rows], "payload": payload,
                   "source_digest": digest(payload["source"]), "context_digest": digest(payload["context"])}
            self.save(job_id, job, {"units": [{"id": row["id"], "target": f"合成译文{number}{i}<&>\n续行"}
                                             for i, row in enumerate(payload["source"])], "issues": []})

    def read(self, suffix):
        return json.loads((self.root / suffix).read_text(encoding="utf-8"))

    def save(self, job_id, job, candidate):
        binding = {"input_digest": digest(job), "prompt_digest": digest(PROMPT), "model": "synthetic"}
        result = {**candidate, **binding, "source_ids": job["source_ids"]}
        claim = {**binding, "status": "completed", "result_digest": digest(result)}
        for suffix, value in [("input", job), ("result", result), ("claim", claim)]:
            write_json(self.root / "jobs" / f"{job_id}.{suffix}.json", value)

    def test_complete_coverage_readable_epub_and_layout_without_source_text(self):
        status = assemble_book(self.root, "E04", self.destination)
        self.assertEqual(status["status"], "assembled")
        self.assertEqual(status["translated_units"], 3)
        self.assertEqual(status["layout_blocks"], 1)
        text = (self.destination / "E04_合成卷_译文.txt").read_text(encoding="utf-8")
        self.assertIn("续行\n\n合成译文20", text)
        self.assertNotIn("PRIVATE SOURCE", text)
        value = self.read("books/E04/translation.json")
        self.assertEqual([u["source_id"] for u in value["units"]], [r["id"] for r in self.source["paragraphs"]])
        self.assertNotIn("PRIVATE SOURCE", json.dumps(value))
        self.assertTrue(value["coverage"]["exact_once"])
        with zipfile.ZipFile(self.destination / "E04_合成卷_译文.epub") as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.infolist()[0].compress_type, zipfile.ZIP_STORED)
            self.assertEqual(archive.read("mimetype"), b"application/epub+zip")
            for name in archive.namelist():
                if name.endswith((".xml", ".xhtml", ".opf")):
                    ET.fromstring(archive.read(name))
                    self.assertNotIn(b"PRIVATE SOURCE", archive.read(name))
            package = ET.fromstring(archive.read("EPUB/package.opf"))
            self.assertEqual(package.get("version"), "3.0")
            self.assertEqual(len(package.find("{http://www.idpf.org/2007/opf}spine")), 2)

    def test_missing_result_returns_progress_without_complete_book(self):
        (self.root / "jobs/J02.result.json").unlink()
        status = assemble_book(self.root, "E04", self.destination)
        self.assertEqual(status["status"], "incomplete")
        self.assertEqual(status["completed_jobs"], 1)
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.root / "books/E04/translation.json").exists())

    def test_textless_spine_is_audited_but_not_an_empty_epub_chapter(self):
        epub = Path(self.tmp.name) / "source.epub"
        with zipfile.ZipFile(epub, "w") as archive:
            archive.writestr("META-INF/container.xml", CONTAINER)
            archive.writestr("OPS/book.opf", OPF)
            archive.writestr("OPS/one.xhtml", '<html><body><img src="cover.jpg"/></body></html>')
            archive.writestr("OPS/two.xhtml", '<html><body><p>PRIVATE SOURCE A</p><p>PRIVATE SOURCE B</p><p>PRIVATE SOURCE C</p></body></html>')
        self.source = import_book(epub)
        write_json(self.root / "imports/F01.json", self.source)
        self.plan["books"][0]["source_snapshot_digest"] = digest(self.source)
        write_json(self.root / "plan.json", self.plan)
        for job_id, rows in [("J01", self.source["paragraphs"][:1]), ("J02", self.source["paragraphs"][1:])]:
            job, result = self.read(f"jobs/{job_id}.input.json"), self.read(f"jobs/{job_id}.result.json")
            job["source_ids"] = [row["id"] for row in rows]
            job["source_hash"] = self.source["book_hash"]
            self.save(job_id, job, result)
        status = assemble_book(self.root, "E04", self.destination)
        self.assertEqual(status["spine_items"], 2)
        self.assertEqual(status["translated_units"], 3)
        with zipfile.ZipFile(self.destination / "E04_合成卷_译文.epub") as archive:
            package = ET.fromstring(archive.read("EPUB/package.opf"))
            self.assertEqual(len(package.find("{http://www.idpf.org/2007/opf}spine")), 1)
            nav = ET.fromstring(archive.read("EPUB/nav.xhtml"))
            self.assertEqual(len(list(nav.iter("{http://www.w3.org/1999/xhtml}a"))), 1)
            self.assertNotIn("EPUB/chapter-0002.xhtml", archive.namelist())
            chapter = ET.fromstring(archive.read("EPUB/chapter-0001.xhtml"))
            targets = [(p.text or "") + ''.join("\n" + (c.tail or "") for c in p)
                       for p in chapter.iter("{http://www.w3.org/1999/xhtml}p")]
            expected = [unit["target"] for unit in self.read("books/E04/translation.json")["units"]]
            self.assertEqual(targets, expected)

    def test_repeat_assembly_is_byte_identical(self):
        assemble_book(self.root, "E04", self.destination)
        before = {p.name: p.read_bytes() for p in self.destination.iterdir()}
        assemble_book(self.root, "E04", self.destination)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.destination.iterdir()})

    def test_conflict_prevents_all_output_and_preserves_existing_file(self):
        self.destination.mkdir()
        conflict = self.destination / "E04_合成卷_译文.epub"
        conflict.write_bytes(b"other content")
        with self.assertRaisesRegex(AssembleError, "assemble_output_conflict"):
            assemble_book(self.root, "E04", self.destination)
        self.assertEqual(conflict.read_bytes(), b"other content")
        self.assertEqual(len(list(self.destination.iterdir())), 1)
        self.assertFalse((self.root / "books/E04/translation.json").exists())

    def test_wrong_source_text_fails_even_with_valid_claims(self):
        job = self.read("jobs/J01.input.json")
        result = self.read("jobs/J01.result.json")
        job["payload"]["source"][0]["text"] = "Other source"
        job["source_digest"] = digest(job["payload"]["source"])
        self.save("J01", job, result)
        with self.assertRaisesRegex(AssembleError, "assemble_source_text_mismatch"):
            assemble_book(self.root, "E04", self.destination)

    def test_reversed_jobs_and_duplicate_coverage_rejected(self):
        self.plan["books"][0]["jobs"] = ["J02", "J01"]
        write_json(self.root / "plan.json", self.plan)
        with self.assertRaisesRegex(AssembleError, "assemble_source_coverage_mismatch"):
            assemble_book(self.root, "E04", self.destination)
        self.plan["books"][0]["jobs"] = ["J01", "J02"]
        write_json(self.root / "plan.json", self.plan)
        job, result = self.read("jobs/J02.input.json"), self.read("jobs/J02.result.json")
        job["source_ids"][0] = self.source["paragraphs"][0]["id"]
        job["payload"]["source"][0]["text"] = self.source["paragraphs"][0]["text"]
        job["source_digest"] = digest(job["payload"]["source"])
        self.save("J02", job, result)
        with self.assertRaisesRegex(AssembleError, "assemble_source_coverage_mismatch"):
            assemble_book(self.root, "E04", self.destination)

    def test_tampered_result_is_not_accepted(self):
        result = self.read("jobs/J01.result.json")
        result["units"][0]["target"] = "changed"
        write_json(self.root / "jobs/J01.result.json", result)
        status = assemble_book(self.root, "E04", self.destination)
        self.assertEqual(status["status"], "incomplete")
        self.assertEqual(status["completed_jobs"], 1)

    def test_changed_snapshot_and_spine_mapping_rejected(self):
        self.source["paragraphs"][0]["text"] = "Changed imported source"
        write_json(self.root / "imports/F01.json", self.source)
        with self.assertRaisesRegex(AssembleError, "assemble_source_changed"):
            assemble_book(self.root, "E04", self.destination)

    def test_partial_source_snapshot_is_not_a_complete_book(self):
        self.source["scope"]["kind"] = "selected_chapter"
        write_json(self.root / "imports/F01.json", self.source)
        self.plan["books"][0]["source_snapshot_digest"] = digest(self.source)
        write_json(self.root / "plan.json", self.plan)
        with self.assertRaisesRegex(AssembleError, "assemble_incomplete_source_snapshot"):
            assemble_book(self.root, "E04", self.destination)

    def test_source_like_markup_in_target_is_inert_and_newlines_preserved(self):
        job = self.read("jobs/J01.input.json")
        result = self.read("jobs/J01.result.json")
        result["units"][0]["target"] = '<script>合成</script>\r\n另行'
        self.save("J01", job, result)
        assemble_book(self.root, "E04", self.destination)
        with zipfile.ZipFile(self.destination / "E04_合成卷_译文.epub") as archive:
            root = ET.fromstring(archive.read("EPUB/chapter-0001.xhtml"))
            self.assertEqual(list(root.iter("{http://www.w3.org/1999/xhtml}script")), [])

    def test_review_issues_and_kana_are_separate_hints_not_added_to_prose(self):
        job = self.read("jobs/J01.input.json")
        result = self.read("jobs/J01.result.json")
        result["units"][0]["target"] = "合成专名カナ"
        result["issues"] = ["合成问题提示"]
        self.save("J01", job, result)
        status = assemble_book(self.root, "E04", self.destination)
        self.assertEqual(status["model_issue_count"], 1)
        self.assertEqual(status["review_hint_units"], 1)
        review = (self.destination / "E04_合成卷_译文_待核查问题.md").read_text(encoding="utf-8")
        text = (self.destination / "E04_合成卷_译文.txt").read_text(encoding="utf-8")
        self.assertIn("合成问题提示", review)
        self.assertIn("不是错误判定", review)
        self.assertIn("J01", review)
        self.assertNotIn("合成问题提示", text)
        self.assertNotIn("结构完整", text)


if __name__ == "__main__":
    unittest.main()
