"""Pure synthetic OCR contract tests. No source, image, DB or file fixtures."""
from copy import deepcopy
import json
import unittest

from ltr.ocr import vision_candidate as vision


class OCRVisionCandidateTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {"schema": vision.INPUT_SCHEMA, "source_sha256": "a" * 64, "page_id": "JA_SCAN_fixture:p0001",
                         "image_sha256": "b" * 64, "width": 1600, "height": 2400}
        self.response = {"segments": [
            {"order": 1, "kind": "main", "text": "SYNTHETIC_PRIVATE_MAIN 日本語\n二行目", "bbox": [600, 100, 900, 900]},
            {"order": 2, "kind": "ruby", "text": "SYNTHETIC_PRIVATE_RUBY", "bbox": [900, 100, 950, 200]},
        ], "uncertain_regions": [{"bbox": [0, 0, 100, 100], "reason": "SYNTHETIC_PRIVATE_REASON"}]}

    def candidate(self):
        return vision.create_candidate(self.manifest, self.response, model="synthetic-model", session_id="synthetic-session")

    def refused(self, call, code=None):
        with self.assertRaises(vision.OCRVisionCandidateError) as error:
            call()
        self.assertTrue(str(error.exception).startswith("ocr_vision_"))
        self.assertEqual(str(error.exception), error.exception.code)
        self.assertNotIn("SYNTHETIC_PRIVATE", str(error.exception))
        if code is not None:
            self.assertEqual(error.exception.code, code)

    def test_create_revalidate_exact_copy_and_fixed_unverified_flags(self):
        before = deepcopy(self.response)
        candidate = self.candidate()
        self.assertEqual(candidate["schema"], vision.CANDIDATE_SCHEMA)
        self.assertEqual(candidate["segments"], before["segments"])
        self.assertEqual(candidate["uncertain_regions"], before["uncertain_regions"])
        for field, expected in vision.FLAGS.items():
            self.assertIs(type(candidate[field]), int)
            self.assertEqual(candidate[field], expected)
        safe = vision.validate_candidate(candidate, self.manifest, model="synthetic-model", session_id="synthetic-session")
        safe["segments"][0]["bbox"][0] = 0
        self.assertEqual(candidate["segments"][0]["bbox"][0], 600)
        self.assertEqual(self.response, before)

    def test_bound_metadata_only_summary_contains_no_text_reason_or_free_identifiers(self):
        candidate = self.candidate()
        candidate["model"] = "SYNTHETIC_PRIVATE_MODEL"
        candidate["session_id"] = "SYNTHETIC_PRIVATE_SESSION"
        summary = vision.summarize(candidate, self.manifest)
        self.assertEqual(summary["segment_count"], 2)
        self.assertEqual(summary["uncertain_region_count"], 1)
        self.assertEqual(summary["kind_counts"], {"main": 1, "ruby": 1, "header": 0, "footer": 0, "other": 0})
        self.assertTrue(summary["structure_valid"])
        self.assertEqual(summary["human_verified"], 0)
        encoded = json.dumps(summary)
        self.assertNotIn("SYNTHETIC_PRIVATE", encoded)
        self.assertNotIn('"bbox"', encoded)
        self.assertNotIn('"reason"', encoded)
        self.assertLess(len(encoded), 2000)

    def test_wrong_source_page_image_or_dimensions_refused(self):
        original = self.candidate()
        for field, value in (("source_sha256", "c" * 64), ("image_sha256", "d" * 64),
                             ("page_id", "JA_SCAN_other:p0001"), ("width", 1601), ("height", 2401), ("page_id", True)):
            candidate = deepcopy(original)
            candidate[field] = value
            self.refused(lambda: vision.validate_candidate(candidate, self.manifest), "ocr_vision_binding_mismatch")

    def test_wrong_model_or_session_expected_binding_refused(self):
        candidate = self.candidate()
        self.refused(lambda: vision.validate_candidate(candidate, self.manifest, model="other-model"), "ocr_vision_binding_mismatch")
        self.refused(lambda: vision.validate_candidate(candidate, self.manifest, session_id="other-session"), "ocr_vision_binding_mismatch")

    def test_manifest_shape_schema_hashes_and_bool_integer_guards(self):
        for field, value in (("schema", "wrong"), ("source_sha256", "z" * 64), ("image_sha256", "b" * 63),
                             ("source_sha256", "A" * 64), ("page_id", True), ("page_id", 0),
                             ("width", 0), ("height", -1), ("width", 1.5), ("width", vision.MAX_INTEGER + 1),
                             ("page_id", 1), ("page_id", ""), ("page_id", "x" * 201),
                             ("page_id", "page/name"), ("page_id", "page name"), ("page_id", "页1"),
                             ("page_id", "page\n1")):
            bad = dict(self.manifest, **{field: value})
            self.refused(lambda: vision.validate_manifest(bad), "ocr_vision_manifest_invalid")
        self.refused(lambda: vision.validate_manifest(dict(self.manifest, path="SYNTHETIC_PRIVATE_PATH")))
        missing = dict(self.manifest)
        missing.pop("height")
        self.refused(lambda: vision.validate_manifest(missing))

    def test_bad_flags_never_promote_candidate(self):
        original = self.candidate()
        for field in vision.FLAGS:
            for value in (True, False, -1, 2, "0", None):
                bad = dict(original, **{field: value})
                self.refused(lambda: vision.validate_candidate(bad, self.manifest), "ocr_vision_flags_invalid")
            wrong = dict(original, **{field: 1 - vision.FLAGS[field]})
            self.refused(lambda: vision.validate_candidate(wrong, self.manifest), "ocr_vision_flags_invalid")

    def test_bbox_requires_four_integer_increasing_normalized_coordinates(self):
        for box in (None, {}, (0, 0, 1, 1), [], [0, 0, 1], [0, 0, 1, 1, 2],
                    [False, 0, 10, 10], [0, 0, 1.5, 10], [-1, 0, 10, 10], [0, 0, 1001, 1000],
                    [5, 0, 5, 10], [0, 8, 10, 3], [10, 0, 1, 10]):
            for kind in ("segments", "uncertain_regions"):
                bad = deepcopy(self.response)
                bad[kind][0]["bbox"] = box
                self.refused(lambda: vision.validate_response(bad), "ocr_vision_bbox_invalid")
        good = deepcopy(self.response)
        good["segments"][0]["bbox"] = [0, 0, 1000, 1000]
        self.assertEqual(vision.validate_response(good)["segments"][0]["bbox"], [0, 0, 1000, 1000])

    def test_segment_order_and_kind_are_exact(self):
        for field, value in (("order", 0), ("order", True), ("order", 2), ("order", 1.0),
                             ("kind", "MAIN"), ("kind", []), ("kind", "dialogue")):
            bad = deepcopy(self.response)
            bad["segments"][0][field] = value
            self.refused(lambda: vision.validate_response(bad), "ocr_vision_segment_invalid")
        for value in (1, 3):
            bad = deepcopy(self.response)
            bad["segments"][1]["order"] = value
            self.refused(lambda: vision.validate_response(bad), "ocr_vision_segment_invalid")
        for kind in vision.KINDS:
            good = deepcopy(self.response)
            good["segments"][0]["kind"] = kind
            self.assertEqual(vision.validate_response(good)["segments"][0]["kind"], kind)

    def test_extra_keys_and_non_json_shapes_refused_at_all_levels(self):
        for payload in (None, [], {}, {"segments": (), "uncertain_regions": []},
                        dict(self.response, prompt="SYNTHETIC_PRIVATE_PROMPT")):
            self.refused(lambda: vision.validate_response(payload))
        for field in ("segments", "uncertain_regions"):
            for change in (None, {"unexpected": "SYNTHETIC_PRIVATE_VALUE"}):
                bad = deepcopy(self.response)
                if change is None:
                    bad[field] = [None]
                else:
                    bad[field][0].update(change)
                self.refused(lambda: vision.validate_response(bad))
        candidate = self.candidate()
        self.refused(lambda: vision.validate_candidate(dict(candidate, human_confirmed=1), self.manifest))
        self.refused(lambda: vision.validate_candidate(dict(candidate, schema="wrong"), self.manifest))

    def test_unsafe_unicode_empty_or_oversize_text_is_never_echoed(self):
        for text in ("", " \n\t", None, 1, "\ud800", "\udfff", "x\x00y", "x\x1by", "x\u202ey", "\ufdd0", "\U0010ffff",
                     "\u200b", "SYNTHETIC_PRIVATE" * vision.MAX_SEGMENT_TEXT_CHARS):
            bad = deepcopy(self.response)
            bad["segments"][0]["text"] = text
            self.refused(lambda: vision.validate_response(bad), "ocr_vision_text_invalid")
        for text in ("かな漢字 😀", "e\u0301", "﨑", "👩\u200d🔬", "first\r\nsecond\tcolumn"):
            good = deepcopy(self.response)
            good["segments"][0]["text"] = text
            self.assertEqual(vision.validate_response(good)["segments"][0]["text"], text)

    def test_reason_and_model_session_text_are_bounded_and_control_free(self):
        for reason in ("", "\ud800", "SYNTHETIC_PRIVATE" * vision.MAX_REASON_CHARS):
            bad = deepcopy(self.response)
            bad["uncertain_regions"][0]["reason"] = reason
            self.refused(lambda: vision.validate_response(bad), "ocr_vision_text_invalid")
        for field in ("model", "session_id"):
            for value in ("", " ", None, 1, "x\ny", "\ud800", "x" * (vision.MAX_IDENTIFIER_CHARS + 1)):
                options = {"model": "synthetic-model", "session_id": "synthetic-session", field: value}
                self.refused(lambda: vision.create_candidate(self.manifest, self.response, **options))

    def test_count_and_total_text_limits_are_enforced(self):
        segment = {"order": 1, "kind": "main", "text": "Synthetic", "bbox": [0, 0, 10, 10]}
        self.refused(lambda: vision.validate_response({"segments": [segment] * (vision.MAX_SEGMENTS + 1), "uncertain_regions": []}),
                     "ocr_vision_limits_exceeded")
        self.refused(lambda: vision.validate_response({"segments": [], "uncertain_regions": self.response["uncertain_regions"] * (vision.MAX_UNCERTAIN_REGIONS + 1)}),
                     "ocr_vision_limits_exceeded")
        rows = [dict(segment, order=i + 1, text="x" * vision.MAX_SEGMENT_TEXT_CHARS) for i in range(25)]
        self.refused(lambda: vision.validate_response({"segments": rows, "uncertain_regions": []}), "ocr_vision_limits_exceeded")

    def test_empty_response_is_unverified_structure_and_copies_input(self):
        candidate = vision.create_candidate(self.manifest, {"segments": [], "uncertain_regions": []}, model="synthetic", session_id="s")
        summary = vision.summarize(candidate, self.manifest)
        self.assertEqual(summary["segment_count"], 0)
        self.assertEqual(summary["human_verified"], 0)
        self.assertEqual(summary["ocr_verified"], 0)
        self.assertEqual(summary["reference_eligible"], 0)

    def test_unknown_error_constructor_does_not_echo_input(self):
        self.assertEqual(str(vision.OCRVisionCandidateError("SYNTHETIC_PRIVATE_ERROR")), "ocr_vision_invalid")


if __name__ == "__main__":
    unittest.main()
