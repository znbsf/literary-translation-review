"""Synthetic-only fresh target planning and frozen evidence validation."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests.ltr.alignment import test_review as fixtures
from ltr.alignment import plan as planner
from ltr.alignment import review as ar
from ltr.alignment import campaign


class AlignmentPlanTests(unittest.TestCase):
    setUp = fixtures.AlignmentReviewRuntimeTests.setUp
    env = fixtures.AlignmentReviewRuntimeTests.env
    fake = fixtures.AlignmentReviewRuntimeTests.fake

    def prepare_source(self, count=8, order=None):
        con = sqlite3.connect(self.db)
        for i in range(4, count + 1):
            con.execute("INSERT INTO dialogues VALUES (?,?,?,?)", (f"z{i}", "zh1", i - 1, f"SYNTHETIC_ZH_{i}"))
        con.commit()
        con.close()
        self.baseline = ar.fingerprint(self.db)
        rows = []
        for i in (order if order is not None else range(1, count + 1)):
            text = fixtures.ZH_MAP.get(f"z{i}", f"SYNTHETIC_ZH_{i}")
            rows.append(fixtures.make_row("z1", ["j1"], "candidate", chinese_id=f"z{i}", chinese_text_sha256=fixtures.sha(text)))
        fixtures.write_batch(self.inp, "align-000.json", self.baseline, rows)
        con = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)
        try:
            con.execute("PRAGMA query_only=ON")
            self.loaded = ar.load_source(self.inp, con, self.baseline, source_order=True)
        finally:
            con.close()
        self.ids = [r["review_id"] for r in self.loaded["candidates"]]
        self.plan_file = self.root / "fresh-plan.json"

    def history(self, statuses, name="frozen", ids=None):
        directory = self.root / name
        directory.mkdir()
        ids = self.ids[:len(statuses)] if ids is None else ids
        metadata = dict(schema=ar._REVIEW_SCHEMA, database_sha256=self.baseline,
                        source_artifact_sha256="a" * 64, zh_edition_id="zh1", ja_edition_id="ja1",
                        book_key="bk", producer_requested_model="producer-model", producer_served_model="producer-served",
                        reviewer_requested_model="reviewer-model", session_id="synthetic-frozen-session",
                        source_order=True, corpus_updated=False)
        calls = []
        valid = failed = 0
        for n, (status, identity) in enumerate(zip(statuses, ids)):
            call = dict(number=n, model="reviewer-model", review_ids=[identity],
                        input_sha256="b" * 64, history_sha256="c" * 64,
                        session_id=metadata["session_id"], status=status)
            if status == "valid_model_review":
                valid += 1
                call.update(rows=1, counts={"keep": 1, "reject": 0, "uncertain": 0})
            elif status == "failed":
                failed += 1
            calls.append(call)
        total = max(len(self.ids), len(ids))
        summary = metadata | dict(candidate_rows_total=total, skipped_unmatched_rows=0, skipped_prefix_rows=0,
            skipped_suffix_rows=total-len(ids), selected_range_rows=len(ids), eligible_rows=len(ids), excluded_reviewed_rows=0,
            attempted_calls=len(ids), valid_calls=valid, failed_calls=failed, attempted_rows=len(ids), valid_rows=valid,
            failed_rows=failed, unattempted_rows=0, remaining=total-valid, keep=valid, reject=0, uncertain=0,
            source_unchanged=True, stop_reason="range_or_budget_complete", finished_at="2026-10-20T01:02:03+00:00")
        (directory / "progress.json").write_text(json.dumps(metadata | {"calls": calls}), encoding="utf-8")
        (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        return directory

    def reviews(self, start=0, end=1):
        directory = self.root / "reviews"
        args = fixtures.AlignmentReviewRuntimeTests.args(self, out=directory, start=start, end=end, source_order=True)
        with self.env(), patch.object(ar, "request_model", side_effect=self.fake([])):
            ar.run(args)
        return directory

    def build(self, reviews=(), histories=(), **kwargs):
        result = planner.build_plan(self.db, self.inp, list(reviews), list(histories), self.plan_file, **kwargs)
        plan, _ = campaign._plan(self.plan_file)
        actual = [self.ids[i] for job in plan["jobs"] for i in range(job["start"], job["end"])]
        self.assertEqual(result["selected_ids_sha256"], ar.digest(actual))
        self.assertEqual(len(actual), result["selected_rows"])
        self.assertFalse(result["corpus_updated"])
        self.assertTrue(result["source_unchanged"])
        self.assertEqual(list(self.root.glob(".alignment-plan-*")), [])
        return result, plan, actual

    def mutate(self, directory, name, function):
        path = directory / name
        data = json.loads(path.read_bytes())
        function(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_valid_failed_unknown_and_historical_ids_are_skipped_distinctly(self):
        self.prepare_source()
        frozen = self.history(["valid_model_review", "failed", "dispatched_outcome_unknown", "failed"],
                              ids=[*self.ids[:3], "historical-absent-id"])
        reviews = self.reviews()
        result, _, actual = self.build([reviews], [frozen])
        self.assertEqual(actual, self.ids[3:])
        self.assertEqual(result["reviewed_current"], 1)
        self.assertEqual(result["attempted_current"], 3)
        self.assertEqual(result["valid_attempted_current"], 1)
        self.assertEqual(result["failed_attempted_current"], 1)
        self.assertEqual(result["unknown_attempted_current"], 1)
        self.assertEqual(result["attempted_without_supplied_opinion_current"], 2)
        self.assertEqual(result["historical_attempted_ids_absent_from_current"], 1)
        self.assertEqual(result["evidence_scope"], "supplied_frozen_evidence_only")

    def test_failed_then_valid_history_with_opinion_is_one_target_without_outstanding_opinion(self):
        self.prepare_source()
        failed = self.history(["failed"], name="failed-history", ids=[self.ids[0]])
        valid = self.history(["valid_model_review"], name="valid-history", ids=[self.ids[0]])
        reviews = self.reviews(start=0, end=1)
        result, _, actual = self.build(reviews=[reviews], histories=[failed, valid])
        self.assertEqual(result["attempted_current"], 1)
        self.assertEqual(result["failed_attempted_current"], 1)
        self.assertEqual(result["valid_attempted_current"], 1)
        self.assertEqual(result["reviewed_current"], 1)
        self.assertEqual(result["failed_or_unknown_ever_attempted_current"], 1)
        self.assertEqual(result["failed_or_unknown_without_supplied_opinion_current"], 0)
        self.assertEqual(result["attempted_without_supplied_opinion_current"], 0)
        self.assertEqual(result["eligible_in_supplied_evidence_current"], len(self.ids) - 1)
        self.assertEqual(actual, self.ids[1:])
        self.assertNotIn("failed_or_unknown_attempted_current_for_triage", result)

    def test_canonical_gaps_partial_batches_charge_separate_calls(self):
        self.prepare_source(order=[8, 4, 3, 1, 2, 7, 6, 5])
        frozen = self.history(["failed", "failed"], ids=[self.ids[1], self.ids[4]])
        result, plan, actual = self.build(histories=[frozen], max_calls=2, batch_size=3)
        self.assertEqual([(j["start"], j["end"]) for j in plan["jobs"]], [(0, 1), (2, 4)])
        self.assertEqual(actual, [self.ids[0], *self.ids[2:4]])
        self.assertEqual(result["planned_calls"], 2)
        self.assertEqual(result["deferred_due_budget_rows"], 3)
        self.assertEqual(result["deferred_due_job_cap_rows"], 0)

    def test_plan_is_overlap_free_and_campaign_preflight_accepts(self):
        self.prepare_source()
        frozen = self.history(["failed"], ids=[self.ids[1]])
        result, plan, _ = self.build(histories=[frozen], max_calls=3, batch_size=2)
        baseline, jobs = campaign._preflight(self.db, self.plan_file, plan, self.root / "campaign-out",
                                              "stub-reviewer", 3)
        self.assertEqual(baseline, self.baseline)
        self.assertEqual(sum(len(j["review_ids"]) for j in jobs), result["selected_rows"])
        self.assertTrue(all(j["exclude_reviewed"] is None for j in plan["jobs"]))
        for job in plan["jobs"]:
            self.assertEqual(job["expected_artifact_sha256"], ar.digest(self.loaded["artifacts"]))
            self.assertEqual(job["expected_selected_ids_sha256"], ar.digest(self.ids[job["start"]:job["end"]]))

    def test_published_plan_refuses_proposal_set_drift_before_any_campaign_output(self):
        self.prepare_source()
        frozen = self.history(["failed"], ids=[self.ids[1]])
        self.build(histories=[frozen], max_calls=3, batch_size=2)
        # Removing the first candidate moves canonical ranges despite the same
        # source DB SHA and individually valid remaining proposal bindings.
        path = self.inp / "align-000.json"
        data = json.loads(path.read_bytes())
        data["rows"] = data["rows"][1:]
        path.write_text(json.dumps(data), encoding="utf-8")
        with patch.object(ar, "run") as runner:
            with self.assertRaisesRegex(campaign.AlignmentCampaignError, "campaign_expected_artifact_mismatch"):
                campaign.run_campaign(self.db, self.plan_file, self.out, "stub-reviewer", 3, execute=True)
            runner.assert_not_called()
        self.assertFalse(self.out.exists())
        self.assertEqual(ar.fingerprint(self.db), self.baseline)

    def test_other_pair_reviews_filtered_but_mixed_artifact_rejected(self):
        self.prepare_source()
        reviews = self.reviews()
        data = json.loads((reviews / "review-000.json").read_bytes())
        other = json.loads(json.dumps(data))
        other["rows"][0].update(zh_edition_id="other-zh", ja_edition_id="other-ja", review_id="unrelated-id")
        (reviews / "review-other.json").write_text(json.dumps(other), encoding="utf-8")
        result, _, _ = self.build(reviews=[reviews])
        self.assertEqual(result["reviewed_current"], 1)
        self.plan_file.unlink()
        data["rows"].append(other["rows"][0])
        (reviews / "review-000.json").write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(planner.AlignmentPlanError):
            self.build(reviews=[reviews])
        self.assertFalse(self.plan_file.exists())

    def test_forged_and_stale_reviews_refused(self):
        self.prepare_source()
        reviews = self.reviews()
        path = reviews / "review-000.json"
        original = path.read_bytes()
        for mutate in (lambda d: d.update(database_sha256="0" * 64),
                       lambda d: d["rows"][0].update(chinese_text_sha256="0" * 64),
                       lambda d: d["rows"][0].update(accepted=1)):
            path.write_bytes(original)
            self.mutate(reviews, path.name, mutate)
            with self.assertRaises(planner.AlignmentPlanError):
                self.build(reviews=[reviews])
            self.assertFalse(self.plan_file.exists())

    def test_history_duplicate_status_ids_counts_and_staleness_refused(self):
        self.prepare_source()
        frozen = self.history(["valid_model_review", "failed"])
        before = {name: (frozen / name).read_bytes() for name in ("progress.json", "summary.json")}
        cases = [
            ("progress.json", lambda d: d["calls"][0].update(status="running")),
            ("progress.json", lambda d: d["calls"][0].update(review_ids=[self.ids[0], self.ids[0]])),
            ("progress.json", lambda d: d["calls"][1].update(review_ids=[self.ids[0]])),
            ("progress.json", lambda d: d["calls"][0].update(review_ids=[True])),
            ("progress.json", lambda d: d.update(session_id="different")),
            ("progress.json", lambda d: d.update(source_artifact_sha256="f" * 64)),
            ("summary.json", lambda d: d.update(database_sha256="0" * 64)),
            ("summary.json", lambda d: d.update(valid_rows=True)),
            ("summary.json", lambda d: d.update(attempted_rows=3)),
            ("summary.json", lambda d: d.update(unattempted_rows=1)),
            ("summary.json", lambda d: d.update(source_unchanged=False)),
            ("summary.json", lambda d: d.update(stop_reason="running")),
            ("summary.json", lambda d: d.update(zh_edition_id="other")),
        ]
        for name, mutation in cases:
            for filename, raw in before.items():
                (frozen / filename).write_bytes(raw)
            self.mutate(frozen, name, mutation)
            with self.assertRaises(planner.AlignmentPlanError):
                self.build(histories=[frozen])
            self.assertFalse(self.plan_file.exists())

    def test_incomplete_history_and_campaign_parent_rejected(self):
        self.prepare_source()
        frozen = self.history(["failed"])
        (frozen / "summary.json").unlink()
        with self.assertRaises(planner.AlignmentPlanError):
            self.build(histories=[frozen])
        (frozen / "summary.json").write_text(json.dumps({"schema": campaign.SCHEMA}), encoding="utf-8")
        with self.assertRaises(planner.AlignmentPlanError):
            self.build(histories=[frozen])
        self.assertFalse(self.plan_file.exists())

    def test_legacy_terminal_history_has_explicit_unspecified_stop_count(self):
        self.prepare_source()
        frozen = self.history(["valid_model_review", "failed"])
        self.mutate(frozen, "progress.json", lambda d: d.pop("source_order"))
        removed = ("stop_reason", "source_order", "selected_range_rows", "eligible_rows",
                   "excluded_reviewed_rows", "skipped_suffix_rows")

        def legacy(data):
            for field in removed:
                data.pop(field)
            data["unattempted_rows"] = len(self.ids) - 2

        self.mutate(frozen, "summary.json", legacy)
        result, _, actual = self.build(histories=[frozen])
        self.assertEqual(result["legacy_unspecified_stop_histories"], 1)
        self.assertEqual(result["legacy_implicit_range_histories"], 1)
        self.assertEqual(actual, self.ids[2:])
        self.plan_file.unlink()
        self.mutate(frozen, "summary.json", lambda d: d.pop("finished_at"))
        with self.assertRaises(planner.AlignmentPlanError):
            self.build(histories=[frozen])
        self.assertFalse(self.plan_file.exists())

    def test_known_stop_with_implicit_range_and_unknown_is_counted_separately(self):
        self.prepare_source()
        frozen = self.history(["valid_model_review", "failed", "dispatched_outcome_unknown"])

        def implicit(data):
            for key in ("skipped_suffix_rows", "selected_range_rows", "eligible_rows", "excluded_reviewed_rows"):
                data.pop(key)
            data["skipped_prefix_rows"] = 2
            data["unattempted_rows"] = len(self.ids) - 2 - 3

        self.mutate(frozen, "summary.json", implicit)
        result, _, actual = self.build(histories=[frozen])
        self.assertEqual(result["legacy_unspecified_stop_histories"], 0)
        self.assertEqual(result["legacy_implicit_range_histories"], 1)
        self.assertEqual(result["attempted_current"], 3)
        self.assertEqual(result["unknown_attempted_current"], 1)
        self.assertEqual(result["reviewed_current"], 0)
        self.assertEqual(actual, self.ids[3:])

    def test_partial_range_fields_refused_with_and_without_stop_reason(self):
        self.prepare_source()
        frozen = self.history(["failed"])
        path = frozen / "summary.json"
        original = json.loads(path.read_bytes())
        fields = ("skipped_suffix_rows", "selected_range_rows", "eligible_rows", "excluded_reviewed_rows")
        for has_stop in (False, True):
            for mask in range(1, 15):
                data = dict(original)
                if not has_stop:
                    data.pop("stop_reason")
                for bit, field in enumerate(fields):
                    if mask & (1 << bit):
                        data.pop(field)
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(planner.AlignmentPlanError):
                    self.build(histories=[frozen])
                self.assertFalse(self.plan_file.exists())

    def test_implicit_range_strict_counts_for_large_prior_subset_histories(self):
        self.prepare_source()
        for number, prefix, valid, failed in ((1, 0, 72, 48), (2, 300, 192, 108)):
            statuses = ["valid_model_review"] * valid + ["failed"] * failed
            frozen = self.history(statuses, name=f"old-{number}",
                                  ids=[f"synthetic-historic-{i}" for i in range(valid + failed)])
            progress_path = frozen / "progress.json"
            progress = json.loads(progress_path.read_bytes())
            compact = []
            for status, size, start in (("valid_model_review", valid, 0), ("failed", failed, valid)):
                for offset in range(0, size, 12):
                    group = progress["calls"][start + offset:start + min(offset + 12, size)]
                    call = dict(group[0], number=len(compact), review_ids=[c["review_ids"][0] for c in group])
                    if status == "valid_model_review":
                        call.update(rows=len(group), counts={"keep": len(group), "reject": 0, "uncertain": 0})
                    compact.append(call)
            progress["calls"] = compact
            progress_path.write_text(json.dumps(progress), encoding="utf-8")
            summary_path = frozen / "summary.json"
            summary = json.loads(summary_path.read_bytes())
            for field in ("skipped_suffix_rows", "selected_range_rows", "eligible_rows", "excluded_reviewed_rows"):
                summary.pop(field)
            summary.update(candidate_rows_total=1157, skipped_prefix_rows=prefix,
                attempted_calls=len(compact), valid_calls=valid // 12, failed_calls=failed // 12,
                unattempted_rows=1157-prefix-valid-failed, remaining=1157-valid)
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            evidence, legacy_stop, legacy_range = planner._history(frozen, self.baseline, ("zh1", "ja1"))
            self.assertEqual(len(evidence["valid_model_review"]), valid)
            self.assertEqual(len(evidence["failed"]), failed)
            self.assertFalse(legacy_stop)
            self.assertTrue(legacy_range)
            summary["unattempted_rows"] += 1
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            with self.assertRaises(planner.AlignmentPlanError):
                planner._history(frozen, self.baseline, ("zh1", "ja1"))

    def test_source_input_history_and_review_changes_refuse_publication(self):
        self.prepare_source()
        frozen = self.history(["failed"])
        reviews = self.reviews(start=1, end=2)
        original_ranges = planner._ranges
        targets = [self.db, self.inp / "align-000.json", frozen / "progress.json", reviews / "review-000.json"]
        for target in targets:
            before = target.read_bytes()

            def change(*args):
                result = original_ranges(*args)
                with target.open("ab") as stream:
                    stream.write(b" ")
                return result

            try:
                with patch.object(planner, "_ranges", side_effect=change):
                    with self.assertRaises(planner.AlignmentPlanError):
                        self.build(reviews=[reviews], histories=[frozen])
                self.assertFalse(self.plan_file.exists())
            finally:
                target.write_bytes(before)

    def test_wal_guard_and_read_only_source(self):
        self.prepare_source()
        before = self.db.read_bytes(), self.db.stat().st_mtime_ns
        result, _, _ = self.build()
        self.assertEqual(before, (self.db.read_bytes(), self.db.stat().st_mtime_ns))
        self.plan_file.unlink()
        wal = Path(str(self.db) + "-wal")
        wal.write_bytes(b"synthetic")
        with self.assertRaises(planner.AlignmentPlanError):
            self.build()
        self.assertFalse(self.plan_file.exists())

    def test_existing_output_race_and_external_boundaries(self):
        self.prepare_source()
        self.plan_file.write_bytes(b"preserve")
        with self.assertRaises(planner.AlignmentPlanError):
            self.build()
        self.assertEqual(self.plan_file.read_bytes(), b"preserve")
        self.plan_file.unlink()
        original = planner.os.link

        def race(source, output):
            self.plan_file.write_bytes(b"racing result")
            original(source, output)

        with patch.object(planner.os, "link", side_effect=race):
            with self.assertRaises(planner.AlignmentPlanError):
                self.build()
        self.assertEqual(self.plan_file.read_bytes(), b"racing result")
        self.assertEqual(list(self.root.glob(".alignment-plan-*")), [])
        for output in (self.db, self.source / "plan.json", self.inp / "plan.json"):
            with self.assertRaises(planner.AlignmentPlanError):
                planner.build_plan(self.db, self.inp, [], [], output)

    def test_bool_bounds_and_missing_output_parent_rejected(self):
        self.prepare_source()
        for budget, batch in ((True, 1), (1, True), (0, 1), (1001, 1), (1, 0), (1, 13)):
            with self.assertRaises(planner.AlignmentPlanError):
                self.build(max_calls=budget, batch_size=batch)
            self.assertFalse(self.plan_file.exists())
        with self.assertRaises(planner.AlignmentPlanError):
            planner.build_plan(self.db, self.inp, [], [], self.root / "missing" / "plan.json")

    def test_job_cap_and_per_job_call_cap(self):
        self.prepare_source(count=205)
        frozen = self.history(["failed"] * 100, ids=self.ids[1:200:2])
        more = self.history(["failed"] * 2, name="frozen-more", ids=self.ids[201:204:2])
        result, plan, actual = self.build(histories=[frozen, more], max_calls=1000, batch_size=1)
        self.assertEqual(len(plan["jobs"]), 100)
        self.assertEqual(len(actual), 100)
        self.assertEqual(result["deferred_due_job_cap_rows"], 3)
        self.plan_file.unlink()
        result, plan, _ = self.build(max_calls=150, batch_size=1)
        self.assertEqual([j["max_calls"] for j in plan["jobs"]], [100, 50])

    def test_actual_standalone_completed_history_is_accepted(self):
        self.prepare_source()
        frozen = self.reviews(start=1, end=3)
        result, _, actual = self.build(reviews=[frozen], histories=[frozen])
        self.assertEqual(result["legacy_unspecified_stop_histories"], 0)
        self.assertEqual(result["reviewed_current"], 2)
        self.assertEqual(result["attempted_current"], 2)
        self.assertEqual(actual, [self.ids[0], *self.ids[3:]])

    def test_history_review_output_trees_and_existing_alias_guard(self):
        self.prepare_source()
        frozen = self.history(["failed"])
        reviews = self.reviews(start=1, end=2)
        for directory in (frozen, reviews):
            with self.assertRaisesRegex(planner.AlignmentPlanError, "alignment_plan_output_boundary"):
                planner.build_plan(self.db, self.inp, [reviews], [frozen], directory / "plan.json")
        self.plan_file.write_bytes(b"existing")
        with patch.object(planner.Path, "resolve", side_effect=AssertionError("must not resolve existing output")):
            with self.assertRaisesRegex(planner.AlignmentPlanError, "alignment_plan_output_exists"):
                planner.build_plan(self.db, self.inp, [], [], self.plan_file)

    def test_builder_never_dispatches_or_reads_runtime_authentication(self):
        self.prepare_source()
        with patch.object(ar, "request_model", side_effect=AssertionError("no model calls")) as request:
            self.build(max_calls=1)
        request.assert_not_called()

    def test_all_known_ids_no_invalid_empty_plan_published(self):
        self.prepare_source(count=3)
        frozen = self.history(["failed"] * 3)
        with self.assertRaisesRegex(planner.AlignmentPlanError, "alignment_plan_no_dispatchable_rows"):
            self.build(histories=[frozen])
        self.assertFalse(self.plan_file.exists())

    def test_no_prose_paths_or_history_bodies_in_return_or_stdout(self):
        self.prepare_source()
        frozen = self.history(["failed"])
        result, _, _ = self.build(histories=[frozen])
        dumped = json.dumps(result)
        for forbidden in (str(self.root), "PRIVATE_ZH", "PRIVATE_JA", "synthetic-frozen-session", "review_ids"):
            self.assertNotIn(forbidden, dumped)
        self.plan_file.unlink()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = planner.main(["--db", str(self.db), "--input", str(self.inp), "--out", str(self.plan_file),
                                 "--history", str(frozen), "--max-calls", "2"])
        self.assertEqual(code, 0)
        self.assertNotIn(str(self.root), stdout.getvalue())
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(planner.main(["--FAKE_PRIVATE_PROSE", str(self.root)]), 1)
        self.assertNotIn("FAKE_PRIVATE_PROSE", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
