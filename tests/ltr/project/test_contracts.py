"""Synthetic boundary tests: no private corpus or model access."""
import unittest
from copy import deepcopy

from ltr.project.contracts import ContractError, compile_context, validate_span


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






if __name__ == "__main__":
    unittest.main()
