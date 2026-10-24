"""Build a finite source-ordered plan from explicitly supplied frozen evidence.

No model is called. Absence from supplied evidence never proves an ID has never
been attempted elsewhere. Callers must freeze evidence and ensure no concurrent
worker is using the same target set before building or executing a plan.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from ltr.alignment import campaign
from ltr.alignment import review as ar
from ltr.alignment.index import load_observations
from ltr.quotes.index import _stable_source

_HASH = re.compile(r"[0-9a-f]{64}")
_STATUSES = {"valid_model_review", "failed", "dispatched_outcome_unknown"}


class AlignmentPlanError(ValueError):
    """Static source-free planning refusal."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _require(condition, code):
    if not condition:
        raise AlignmentPlanError(code)


def _integer(value, low=0, high=None):
    return type(value) is int and value >= low and (high is None or value <= high)


def _hash(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _string(value):
    return isinstance(value, str) and 0 < len(value) <= 200 and not re.search(r"[\x00-\x1f\x7f]", value)


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "alignment_plan_evidence_invalid")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def _snapshot(directory, pattern):
    _require(directory.is_dir(), "alignment_plan_evidence_changed")
    return [(file.relative_to(directory).as_posix(), ar.fingerprint(file))
            for file in sorted(directory.rglob(pattern)) if file.is_file()]


def _history(directory, baseline, pair):
    progress = _json((directory / "progress.json").read_bytes())
    summary = _json((directory / "summary.json").read_bytes())
    code = "alignment_plan_history_invalid"
    _require(isinstance(progress, dict) and isinstance(summary, dict), code)
    for item in (progress, summary):
        _require(item.get("schema") == ar._REVIEW_SCHEMA and item.get("database_sha256") == baseline
                 and (item.get("zh_edition_id"), item.get("ja_edition_id")) == pair
                 and _string(item.get("session_id")) and _hash(item.get("source_artifact_sha256"))
                 and item.get("corpus_updated") is False, code)
    for key in ("session_id", "source_artifact_sha256", "book_key", "producer_requested_model",
                "producer_served_model", "reviewer_requested_model"):
        _require(_string(progress.get(key)) and progress.get(key) == summary.get(key), code)
    _require(progress["reviewer_requested_model"] != progress["producer_requested_model"], code)
    if "source_order" in progress or "source_order" in summary:
        _require(type(progress.get("source_order")) is bool
                 and progress["source_order"] is summary.get("source_order"), code)
    if "history_mode" in progress or "history_mode" in summary:
        _require(type(progress.get("history_mode")) is str and progress["history_mode"] in ("text", "preserve-thinking")
                 and progress["history_mode"] == summary.get("history_mode"), code)
    legacy = "stop_reason" not in summary
    _require(summary.get("source_unchanged") is True
             and (legacy or summary.get("stop_reason") in campaign.STOP_REASONS - {"source_changed"}), code)
    # A terminal UTC timestamp distinguishes a completed summary from progress.
    from datetime import datetime, timedelta
    finished = summary.get("finished_at")
    _require(isinstance(finished, str), code)
    parsed = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    _require(parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0), code)
    calls = progress.get("calls")
    _require(isinstance(calls, list) and len(calls) <= 100, code)
    ids_by_status = {status: set() for status in _STATUSES}
    seen = set()
    counts = dict.fromkeys(campaign.COUNTS, 0)
    decisions = dict.fromkeys(("keep", "reject", "uncertain"), 0)
    for number, call in enumerate(calls):
        _require(isinstance(call, dict) and type(call.get("number")) is int and call["number"] == number
                 and call.get("status") in _STATUSES and call.get("session_id") == progress["session_id"]
                 and call.get("model") == progress["reviewer_requested_model"]
                 and _hash(call.get("input_sha256")) and _hash(call.get("history_sha256")), code)
        ids = call.get("review_ids")
        _require(isinstance(ids, list) and 1 <= len(ids) <= 12 and all(_string(i) for i in ids)
                 and len(set(ids)) == len(ids) and not seen.intersection(ids), code)
        seen.update(ids)
        status = call["status"]
        _require(not legacy or status != "dispatched_outcome_unknown", code)
        ids_by_status[status].update(ids)
        counts["attempted_calls"] += 1
        counts["attempted_rows"] += len(ids)
        kind = {"valid_model_review": "valid", "failed": "failed"}.get(status)
        if kind:
            counts[kind + "_calls"] += 1
            counts[kind + "_rows"] += len(ids)
        if kind == "valid":
            _require(type(call.get("rows")) is int and call["rows"] == len(ids), code)
            tally = call.get("counts")
            _require(isinstance(tally, dict) and set(tally) == set(decisions)
                     and all(_integer(v) for v in tally.values()) and sum(tally.values()) == len(ids), code)
            for key in decisions:
                decisions[key] += tally[key]
    for key, value in counts.items():
        _require(type(summary.get(key)) is int and summary[key] == value, code)
    for key, value in decisions.items():
        _require(type(summary.get(key)) is int and summary[key] == value, code)
    for key in ("candidate_rows_total", "skipped_unmatched_rows", "skipped_prefix_rows", "unattempted_rows", "remaining"):
        _require(_integer(summary.get(key)), code)
    range_fields = {"skipped_suffix_rows", "excluded_reviewed_rows", "selected_range_rows", "eligible_rows"}
    present = range_fields.intersection(summary)
    _require(not present or present == range_fields, code)
    implicit_range = not present
    if implicit_range:
        suffix = excluded = 0
        selected = eligible = summary["candidate_rows_total"] - summary["skipped_prefix_rows"]
    else:
        suffix, excluded = summary["skipped_suffix_rows"], summary["excluded_reviewed_rows"]
        selected, eligible = summary["selected_range_rows"], summary["eligible_rows"]
    _require(all(_integer(value) for value in (suffix, excluded, selected, eligible)), code)
    _require(summary["candidate_rows_total"] == summary["skipped_prefix_rows"] + selected + suffix
             and selected == eligible + excluded
             and eligible == counts["attempted_rows"] + summary["unattempted_rows"]
             and summary["remaining"] == summary["candidate_rows_total"] - counts["valid_rows"] - excluded, code)
    return ids_by_status, legacy, implicit_range


