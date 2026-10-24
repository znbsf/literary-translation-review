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


def validate_units(units, requested_ids):
    """Require exact source coverage; status validity is not human authorization.

    The caller must separately enforce that only a user can accept a draft.
    """
    _require(isinstance(requested_ids, list)
             and all(isinstance(i, str) and bool(i.strip()) for i in requested_ids),
             "invalid_requested_ids")
    _require(len(requested_ids) == len(set(requested_ids)), "duplicate_requested_id")
    _require(isinstance(units, list), "invalid_units")
    seen = set()
    for unit in units:
        _require(isinstance(unit, dict), "invalid_unit")
        unit_id = unit.get("id")
        _require(isinstance(unit_id, str) and bool(unit_id.strip()), "invalid_unit_id")
        _require(unit_id not in seen, "duplicate_unit_id")
        seen.add(unit_id)
        _require(isinstance(unit.get("target"), str) and bool(unit["target"].strip()),
                 "empty_target")
        _require(_integer(unit.get("revision"), 1), "invalid_revision")
        _require(unit.get("status") in ("draft", "needs_review", "accepted"),
                 "invalid_unit_status")
    _require(seen == set(requested_ids), "coverage_mismatch")
    return deepcopy(units)


class Budget:
    """Reserve a whole call before dispatch, including its maximum output.

    Failed calls still consume their reservation. No retries or automatic refund
    are implemented; this intentionally overestimates usage for fail-closed caps.
    """

    def __init__(self, max_calls, max_input_tokens, max_output_tokens):
        _require(all(_integer(v) for v in
                     (max_calls, max_input_tokens, max_output_tokens)), "invalid_budget")
        self.max_calls = max_calls
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def consume(self, input_tokens, output_limit):
        _require(_integer(input_tokens) and _integer(output_limit, 1), "invalid_usage")
        _require(self.calls + 1 <= self.max_calls, "call_budget_exhausted")
        _require(self.input_tokens + input_tokens <= self.max_input_tokens,
                 "input_budget_exhausted")
        _require(self.output_tokens + output_limit <= self.max_output_tokens,
                 "output_budget_exhausted")
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_limit


def invalidate_dependencies(units, entry_id, new_revision):
    """Copy records and mark only stale dependencies for human review."""
    _require(isinstance(entry_id, str) and bool(entry_id.strip()), "invalid_context_id")
    _require(_integer(new_revision, 1), "invalid_revision")
    _require(isinstance(units, list), "invalid_units")
    result = deepcopy(units)
    for unit in result:
        _require(isinstance(unit, dict), "invalid_unit")
        dependencies = unit.get("dependencies", [])
        if isinstance(dependencies, dict):
            dependencies = [{"entry_id": key, "revision": value}
                            for key, value in dependencies.items()]
        _require(isinstance(dependencies, list), "invalid_dependencies")
        for dependency in dependencies:
            _require(isinstance(dependency, dict)
                     and isinstance(dependency.get("entry_id"), str)
                     and _integer(dependency.get("revision"), 1), "invalid_dependencies")
            if dependency["entry_id"] == entry_id and dependency["revision"] != new_revision:
                unit["status"] = "needs_review"
    return result
