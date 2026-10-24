"""Pure/synthetic preserve-thinking protocol and worker session tests."""
import contextlib
import copy
import io
import json
import os
import sqlite3
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from tests.ltr.model import test_redirects as fixtures
from ltr import response_history as protocol
from ltr.quotes import batch
from ltr.alignment import review as ar
from ltr.alignment import campaign
from ltr.attribution import review as attribution
from ltr.alignment import plan as planner

MODE = "preserve-thinking"
THOUGHT = "PRIVATE_SYNTHETIC_THOUGHT"
SIGNATURE = "PRIVATE_SYNTHETIC_SIGNATURE"


class DomainOpener:
    def __init__(self, kind="review", signature=SIGNATURE, bad=False):
        self.kind, self.signature, self.bad, self.sent = kind, signature, bad, []
    def open(self, request, timeout):
        wire = json.loads(request.data); self.sent.append((request.data, wire))
        payload = json.loads(wire["messages"][-1]["content"])
        if self.kind == "review":
            reply = {"reviews": [{"review_id": row["review_id"], "decision": "keep", "basis": "semantic_match"} for row in payload["items"]]}
        else:
            reply = {"suggestions": [fixtures.attribution_fixtures.AttributionReviewTests.valid_responder(row) for row in payload["targets"]]}
        content = [{"type": "thinking", "thinking": THOUGHT, "signature": self.signature},
                   {"type": "redacted_thinking", "data": ""}, {"type": "text", "text": json.dumps(reply)}]
        if self.bad: content.append({"type": "tool_use", "name": "private-tool", "input": {}})
        return io.BytesIO(json.dumps({"stop_reason": "end_turn", "content": content,
            "usage": {"input_tokens": 11, "output_tokens": 7, "cache_read_input_tokens": 3}}).encode())


class FailedFirstOpener(DomainOpener):
    """Keep a complete transport response while failing JSON or domain evidence."""
    def __init__(self, kind, failure):
        super().__init__(kind)
        self.failure, self.failed_content = failure, None

    def open(self, request, timeout):
        response = json.loads(super().open(request, timeout).read())
        if len(self.sent) == 1:
            if self.failure == "json":
                response["content"][-1]["text"] = "SYNTHETIC_NOT_JSON"
            else:
                payload = json.loads(response["content"][-1]["text"])
                if self.kind == "review":
                    payload["reviews"][0]["basis"] = "invalid_evidence_basis"
                else:
                    payload["suggestions"][0]["evidence"][0]["text"] = "SYNTHETIC_ABSENT_EVIDENCE"
                response["content"][-1]["text"] = json.dumps(payload)
            self.failed_content = copy.deepcopy(response["content"])
        return io.BytesIO(json.dumps(response).encode())


