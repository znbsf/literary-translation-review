"""Finite campaign tests using invented source and a stub model transport."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests.ltr.alignment import test_review as fixtures
from ltr.alignment import campaign
from ltr.alignment import review as ar


class AlignmentCampaignTests(unittest.TestCase):
    setUp = fixtures.AlignmentReviewRuntimeTests.setUp
    env = fixtures.AlignmentReviewRuntimeTests.env
    fake = fixtures.AlignmentReviewRuntimeTests.fake

    def plan(self, jobs=None, **over):
        if jobs is None:
            jobs = [self.job("one")]
        self.plan_path = self.root / "plan.json"
        data = dict(schema=campaign.SCHEMA, source_sha256=self.baseline, jobs=jobs)
        data.update(over)
        self.plan_path.write_text(json.dumps(data), encoding="utf-8")
        return self.plan_path

    def job(self, identity, directory="input", **over):
        return dict(job_id=identity, input=directory, max_calls=5, batch_size=1,
                    start=0, end=None, exclude_reviewed=None) | over

    def bound_job(self, **over):
        job = self.job("bound", **over)
        con = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)
        try:
            con.execute("PRAGMA query_only=ON")
            loaded = ar.load_source(self.inp, con, self.baseline, source_order=True)
        finally:
            con.close()
        ids = [row["review_id"] for row in loaded["candidates"]][job["start"]:job["end"]]
        return job | {"expected_artifact_sha256": ar.digest(loaded["artifacts"]),
                      "expected_selected_ids_sha256": ar.digest(ids[:job["max_calls"] * job["batch_size"]])}

    def batch(self, ids=("z1", "z2", "z3"), directory=None):
        directory = self.inp if directory is None else directory
        directory.mkdir(exist_ok=True)
        fixtures.write_batch(directory, "align-000.json", self.baseline,
                             [fixtures.make_row(cid, ["j1"], "candidate") for cid in ids])

    def execute(self, calls=None, max_calls=10, effect=None, **over):
        calls = [] if calls is None else calls
        with self.env(), patch.object(ar, "request_model", side_effect=effect or self.fake(calls)):
            return campaign.run_campaign(self.db, self.plan_path, self.out, "reviewer-model",
                                         max_calls, execute=True, **over)

    def assert_balanced(self, result):
        self.assertEqual(result["planned_rows"], result["attempted_rows"] + result["unattempted_rows"])
        self.assertEqual(result["attempted_rows"], result["valid_rows"] + result["failed_rows"] + result["unknown_rows"])
        self.assertLessEqual(result["attempted_calls"], result["max_calls"])
        self.assertFalse(result["corpus_updated"])

    def test_shared_session_history_across_different_edition_pairs(self):
        con = sqlite3.connect(self.db)
        con.executescript("""
            INSERT INTO editions VALUES ('zh2','synthetic','zh','bk2'),('ja2','synthetic','ja','bk2');
            INSERT INTO dialogues VALUES ('zz1','zh2',0,'SYNTHETIC_SECOND_ZH'),
                                         ('jj1','ja2',0,'SYNTHETIC_SECOND_JA');
        """)
        con.commit()
        con.close()
        self.baseline = ar.fingerprint(self.db)
        self.batch(("z1",))
        other = self.root / "second"
        other.mkdir()
        row = fixtures.make_row("z1", ["j1"], "candidate") | {
            "chinese_id": "zz1", "chinese_text_sha256": fixtures.sha("SYNTHETIC_SECOND_ZH"),
            "japanese_ids": ["jj1"], "japanese": [{"id": "jj1", "text_sha256": fixtures.sha("SYNTHETIC_SECOND_JA")}]
        }
        fixtures.write_batch(other, "align-000.json", self.baseline, [row],
                             zh_edition_id="zh2", ja_edition_id="ja2", book_key="bk2")
        self.plan([self.job("one"), self.job("two", "second")])
        calls = []
        result = self.execute(calls)
        self.assertEqual(result["valid_rows"], 2)
        self.assertEqual(result["stop_reason"], "finite_scope_complete")
        self.assertEqual([len(call["history"]) for call in calls], [0, 2])
        self.assertEqual(calls[0]["session_id"], calls[1]["session_id"])
        self.assertEqual(calls[0]["session_id"], result["session_id"])
        self.assertEqual(calls[1]["payload"]["japanese_reference"][0]["id"], "jj1")
        self.assertEqual(result['cache_read_input_tokens'], 30)
        self.assertEqual([job['cache_read_input_tokens'] for job in result['jobs']], [0, 30])
        self.assert_balanced(result)

    def test_incremental_reference_delivery_is_shared_and_deduplicated_across_jobs(self):
        fixtures.write_batch(self.inp, 'align-000.json', self.baseline,
            [fixtures.make_row('z1', ['j1'], 'candidate'), fixtures.make_row('z2', ['j2'], 'candidate'),
             fixtures.make_row('z3', ['j1'], 'candidate')])
        self.plan([self.job('one', end=1), self.job('two', start=1, end=3)])
        calls = []
        result = self.execute(calls, incremental_references=True)
        self.assertEqual([r['id'] for r in calls[0]['payload']['japanese_reference']], ['j1'])
        self.assertEqual([r['id'] for r in calls[1]['payload']['japanese_reference']], ['j2'])
        self.assertNotIn('japanese_reference', calls[2]['payload'])
        self.assertEqual([len(call['history']) for call in calls], [0, 2, 4])
        self.assertEqual(len({call['session_id'] for call in calls}), 1)
        self.assertEqual(result['reference_delivery'], 'incremental')
        self.assertEqual([job['reference_delivery'] for job in result['jobs']], ['incremental', 'incremental'])
        self.assertEqual(result['valid_rows'], 3)
        self.assert_balanced(result)

    def test_incremental_reference_flag_requires_strict_bool_before_output(self):
        self.batch()
        self.plan()
        for value in (None, 0, 1, 'true', [], {}):
            with patch.object(ar, 'run') as request:
                with self.assertRaises(campaign.AlignmentCampaignError):
                    self.execute(incremental_references=value)
                request.assert_not_called()
            self.assertFalse(self.out.exists())

    def test_request_byte_budget_stops_all_jobs_without_attempt_or_failure(self):
        self.batch()
        self.plan([self.job('one', end=1), self.job('two', start=1)])
        calls = []
        result = self.execute(calls, max_request_bytes=1)
        self.assertEqual(calls, [])
        self.assertEqual(result['stop_reason'], 'request_byte_budget_exhausted')
        self.assertEqual(result['request_byte_budget'], 1)
        self.assertGreater(result['next_request_bytes'], 1)
        self.assertEqual((result['attempted_calls'], result['failed_calls'], result['failed_rows']), (0, 0, 0))
        self.assertEqual(result['unattempted_rows'], result['planned_rows'])
        self.assertEqual(result['jobs'][1]['status'], 'not_started')
        self.assertFalse((self.out / 'two').exists())
        self.assertEqual(result['jobs'][0]['request_byte_budget'], 1)
        self.assert_balanced(result)

    def test_same_byte_budget_and_complete_history_are_shared_across_jobs(self):
        self.batch()
        self.plan([self.job('one', end=1), self.job('two', start=1)])
        calls = []
        result = self.execute(calls, max_request_bytes=16 * 1024 * 1024)
        self.assertEqual(result['valid_rows'], 3)
        self.assertEqual([len(call['history']) for call in calls], [0, 2, 4])
        self.assertEqual(len({call['session_id'] for call in calls}), 1)
        self.assertEqual([job['request_byte_budget'] for job in result['jobs']], [16 * 1024 * 1024] * 2)
        self.assertIsNone(result['next_request_bytes'])
        self.assert_balanced(result)

    def test_invalid_byte_budgets_fail_before_output_or_runtime(self):
        self.batch()
        self.plan()
        for value in (True, False, 0, -1, 16 * 1024 * 1024 + 1, '100', 1.1, [], {}):
            with patch.object(ar, 'run') as runner:
                with self.assertRaises(campaign.AlignmentCampaignError):
                    self.execute(max_request_bytes=value)
                runner.assert_not_called()
            self.assertFalse(self.out.exists())

    def test_cli_max_request_bytes_is_optional_and_forwarded(self):
        argv = ['--db', str(self.db), '--plan', str(self.root / 'plan.json'), '--out', str(self.out),
                '--model', 'reviewer', '--max-calls', '1', '--execute']
        for flag, expected in (([], None), (['--max-request-bytes', '12345'], 12345)):
            with redirect_stdout(io.StringIO()), patch.object(campaign, 'run_campaign',
                    return_value={'stop_reason': 'finite_scope_complete'}) as runner:
                self.assertEqual(campaign.main(argv + flag), 0)
            self.assertEqual(runner.call_args.kwargs['max_request_bytes'], expected)

    def test_explicit_output_token_budget_forwarded_across_shared_session_jobs(self):
        self.batch()
        self.plan([self.job('one', end=1), self.job('two', start=1)])
        calls, budgets = [], []
        successful = self.fake(calls)

        def request(*args, **kwargs):
            budgets.append(kwargs.pop('max_output_tokens'))
            return successful(*args, **kwargs)

        result = self.execute(effect=request, max_output_tokens=16000)
        self.assertEqual(budgets, [16000] * 3)
        self.assertEqual(result['output_token_budget'], 16000)
        self.assertEqual([job['output_token_budget'] for job in result['jobs']], [16000, 16000])
        self.assertEqual([len(call['history']) for call in calls], [0, 2, 4])
        self.assertEqual(len({call['session_id'] for call in calls}), 1)
        self.assertEqual(result['valid_rows'], 3)
        self.assert_balanced(result)

    def test_invalid_output_token_budget_fails_before_preflight_or_output(self):
        self.batch()
        self.plan()
        for value in (True, False, 0, -1, 131073, 1.5, '16000', [], {}):
            with patch.object(campaign, '_preflight') as preflight, patch.object(ar, 'run') as runner:
                with self.assertRaises(campaign.AlignmentCampaignError):
                    self.execute(max_output_tokens=value)
                preflight.assert_not_called()
                runner.assert_not_called()
            self.assertFalse(self.out.exists())

    def test_cli_output_token_budget_optional_and_forwarded(self):
        argv = ['--db', str(self.db), '--plan', str(self.root / 'plan.json'), '--out', str(self.out),
                '--model', 'reviewer', '--max-calls', '1', '--execute']
        for flag, expected in (([], None), (['--max-output-tokens', '16000'], 16000)):
            with redirect_stdout(io.StringIO()), patch.object(campaign, 'run_campaign',
                    return_value={'stop_reason': 'finite_scope_complete'}) as runner:
                self.assertEqual(campaign.main(argv + flag), 0)
            self.assertEqual(runner.call_args.kwargs['max_output_tokens'], expected)

    def test_cli_incremental_reference_flag_is_explicit_opt_in(self):
        argv = ['--db', str(self.db), '--plan', str(self.root / 'plan.json'), '--out', str(self.out),
                '--model', 'reviewer', '--max-calls', '1', '--execute']
        for flag, expected in (([], False), (['--incremental-references'], True)):
            with redirect_stdout(io.StringIO()), patch.object(campaign, 'run_campaign',
                    return_value={'stop_reason': 'finite_scope_complete'}) as runner:
                self.assertEqual(campaign.main(argv + flag), 0)
            self.assertIs(runner.call_args.kwargs['incremental_references'], expected)

    def test_global_budget_caps_large_job_budgets_and_reports_pending(self):
        self.batch()
        self.plan([self.job("one", max_calls=100, end=2), self.job("two", start=2)])
        calls = []
        result = self.execute(calls, max_calls=1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["planned_rows"], 1)
        self.assertEqual(result["pending_beyond_budget_rows"], 2)
        self.assertEqual(result["jobs"][1]["status"], "not_started")
        self.assertEqual(result["stop_reason"], "global_budget_exhausted")
        self.assertFalse((self.out / "two").exists())
        self.assert_balanced(result)

    def test_reported_cache_from_invalid_reply_is_in_campaign_total(self):
        self.batch(('z1', 'z2'))
        self.plan([self.job('one', end=1), self.job('two', start=1)])
        good = self.fake([])
        calls = []
        def model(*args, **kwargs):
            calls.append(1)
            reply = good(*args, **kwargs)
            reply['usage'] = {'cache_read_input_tokens': 11 if len(calls) == 1 else 17}
            if len(calls) == 1:
                reply['payload'] = {'reviews': []}
            return reply
        result = self.execute(effect=model)
        self.assertEqual(result['cache_read_input_tokens'], 28)
        self.assertEqual(result['failed_rows'], 1)
        self.assertEqual(result['valid_rows'], 1)
        self.assertEqual([job['cache_read_input_tokens'] for job in result['jobs']], [11, 17])
        self.assert_balanced(result)

    def test_short_first_job_leaves_global_calls_for_next(self):
        self.batch()
        self.plan([self.job("one", end=1, max_calls=100), self.job("two", start=1)])
        calls = []
        result = self.execute(calls, max_calls=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["planned_rows"], 2)
        self.assertEqual(result["pending_beyond_budget_rows"], 1)
        self.assertEqual([r["planned_calls"] for r in result["jobs"]], [1, 1])
        self.assert_balanced(result)

    def test_request_rejection_stops_later_job(self):
        self.batch()
        self.plan([self.job("one", end=1), self.job("two", start=1)])
        failure = urllib.error.HTTPError("http://FAKE_PRIVATE_PATH", 413, "FAKE_PRIVATE_PROSE", {}, None)
        result = self.execute(effect=failure)
        self.assertEqual(result["stop_reason"], "request_rejected")
        self.assertEqual(result["attempted_calls"], 1)
        self.assertEqual(result["failed_rows"], 1)
        self.assertEqual(result["unattempted_rows"], 2)
        self.assertFalse((self.out / "two").exists())
        self.assert_balanced(result)

    def test_consecutive_invalid_responses_stop_campaign(self):
        self.batch()
        other = self.root / "second"
        self.batch(("z1",), other)
        self.plan([self.job("one"), self.job("two", "second", start=1)])
        result = self.execute(effect=lambda *a, **kw: {"payload": {"reviews": []}})
        self.assertEqual(result["stop_reason"], "consecutive_invalid_responses")
        self.assertEqual(result["failed_calls"], 3)
        self.assertEqual(result["valid_rows"], 0)
        self.assertFalse((self.out / "two").exists())
        self.assert_balanced(result)

    def test_source_change_during_call_stops_later_job(self):
        self.batch()
        self.plan([self.job("one", end=1), self.job("two", start=1)])
        fake = self.fake([])

        def changed(*args, **kwargs):
            response = fake(*args, **kwargs)
            with self.db.open("ab") as stream:
                stream.write(b"synthetic mutation")
            return response

        result = self.execute(effect=changed)
        self.assertEqual(result["stop_reason"], "source_changed")
        self.assertEqual(result["failed_rows"], 1)
        self.assertFalse(result["source_unchanged"])
        self.assertFalse((self.out / "two").exists())
        self.assert_balanced(result)

    def test_invalid_later_input_fails_before_output_or_model(self):
        self.batch()
        other = self.root / "invalid"
        other.mkdir()
        (other / "align-000.json").write_text("FAKE_PRIVATE_PROSE", encoding="utf-8")
        self.plan([self.job("one"), self.job("two", "invalid")])
        with patch.object(ar, "request_model") as request:
            with self.assertRaises(campaign.AlignmentCampaignError):
                self.execute()
            request.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_duplicate_job_overlap_source_and_boolean_bounds_fail_preflight(self):
        self.batch()
        cases = [([self.job("one"), self.job("one")], {}),
                 ([self.job("one"), self.job("two")], {}),
                 ([self.job("one")], {"source_sha256": "0" * 64})]
        for field in ("max_calls", "batch_size", "start", "end"):
            cases.append(([self.job("one", **{field: True})], {}))
        for jobs, overrides in cases:
            with self.subTest(jobs=jobs, overrides=overrides):
                self.plan(jobs, **overrides)
                with self.assertRaises(campaign.AlignmentCampaignError):
                    self.execute()
                self.assertFalse(self.out.exists())

    def test_strict_plan_contract_and_size(self):
        self.batch()
        for value in ([], {"schema": campaign.SCHEMA},
                      {"schema": campaign.SCHEMA, "source_sha256": self.baseline, "jobs": []},
                      {"schema": campaign.SCHEMA, "source_sha256": self.baseline,
                       "jobs": [self.job("UPPERCASE")]},
                      {"schema": campaign.SCHEMA, "source_sha256": self.baseline,
                       "jobs": [self.job("one")], "unexpected": "FAKE_PRIVATE_PROSE"}):
            self.plan()
            self.plan_path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(campaign.AlignmentCampaignError):
                self.execute()
            self.assertFalse(self.out.exists())
        self.plan_path.write_bytes(b" " * (campaign.MAX_PLAN_BYTES + 1))
        with self.assertRaises(campaign.AlignmentCampaignError):
            self.execute()
        self.assertFalse(self.out.exists())

    def test_matching_optional_job_bindings_dispatch_exact_scope(self):
        self.batch()
        self.plan([self.bound_job(start=1, end=3)])
        calls = []
        result = self.execute(calls)
        self.assertEqual([call["payload"]["items"][0]["chinese"]["id"] for call in calls], ["z2", "z3"])
        self.assertEqual(result["valid_rows"], 2)

    def test_partial_or_malformed_job_bindings_refuse_before_output(self):
        self.batch()
        good = self.bound_job()
        cases = []
        for key in campaign.JOB_BINDING_KEYS:
            missing = dict(good)
            missing.pop(key)
            cases.append(missing)
            for value in (None, True, 1, [], {}, "f" * 63, "A" * 64, "g" * 64):
                cases.append(good | {key: value})
        for job in cases:
            with self.subTest(job=job):
                self.plan([job])
                with patch.object(ar, "run") as runner:
                    with self.assertRaises(campaign.AlignmentCampaignError):
                        self.execute()
                    runner.assert_not_called()
                self.assertFalse(self.out.exists())

    def test_bound_artifact_addition_or_deletion_rejected_same_database(self):
        self.batch(("z2", "z3"))
        job = self.bound_job(start=1, end=2)
        self.plan([job])
        source_sha = ar.fingerprint(self.db)
        # Add a valid earlier source row without modifying the source database:
        # canonical index 1 now means z2 rather than the planned z3.
        fixtures.write_batch(self.inp, "align-added.json", self.baseline,
                             [fixtures.make_row("z1", ["j1"], "candidate")])
        with patch.object(ar, "run") as runner:
            with self.assertRaisesRegex(campaign.AlignmentCampaignError, "campaign_expected_artifact_mismatch"):
                self.execute()
            runner.assert_not_called()
        self.assertFalse(self.out.exists())
        self.assertEqual(ar.fingerprint(self.db), source_sha)
        # Freeze this larger set, then delete its added member.
        job = self.bound_job(start=1, end=2)
        self.plan([job])
        (self.inp / "align-added.json").unlink()
        with patch.object(ar, "run") as runner:
            with self.assertRaisesRegex(campaign.AlignmentCampaignError, "campaign_expected_artifact_mismatch"):
                self.execute()
            runner.assert_not_called()
        self.assertFalse(self.out.exists())
        self.assertEqual(ar.fingerprint(self.db), source_sha)

    def test_bound_range_or_budget_drift_rejected_without_dispatch(self):
        self.batch()
        good = self.bound_job(start=0, end=1)
        for changed in (good | {"start": 1, "end": 2},
                        good | {"expected_selected_ids_sha256": "0" * 64}):
            self.plan([changed])
            with patch.object(ar, "run") as runner:
                with self.assertRaisesRegex(campaign.AlignmentCampaignError, "campaign_expected_selected_ids_mismatch"):
                    self.execute()
                runner.assert_not_called()
            self.assertFalse(self.out.exists())
        self.plan([self.bound_job()])
        with patch.object(ar, "run") as runner:
            with self.assertRaisesRegex(campaign.AlignmentCampaignError, "campaign_expected_selected_ids_mismatch"):
                self.execute(max_calls=1)
            runner.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_explicit_execute_global_bounds_and_producer_independence(self):
        self.batch()
        self.plan()
        for execute, budget, model in ((False, 1, "reviewer"), (True, True, "reviewer"),
                                        (True, 0, "reviewer"), (True, 1001, "reviewer"),
                                        (True, 1, "producer-model"), (True, 1, "producer-served")):
            with self.assertRaises(campaign.AlignmentCampaignError):
                campaign.run_campaign(self.db, self.plan_path, self.out, model, budget, execute=execute)
            self.assertFalse(self.out.exists())

    def test_out_of_order_artifacts_dispatch_in_canonical_source_order(self):
        self.batch(("z2", "z1"))
        self.plan()
        calls = []
        result = self.execute(calls)
        self.assertEqual([call["payload"]["items"][0]["chinese"]["id"] for call in calls], ["z1", "z2"])
        planned = result["jobs"][0]["review_ids"]
        dispatched = [item["review_id"] for call in calls for item in call["payload"]["items"]]
        self.assertEqual(planned, dispatched)
        self.assertIs(result["source_order"], True)
        self.assertIs(result["jobs"][0]["source_order"], True)
        self.assert_balanced(result)

    def test_canonical_source_order_applies_before_job_ranges(self):
        self.batch(("z3", "z2", "z1"))
        self.plan([self.job("one", end=1), self.job("two", start=1, end=2)])
        calls = []
        result = self.execute(calls)
        self.assertEqual([call["payload"]["items"][0]["chinese"]["id"] for call in calls], ["z1", "z2"])
        self.assertEqual([row["review_ids"] for row in result["jobs"]],
                         [[calls[0]["payload"]["items"][0]["review_id"]],
                          [calls[1]["payload"]["items"][0]["review_id"]]])

    def test_artifact_change_after_preflight_refuses_next_job(self):
        self.batch(("z1",))
        other = self.root / "second"
        self.batch(("z2",), other)
        self.plan([self.job("one"), self.job("two", "second")])
        fake = self.fake([])

        def mutate(*args, **kwargs):
            result = fake(*args, **kwargs)
            (other / "align-new.json").write_text("FAKE_PRIVATE_PROSE", encoding="utf-8")
            return result

        result = self.execute(effect=mutate)
        self.assertEqual(result["valid_rows"], 1)
        self.assertEqual(result["stop_reason"], "campaign_artifact_changed")
        self.assertFalse((self.out / "two").exists())
        self.assert_balanced(result)

    def test_exclusion_identity_change_refuses_next_job(self):
        self.batch()
        exclusion = self.root / "exclusion"
        exclusion.mkdir()
        self.plan([self.job("one", end=1), self.job("two", start=1, exclude_reviewed="exclusion")])
        fake = self.fake([])

        def mutate(*args, **kwargs):
            result = fake(*args, **kwargs)
            (exclusion / "review-new.json").write_text("FAKE_PRIVATE_PROSE", encoding="utf-8")
            return result

        result = self.execute(effect=mutate)
        self.assertEqual(result["stop_reason"], "campaign_artifact_changed")
        self.assertEqual(result["valid_rows"], 1)
        self.assertFalse((self.out / "two").exists())

    def test_source_bound_exclusions_snapshot_and_dispatch_overlap(self):
        self.batch()
        exclusion = self.root / "exclusion"
        args = fixtures.AlignmentReviewRuntimeTests.args(self, out=exclusion, end=1)
        with self.env(), patch.object(ar, "request_model", side_effect=self.fake([])):
            ar.run(args)
        self.plan([self.job("excluded", end=1, exclude_reviewed="exclusion"), self.job("dispatch", end=1)])
        calls = []
        result = self.execute(calls)
        self.assertEqual(len(calls), 1)
        first, second = result["jobs"]
        self.assertEqual(first["excluded_reviewed_rows"], 1)
        self.assertEqual(first["status"], "no_dispatch")
        self.assertEqual(first["planned_rows"], 0)
        self.assertEqual(first["excluded_ids"], second["review_ids"])
        self.assert_balanced(result)

    def test_forged_exclusion_refused_before_output(self):
        self.batch()
        exclusion = self.root / "exclusion"
        args = fixtures.AlignmentReviewRuntimeTests.args(self, out=exclusion, end=1)
        with self.env(), patch.object(ar, "request_model", side_effect=self.fake([])):
            ar.run(args)
        receipt = exclusion / "review-000.json"
        data = json.loads(receipt.read_bytes())
        data["rows"][0]["chinese_text_sha256"] = "0" * 64
        receipt.write_text(json.dumps(data), encoding="utf-8")
        self.plan([self.job("one", exclude_reviewed="exclusion")])
        with self.assertRaises(campaign.AlignmentCampaignError):
            self.execute()
        self.assertFalse(self.out.exists())

    def test_plan_change_after_preflight_stops_dispatch(self):
        self.batch()
        self.plan([self.job("one", end=1), self.job("two", start=1)])
        fake = self.fake([])

        def mutate(*args, **kwargs):
            result = fake(*args, **kwargs)
            with self.plan_path.open("ab") as stream:
                stream.write(b" ")
            return result

        result = self.execute(effect=mutate)
        self.assertEqual(result["stop_reason"], "campaign_plan_changed")
        self.assertEqual(result["valid_rows"], 1)
        self.assertFalse((self.out / "two").exists())

    def test_plan_inside_formal_source_root_refused(self):
        self.batch()
        self.plan()
        internal = self.source / "plan.json"
        internal.write_bytes(self.plan_path.read_bytes())
        self.plan_path = internal
        with self.assertRaises(campaign.AlignmentCampaignError):
            self.execute()
        self.assertFalse(self.out.exists())

    def test_duplicate_json_keys_refused(self):
        self.batch()
        self.plan()
        raw = self.plan_path.read_text(encoding="utf-8")
        raw = raw.replace('"start": 0', '"start": 1, "start": 0')
        self.plan_path.write_text(raw, encoding="utf-8")
        with self.assertRaisesRegex(campaign.AlignmentCampaignError, "campaign_plan_invalid"):
            self.execute()
        self.assertFalse(self.out.exists())

    def test_source_change_between_jobs_refuses_next(self):
        self.batch()
        self.plan([self.job("one", end=1), self.job("two", start=1)])
        original = ar.run

        def mutate(*args, **kwargs):
            result = original(*args, **kwargs)
            Path(str(self.db) + "-wal").write_bytes(b"synthetic WAL")
            return result

        with patch.object(ar, "run", side_effect=mutate):
            result = self.execute()
        self.assertEqual(result["stop_reason"], "source_changed")
        self.assertEqual(result["valid_rows"], 1)
        self.assertFalse((self.out / "two").exists())

    def test_existing_output_and_input_tree_protection(self):
        self.batch()
        self.plan()
        self.out.mkdir()
        marker = self.out / "preserve.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaises(campaign.AlignmentCampaignError):
            self.execute()
        self.assertEqual(marker.read_text(), "keep")
        for output in (self.source / "new", self.inp / "new"):
            with self.assertRaises(campaign.AlignmentCampaignError):
                campaign.run_campaign(self.db, self.plan_path, output, "reviewer", 1, execute=True)
            self.assertFalse(output.exists())

    def test_unknown_dispatch_failure_preserves_partial_counts_without_leaks(self):
        self.batch()
        self.plan()
        result = self.execute(effect=RuntimeError("FAKE_PRIVATE_PROSE " + str(self.root)))
        self.assertEqual(result["stop_reason"], "campaign_failed")
        self.assertEqual(result["attempted_calls"], 1)
        self.assertEqual(result["unknown_rows"], 1)
        self.assertTrue((self.out / "one" / "progress.json").is_file())
        self.assert_balanced(result)

    def test_receipts_and_cli_have_no_paths_history_or_prose(self):
        self.batch(("z1",))
        self.plan()
        result = self.execute()
        receipts = (self.out / "progress.json").read_text() + (self.out / "summary.json").read_text()
        for forbidden in (str(self.root), "PRIVATE_ZH", "PRIVATE_JA", '"history":', "producer-model", "reviewer-model"):
            self.assertNotIn(forbidden, receipts)
        self.assertTrue(all('input' not in job and 'exclude_reviewed' not in job for job in result['jobs']))
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = campaign.main(["--db", str(self.db), "--plan", str(self.plan_path),
                                  "--out", str(self.out), "--model", "reviewer", "--max-calls", "1", "--execute"])
        self.assertEqual(code, 1)
        self.assertNotIn(str(self.root), stdout.getvalue())
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(campaign.main(["--FAKE_PRIVATE_PROSE", str(self.root)]), 1)
        self.assertNotIn("FAKE_PRIVATE_PROSE", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
