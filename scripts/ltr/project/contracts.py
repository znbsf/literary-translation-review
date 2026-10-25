"""Deterministic translation contracts; errors never include private content."""

from copy import deepcopy


class ContractError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise ContractError(code)


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def validate_span(span, paragraphs):
    """Validate half-open Python Unicode codepoint offsets (not byte offsets)."""
    _require(isinstance(span, dict), "invalid_span")
    paragraph_id = span.get("paragraph_id")
    _require(isinstance(paragraph_id, str) and paragraph_id in paragraphs,
             "unknown_paragraph")
    text = paragraphs[paragraph_id]
    _require(isinstance(text, str), "invalid_paragraph")
    start, end = span.get("start"), span.get("end")
    _require(_integer(start) and _integer(end, 1)
             and start < end <= len(text), "invalid_offsets")
    return deepcopy(span)


def compile_context(entries, paragraphs, order, horizon, book_hash):
    """Return verified records or reject the entire selection before dispatch."""
    _require(isinstance(book_hash, str) and bool(book_hash.strip()), "missing_book_hash")
    _require(isinstance(order, list) and all(isinstance(p, str) for p in order),
             "invalid_order")
    _require(len(order) == len(set(order)) and set(order) == set(paragraphs),
             "invalid_order")
    positions = {p: i for i, p in enumerate(order)}
    _require(isinstance(horizon, str) and horizon in positions, "invalid_horizon")
    _require(isinstance(entries, list), "invalid_context")
    seen = set()
    for entry in entries:
        _require(isinstance(entry, dict), "invalid_context")
        entry_id = entry.get("id")
        _require(isinstance(entry_id, str) and bool(entry_id.strip()), "invalid_context_id")
        _require(entry_id not in seen, "duplicate_context_id")
        seen.add(entry_id)
        _require(_integer(entry.get("revision"), 1), "invalid_revision")
        _require(isinstance(entry.get("claim"), str) and bool(entry["claim"].strip()),
                 "invalid_claim")
        _require(entry.get("epistemic") in ("explicit", "inferred", "ambiguous"),
                 "invalid_epistemic")
        _require(entry.get("review_status") == "verified", "unverified_context")
        evidence = entry.get("evidence")
        _require(isinstance(evidence, list) and bool(evidence), "missing_evidence")
        evidence_positions = []
        for span in evidence:
            validate_span(span, paragraphs)
            _require(span.get("book_hash") == book_hash, "evidence_book_mismatch")
            position = positions[span["paragraph_id"]]
            _require(position <= positions[horizon], "future_evidence")
            evidence_positions.append(position)
        available = entry.get("available_from")
        _require(isinstance(available, str) and available in positions, "invalid_availability")
        _require(positions[available] >= max(evidence_positions), "premature_availability")
        _require(positions[available] <= positions[horizon], "future_context")
    return deepcopy(entries)