def _reviews(directories, baseline, pair, proposals):
    reviewed = set()
    with tempfile.TemporaryDirectory(prefix="alignment-plan-review-") as staging:
        stage = Path(staging)
        number = 0
        for directory in directories:
            for file in sorted(directory.rglob("review-*.json")):
                if not file.is_file():
                    continue
                raw = file.read_bytes()
                data = _json(raw)
                code = "alignment_plan_review_invalid"
                _require(isinstance(data, dict), code)
                if data.get("schema") != ar._REVIEW_SCHEMA:
                    continue
                rows = data.get("rows")
                _require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), code)
                matches = [(row.get("zh_edition_id"), row.get("ja_edition_id")) == pair for row in rows]
                if not any(matches):
                    continue
                _require(all(matches) and data.get("database_sha256") == baseline, code)
                (stage / f"review-{number:06d}.json").write_bytes(raw)
                number += 1
        reviewed.update(load_observations(stage, baseline, proposals))
    return reviewed


def _ranges(candidates, excluded, directory, max_calls, batch_size):
    eligible = [i for i, row in enumerate(candidates) if row["review_id"] not in excluded]
    jobs, selected = [], []
    cursor = 0
    remaining = max_calls
    while cursor < len(eligible) and remaining and len(jobs) < 100:
        start = eligible[cursor]
        end = cursor + 1
        while end < len(eligible) and eligible[end] == eligible[end - 1] + 1:
            end += 1
        # A campaign job supports at most 100 calls; split long runs explicitly.
        length = min(end - cursor, min(100, remaining) * batch_size)
        indices = eligible[cursor:cursor + length]
        calls = (length + batch_size - 1) // batch_size
        jobs.append({"job_id": f"new-{len(jobs) + 1:03d}", "input": str(directory),
                     "max_calls": calls, "batch_size": batch_size, "start": start,
                     "end": start + length, "exclude_reviewed": None})
        selected.extend(candidates[i]["review_id"] for i in indices)
        remaining -= calls
        cursor += length
    _require(jobs, "alignment_plan_no_dispatchable_rows")
    # Independently reconstruct every selected range against canonical indices.
    actual = [row["review_id"] for job in jobs for row in candidates[job["start"]:job["end"]]]
    _require(actual == selected and len(set(actual)) == len(actual) and not excluded.intersection(actual)
             and sum(j["max_calls"] for j in jobs) <= max_calls, "alignment_plan_scope_invalid")
    deferred = len(eligible) - len(selected)
    return jobs, selected, deferred if not remaining else 0, deferred if remaining else 0


