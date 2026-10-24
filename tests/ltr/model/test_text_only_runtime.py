"""Default text-only model protocol, with invented inputs and no external calls."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from tests.ltr.model import test_redirects as fixtures
from ltr.quotes import batch
from ltr.quotes import review as candidate_review
from ltr.alignment import candidates as alignment_candidates
from ltr.alignment import review as alignment_review
from ltr.attribution import review as attribution_review
from ltr.alignment import campaign as alignment_campaign
from ltr.alignment import plan as alignment_plan

USAGE = {"input_tokens": 17, "output_tokens": 9, "cache_read_input_tokens": 31,
         "cache_creation_input_tokens": 2, "private": "DO_NOT_PERSIST"}


class Opener:
    def __init__(self, response): self.response, self.sent = response, []
    def open(self, request, timeout):
        self.sent.append(request)
        return io.BytesIO(json.dumps(self.response).encode("utf-8"))


def unsupported():
    return {"stop_reason": "end_turn", "content": [{"type": "thinking", "thinking": "PRIVATE_REASONING", "signature": "PRIVATE_SIGNATURE"},
        {"type": "text", "text": '{"candidates":[]}'}], "usage": USAGE}


class TextOnlyTransportTests(unittest.TestCase):
    def test_all_nontext_and_malformed_shapes_refused_before_history_mutation(self):
        text = {"type": "text", "text": '{"ok":true}'}
        shapes = [None, [], {}, "private content", [None], ["private block"], [{}], [{"text": "{}"}],
                  [{"type": "text"}], [{"type": "text", "text": None}], [{"type": "text", "text": 1}],
                  [{"type": "thinking", "thinking": "PRIVATE"}, text],
                  [text, {"type": "redacted_thinking", "data": "PRIVATE"}],
                  [text, {"type": "tool_use", "name": "PRIVATE", "input": {}}],
                  [text, {"type": "unknown", "text": "PRIVATE"}],
                  [{"type": "text", "text": "{}", "unknown_metadata": "PRIVATE"}]]
        # An actual unpaired Unicode surrogate is invalid UTF-8 too.
        shapes.append([{"type": "text", "text": "\ud800"}])
        for number, content in enumerate(shapes):
            with self.subTest(number=number):
                response = {"stop_reason": "end_turn", "content": content, "usage": USAGE}
                opener = Opener(response)
                history = [{"role": "user", "content": "old synthetic"}, {"role": "assistant", "content": "old answer"}]
                before = copy.deepcopy(history)
                with patch.object(batch.urllib.request, "build_opener", return_value=opener):
                    with self.assertRaises(batch.ModelResponseError) as caught:
                        batch.request_model("http://synthetic.invalid", "dummy-token", "model", {}, history=history, session_id="fixed")
                self.assertEqual(str(caught.exception), "unsupported_response_content")
                self.assertEqual(caught.exception.usage, {k: v for k, v in USAGE.items() if type(v) is int})
                self.assertEqual(history, before)
                self.assertNotIn("PRIVATE", json.dumps(batch.failure_metadata(caught.exception)))
                self.assertEqual(len(opener.sent), 1)

    def test_missing_content_and_nontext_incomplete_response_are_also_incompatible(self):
        for response in [{"stop_reason": "end_turn", "usage": USAGE}, unsupported() | {"stop_reason": "max_tokens"}]:
            opener = Opener(response)
            with patch.object(batch.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(batch.ModelResponseError) as caught:
                    batch.request_model("http://synthetic.invalid", "dummy-token", "model", {})
            self.assertEqual(caught.exception.code, "unsupported_response_content")

    def test_known_top_level_reasoning_and_tool_fields_reject_before_history(self):
        for field in ["reasoning_content", "reasoning", "thinking", "tool_calls"]:
            for value in [None, "PRIVATE_TOP_LEVEL_CONTENT", [], {"SECRET_KEY": "PRIVATE_TOP_LEVEL_CONTENT"}]:
                with self.subTest(field=field, value_type=type(value).__name__):
                    response = {"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"ok":true}'}],
                                "usage": USAGE, field: value}
                    opener = Opener(response)
                    history = [{"role": "user", "content": "earlier synthetic"}, {"role": "assistant", "content": "earlier answer"}]
                    before = copy.deepcopy(history)
                    with patch.object(batch.urllib.request, "build_opener", return_value=opener):
                        with self.assertRaises(batch.ModelResponseError) as caught:
                            batch.request_model("http://synthetic.invalid", "dummy", "model", {}, history=history)
                    self.assertEqual(caught.exception.code, "unsupported_response_content")
                    self.assertEqual(history, before)
                    self.assertEqual(caught.exception.usage, {key: value for key, value in USAGE.items() if type(value) is int})
                    metadata = batch.failure_metadata(caught.exception)
                    self.assertEqual(metadata["usage"]["output_tokens"], 9)
                    for secret in ["PRIVATE_TOP_LEVEL_CONTENT", "SECRET_KEY", "DO_NOT_PERSIST", field]:
                        self.assertNotIn(secret, json.dumps(metadata))
                    self.assertEqual(len(opener.sent), 1)

    def test_normal_top_level_envelope_metadata_remains_compatible(self):
        response = {"id": "synthetic-id", "type": "message", "role": "assistant", "model": "served-model",
            "stop_reason": "end_turn", "stop_sequence": None, "content": [{"type": "text", "text": '{"ok":true}'}],
            "usage": USAGE, "normal_future_metadata": {"safe": True}}
        opener = Opener(response); history = []
        with patch.object(batch.urllib.request, "build_opener", return_value=opener):
            result = batch.request_model("http://synthetic.invalid", "dummy", "model", {}, history=history)
        self.assertEqual(result["payload"], {"ok": True})
        self.assertEqual(result["served_model"], "served-model")
        self.assertEqual(len(history), 2)

    def test_plain_multi_text_wire_and_history_keep_original_behavior(self):
        blocks = [{"type": "text", "text": "  {\"ok\":"}, {"type": "text", "text": "\ntrue}  "}, {"type": "text", "text": ""}]
        opener = Opener({"stop_reason": "end_turn", "content": blocks, "usage": USAGE})
        history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "answer"}]
        before = copy.deepcopy(history); payload = {"safe": "synthetic"}
        expected = batch.encode_request("model", payload, history=before, session_id="fixed", max_output_tokens=456)
        with patch.object(batch.urllib.request, "build_opener", return_value=opener):
            response = batch.request_model("http://synthetic.invalid", "dummy-token", "model", payload,
                history=history, session_id="fixed", max_output_tokens=456)
        self.assertEqual(response["payload"], {"ok": True})
        self.assertEqual(opener.sent[0].data, expected)
        self.assertEqual(history[:2], before)
        self.assertEqual(history[-1], {"role": "assistant", "content": '{"ok":\ntrue}'})

    def test_plain_incomplete_response_keeps_prior_code_usage_and_no_history(self):
        opener = Opener({"stop_reason": "max_tokens", "content": [{"type": "text", "text": "{"}], "usage": USAGE})
        history = []
        with patch.object(batch.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(batch.ModelResponseError) as caught:
                batch.request_model("http://synthetic.invalid", "dummy", "model", {}, history=history)
        self.assertEqual(caught.exception.code, "incomplete_model_response")
        self.assertEqual(history, [])
        self.assertEqual(caught.exception.usage["output_tokens"], 9)

    def test_unsupported_usage_is_numeric_only_not_fabricated(self):
        opener = Opener(unsupported() | {"usage": {"input_tokens": True, "output_tokens": -1, "cache_read_input_tokens": 8, "private": "SECRET"}})
        with patch.object(batch.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(batch.ModelResponseError) as caught:
                batch.request_model("http://synthetic.invalid", "dummy", "model", {})
        self.assertEqual(caught.exception.usage, {"cache_read_input_tokens": 8})


class TextOnlyRuntimeTests(unittest.TestCase):
    fixture = fixtures.RedirectRuntimeTests.fixture
    batch_fixture = fixtures.RedirectRuntimeTests.batch_fixture

    def check_failure(self, output):
        progress = json.loads((output / "progress.json").read_bytes())
        calls = progress if type(progress) is list else progress["calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "failed")
        self.assertEqual(calls[0]["error_code"], "unsupported_response_content")
        self.assertEqual(calls[0]["usage"]["output_tokens"], 9)
        for private in ["PRIVATE_REASONING", "PRIVATE_SIGNATURE", "DO_NOT_PERSIST"]:
            self.assertNotIn(private, json.dumps(progress))

    def test_candidate_extraction_does_not_fallback_models_or_advance_batches(self):
        _, _, args = self.batch_fixture(); opener = Opener(unsupported())
        with patch.dict(os.environ, fixtures.DUMMY_ENV), patch.object(batch.urllib.request, "build_opener", return_value=opener):
            result = batch.run(args)
        self.assertEqual(len(opener.sent), 1)
        self.assertEqual(result["stop_reason"], "unsupported_response_content")
        self.assertEqual(result["unattempted_batch_starts"], [1, 2])
        self.assertEqual((result["failed_calls"], result["valid_calls"]), (1, 0))
        self.check_failure(args.out)

    def test_candidate_review_does_not_advance_to_next_candidate(self):
        root, targets, args = self.batch_fixture()
        source = root / "input"; source.mkdir()
        rows = batch.validate({"candidates": [{"paragraph_id": row["id"], "text": "spoken", "speaker": None,
            "addressee": None, "kind": "spoken"} for row in targets]}, targets)
        (source / "batch-000.json").write_text(json.dumps({"database_sha256": batch.fingerprint(args.db), "edition_id": "edition", "candidates": rows}))
        argv = ["--db", str(args.db), "--input", str(source), "--out", str(args.out), "--model", "reviewer", "--batch-size", "1", "--max-calls", "5", "--execute"]
        opener = Opener(unsupported())
        with patch.dict(os.environ, fixtures.DUMMY_ENV), patch.object(batch.urllib.request, "build_opener", return_value=opener), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(candidate_review.main(argv), 0)
        result = json.loads((args.out / "summary.json").read_bytes())
        self.assertEqual(result["stop_reason"], "unsupported_response_content")
        self.assertEqual((result["failed_rows"], result["unattempted_rows"]), (1, 2))
        self.assertEqual(result["cache_read_tokens_reported"], 31)
        self.assertEqual(len(opener.sent), 1); self.check_failure(args.out)

    def test_alignment_candidate_and_review_chains_stop_first_incompatible_call(self):
        for mode in ["candidate", "review"]:
            opener = Opener(unsupported())
            if mode == "candidate":
                case = self.fixture(fixtures.candidate_fixtures.AlignmentRuntimeTests)
                module = alignment_candidates
            else:
                case = self.fixture(fixtures.review_fixtures.AlignmentReviewRuntimeTests)
                fixtures.review_fixtures.write_batch(case.inp, "align-000.json", case.baseline,
                    [fixtures.review_fixtures.make_row(cid, ["j1"], "candidate") for cid in ["z1", "z2", "z3"]])
                module = alignment_review
            with case.env(), patch.object(batch.urllib.request, "build_opener", return_value=opener):
                result = module.run(case.args(batch_size=1))
            self.assertEqual(result["stop_reason"], "unsupported_response_content")
            self.assertEqual((result["failed_rows"], result["unattempted_rows"]), (1, 2))
            self.assertEqual(result["cache_read_tokens" if mode == "candidate" else "cache_read_input_tokens"], 31)
            self.assertEqual(len(opener.sent), 1); self.check_failure(case.out)

    def test_attribution_stops_first_incompatible_response(self):
        case = self.fixture(fixtures.attribution_fixtures.AttributionReviewTests); args = case._args(batch_size=1, max_calls=5)
        opener = Opener(unsupported())
        with patch.dict(os.environ, fixtures.DUMMY_ENV), patch.object(batch.urllib.request, "build_opener", return_value=opener):
            result = attribution_review.run(args)
        self.assertEqual(result["stop_reason"], "unsupported_response_content")
        self.assertEqual((result["failed_rows"], result["unattempted_rows"]), (1, 4))
        self.assertEqual(result["cache_read_input_tokens"], 31)
        self.assertEqual(len(opener.sent), 1); self.check_failure(args.out)

    def test_success_prefix_is_preserved_before_incompatible_second_batch(self):
        case = self.fixture(fixtures.review_fixtures.AlignmentReviewRuntimeTests)
        fixtures.review_fixtures.write_batch(case.inp, "align-000.json", case.baseline,
            [fixtures.review_fixtures.make_row(cid, ["j1"], "candidate") for cid in ["z1", "z2", "z3"]])
        sent = []
        class Sequence:
            def open(inner, request, timeout):
                wire = json.loads(request.data); sent.append(wire)
                if len(sent) == 1:
                    items = json.loads(wire["messages"][-1]["content"])["items"]
                    payload = {"reviews": [{"review_id": item["review_id"], "decision": "keep", "basis": "semantic_match"} for item in items]}
                    response = {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(payload)}], "usage": {"cache_read_input_tokens": 3}}
                else: response = unsupported()
                return io.BytesIO(json.dumps(response).encode())
        state = {}
        with case.env(), patch.object(batch.urllib.request, "build_opener", return_value=Sequence()):
            result = alignment_review.run(case.args(batch_size=1), session=state)
        self.assertEqual(result["stop_reason"], "unsupported_response_content")
        self.assertEqual((result["valid_rows"], result["failed_rows"], result["unattempted_rows"]), (1, 1, 1))
        self.assertEqual(result["cache_read_input_tokens"], 34)
        self.assertEqual(len(sent), 2)
        self.assertEqual([len(wire["messages"]) for wire in sent], [1, 3])
        self.assertEqual(len(state["history"]), 2)
        self.assertEqual(state["history"], sent[1]["messages"][:2])
        self.assertTrue((case.out / "review-000.json").exists())
        self.assertFalse((case.out / "review-001.json").exists())

    def test_incompatible_third_attribution_response_keeps_fatal_reason(self):
        case = self.fixture(fixtures.attribution_fixtures.AttributionReviewTests)
        effects = [ValueError("synthetic invalid"), ValueError("synthetic invalid"), batch.ModelResponseError("unsupported_response_content", usage=USAGE)]
        with patch.dict(os.environ, fixtures.DUMMY_ENV), patch.object(attribution_review, "request_model", side_effect=effects) as caller:
            result = attribution_review.run(case._args(batch_size=1, max_calls=5))
        self.assertEqual(result["stop_reason"], "unsupported_response_content")
        self.assertEqual((result["failed_rows"], result["unattempted_rows"]), (3, 2))
        self.assertEqual(caller.call_count, 3)

    def test_campaign_stops_later_job_and_planner_accepts_failed_terminal_history(self):
        case = self.fixture(fixtures.campaign_fixtures.AlignmentCampaignTests)
        case.batch(); case.plan([case.job("one", end=1), case.job("two", start=1)])
        opener = Opener(unsupported())
        with case.env(), patch.object(batch.urllib.request, "build_opener", return_value=opener):
            result = alignment_campaign.run_campaign(case.db, case.plan_path, case.out, "reviewer-model", 10, execute=True)
        self.assertEqual(result["stop_reason"], "unsupported_response_content")
        self.assertEqual((result["failed_rows"], result["unattempted_rows"]), (1, 2))
        self.assertEqual(result["cache_read_input_tokens"], 31)
        self.assertFalse((case.out / "two").exists())
        self.assertEqual(len(opener.sent), 1); self.check_failure(case.out / "one")
        planned = alignment_plan.build_plan(case.db, case.inp, [], [case.out / "one"], case.root / "next-plan.json")
        self.assertEqual((planned["attempted_current"], planned["failed_attempted_current"], planned["reviewed_current"]), (1, 1, 0))
        self.assertEqual(planned["selected_rows"], 2)


if __name__ == "__main__": unittest.main()
