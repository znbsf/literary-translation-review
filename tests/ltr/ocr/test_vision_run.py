"""Whole-page OCR vision runner: plan binding, single session, stop on first failure (synthetic images)."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path

from ltr.ocr import vision_run as worker


class VisionRunTest(unittest.TestCase):
    def setUp(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("optional Pillow dependency unavailable")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "source"
        self.scans = self.root / "private" / "scans"
        self.scans.mkdir(parents=True)
        self.db = self.root / "dialogue.sqlite"
        connection = sqlite3.connect(self.db)
        connection.execute("CREATE TABLE scan_pages(id TEXT PRIMARY KEY,image_path TEXT,image_sha256 TEXT,"
                           "width INTEGER,height INTEGER)")
        for number, suffix in [(1, "png"), (2, "jpg"), (3, "png")]:
            image = self.scans / ("p%d.%s" % (number, suffix))
            Image.new("RGB", (8, 12), (number * 40, 20, 30)).save(image)
            connection.execute("INSERT INTO scan_pages VALUES (?,?,?,?,?)",
                               ("JA:p%d" % number, image.name, hashlib.sha256(image.read_bytes()).hexdigest(), 8, 12))
        connection.commit()
        connection.close()
        self.plan = self.base / "plan.json"
        worker.build_plan(self.db, ["JA:p1", "JA:p2", "JA:p3"], self.plan)
        self.output = self.base / "result"
        self.calls = []
        self.replies = []

    def reply(self, text=None):
        body = text if text is not None else json.dumps({"segments": [
            {"order": 1, "kind": "main", "text": "synthetic", "bbox": [1, 2, 900, 990]}], "uncertain_regions": []})
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": body}],
                "usage": {"input_tokens": 20, "output_tokens": 5, "unsafe": "never persist"}}

    def sender(self, encoded, session):
        self.calls.append((json.loads(encoded), session))
        return self.replies.pop(0) if self.replies else self.reply()

    def run_worker(self, **kwargs):
        options = dict(db=self.db, plan_path=self.plan, output_dir=self.output, model="synthetic-route", max_calls=3,
                       max_request_bytes=worker.MAX_REQUEST_BYTES, max_output_tokens=1234, execute=True,
                       transport=self.sender)
        options.update(kwargs)
        return worker.run(**options)

    def test_plan_binds_pages_in_order_without_paths(self):
        plan = json.loads(self.plan.read_bytes())
        self.assertEqual([p["page_id"] for p in plan["pages"]], ["JA:p1", "JA:p2", "JA:p3"])
        self.assertEqual(plan["source_sha256"], hashlib.sha256(self.db.read_bytes()).hexdigest())
        self.assertNotIn(str(self.base), self.plan.read_text())
        with self.assertRaises(worker.OCRVisionRunError):
            worker.build_plan(self.db, ["JA:p1"], self.plan)  # never overwrites
        with self.assertRaises(worker.OCRVisionRunError):
            worker.build_plan(self.db, ["JA:p1"], self.root / "inside.json")

    def test_one_session_all_pages_and_safe_summary(self):
        summary = self.run_worker()
        self.assertEqual((summary["valid"], summary["failed"], summary["unattempted"]), (3, 0, 0))
        self.assertEqual(len({session for _, session in self.calls}), 1)
        self.assertEqual([len(body["messages"]) for body, _ in self.calls], [1, 3, 5])
        self.assertEqual(summary["usage"]["input_tokens"], 60)
        self.assertEqual(len(list((self.output / "candidates").iterdir())), 3)
        persisted = (self.output / "progress.json").read_text() + (self.output / "summary.json").read_text()
        self.assertNotIn("never persist", persisted)
        self.assertNotIn("synthetic", persisted.replace("synthetic-route", ""))
        self.assertTrue(summary["source_unchanged"])

    def test_requires_execute_budgets_and_fresh_output(self):
        for kwargs in ({"execute": False}, {"max_calls": 0}, {"max_output_tokens": 0}, {"history_mode": "x"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(worker.OCRVisionRunError):
                self.run_worker(**kwargs)
        self.assertEqual(self.calls, [])
        self.output.mkdir()
        with self.assertRaises(worker.OCRVisionRunError):
            self.run_worker()

    def test_call_and_byte_budgets_stop_cleanly(self):
        summary = self.run_worker(max_calls=1)
        self.assertEqual((summary["stop_reason"], summary["valid"], summary["unattempted"]),
                         ("call_budget_exhausted", 1, 2))
        summary = self.run_worker(output_dir=self.base / "small", max_request_bytes=10)
        self.assertEqual(summary["stop_reason"], "request_byte_budget_exhausted")
        self.assertGreater(summary["next_request_bytes"], 10)

    def test_invalid_reply_stops_and_is_kept_only_when_asked(self):
        self.replies = [self.reply(), self.reply("not json")]
        summary = self.run_worker()
        self.assertEqual((summary["valid"], summary["failed"], summary["stop_reason"]), (1, 1, "invalid_model_json"))
        self.assertFalse((self.output / "rejected").exists())
        self.replies = [self.reply("not json")]
        summary = self.run_worker(output_dir=self.base / "kept", retain_rejected=True)
        self.assertEqual(len(summary["rejected"]), 1)
        rejected = json.loads(next((self.base / "kept" / "rejected").iterdir()).read_text())
        self.assertEqual((rejected["response_text"], rejected["requires_review"]), ("not json", 1))

    def test_transport_errors_are_classified(self):
        def rejected(encoded, session):
            raise urllib.error.HTTPError("u", 429, "busy", {}, None)
        summary = self.run_worker(transport=rejected)
        self.assertEqual((summary["stop_reason"], summary["failed"]), ("request_rejected", 1))

        def timeout(encoded, session):
            raise TimeoutError
        self.assertEqual(self.run_worker(output_dir=self.base / "t", transport=timeout)["stop_reason"], "request_timeout")

    def test_changed_image_stops_before_publishing(self):
        def change_after_first(encoded, session):
            (self.scans / "p1.png").write_bytes(b"changed")
            return self.reply()
        summary = self.run_worker(transport=change_after_first)
        self.assertEqual(summary["valid"], 0)  # the post-call input check fails before anything is written
        self.assertEqual(summary["stop_reason"], "image_changed")
        self.assertEqual(list((self.output / "candidates").iterdir()), [])

    def test_markdown_fence_is_accepted_once(self):
        fenced = "```json\n" + json.dumps({"segments": [], "uncertain_regions": []}) + "\n```"
        self.replies = [self.reply(fenced)]
        self.assertEqual(self.run_worker(max_calls=1)["valid"], 1)

    def test_cli_reports_static_errors(self):
        code = worker.main(["run", "--db", str(self.db), "--plan", str(self.plan), "--out", str(self.output),
                            "--model", "m", "--max-calls", "1", "--max-request-bytes", "1", "--max-output-tokens", "1"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
