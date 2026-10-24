"""Run region OCR for explicit pages and append the candidates to a RegionStore.

One worker, one model session, one page per call, stop on the first failure.
Before every call the source database and page image are re-checked against the
hashes frozen at the start; nothing is retried. Raw responses are never stored —
each call keeps only sizes, hashes, usage and a static error code.

    python -m ltr.ocr.regions_run --config cfg.json --corpus haruhi \
        --targets targets.json --store regions.sqlite --model <id> --execute
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ltr import model as wire
from ltr.ocr import regions
from ltr.core import ContractError, canonical, is_hash, is_id, need, read_bounded, sha256

MAX_TARGETS = 200
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_TOKENS = 32768


class RunError(ContractError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _page_image(corpus, database, page_id: str, expected_sha: str, width: int, height: int) -> tuple[bytes, str]:
    from ltr.workbench import _resolved_scan_path
    rows = database.query("SELECT image_path, image_sha256, width, height FROM scan_pages WHERE id=?", [page_id])
    need(len(rows) == 1 and rows[0]["image_sha256"] == expected_sha, "page_binding_changed", RunError)
    raw = read_bounded(_resolved_scan_path(corpus, rows[0]["image_path"]), MAX_IMAGE_BYTES)
    need(sha256(raw) == expected_sha, "image_changed", RunError)
    size, media_type = wire.image_info(raw)
    need(size == (width, height), "image_dimensions_mismatch", RunError)
    return raw, media_type


def _code(exc: BaseException, fallback: str) -> str:
    return exc.code if isinstance(exc, ContractError) else fallback


def run(corpus, database, targets: list[dict], store_path, model: str, *, transport=None, execute: bool = False,
        max_calls: int | None = None, max_request_bytes: int = MAX_REQUEST_BYTES,
        max_output_tokens: int = MAX_OUTPUT_TOKENS, history_mode: str = "preserve-thinking",
        batch_id: str | None = None) -> dict:
    need(execute is True, "explicit_execute_required", RunError)
    need(type(targets) is list and 1 <= len(targets) <= MAX_TARGETS, "targets_invalid", RunError)
    need(all(type(t) is dict and set(t) == {"id", "candidate_sha256"} and is_id(t["id"])
             and is_hash(t["candidate_sha256"]) for t in targets)
         and len({t["id"] for t in targets}) == len(targets), "targets_invalid", RunError)
    max_calls = len(targets) if max_calls is None else max_calls
    need(type(max_calls) is int and 1 <= max_calls <= len(targets), "invalid_call_budget", RunError)
    need(type(max_request_bytes) is int and 1 <= max_request_bytes <= MAX_REQUEST_BYTES, "invalid_request_byte_budget", RunError)
    need(type(max_output_tokens) is int and 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS, "invalid_output_token_budget", RunError)
    need(history_mode in wire.HISTORY_MODES, "invalid_history_mode", RunError)
    sender = transport or wire.environment_transport()
    need(callable(sender), "transport_invalid", RunError)

    source = database.source_sha256(refresh=True)
    payloads = [regions.payload_from_reading_units(corpus, database, t["id"], t["candidate_sha256"]) for t in targets]
    need(all(p["source_sha256"] == source for p in payloads), "source_changed", RunError)

    session = str(uuid.uuid4())
    batch_id = batch_id or "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    store_path = Path(store_path).absolute()
    store = (regions.RegionStore(store_path, writable=True) if store_path.exists()
             else regions.RegionStore.create(store_path, corpus_root=corpus.root))
    provenance_base = {"runner": "ltr.ocr.regions_run", "batch_id": batch_id,
                       "contract_module_sha256": sha256(Path(regions.__file__).read_bytes())}
    summary = {"planned": len(targets), "call_budget": max_calls, "history_mode": history_mode,
               "source_sha256": source, "calls": [], "stop_reason": "running"}
    store.add_batch(batch_id, origin="region-run", created_at=_now(), model=model, session_id=session, summary=summary)

    history: list[dict] = []
    totals = {key: 0 for key in wire.USAGE_KEYS}
    stop = "planned_pages_complete"
    try:
        for number, payload in enumerate(payloads):
            if number >= max_calls:
                stop = "call_budget_exhausted"
                break
            record = {"call": number + 1, "page_id": payload["page_id"], "status": "not_sent"}
            try:
                need(database.source_sha256(refresh=True) == source, "source_changed", RunError)
                image, media_type = _page_image(corpus, database, payload["page_id"], payload["image_sha256"],
                                                payload["width"], payload["height"])
            except Exception as exc:
                stop = _code(exc, "source_changed")
                record.update(status="failed", error_code=stop)
                summary["calls"].append(record)
                break
            message = wire.image_message(image, media_type, canonical(payload).decode("utf-8"))
            encoded = wire.encode_request(model, message, history, session, max_output_tokens, system=regions.SYSTEM)
            if len(encoded) > max_request_bytes:
                stop = "request_byte_budget_exhausted"
                break
            record.update(status="dispatched_outcome_unknown", request_bytes=len(encoded),
                          history_messages=len(history))
            summary["calls"].append(record)
            try:
                response = sender(encoded, session)
            except urllib.error.HTTPError as exc:
                record.update(status="failed", error_code="request_rejected", http_status=exc.code)
                stop = "request_rejected"
                break
            except Exception:
                record["error_code"] = stop = "transport_outcome_unknown"
                break
            record["usage"] = wire.usage(response)
            for key, value in record["usage"].items():
                totals[key] += value
            try:
                text, assistant, shape = wire.assistant_reply(response, history_mode)
                record.update(response_bytes=len(text.encode("utf-8", "surrogatepass")),
                              response_sha256=sha256(text.encode("utf-8", "surrogatepass")), content_shape=shape)
                rows = regions.parse_response(text, payload)
                need(database.source_sha256(refresh=True) == source, "source_changed", RunError)
                candidate = regions.build_candidate(payload, rows, model=model, session_id=session,
                                                    provenance=dict(provenance_base, call=number + 1))
                digest = store.add_candidate(batch_id, candidate)
            except Exception as exc:
                record.update(status="failed", error_code=_code(exc, "response_contract_invalid"))
                stop = record["error_code"]
                break
            record.update(status="valid_candidate", candidate_sha256=digest,
                          unreadable_count=candidate["unreadable_count"])
            history.extend([message, assistant])
    finally:
        calls = summary["calls"]
        summary.update(
            stop_reason=stop, usage=totals, session_id=session,
            attempted=sum("request_bytes" in c for c in calls),
            valid=sum(c["status"] == "valid_candidate" for c in calls),
            unknown=sum(c["status"] == "dispatched_outcome_unknown" for c in calls),
            source_unchanged=database.source_sha256(refresh=True) == source)
        summary["failed"] = summary["attempted"] - summary["valid"] - summary["unknown"]
        summary["unattempted"] = summary["planned"] - summary["attempted"]
        store.update_batch_summary(batch_id, summary)
        store.close()
    return {key: value for key, value in summary.items() if key != "calls"} | {"batch_id": batch_id}


def main(argv=None) -> int:
    from ltr.workbench import CorpusDatabase, WorkbenchConfig
    parser = argparse.ArgumentParser(prog="python -m ltr.ocr.regions_run", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", required=True)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--targets", required=True, help='JSON list of {"id": page_id, "candidate_sha256": geometry sha}')
    parser.add_argument("--store", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--max-output-tokens", type=int, default=MAX_OUTPUT_TOKENS)
    parser.add_argument("--request-timeout-seconds", type=int, default=300)
    parser.add_argument("--history-mode", choices=sorted(wire.HISTORY_MODES), default="preserve-thinking")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = WorkbenchConfig.load(args.config)
        corpus = next((c for c in config.corpora if c.id == args.corpus), None)
        need(corpus is not None, "corpus_not_configured", RunError)
        targets = json.loads(Path(args.targets).read_text(encoding="utf-8"))
        result = run(corpus, CorpusDatabase(corpus), targets, args.store, args.model,
                     transport=wire.environment_transport(args.request_timeout_seconds), execute=args.execute,
                     max_calls=args.max_calls, max_output_tokens=args.max_output_tokens, history_mode=args.history_mode)
    except ContractError as exc:
        print(json.dumps({"error": exc.code}))
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
