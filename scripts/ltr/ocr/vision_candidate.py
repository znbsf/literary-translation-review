"""Pure contracts for private, unverified OCR vision candidates.

No files, images, databases, network, model transport or command-line entrypoint
are used here. Structural validity does not establish OCR accuracy, reading
order, image authenticity or source provenance; those require external review.
Text is preserved exactly, without Unicode normalization or OCR corrections.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

INPUT_SCHEMA = "corpus-ocr-vision-input-v1"
CANDIDATE_SCHEMA = "corpus-ocr-vision-candidate-v1"
SUMMARY_SCHEMA = "corpus-ocr-vision-summary-v1"
KINDS = frozenset({"main", "ruby", "header", "footer", "other"})
FLAGS = {"requires_review": 1, "human_verified": 0, "reference_eligible": 0, "ocr_verified": 0, "accepted": 0}
MAX_SEGMENTS = 1000
MAX_SEGMENT_TEXT_CHARS = 4096
MAX_UNCERTAIN_REGIONS = 200
MAX_REASON_CHARS = 500
MAX_TOTAL_TEXT_CHARS = 100000
MAX_IDENTIFIER_CHARS = 200
MAX_INTEGER = (1 << 63) - 1

_BINDINGS = {"source_sha256", "page_id", "image_sha256", "width", "height"}
_INPUT_KEYS = {"schema", *_BINDINGS}
_RESPONSE_KEYS = {"segments", "uncertain_regions"}
_CANDIDATE_KEYS = {"schema", *_BINDINGS, "model", "session_id", *_RESPONSE_KEYS, *FLAGS}
_HASH = re.compile(r"[0-9a-f]{64}")
_PAGE_ID = re.compile(r"[A-Za-z0-9._:-]{1,200}")
_BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_ERROR_CODES = frozenset({
    "ocr_vision_invalid", "ocr_vision_manifest_invalid", "ocr_vision_response_invalid",
    "ocr_vision_candidate_invalid", "ocr_vision_binding_mismatch", "ocr_vision_flags_invalid",
    "ocr_vision_segment_invalid", "ocr_vision_bbox_invalid", "ocr_vision_text_invalid",
    "ocr_vision_uncertain_region_invalid", "ocr_vision_model_invalid", "ocr_vision_session_invalid",
    "ocr_vision_limits_exceeded",
})


class OCRVisionCandidateError(ValueError):
    """A static code only: input text, paths and parser errors never escape."""

    def __init__(self, code="ocr_vision_invalid"):
        self.code = code if type(code) is str and code in _ERROR_CODES else "ocr_vision_invalid"
        super().__init__(self.code)


def _require(condition, code):
    if not condition:
        raise OCRVisionCandidateError(code)


def _keys(value, expected, code):
    _require(type(value) is dict and set(value) == expected, code)


def _text(value, limit, code, *, multiline=True):
    _require(type(value) is str and 0 < len(value) <= limit, code)
    _require(value.strip() and any(not char.isspace() and unicodedata.category(char) != "Cf" for char in value), code)
    for char in value:
        point = ord(char)
        _require(not (0xD800 <= point <= 0xDFFF or 0xFDD0 <= point <= 0xFDEF
                      or point & 0xFFFF in (0xFFFE, 0xFFFF) or char in _BIDI_CONTROLS), code)
        _require(unicodedata.category(char) != "Cc" or (multiline and char in "\n\r\t"), code)
    return value


def _positive_integer(value):
    return type(value) is int and 1 <= value <= MAX_INTEGER


def _bbox(value):
    code = "ocr_vision_bbox_invalid"
    _require(type(value) is list and len(value) == 4
             and all(type(coordinate) is int and 0 <= coordinate <= 1000 for coordinate in value), code)
    x0, y0, x1, y1 = value
    _require(x0 < x1 and y0 < y1, code)
    return list(value)


def validate_manifest(manifest):
    """Validate exact keys, lowercase hashes, page ID and positive int64 sizes.

    SHA fields are declarative bindings; this module cannot inspect the claimed
    source or image to establish whether their hashes or dimensions are true.
    """
    code = "ocr_vision_manifest_invalid"
    _keys(manifest, _INPUT_KEYS, code)
    _require(manifest["schema"] == INPUT_SCHEMA, code)
    for field in ("source_sha256", "image_sha256"):
        _require(type(manifest[field]) is str and _HASH.fullmatch(manifest[field]), code)
    _require(type(manifest["page_id"]) is str and _PAGE_ID.fullmatch(manifest["page_id"]), code)
    for field in ("width", "height"):
        _require(_positive_integer(manifest[field]), code)
    return dict(manifest)


def validate_response(model_json):
    """Validate a parsed model JSON object and return an independent copy.

    Empty lists represent an unverified empty response, never proof of a blank
    image. Segment order must be exactly 1..N; bbox coordinates are integers in
    the normalized 0..1000 system, with no implicit clipping or conversion.
    """
    _keys(model_json, _RESPONSE_KEYS, "ocr_vision_response_invalid")
    segments, uncertain = model_json["segments"], model_json["uncertain_regions"]
    _require(type(segments) is list and type(uncertain) is list, "ocr_vision_response_invalid")
    _require(len(segments) <= MAX_SEGMENTS and len(uncertain) <= MAX_UNCERTAIN_REGIONS,
             "ocr_vision_limits_exceeded")
    safe_segments, safe_uncertain, total = [], [], 0
    for position, segment in enumerate(segments, 1):
        _keys(segment, {"order", "kind", "text", "bbox"}, "ocr_vision_segment_invalid")
        _require(type(segment["order"]) is int and segment["order"] == position
                 and type(segment["kind"]) is str and segment["kind"] in KINDS, "ocr_vision_segment_invalid")
        text = _text(segment["text"], MAX_SEGMENT_TEXT_CHARS, "ocr_vision_text_invalid")
        total += len(text)
        _require(total <= MAX_TOTAL_TEXT_CHARS, "ocr_vision_limits_exceeded")
        safe_segments.append({"order": position, "kind": segment["kind"], "text": text, "bbox": _bbox(segment["bbox"])})
    for region in uncertain:
        _keys(region, {"bbox", "reason"}, "ocr_vision_uncertain_region_invalid")
        reason = _text(region["reason"], MAX_REASON_CHARS, "ocr_vision_text_invalid")
        total += len(reason)
        _require(total <= MAX_TOTAL_TEXT_CHARS, "ocr_vision_limits_exceeded")
        safe_uncertain.append({"bbox": _bbox(region["bbox"]), "reason": reason})
    return {"segments": safe_segments, "uncertain_regions": safe_uncertain}


def create_candidate(manifest, model_json, *, model, session_id):
    """Build a private unverified candidate, without persisting any content."""
    source = validate_manifest(manifest)
    candidate = {"schema": CANDIDATE_SCHEMA, **{key: source[key] for key in _BINDINGS},
                 "model": model, "session_id": session_id, **validate_response(model_json), **FLAGS}
    return validate_candidate(candidate, source, model=model, session_id=session_id)


def validate_candidate(candidate, manifest, *, model=None, session_id=None):
    """Revalidate all content, exact manifest bindings and fixed review flags."""
    source = validate_manifest(manifest)
    _keys(candidate, _CANDIDATE_KEYS, "ocr_vision_candidate_invalid")
    _require(candidate["schema"] == CANDIDATE_SCHEMA, "ocr_vision_candidate_invalid")
    for field in _BINDINGS:
        # bool compares equal to 1, so require the manifest's primitive type too.
        _require(type(candidate[field]) is type(source[field]) and candidate[field] == source[field],
                 "ocr_vision_binding_mismatch")
    for field, expected in FLAGS.items():
        _require(type(candidate[field]) is int and candidate[field] == expected, "ocr_vision_flags_invalid")
    safe_model = _text(candidate["model"], MAX_IDENTIFIER_CHARS, "ocr_vision_model_invalid", multiline=False)
    safe_session = _text(candidate["session_id"], MAX_IDENTIFIER_CHARS, "ocr_vision_session_invalid", multiline=False)
    if model is not None:
        _require(safe_model == _text(model, MAX_IDENTIFIER_CHARS, "ocr_vision_model_invalid", multiline=False),
                 "ocr_vision_binding_mismatch")
    if session_id is not None:
        _require(safe_session == _text(session_id, MAX_IDENTIFIER_CHARS, "ocr_vision_session_invalid", multiline=False),
                 "ocr_vision_binding_mismatch")
    response = validate_response({key: candidate[key] for key in _RESPONSE_KEYS})
    return {"schema": CANDIDATE_SCHEMA, **{key: source[key] for key in _BINDINGS},
            "model": safe_model, "session_id": safe_session, **response, **FLAGS}


def summarize(candidate, manifest):
    """Return bounded metadata only; never return OCR text or uncertainty prose."""
    safe = validate_candidate(candidate, manifest)
    return {"schema": SUMMARY_SCHEMA, **{key: safe[key] for key in _BINDINGS},
            "model_sha256": hashlib.sha256(safe["model"].encode("utf-8")).hexdigest(),
            "session_sha256": hashlib.sha256(safe["session_id"].encode("utf-8")).hexdigest(),
            "segment_count": len(safe["segments"]), "uncertain_region_count": len(safe["uncertain_regions"]),
            "text_characters": sum(len(row["text"]) for row in safe["segments"]),
            "kind_counts": {kind: sum(row["kind"] == kind for row in safe["segments"]) for kind in sorted(KINDS)},
            "structure_valid": True, **FLAGS}
