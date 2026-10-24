"""Synthetic boundary tests: no private corpus or model access."""
import unittest
from copy import deepcopy

from ltr.project.contracts import (Budget, ContractError, compile_context,
                                   invalidate_dependencies, validate_span, validate_units)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.paragraphs = {"p1": "甲😀乙", "p2": "Synthetic later fact."}
        self.entry = {"id": "c1", "revision": 1, "claim": "Synthetic hypothesis",
                      "epistemic": "inferred", "review_status": "verified",
                      "available_from": "p1", "evidence": [
                          {"paragraph_id": "p1", "start": 1, "end": 2, "book_hash": "hash"}]}

    def assertCode(self, code, fn, *args):
        with self.assertRaises(ContractError) as caught:
            fn(*args)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def compile(self, entry):
        return compile_context([entry], self.paragraphs, ["p1", "p2"], "p1", "hash")

    def test_codepoint_span(self):
        span = validate_span(self.entry["evidence"][0], self.paragraphs)
        self.assertEqual(self.paragraphs["p1"][span["start"]:span["end"]], "😀")

    def test_invalid_offsets_and_unknown_paragraph(self):
        for start, end in [(True, 2), (0, False), (-1, 1), (2, 2), (0, 4), (0.0, 1)]:
            self.assertCode("invalid_offsets", validate_span,
                            {"paragraph_id": "p1", "start": start, "end": end}, self.paragraphs)
        self.assertCode("unknown_paragraph", validate_span,
                        {"paragraph_id": "missing", "start": 0, "end": 1}, self.paragraphs)

    def test_verified_inference_remains_labeled_and_copied(self):
        result = self.compile(self.entry)
        self.assertEqual(result[0]["epistemic"], "inferred")
        result[0]["evidence"][0]["start"] = 0
        self.assertEqual(self.entry["evidence"][0]["start"], 1)

    def test_context_rejected_without_silent_filter(self):
        for field, value, code in [("review_status", "pending", "unverified_context"),
                                   ("epistemic", "fact", "invalid_epistemic"),
                                   ("evidence", [], "missing_evidence"),
                                   ("available_from", "p2", "future_context"),
                                   ("revision", True, "invalid_revision")]:
            entry = deepcopy(self.entry)
            entry[field] = value
            self.assertCode(code, self.compile, entry)

    def test_evidence_identity_and_horizon(self):
        for field, value, code in [("book_hash", None, "evidence_book_mismatch"),
                                   ("book_hash", "other", "evidence_book_mismatch"),
                                   ("paragraph_id", "unknown", "unknown_paragraph"),
                                   ("paragraph_id", "p2", "future_evidence")]:
            entry = deepcopy(self.entry)
            entry["evidence"][0][field] = value
            self.assertCode(code, self.compile, entry)

    def test_availability_cannot_precede_evidence(self):
        self.entry["evidence"][0]["paragraph_id"] = "p2"
        self.assertCode("premature_availability", compile_context, [self.entry],
                        self.paragraphs, ["p1", "p2"], "p2", "hash")

    def test_duplicate_context_and_bad_order(self):
        self.assertCode("duplicate_context_id", compile_context, [self.entry, self.entry],
                        self.paragraphs, ["p1", "p2"], "p1", "hash")
        self.assertCode("invalid_order", compile_context, [], self.paragraphs,
                        ["p1", "p1"], "p1", "hash")

    def test_exact_coverage_and_nonempty_translation(self):
        unit = {"id": "p1", "target": "Synthetic translation", "revision": 1, "status": "draft"}
        self.assertEqual(validate_units([unit], ["p1"]), [unit])
        self.assertCode("coverage_mismatch", validate_units, [unit], ["p1", "p2"])
        self.assertCode("duplicate_unit_id", validate_units, [unit, unit], ["p1"])
        self.assertCode("duplicate_requested_id", validate_units, [unit], ["p1", "p1"])
        unit["target"] = "  "
        self.assertCode("empty_target", validate_units, [unit], ["p1"])

    def test_budget_exact_cap_and_atomic_failure(self):
        budget = Budget(2, 3, 4)
        budget.consume(1, 2)
        self.assertCode("input_budget_exhausted", budget.consume, 3, 1)
        self.assertEqual((budget.calls, budget.input_tokens, budget.output_tokens), (1, 1, 2))
        self.assertCode("output_budget_exhausted", budget.consume, 0, 3)
        budget.consume(2, 2)
        self.assertCode("call_budget_exhausted", budget.consume, 0, 1)

    def test_budget_zero_calls_and_invalid_numbers(self):
        self.assertCode("call_budget_exhausted", Budget(0, 0, 0).consume, 0, 1)
        for value in [True, -1, 1.5, "1"]:
            self.assertCode("invalid_budget", Budget, value, 3, 3)
            self.assertCode("invalid_usage", Budget(1, 3, 3).consume, value, 1)
        self.assertCode("invalid_usage", Budget(1, 3, 3).consume, 0, 0)

    def test_dependency_invalidation_preserves_history_and_unrelated_units(self):
        units = [{"status": "accepted", "dependencies": [{"entry_id": "c1", "revision": 1}]},
                 {"status": "accepted", "dependencies": {"c1": 2}},
                 {"status": "accepted", "dependencies": {"c2": 1}}]
        result = invalidate_dependencies(units, "c1", 2)
        self.assertEqual([u["status"] for u in result], ["needs_review", "accepted", "accepted"])
        self.assertTrue(all(u["status"] == "accepted" for u in units))
        self.assertEqual(result[0]["dependencies"][0]["revision"], 1)


if __name__ == "__main__":
    unittest.main()