class PreserveProtocolTests(unittest.TestCase):
    def test_exact_order_empty_opaque_and_deep_copy(self):
        original = [{"type": "thinking", "thinking": "", "signature": ""},
                    {"type": "text", "text": " {\"x\":1} "}, {"type": "redacted_thinking", "data": ""}]
        before = copy.deepcopy(original)
        text, assistant, shape = protocol.assistant_content(original, MODE)
        self.assertEqual(text, '{"x":1}')
        self.assertEqual(assistant, before)
        self.assertEqual(shape, {"blocks": 3, "text": 1, "thinking": 1, "redacted_thinking": 1})
        original[0]["signature"] = "mutated"; original.reverse()
        self.assertEqual(assistant, before)

    def test_two_round_helper_retains_blocks_and_exact_count_without_claiming_session_binding(self):
        history = []
        content = [{"type": "thinking", "thinking": THOUGHT, "signature": SIGNATURE}, {"type": "text", "text": '{"x":1}'}]
        class Opener:
            sent = []
            def open(inner, request, timeout):
                inner.sent.append(request.data)
                return io.BytesIO(json.dumps({"stop_reason": "end_turn", "content": content}).encode())
        opener = Opener()
        with patch.object(batch.urllib.request, "build_opener", return_value=opener):
            first = batch.request_model("http://synthetic.invalid", "dummy", "model", {"round": 1}, history=history, session_id="fixed", history_mode=MODE)
            expected = batch.encode_request("model", {"round": 2}, history=history, session_id="fixed", history_mode=MODE)
            count = batch.request_byte_count("model", {"round": 2}, history=history, session_id="fixed", history_mode=MODE)
            batch.request_model("http://synthetic.invalid", "dummy", "model", {"round": 2}, history=history, session_id="fixed", history_mode=MODE)
        self.assertEqual(first["payload"], {"x": 1})
        self.assertEqual(json.loads(opener.sent[1])["messages"][1]["content"], content)
        self.assertEqual(opener.sent[1], expected); self.assertEqual(len(expected), count)
        self.assertEqual(len(history), 4)

    def test_default_wire_is_unchanged_and_preserve_is_not_a_wire_parameter(self):
        args = dict(history=[{"role": "assistant", "content": "synthetic"}], session_id="fixed")
        self.assertEqual(batch.encode_request("model", {}, **args), batch.encode_request("model", {}, history_mode="text", **args))
        body = json.loads(batch.encode_request("model", {}, history=[], history_mode=MODE))
        self.assertNotIn("history_mode", body)
        for invalid in [None, True, 1, "other"]:
            with patch.object(batch.urllib.request, "build_opener") as send:
                with self.assertRaisesRegex(ValueError, "invalid_history_mode"):
                    batch.request_model("http://synthetic.invalid", "dummy", "model", {}, history_mode=invalid)
                send.assert_not_called()

    def test_preserve_rejects_unknown_tool_malformed_opaque_and_root_fields_before_history(self):
        text = {"type": "text", "text": '{"ok":true}'}
        contents = [[{"type": "thinking", "thinking": "x", "signature": None}, text],
                    [{"type": "thinking", "thinking": "x"}, text], [{"type": "redacted_thinking", "data": 1}, text],
                    [text, {"type": "tool_result", "content": "private"}], [text, {"type": "unknown"}],
                    [{"type": "thinking", "thinking": "x", "signature": "\ud800"}, text]]
        for content in contents:
            with self.assertRaisesRegex(ValueError, "unsupported_response_content"):
                protocol.assistant_content(content, MODE)
        for field in protocol.TOP_LEVEL_FIELDS:
            response = {"stop_reason": "end_turn", "content": [text], "usage": {"output_tokens": 7}, field: "SECRET"}
            class Opener:
                def open(self, *args, **kwargs): return io.BytesIO(json.dumps(response).encode())
            history = []
            with patch.object(batch.urllib.request, "build_opener", return_value=Opener()):
                with self.assertRaises(batch.ModelResponseError) as caught:
                    batch.request_model("http://synthetic.invalid", "dummy", "model", {}, history=history, history_mode=MODE)
            self.assertEqual(caught.exception.code, "unsupported_response_content")
            self.assertEqual(caught.exception.usage, {"output_tokens": 7}); self.assertEqual(history, [])

    def test_escaped_opaque_value_is_bounded_by_complete_serialized_assistant_bytes(self):
        opaque = "\\" * (protocol.MAX_CONTENT_BYTES // 2 + 1)
        content = [{"type": "thinking", "thinking": "", "signature": opaque}, {"type": "text", "text": "{}"}]
        raw_value_bytes = sum(len(value.encode("utf-8")) for block in content for value in block.values())
        self.assertLess(raw_value_bytes, protocol.MAX_CONTENT_BYTES)
        self.assertGreater(len(json.dumps({"role": "assistant", "content": content}, ensure_ascii=False).encode("utf-8")), protocol.MAX_CONTENT_BYTES)
        with self.assertRaisesRegex(ValueError, "unsupported_response_content"):
            protocol.assistant_content(content, MODE)

    def test_preserve_block_and_content_limits_and_history_mode_shapes(self):
        with self.assertRaises(ValueError): protocol.assistant_content([{"type": "text", "text": ""}] * 1001, MODE)
        with self.assertRaises(ValueError): protocol.assistant_content([{"type": "thinking", "thinking": "", "signature": "x" * (protocol.MAX_CONTENT_BYTES + 1)}], MODE)
        for history, mode in [([{"role": "assistant", "content": "old text"}], MODE),
                              ([{"role": "assistant", "content": [{"type": "text", "text": "x"}]}], "text")]:
            with self.assertRaisesRegex(ValueError, "invalid_session"): protocol.validate_history(history, mode)


class PreserveWorkerTests(unittest.TestCase):
    fixture = fixtures.RedirectRuntimeTests.fixture

    def review(self):
        case = self.fixture(fixtures.review_fixtures.AlignmentReviewRuntimeTests)
        fixtures.review_fixtures.write_batch(case.inp, "align-000.json", case.baseline,
            [fixtures.review_fixtures.make_row(cid, [jid], "candidate") for cid, jid in [("z1", "j1"), ("z2", "j2"), ("z3", "j1")]])
        return case

    def assert_private(self, output):
        for path in output.rglob("*.json"):
            body = path.read_text(encoding="utf-8")
            for secret in [THOUGHT, SIGNATURE, '"signature":', '"thinking": "']:
                self.assertNotIn(secret, body)

    def test_review_failed_complete_prefix_continues_different_target_with_guards(self):
        for failure in ["json", "evidence"]:
            with self.subTest(failure=failure):
                case = self.review(); state = {}; opener = FailedFirstOpener("review", failure)
                with case.env(), patch.object(batch.urllib.request, "build_opener", return_value=opener):
                    first = ar.run(case.args(batch_size=1, max_calls=1, end=1, history_mode=MODE), session=state)
                    self.assertEqual((first["attempted_rows"], first["failed_rows"], first["valid_rows"]), (1, 1, 0))
                    record = json.loads((case.out / "progress.json").read_bytes())["calls"][0]
                    self.assertEqual(record["error_code"], "invalid_model_json" if failure == "json" else "invalid_review_enum")
                    self.assertEqual(state["history"][1]["content"], opener.failed_content)
                    prefix = copy.deepcopy(state["history"])
                    second = ar.run(case.args(out=case.root / "continued", batch_size=1, max_calls=1,
                                              start=1, end=2, history_mode=MODE), session=state)
                    self.assertEqual((second["attempted_rows"], second["valid_rows"], second["failed_rows"]), (1, 1, 0))
                    self.assertEqual(opener.sent[1][1]["messages"][:2], prefix)
                    self.assertEqual(state["history"][:2], prefix)
                    first_id = json.loads(prefix[0]["content"])["items"][0]["review_id"]
                    second_id = json.loads(state["history"][2]["content"])["items"][0]["review_id"]
                    self.assertNotEqual(first_id, second_id)
                    self.assertEqual(len({wire["metadata"]["user_id"] for _, wire in opener.sent}), 1)
                    for name, mode in [("mode-refused", "text"), ("source-refused", MODE)]:
                        if name == "source-refused":
                            with sqlite3.connect(case.db) as con:
                                con.execute("UPDATE dialogues SET ordinal=ordinal+100 WHERE id='z1'")
                        with self.assertRaises(ValueError):
                            ar.run(case.args(out=case.root / name, start=2, history_mode=mode), session=state)
                        self.assertFalse((case.root / name).exists())
                    self.assertEqual(len(opener.sent), 2)
                    self.assertEqual(len(state["history"]), 4)
                self.assert_private(case.out); self.assert_private(case.root / "continued")

    def test_attribution_failed_complete_prefix_continues_different_target_with_guards(self):
        for failure in ["json", "evidence"]:
            with self.subTest(failure=failure):
                case = self.fixture(fixtures.attribution_fixtures.AttributionReviewTests)
                state = {}; opener = FailedFirstOpener("attribution", failure)
                with patch.dict(os.environ, fixtures.DUMMY_ENV), patch.object(batch.urllib.request, "build_opener", return_value=opener):
                    first = attribution.run(case._args(batch_size=1, max_calls=1, history_mode=MODE), session=state)
                    self.assertEqual((first["attempted_rows"], first["failed_rows"], first["valid_rows"]), (1, 1, 0))
                    record = json.loads((case.root / "out" / "progress.json").read_bytes())["calls"][0]
                    self.assertEqual(record["error_code"], "invalid_model_json" if failure == "json" else "non_unique_evidence_span")
                    self.assertEqual(state["history"][1]["content"], opener.failed_content)
                    prefix = copy.deepcopy(state["history"])
                    second = attribution.run(case._args(out=case.root / "continued", start=1, batch_size=1,
                                                       max_calls=1, history_mode=MODE), session=state)
                    self.assertEqual((second["attempted_rows"], second["valid_rows"], second["failed_rows"]), (1, 1, 0))
                    self.assertEqual(opener.sent[1][1]["messages"][:2], prefix)
                    self.assertEqual(state["history"][:2], prefix)
                    self.assertNotEqual(json.loads(prefix[0]["content"])["targets"][0]["dialogue_id"],
                                        json.loads(state["history"][2]["content"])["targets"][0]["dialogue_id"])
                    self.assertEqual(len({wire["metadata"]["user_id"] for _, wire in opener.sent}), 1)
                    for name, mode in [("mode-refused", "text"), ("source-refused", MODE)]:
                        if name == "source-refused":
                            with sqlite3.connect(case.db) as con:
                                con.execute("UPDATE dialogues SET ordinal=ordinal+100 WHERE id='d1'")
                        with self.assertRaises(ValueError):
                            attribution.run(case._args(out=case.root / name, start=2, history_mode=mode), session=state)
                        self.assertFalse((case.root / name).exists())
                    self.assertEqual(len(opener.sent), 2)
                    self.assertEqual(len(state["history"]), 4)
                self.assert_private(case.root / "out"); self.assert_private(case.root / "continued")

    def test_review_two_batches_keep_opaque_history_and_static_receipts(self):
        case = self.review(); state = {}; opener = DomainOpener()
        with case.env(), patch.object(batch.urllib.request, "build_opener", return_value=opener):
            result = ar.run(case.args(batch_size=1, max_calls=2, history_mode=MODE, incremental_references=True), session=state)
        self.assertEqual((result["valid_rows"], result["failed_rows"]), (2, 0))
        self.assertEqual(result["history_mode"], MODE)
        self.assertEqual(len(state["history"]), 4)
        self.assertEqual([len(wire["messages"]) for _, wire in opener.sent], [1, 3])
        self.assertEqual(opener.sent[1][1]["messages"][1]["content"][0]["signature"], SIGNATURE)
        progress = json.loads((case.out / "progress.json").read_bytes())
        self.assertEqual(progress["calls"][0]["content_shape"], {"blocks": 3, "text": 1, "thinking": 1, "redacted_thinking": 1})
        self.assert_private(case.out)

    def test_review_mode_change_and_corrupted_preserved_history_refused_before_output(self):
        for first, second in [("text", MODE), (MODE, "text")]:
            case = self.review(); state = {}; opener = DomainOpener()
            fake = case.fake([]) if first == "text" else None
            with case.env(), patch.object(batch.urllib.request, "build_opener", return_value=opener):
                manager = patch.object(ar, "request_model", side_effect=fake) if fake else contextlib.nullcontext()
                with manager: ar.run(case.args(max_calls=1, end=1, history_mode=first), session=state)
                destination = case.root / "next"
                with patch.object(ar, "request_model") as sender:
                    with self.assertRaisesRegex(ValueError, "invalid_session"):
                        ar.run(case.args(out=destination, start=1, history_mode=second), session=state)
                    sender.assert_not_called()
                self.assertFalse(destination.exists())
        state["history_mode"] = MODE; state["history"][1]["content"] = [{"type": "thinking", "thinking": "x", "signature": None}]
        with case.env(), self.assertRaisesRegex(ValueError, "invalid_session"):
            ar.run(case.args(out=case.root / "corrupt", history_mode=MODE), session=state)
        self.assertFalse((case.root / "corrupt").exists())

    def test_preserved_signature_counts_toward_request_budget_and_next_row_is_unattempted(self):
        case = self.review(); opener = DomainOpener(signature="opaque" * 40000)
        with case.env(), patch.object(batch.urllib.request, "build_opener", return_value=opener):
            result = ar.run(case.args(batch_size=1, history_mode=MODE, max_request_bytes=100000))
        self.assertEqual(result["stop_reason"], "request_byte_budget_exhausted")
        self.assertEqual((result["attempted_rows"], result["valid_rows"], result["failed_rows"], result["unattempted_rows"]), (1, 1, 0, 2))
        self.assertEqual(len(opener.sent), 1)
        self.assertGreater(result["next_request_bytes"], 240000)
        self.assert_private(case.out)

    def test_campaign_same_mode_session_full_history_crosses_jobs(self):
        case = self.fixture(fixtures.campaign_fixtures.AlignmentCampaignTests)
        case.batch(); case.plan([case.job("one", end=1), case.job("two", start=1)])
        opener = DomainOpener()
        with case.env(), patch.object(batch.urllib.request, "build_opener", return_value=opener):
            result = campaign.run_campaign(case.db, case.plan_path, case.out, "reviewer-model", 10, execute=True, history_mode=MODE, incremental_references=True)
        self.assertEqual(result["valid_rows"], 3)
        self.assertEqual([len(wire["messages"]) for _, wire in opener.sent], [1, 3, 5])
        self.assertEqual(len({wire["metadata"]["user_id"] for _, wire in opener.sent}), 1)
        self.assertEqual(result["history_mode"], MODE)
        self.assertTrue(all(row["history_mode"] == MODE for row in result["jobs"]))
        self.assert_private(case.out)

    def test_attribution_reference_history_reuse_and_mode_binding(self):
        case = self.fixture(fixtures.attribution_fixtures.AttributionReviewTests); state = {}; opener = DomainOpener("attribution")
        with patch.dict(os.environ, fixtures.DUMMY_ENV), patch.object(batch.urllib.request, "build_opener", return_value=opener):
            result = attribution.run(case._args(batch_size=1, max_calls=2, history_mode=MODE, reuse_context=True), session=state)
            self.assertEqual(result["valid_rows"], 2)
            self.assertEqual(len(state["history"]), 4)
            self.assertEqual(opener.sent[1][1]["messages"][1]["content"][0]["signature"], SIGNATURE)
            continued = case.root / "continued"
            continuation = attribution.run(case._args(out=continued, start=2, max_calls=1, batch_size=1, history_mode=MODE, reuse_context=True), session=state)
            self.assertEqual(continuation["valid_rows"], 1)
            self.assertEqual(len(state["history"]), 6)
            self.assertEqual(len(opener.sent[2][1]["messages"]), 5)
            destination = case.root / "next"
            with self.assertRaisesRegex(ValueError, "invalid_session"):
                attribution.run(case._args(out=destination, start=3, history_mode="text", reuse_context=True), session=state)
            self.assertFalse(destination.exists()); self.assertEqual(len(opener.sent), 3)
        self.assert_private(case.root / "out")

    def test_failed_preserve_history_is_valid_planner_evidence_and_mode_metadata_is_checked(self):
        case = self.review(); opener = DomainOpener(bad=True)
        with case.env(), patch.object(batch.urllib.request, "build_opener", return_value=opener):
            result = ar.run(case.args(batch_size=1, history_mode=MODE))
        self.assertEqual(result["stop_reason"], "unsupported_response_content")
        summary = planner.build_plan(case.db, case.inp, [], [case.out], case.root / "next-plan.json")
        self.assertEqual((summary["failed_attempted_current"], summary["reviewed_current"], summary["selected_rows"]), (1, 0, 2))
        path = case.out / "summary.json"; body = json.loads(path.read_bytes()); body["history_mode"] = "text"; path.write_text(json.dumps(body))
        with self.assertRaises(planner.AlignmentPlanError): planner.build_plan(case.db, case.inp, [], [case.out], case.root / "forged-plan.json")
        self.assertFalse((case.root / "forged-plan.json").exists()); self.assert_private(case.out)

    def test_cli_flags_and_invalid_modes(self):
        for module, argv in [(ar, ["--db", "synthetic", "--input", "synthetic", "--out", "synthetic", "--model", "m", "--max-calls", "1", "--execute"]),
                             (attribution, ["--db", "synthetic", "--kind", "haruhi_unknown", "--edition", "e", "--out", "synthetic", "--model", "m", "--execute"])]:
            with patch.object(module, "run", return_value={"source_unchanged": True}) as runner, contextlib.redirect_stdout(io.StringIO()):
                module.main(argv + ["--history-mode", MODE])
            self.assertEqual(runner.call_args.args[0].history_mode, MODE)
        with patch.object(campaign, "run_campaign", return_value={"stop_reason": "finite_scope_complete", "jobs": []}) as runner, contextlib.redirect_stdout(io.StringIO()):
            campaign.main(["--db", "synthetic", "--plan", "synthetic", "--out", "synthetic", "--model", "m", "--max-calls", "1", "--execute", "--history-mode", MODE])
        self.assertEqual(runner.call_args.kwargs["history_mode"], MODE)
        case = self.review()
        for value in [None, True, "unknown"]:
            with self.assertRaisesRegex(ValueError, "invalid_history_mode"):
                ar.run(case.args(history_mode=value))
            self.assertFalse(case.out.exists())
            with patch.object(ar, "run") as dispatch:
                with self.assertRaises(campaign.AlignmentCampaignError):
                    campaign.run_campaign(case.db, case.root / "missing-plan.json", case.out, "reviewer", 1, execute=True, history_mode=value)
                dispatch.assert_not_called()
            self.assertFalse(case.out.exists())


if __name__ == "__main__": unittest.main()
