"""Finite serial alignment review jobs sharing one private in-memory session.

This controller delegates every model call and source/reference decision to
alignment_review. Its own receipts contain only hashes, IDs, counts and flags.
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
from types import SimpleNamespace
import uuid

from ltr.response_history import resolve_history_mode
from ltr.alignment import review as ar
from ltr.alignment.index import load_observations
from ltr.quotes.index import _stable_source

SCHEMA = "corpus-alignment-review-campaign-v1"
MAX_PLAN_BYTES = 1024 * 1024
JOB_KEYS = {"job_id", "input", "max_calls", "batch_size", "start", "end", "exclude_reviewed"}
JOB_BINDING_KEYS = {"expected_artifact_sha256", "expected_selected_ids_sha256"}
COUNTS = ("attempted_calls", "valid_calls", "failed_calls", "attempted_rows", "valid_rows", "failed_rows")
METRICS = (*COUNTS, "cache_read_input_tokens")
STOP_REASONS = {"range_or_budget_complete", "request_rejected", "consecutive_invalid_responses", "source_changed",
                "request_byte_budget_exhausted", "unsupported_response_content"}


class AlignmentCampaignError(ValueError):
    """Static, sanitized controller rejection code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _require(condition, code):
    if not condition:
        raise AlignmentCampaignError(code)