def _build(db, input_dir, review_dirs, history_dirs, output_path, max_calls, batch_size):
    _require(_integer(max_calls, 1, 1000) and _integer(batch_size, 1, 12), "alignment_plan_bounds_invalid")
    _require(not os.path.lexists(output_path), "alignment_plan_output_exists")
    database = Path(db).resolve(strict=True)
    output = Path(output_path).resolve()
    _require(isinstance(review_dirs, (list, tuple)) and isinstance(history_dirs, (list, tuple)), "alignment_plan_paths_invalid")
    lexical = [Path(input_dir), *(Path(p) for p in review_dirs), *(Path(p) for p in history_dirs)]
    resolved = [p.resolve(strict=True) for p in lexical]
    directory = resolved[0]
    reviews = resolved[1:1 + len(review_dirs)]
    histories = resolved[1 + len(review_dirs):]
    _require(len(set(reviews)) == len(reviews) and len(set(histories)) == len(histories), "alignment_plan_duplicate_evidence")
    _require(output.parent.is_dir() and all(p.is_dir() for p in resolved), "alignment_plan_paths_invalid")
    _require(not any(campaign._inside(output, p) for p in [database.parent, *resolved]), "alignment_plan_output_boundary")
    _stable_source(database)
    baseline = ar.fingerprint(database)
    snapshots = [(directory, "align-*.json", _snapshot(directory, "align-*.json"))]
    snapshots += [(p, "review-*.json", _snapshot(p, "review-*.json")) for p in reviews]
    # Bind exactly the standalone job control files; nested campaign parents are
    # rejected by their schema rather than inferred as completed job histories.
    snapshots += [(p, "*.json", [(name, ar.fingerprint(p / name))
                                 for name in ("progress.json", "summary.json")]) for p in histories]
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        loaded = ar.load_source(directory, connection, baseline, source_order=True)
    finally:
        connection.close()
    candidates = loaded["candidates"]
    current = {row["review_id"] for row in candidates}
    reviewed = _reviews(reviews, baseline, loaded["pair"], campaign._proposals(candidates))
    attempts = {status: set() for status in _STATUSES}
    legacy_histories = 0
    implicit_range_histories = 0
    for history in histories:
        evidence, legacy, implicit_range = _history(history, baseline, loaded["pair"])
        legacy_histories += int(legacy)
        implicit_range_histories += int(implicit_range)
        for key in attempts:
            attempts[key].update(evidence[key])
    attempted = set().union(*attempts.values())
    excluded = reviewed | attempted
    jobs, selected, budget_deferred, job_deferred = _ranges(candidates, excluded, directory, max_calls, batch_size)
    for job in jobs:
        job["expected_artifact_sha256"] = ar.digest(loaded["artifacts"])
        job["expected_selected_ids_sha256"] = ar.digest([
            row["review_id"] for row in candidates[job["start"]:job["end"]]])
    payload = {"schema": campaign.SCHEMA, "source_sha256": baseline, "jobs": jobs}
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    _require(len(encoded) <= campaign.MAX_PLAN_BYTES, "alignment_plan_too_large")

    def guard():
        _require(ar._source_unchanged(database, baseline), "alignment_plan_source_changed")
        _require(all(p.resolve(strict=True) == expected for p, expected in zip(lexical, resolved)), "alignment_plan_evidence_changed")
        for root, pattern, before in snapshots:
            after = ([(name, ar.fingerprint(root / name)) for name in ("progress.json", "summary.json")]
                     if root in histories and pattern == "*.json" else _snapshot(root, pattern))
            _require(before == after, "alignment_plan_evidence_changed")

    temporary = None
    try:
        guard()
        descriptor, name = tempfile.mkstemp(prefix=".alignment-plan-", suffix=".json", dir=output.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        # Reuse the maintained campaign's strict on-disk schema parser.
        checked, _ = campaign._plan(temporary)
        _require(checked == payload, "alignment_plan_scope_invalid")
        guard()
        try:
            os.link(temporary, output)
        except FileExistsError:
            raise AlignmentPlanError("alignment_plan_output_exists") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {
        "source_sha256": baseline, "input_sha256": ar.digest(snapshots[0][2]),
        "review_evidence_sha256": ar.digest(sorted(ar.digest(s[2]) for s in snapshots[1:1 + len(reviews)])),
        "history_evidence_sha256": ar.digest(sorted(ar.digest(s[2]) for s in snapshots[1 + len(reviews):])),
        "plan_sha256": hashlib.sha256(encoded).hexdigest(), "selected_ids_sha256": ar.digest(selected),
        "current_proposals": len(current), "reviewed_current": len(reviewed),
        "attempted_current": len(attempted & current),
        "valid_attempted_current": len(attempts["valid_model_review"] & current),
        "failed_attempted_current": len(attempts["failed"] & current),
        "unknown_attempted_current": len(attempts["dispatched_outcome_unknown"] & current),
        "failed_or_unknown_ever_attempted_current": len(
            (attempts["failed"] | attempts["dispatched_outcome_unknown"]) & current),
        "failed_or_unknown_without_supplied_opinion_current": len(
            ((attempts["failed"] | attempts["dispatched_outcome_unknown"]) & current) - reviewed),
        "historical_attempted_ids_absent_from_current": len(attempted - current),
        "legacy_unspecified_stop_histories": legacy_histories,
        "legacy_implicit_range_histories": implicit_range_histories,
        "attempted_without_supplied_opinion_current": len((attempted & current) - reviewed),
        "unattempted_in_supplied_history_current": len(current - attempted),
        "eligible_in_supplied_evidence_current": len(current - excluded),
        "selected_rows": len(selected), "planned_jobs": len(jobs), "planned_calls": sum(j["max_calls"] for j in jobs),
        "max_calls": max_calls, "batch_size": batch_size,
        "deferred_due_budget_rows": budget_deferred, "deferred_due_job_cap_rows": job_deferred,
        "evidence_scope": "supplied_frozen_evidence_only", "source_order": True,
        "source_unchanged": True, "corpus_updated": False,
    }


def build_plan(db, input_dir, review_dirs, history_dirs, output_path, max_calls=70, batch_size=3):
    """Publish a fresh plan, skipping known IDs only in supplied frozen evidence."""
    try:
        return _build(db, input_dir, review_dirs, history_dirs, output_path, max_calls, batch_size)
    except AlignmentPlanError:
        raise
    except Exception:
        raise AlignmentPlanError("alignment_plan_invalid") from None


def main(argv=None):
    class SafeParser(argparse.ArgumentParser):
        def error(self, message):
            raise AlignmentPlanError("alignment_plan_arguments_invalid")
    parser = SafeParser(description=__doc__)
    for flag in ("db", "input", "out"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--reviews", action="append", default=[])
    parser.add_argument("--history", action="append", default=[])
    parser.add_argument("--max-calls", type=int, default=70)
    parser.add_argument("--batch-size", type=int, default=3)
    try:
        args = parser.parse_args(argv)
        print(json.dumps(build_plan(args.db, args.input, args.reviews, args.history, args.out,
                                    args.max_calls, args.batch_size), sort_keys=True))
        return 0
    except AlignmentPlanError as exc:
        print(json.dumps({"error": exc.code, "corpus_updated": False}))
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
