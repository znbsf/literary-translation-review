"""Model HTTP redirect safety, using invented fixtures and loopback-only servers."""
import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

from tests.ltr.alignment import test_candidates as candidate_fixtures
from tests.ltr.alignment import test_review as review_fixtures
from tests.ltr.alignment import test_campaign as campaign_fixtures
from tests.ltr.attribution import test_review as attribution_fixtures
from ltr.quotes import batch
from ltr.quotes import review as candidate_review
from ltr.alignment import candidates as alignment_candidates
from ltr.alignment import review as alignment_review
from ltr.attribution import review as attribution_review

STATUSES = (301, 302, 303, 307, 308)
DUMMY_ENV = {"ANTHROPIC_BASE_URL": "http://synthetic.invalid", "ANTHROPIC_AUTH_TOKEN": "dummy-synthetic-token"}


def refusal(status):
    return urllib.error.HTTPError("http://synthetic.invalid/private", status, "PRIVATE_REDIRECT_PROSE", {}, None)


class RedirectRuntimeTests(unittest.TestCase):
    def fixture(self, cls):
        case = cls(unittest.defaultTestLoader.getTestCaseNames(cls)[0])
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def check_progress(self, output, status):
        data = json.loads((output / "progress.json").read_bytes())
        calls = data if type(data) is list else data["calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "failed")
        self.assertEqual(calls[0]["http_status"], status)
        self.assertEqual(calls[0]["error_code"], "http_redirect_rejected")
        self.assertNotIn("PRIVATE_REDIRECT_PROSE", json.dumps(data))
        return calls

    def batch_fixture(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name); source = root / "source"; source.mkdir()
        database = source / "synthetic.sqlite"
        connection = sqlite3.connect(database)
        targets = [{"id": "p%d" % n, "ordinal": n, "text": "Synthetic spoken %d." % n} for n in range(3)]
        try:
            connection.executescript("CREATE TABLE paragraphs(id TEXT, edition_id TEXT, ordinal INTEGER, text TEXT); CREATE TABLE dialogues(edition_id TEXT);")
            connection.executemany("INSERT INTO paragraphs VALUES (?,?,?,?)", [(row["id"], "edition", row["ordinal"], row["text"]) for row in targets])
            connection.commit()
        finally: connection.close()
        args = SimpleNamespace(db=database, edition="edition", out=root / "out", models=["first", "fallback"],
            max_calls=10, batch_size=1, start=0, max_batches=3, batch_starts=None)
        return root, targets, args

    def test_candidate_extraction_stops_models_and_batches_with_unattempted_scope(self):
        for status in STATUSES:
            with self.subTest(status=status):
                _, _, args = self.batch_fixture()
                with patch.dict(os.environ, DUMMY_ENV), patch.object(batch, "request_model", side_effect=refusal(status)) as request:
                    result = batch.run(args)
                self.assertEqual(request.call_count, 1)
                self.assertEqual((result["calls"], result["failed_calls"], result["valid_calls"]), (1, 1, 0))
                self.assertEqual(result["stop_reason"], "request_rejected")
                self.assertEqual(result["unattempted_batch_starts"], [1, 2])
                self.check_progress(args.out, status)
                self.assertFalse(list(args.out.glob("batch-*.json")))

    def test_candidate_review_stops_without_dispatching_next_candidate(self):
        for status in STATUSES:
            with self.subTest(status=status):
                root, targets, args = self.batch_fixture()
                source = root / "input"; source.mkdir()
                rows = batch.validate({"candidates": [{"paragraph_id": row["id"], "text": "spoken", "speaker": None,
                    "addressee": None, "kind": "spoken"} for row in targets]}, targets)
                (source / "batch-000.json").write_text(json.dumps({"database_sha256": batch.fingerprint(args.db), "edition_id": "edition", "candidates": rows}))
                argv = ["--db", str(args.db), "--input", str(source), "--out", str(args.out), "--model", "reviewer",
                    "--batch-size", "1", "--max-calls", "5", "--execute"]
                stream = io.StringIO()
                with patch.dict(os.environ, DUMMY_ENV), patch.object(candidate_review, "request_model", side_effect=refusal(status)) as request, contextlib.redirect_stdout(stream):
                    self.assertEqual(candidate_review.main(argv), 0)
                self.assertEqual(request.call_count, 1)
                result = json.loads((args.out / "summary.json").read_bytes())
                self.assertEqual((result["attempted_rows"], result["failed_rows"], result["unattempted_rows"]), (1, 1, 2))
                self.assertEqual(result["stop_reason"], "request_rejected")
                self.check_progress(args.out, status)

    def test_alignment_candidates_stop_first_redirect_with_failed_and_unattempted_rows(self):
        for status in STATUSES:
            with self.subTest(status=status):
                case = self.fixture(candidate_fixtures.AlignmentRuntimeTests)
                with case.env(), patch.object(alignment_candidates, "request_model", side_effect=refusal(status)) as request:
                    result = alignment_candidates.run(case.args(batch_size=1))
                self.assertEqual(request.call_count, 1)
                self.assertEqual(result["stop_reason"], "request_rejected")
                self.assertEqual((result["attempted_rows"], result["failed_rows"], result["unattempted_rows"]), (1, 1, 2))
                self.check_progress(case.out, status)

    def test_alignment_review_stops_first_redirect_and_preserves_unattempted_rows(self):
        for status in STATUSES:
            with self.subTest(status=status):
                case = self.fixture(review_fixtures.AlignmentReviewRuntimeTests)
                review_fixtures.write_batch(case.inp, "align-000.json", case.baseline,
                    [review_fixtures.make_row(cid, ["j1"], "candidate") for cid in ["z1", "z2", "z3"]])
                with case.env(), patch.object(alignment_review, "request_model", side_effect=refusal(status)) as request:
                    result = alignment_review.run(case.args(batch_size=1))
                self.assertEqual(request.call_count, 1)
                self.assertEqual(result["stop_reason"], "request_rejected")
                self.assertEqual((result["attempted_rows"], result["failed_rows"], result["unattempted_rows"]), (1, 1, 2))
                self.check_progress(case.out, status)

    def test_attribution_stops_first_redirect_without_next_targets(self):
        for status in STATUSES:
            with self.subTest(status=status):
                case = self.fixture(attribution_fixtures.AttributionReviewTests)
                args = case._args(batch_size=1, max_calls=5)
                with patch.dict(os.environ, DUMMY_ENV), patch.object(attribution_review, "request_model", side_effect=refusal(status)) as request:
                    result = attribution_review.run(args)
                self.assertEqual(request.call_count, 1)
                self.assertEqual(result["stop_reason"], "request_rejected")
                self.assertEqual((result["attempted_rows"], result["failed_rows"], result["unattempted_rows"]), (1, 1, 4))
                self.check_progress(args.out, status)

    def test_third_attribution_failure_keeps_redirect_reason_and_stops(self):
        case = self.fixture(attribution_fixtures.AttributionReviewTests)
        with patch.dict(os.environ, DUMMY_ENV), patch.object(attribution_review, "request_model", side_effect=[ValueError("invalid"), ValueError("invalid"), refusal(302)]) as request:
            result = attribution_review.run(case._args(batch_size=1, max_calls=5))
        self.assertEqual(request.call_count, 3)
        self.assertEqual(result["stop_reason"], "request_rejected")
        self.assertEqual((result["failed_rows"], result["unattempted_rows"]), (3, 2))

    def test_campaign_child_redirect_stops_remaining_job_without_child_output(self):
        for status in STATUSES:
            with self.subTest(status=status):
                case = self.fixture(campaign_fixtures.AlignmentCampaignTests)
                case.batch(); case.plan([case.job("one", end=1), case.job("two", start=1)])
                with patch.object(alignment_review, "request_model", side_effect=refusal(status)) as request, case.env():
                    result = campaign_fixtures.campaign.run_campaign(case.db, case.plan_path, case.out, "reviewer-model", 10, execute=True)
                self.assertEqual(request.call_count, 1)
                self.assertEqual(result["stop_reason"], "request_rejected")
                self.assertEqual((result["attempted_rows"], result["failed_rows"], result["unattempted_rows"]), (1, 1, 2))
                self.assertFalse((case.out / "two").exists())
                self.check_progress(case.out / "one", status)
                case.assert_balanced(result)

    def test_non_redirect_http_fallback_behavior_is_unchanged(self):
        _, _, args = self.batch_fixture(); args.max_batches = 1
        valid = {"payload": {"candidates": []}, "served_model": "fallback", "usage": {}}
        with patch.dict(os.environ, DUMMY_ENV), patch.object(batch, "request_model", side_effect=[refusal(500), valid]) as request:
            result = batch.run(args)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(result["valid_calls"], 1)
        self.assertNotIn("stop_reason", result)
        self.assertFalse(batch.is_redirect_rejection(refusal(500)))


class SharedRedirectTransportTests(unittest.TestCase):
    def setUp(self):
        class Quiet(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def log_error(self, *args): pass
            def do_GET(self): self.respond()
            def do_POST(self): self.respond()
            def respond(handler):
                length = int(handler.headers.get("Content-Length", "0"))
                handler.server.records.append((handler.command, handler.headers.get("x-api-key"), handler.headers.get("X-Session-Id"), handler.rfile.read(length)))
                handler.send_response(handler.server.status)
                if handler.server.destination: handler.send_header("Location", handler.server.destination)
                handler.send_header("Content-Length", "0"); handler.end_headers()
        def serve():
            server = http.server.HTTPServer(("127.0.0.1", 0), Quiet)
            server.records, server.destination, server.status = [], None, 200
            thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True); thread.start()
            self.addCleanup(server.server_close); self.addCleanup(thread.join, 2); self.addCleanup(server.shutdown)
            return server
        self.target, self.origin = serve(), serve()
        self.origin.destination = "http://127.0.0.1:%d/target" % self.target.server_address[1]

    def test_real_redirects_never_forward_auth_or_make_second_request_and_preserve_wire_history(self):
        history = [{"role": "user", "content": "old synthetic"}, {"role": "assistant", "content": "old answer"}]
        original = list(history)
        payload = {"synthetic": "only"}
        expected = batch.encode_request("model", payload, history=history, session_id="fixed-session", max_output_tokens=456)
        for number, status in enumerate(STATUSES, 1):
            with self.subTest(status=status):
                self.origin.status = status
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    batch.request_model("http://127.0.0.1:%d" % self.origin.server_address[1], "dummy-synthetic-token", "model", payload,
                        history=history, session_id="fixed-session", max_output_tokens=456)
                self.assertEqual(caught.exception.code, status)
                self.assertEqual(len(self.origin.records), number)
                self.assertEqual(self.target.records, [])
                method, token, session, raw = self.origin.records[-1]
                self.assertEqual((method, token, session), ("POST", "dummy-synthetic-token", "fixed-session"))
                self.assertEqual(raw, expected)
                self.assertEqual(history, original)
                metadata = batch.failure_metadata(caught.exception)
                self.assertEqual(metadata["error_code"], "http_redirect_rejected")
                self.assertEqual(metadata["request_metrics"]["request_bytes"], len(expected))

    def test_redirect_handler_also_blocks_same_origin_and_https_downgrade(self):
        request = batch.urllib.request.Request("https://synthetic.invalid/v1/messages", data=b"synthetic")
        for target in ["https://synthetic.invalid/other", "http://synthetic.invalid/other"]:
            response = io.BytesIO(b"synthetic")
            with self.assertRaises(urllib.error.HTTPError):
                batch._RejectRedirects().redirect_request(request, response, 302, "Found", {}, target)
            self.assertTrue(response.closed)


if __name__ == "__main__": unittest.main()