def _inside(path, directory):
    return path == directory or directory in path.parents


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _plan(path):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "campaign_plan_invalid")
            result[key] = value
        return result

    with path.open("rb") as stream:
        raw = stream.read(MAX_PLAN_BYTES + 1)
    _require(len(raw) <= MAX_PLAN_BYTES, "campaign_plan_too_large")
    data = json.loads(raw, object_pairs_hook=unique_object)
    _require(isinstance(data, dict) and set(data) == {"schema", "source_sha256", "jobs"}
             and data["schema"] == SCHEMA, "campaign_plan_invalid")
    _require(isinstance(data["source_sha256"], str)
             and re.fullmatch(r"[0-9a-f]{64}", data["source_sha256"]), "campaign_plan_invalid")
    _require(isinstance(data["jobs"], list) and 1 <= len(data["jobs"]) <= 100, "campaign_plan_invalid")
    seen = set()
    for job in data["jobs"]:
        _require(isinstance(job, dict) and set(job) in (JOB_KEYS, JOB_KEYS | JOB_BINDING_KEYS), "campaign_job_invalid")
        if JOB_BINDING_KEYS <= set(job):
            _require(all(isinstance(job[key], str) and re.fullmatch(r"[0-9a-f]{64}", job[key])
                         for key in JOB_BINDING_KEYS), "campaign_job_binding_invalid")
        identity = job["job_id"]
        _require(isinstance(identity, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", identity)
                 and identity not in seen, "campaign_job_invalid")
        seen.add(identity)
        _require(_integer(job["max_calls"], 1, 100) and _integer(job["batch_size"], 1, 12)
                 and type(job["start"]) is int and job["start"] >= 0
                 and (job["end"] is None or (type(job["end"]) is int and job["end"] >= job["start"]))
                 and isinstance(job["input"], str) and bool(job["input"])
                 and (job["exclude_reviewed"] is None or
                      (isinstance(job["exclude_reviewed"], str) and bool(job["exclude_reviewed"]))),
                 "campaign_job_invalid")
    return data, hashlib.sha256(raw).hexdigest()


def _tree(directory, pattern):
    # Relative names bind identity but are hashed privately, never in receipts.
    _require(directory.is_dir(), "campaign_artifact_changed")
    return ar.digest([(p.relative_to(directory).as_posix(), ar.fingerprint(p))
                      for p in sorted(directory.rglob(pattern)) if p.is_file()])


def _proposals(candidates):
    return {item["review_id"]: {
        "edition_id": item["zh_edition_id"], "ja_edition_id": item["ja_edition_id"],
        "chinese_id": item["chinese_id"], "chinese_text_sha256": item["chinese_text_sha256"],
        "japanese_json": json.dumps([{"id": identity, "text_sha256": text_hash}
                                    for identity, text_hash in zip(item["japanese_ids"], item["japanese_hashes"])])
    } for item in candidates}


def _preflight(database, plan, plan_data, output, model, max_calls):
    _stable_source(database)
    baseline = ar.fingerprint(database)
    _require(baseline == plan_data["source_sha256"], "campaign_source_mismatch")
    _require(not _inside(plan, database.parent) and not _inside(output, database.parent), "campaign_output_boundary")
    _require(not os.path.lexists(output), "campaign_output_exists")
    jobs, seen = [], set()
    remaining = max_calls
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        for specification in plan_data["jobs"]:
            job = dict(specification)
            for field in ("input", "exclude_reviewed"):
                value = job[field]
                if value is not None:
                    lexical = plan.parent / value
                    job[field + "_lexical"] = lexical
                    job[field] = lexical.resolve(strict=True)
                    _require(job[field].is_dir(), "campaign_input_boundary")
                    # Also reject an output ancestor: it would already contain
                    # an input/exclusion tree and is not a fresh output root.
                    _require(not _inside(output, job[field]) and not _inside(job[field], output), "campaign_output_boundary")
            artifact = _tree(job["input"], "align-*.json")
            exclusion = _tree(job["exclude_reviewed"], "review-*.json") if job["exclude_reviewed"] else None
            loaded = ar.load_source(job["input"], connection, baseline, source_order=True)
            source_artifact_sha256 = ar.digest(loaded["artifacts"])
            if "expected_artifact_sha256" in job:
                _require(job["expected_artifact_sha256"] == source_artifact_sha256,
                         "campaign_expected_artifact_mismatch")
            _require(model not in (loaded["producer_requested"], loaded["producer_served"]),
                     "reviewer_must_differ_from_producer")
            candidates = loaded["candidates"]
            completed = set(load_observations(job["exclude_reviewed"], baseline, _proposals(candidates)))
            selected = candidates[job["start"]:job["end"]]
            eligible = [item["review_id"] for item in selected if item["review_id"] not in completed]
            budget = min(job["max_calls"], remaining)
            scope = eligible[:budget * job["batch_size"]]
            if "expected_selected_ids_sha256" in job:
                _require(job["expected_selected_ids_sha256"] == ar.digest(scope),
                         "campaign_expected_selected_ids_mismatch")
            _require(not seen.intersection(scope), "campaign_overlapping_jobs")
            seen.update(scope)
            planned_calls = (len(scope) + job["batch_size"] - 1) // job["batch_size"]
            remaining -= planned_calls
            _require(_tree(job["input"], "align-*.json") == artifact and
                     (not job["exclude_reviewed"] or _tree(job["exclude_reviewed"], "review-*.json") == exclusion),
                     "campaign_artifact_changed")
            job.update(artifact_sha256=artifact, source_artifact_sha256=source_artifact_sha256,
                       exclusion_sha256=exclusion, review_ids=scope,
                       excluded_ids=sorted(completed), planned_calls=planned_calls,
                       source_candidate_rows=len(candidates), selected_range_rows=len(selected),
                       excluded_reviewed_rows=len(selected) - len(eligible), eligible_rows=len(eligible),
                       pending_beyond_budget_rows=len(eligible) - len(scope))
            jobs.append(job)
    finally:
        connection.close()
    _require(ar._source_unchanged(database, baseline), "campaign_source_changed")
    return baseline, jobs


def _check(database, baseline, plan, plan_sha, job):
    _require(ar._source_unchanged(database, baseline), "campaign_source_changed")
    _require(ar.fingerprint(plan) == plan_sha, "campaign_plan_changed")
    for field, pattern, digest_key in (("input", "align-*.json", "artifact_sha256"),
                                       ("exclude_reviewed", "review-*.json", "exclusion_sha256")):
        if job[field] is not None:
            _require(job[field + "_lexical"].resolve(strict=True) == job[field]
                     and _tree(job[field], pattern) == job[digest_key], "campaign_artifact_changed")


def _write(path, payload):
    descriptor, name = tempfile.mkstemp(prefix=".campaign-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=True, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_error(exc):
    if isinstance(exc, AlignmentCampaignError):
        return exc.code
    if isinstance(exc, ValueError) and str(exc) in ar._CODES:
        return str(exc)
    return "campaign_failed"


def _recover_counts(child):
    """Recover real partial dispatch evidence if run() failed before summary."""
    counts = dict.fromkeys(METRICS, 0)
    try:
        progress = json.loads((child / "progress.json").read_bytes())
        for call in progress["calls"]:
            ids = call["review_ids"]
            _require(isinstance(ids, list), "campaign_partial_invalid")
            counts["attempted_calls"] += 1
            counts["attempted_rows"] += len(ids)
            counts["cache_read_input_tokens"] += ar._numeric_usage(call.get("usage")).get("cache_read_input_tokens", 0)
            kind = {"valid_model_review": "valid", "failed": "failed"}.get(call["status"])
            if kind:
                counts[kind + "_calls"] += 1
                counts[kind + "_rows"] += len(ids)
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return counts


def _campaign(db, plan_path, output_dir, model, max_calls, execute, incremental_references, max_request_bytes,
              max_output_tokens, history_mode):
    history_mode = resolve_history_mode(history_mode)
    output_token_budget = ar.resolve_output_token_budget(max_output_tokens)
    _require(execute is True and _integer(max_calls, 1, 1000)
             and isinstance(model, str) and 0 < len(model) <= 200
             and not re.search(r"[\x00-\x1f\x7f]", model)
             and type(incremental_references) is bool
             and (max_request_bytes is None or _integer(max_request_bytes, 1, ar.MAX_REQUEST_BYTES)), "campaign_dispatch_invalid")
    reference_delivery = "incremental" if incremental_references else "prefetch"
    _require(not os.path.lexists(output_dir), "campaign_output_exists")
    database = Path(db).resolve(strict=True)
    plan = Path(plan_path).resolve(strict=True)
    output = Path(output_dir).resolve()
    _require(not _inside(plan, database.parent) and not _inside(output, database.parent), "campaign_output_boundary")
    data, plan_sha = _plan(plan)
    baseline, jobs = _preflight(database, plan, data, output, model, max_calls)
    _require(ar.fingerprint(plan) == plan_sha, "campaign_plan_changed")
    # Exclusive mkdir is the ownership boundary; never resume/reuse a directory.
    output.mkdir()
    state = {"model": model, "database_sha256": baseline, "system_sha256": ar.digest(ar.SYSTEM),
             "session_id": str(uuid.uuid4()), "reference_delivery": reference_delivery,
             "request_byte_budget": max_request_bytes, "output_token_budget": output_token_budget, "history_mode": history_mode, "history": []}
    receipts = []
    for job in jobs:
        receipts.append({key: job[key] for key in (
            "job_id", "artifact_sha256", "source_artifact_sha256", "exclusion_sha256", "review_ids", "excluded_ids", "planned_calls",
            "source_candidate_rows", "selected_range_rows", "excluded_reviewed_rows", "eligible_rows",
            "pending_beyond_budget_rows")} | {"planned_rows": len(job["review_ids"]),
                "source_order": True, "reference_delivery": reference_delivery,
                "request_byte_budget": max_request_bytes, "next_request_bytes": None,
                "output_token_budget": output_token_budget, "history_mode": history_mode,
                "status": "not_started", "stop_reason": None, **dict.fromkeys(METRICS, 0)})
    summary = {"schema": SCHEMA, "source_sha256": baseline, "plan_sha256": plan_sha,
               "model_sha256": hashlib.sha256(model.encode()).hexdigest(),
               "session_id": state["session_id"], "max_calls": max_calls,
               "jobs": receipts, "source_order": True, "reference_delivery": reference_delivery,
               "request_byte_budget": max_request_bytes, "next_request_bytes": None,
               "output_token_budget": output_token_budget, "history_mode": history_mode,
               "corpus_updated": False, "stop_reason": "running",
               "cache_usage_scope": "reported_response_usage_including_invalid_outputs"}

    def checkpoint(final=False):
        for key in METRICS:
            summary[key] = sum(row[key] for row in receipts)
        summary["planned_rows"] = sum(row["planned_rows"] for row in receipts)
        summary["unattempted_rows"] = summary["planned_rows"] - summary["attempted_rows"]
        summary["unknown_rows"] = summary["attempted_rows"] - summary["valid_rows"] - summary["failed_rows"]
        summary["pending_beyond_budget_rows"] = sum(row["pending_beyond_budget_rows"] for row in receipts)
        for row in receipts:
            row["unattempted_rows"] = row["planned_rows"] - row["attempted_rows"]
            row["unknown_rows"] = row["attempted_rows"] - row["valid_rows"] - row["failed_rows"]
        summary["remaining_calls"] = max_calls - summary["attempted_calls"]
        _write(output / ("summary.json" if final else "progress.json"), summary)

    checkpoint()
    for job, receipt in zip(jobs, receipts):
        child = output / job["job_id"]
        try:
            _check(database, baseline, plan, plan_sha, job)
            remaining = max_calls - summary["attempted_calls"]
            if remaining <= 0:
                summary["stop_reason"] = "global_budget_exhausted"
                break
            if not receipt["planned_rows"]:
                receipt.update(status="no_dispatch", stop_reason="range_or_budget_complete")
                continue
            receipt["status"] = "running"
            checkpoint()
            args = SimpleNamespace(db=database, input=job["input"], out=child, model=model,
                max_calls=min(job["max_calls"], remaining, job["planned_calls"]), batch_size=job["batch_size"],
                start=job["start"], end=job["end"], exclude_reviewed=job["exclude_reviewed"],
                source_order=True, incremental_references=incremental_references,
                max_request_bytes=max_request_bytes, max_output_tokens=max_output_tokens, history_mode=history_mode, execute=True)
            result = ar.run(args, session=state)
            for key in METRICS:
                _require(type(result[key]) is int and result[key] >= 0, "campaign_result_invalid")
                receipt[key] = result[key]
            reason = result["stop_reason"]
            _require(reason in STOP_REASONS and result["session_id"] == state["session_id"]
                     and result["database_sha256"] == baseline
                     and result["source_artifact_sha256"] == job["source_artifact_sha256"]
                     and result["source_order"] is True
                     and result["reference_delivery"] == reference_delivery
                     and result["request_byte_budget"] == max_request_bytes
                     and result["output_token_budget"] == output_token_budget
                     and result.get("history_mode", "text") == history_mode
                     and receipt["attempted_rows"] <= receipt["planned_rows"]
                     and receipt["attempted_calls"] <= args.max_calls, "campaign_result_invalid")
            receipt.update(status="finished", stop_reason=reason)
            receipt['next_request_bytes'] = result['next_request_bytes']
            summary['next_request_bytes'] = result['next_request_bytes']
            if reason != "range_or_budget_complete" or result["source_unchanged"] is not True:
                summary["stop_reason"] = reason if reason != "range_or_budget_complete" else "source_changed"
                checkpoint()
                break
            _check(database, baseline, plan, plan_sha, job)
            checkpoint()
            if summary["remaining_calls"] == 0:
                summary["stop_reason"] = "global_budget_exhausted"
                break
        except Exception as exc:
            receipt.update(_recover_counts(child), status="stopped", stop_reason=_safe_error(exc))
            summary["stop_reason"] = receipt["stop_reason"]
            break
    else:
        summary["stop_reason"] = "finite_scope_complete"
    try:
        summary["source_unchanged"] = ar._source_unchanged(database, baseline)
    except OSError:
        summary["source_unchanged"] = False
    if not summary["source_unchanged"]:
        summary["stop_reason"] = "source_changed"
    checkpoint(final=True)
    _write(output / "progress.json", summary)
    return summary


def run_campaign(db, plan_path, output_dir, model, max_calls, execute=False, *, incremental_references=False,
                 max_request_bytes=None, max_output_tokens=None, history_mode="text"):
    """Run a fresh finite campaign, requiring explicit execution authorization."""
    try:
        return _campaign(db, plan_path, output_dir, model, max_calls, execute, incremental_references,
                         max_request_bytes, max_output_tokens, history_mode)
    except AlignmentCampaignError:
        raise
    except Exception as exc:
        raise AlignmentCampaignError(_safe_error(exc)) from None


def main(argv=None):
    class SafeParser(argparse.ArgumentParser):
        def error(self, message):
            raise AlignmentCampaignError("campaign_arguments_invalid")

    parser = SafeParser(description=__doc__)
    for flag in ("db", "plan", "out", "model"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--max-calls", type=int, required=True)
    parser.add_argument("--history-mode", choices=("text", "preserve-thinking"), default="text")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--incremental-references", action="store_true")
    parser.add_argument("--max-request-bytes", type=int)
    parser.add_argument("--max-output-tokens", type=int)
    try:
        args = parser.parse_args(argv)
        result = run_campaign(args.db, args.plan, args.out, args.model, args.max_calls, args.execute,
                              incremental_references=args.incremental_references, max_request_bytes=args.max_request_bytes,
                              max_output_tokens=args.max_output_tokens, history_mode=args.history_mode)
        print(json.dumps({key: value for key, value in result.items() if key != "jobs"}, sort_keys=True))
        return 0 if result["stop_reason"] in {"finite_scope_complete", "global_budget_exhausted"} else 1
    except AlignmentCampaignError as exc:
        print(json.dumps({"error": exc.code, "corpus_updated": False}))
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
