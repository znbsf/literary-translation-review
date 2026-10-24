"""Private second-model review of source-bound zh->ja OCR alignment candidates.

Source prose lives only in memory, the authorized model payload and private
review outputs. Progress ledgers, console and errors carry identifiers, hashes,
counts and error classes only. A model review is never human verification and
never promotes a candidate. The source database is only ever opened read-only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import urllib.error
import uuid

from ltr.quotes.batch import is_unsupported_response_content, is_redirect_rejection, digest, request_model, request_byte_count, resolve_output_token_budget, failure_metadata
from ltr.response_history import resolve_history_mode, validate_history, safe_counts
from ltr.library.review_queue import fingerprint


SYSTEM = """你是第二位独立的 zh->ja OCR 对话对齐复核员。输入给出中文对话（含其同版本
上下文）、候选日语 ID 以及 review_id。japanese_reference 提供新增日语 ID 的原文
与上下文；后续批次可补入新参照，已有参照沿用同一历史并按 ID 引用，ocr_status 一律 unverified。
请独立判断该中文对话与所给日语候选是否语义与顺序一致，不要因为提取模型给出了候选
就倾向 keep；OCR 文本可能残缺或错误，证据不足或文本不清时用 uncertain。
禁止改写、翻译或编造任何原文。只处理 items 内条目，context 仅帮助理解。
只输出唯一 JSON 对象
{"reviews":[{"review_id":"给定ID","decision":"keep|reject|uncertain","basis":"semantic_match|semantic_mismatch|ocr_unclear|insufficient_context|partial_match"}]}。
必须完整且只覆盖给定 review_id 一次，不输出原文、代码围栏或额外说明。
所有输出只是机器复核，不是人工确认，也不构成任何晋升。"""

_DECISION = {"keep", "reject", "uncertain"}
_BASIS = {"semantic_match", "semantic_mismatch", "ocr_unclear",
          "insufficient_context", "partial_match"}
_REVIEW_FIELDS = {"review_id", "decision", "basis"}
_REPLY_CODES = {"invalid_review_contract", "inexact_review_coverage", "invalid_review_enum"}
_BATCH_FIELDS = {"schema", "database_sha256", "zh_edition_id", "ja_edition_id",
                 "book_key", "requested_model", "served_model", "input_sha256",
                 "session_id", "rows"}
_ROW_FIELDS = {"chinese_id", "japanese_ids", "status", "confidence", "requires_review",
               "accepted", "human_confirmed", "semantic_verified", "reference_eligible",
               "ocr_verified", "chinese_text_sha256", "japanese"}
_JA_FIELDS = {"id", "text_sha256"}
_ZERO_FLAGS = ("accepted", "human_confirmed", "semantic_verified",
               "reference_eligible", "ocr_verified")
_SCHEMA = "haruhi-alignment-candidates-v1"
_REVIEW_SCHEMA = "haruhi-alignment-model-review-v1"
MAX_REQUEST_BYTES = 16 * 1024 * 1024

_CODES = {
    "invalid_dispatch_bounds", "invalid_source_order", "invalid_reference_delivery",
    "invalid_request_byte_budget", "invalid_output_token_budget", "unstable_database_snapshot",
    "alignment_review_output_exists", "alignment_review_output_must_be_external",
    "missing_alignment_input", "invalid_alignment_batch_contract",
    "invalid_alignment_batch_schema", "stale_alignment_source",
    "editions_must_be_distinct", "inconsistent_edition_pair", "inconsistent_book_key",
    "ambiguous_or_missing_edition_id", "edition_language_mismatch", "book_key_mismatch",
    "empty_edition", "ambiguous_dialogue_id", "invalid_alignment_row_contract",
    "invalid_chinese_reference", "duplicate_chinese_id", "invalid_review_flag",
    "invalid_japanese_reference", "invalid_alignment_status", "status_reference_mismatch",
    "ambiguous_or_missing_dialogue_id", "dialogue_edition_mismatch", "changed_source_text",
    "invalid_japanese_binding", "duplicate_review_id", "invalid_producer_model",
    "reviewer_must_differ_from_producer", "source_database_changed",
    "missing_runtime_authentication", "invalid_session",
    "conflicting_japanese_reference",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _numeric_usage(value):
    if not isinstance(value, dict):
        return {}
    return {key: count for key, count in value.items()
            if key in {"input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"}
            and type(count) is int and count >= 0}


def validate_review(payload: object, ids: list) -> list:
    """Validate one model review reply against exact review ids."""
    if not isinstance(payload, dict) or set(payload) != {"reviews"}:
        raise ValueError("invalid_review_contract")
    rows = payload["reviews"]
    if not isinstance(rows, list) or not isinstance(ids, list):
        raise ValueError("invalid_review_contract")
    if len(rows) != len(ids):
        raise ValueError("inexact_review_coverage")
    if len(set(ids)) != len(ids):
        raise ValueError("inexact_review_coverage")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != _REVIEW_FIELDS:
            raise ValueError("invalid_review_contract")
        if not isinstance(row["review_id"], str) or row["review_id"] not in ids:
            raise ValueError("inexact_review_coverage")
        if row["review_id"] in seen:
            raise ValueError("inexact_review_coverage")
        seen.add(row["review_id"])
        if (not isinstance(row["decision"], str) or not isinstance(row["basis"], str)
                or row["decision"] not in _DECISION or row["basis"] not in _BASIS):
            raise ValueError("invalid_review_enum")
    if seen != set(ids):
        raise ValueError("inexact_review_coverage")
    return rows


def _reply_diagnostics(payload, expected_ids):
    """Count reply shape only; never retain keys, IDs, enum values or prose.

    ID coverage counts describe only the inspected prefix when rows_truncated
    is true. Unknown diagnostic failures are deliberately omitted, rather than
    affecting the review validator or exposing an exception's source content.
    """
    try:
        def static_type(value):
            return {type(None): "null", dict: "object", list: "array",
                    str: "string", bool: "boolean", int: "integer",
                    float: "number"}.get(type(value), "other")

        if type(expected_ids) is not list or any(type(value) is not str for value in expected_ids):
            return None
        expected = set(expected_ids)
        has_reviews = type(payload) is dict and "reviews" in payload
        rows = payload["reviews"] if has_reviews else None
        returned = len(rows) if type(rows) is list else 0
        inspected = min(returned, 1000)
        seen = set()
        strings = 0
        for index in range(inspected):
            row = rows[index]
            value = row.get("review_id") if type(row) is dict else None
            if type(value) is str:
                strings += 1
                seen.add(value)
        return {"root_type": static_type(payload), "has_reviews": has_reviews,
                "root_key_count": len(payload) if type(payload) is dict else 0,
                "reviews_type": static_type(rows) if has_reviews else "missing",
                "returned_rows": returned, "inspected_rows": inspected,
                "rows_truncated": returned > inspected, "expected_rows": len(expected_ids),
                "string_id_rows": strings, "invalid_id_rows": inspected - strings,
                "unique_ids": len(seen), "duplicate_id_rows": strings - len(seen),
                "matched_ids": len(seen & expected), "unexpected_ids": len(seen - expected),
                "missing_ids": len(expected - seen)}
    except Exception:
        return None


def _atomic_write(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _source_unchanged(database: Path, baseline: str) -> bool:
    return (not any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-journal"))
            and fingerprint(database) == baseline)


def _neighbour_context(ordered: list, dialogue_id: str) -> list:
    index = next(i for i, row in enumerate(ordered) if row["id"] == dialogue_id)
    window = ordered[max(0, index - 1):index] + ordered[index + 1:index + 2]
    return [{"ordinal": row["ordinal"], "text": row["text"]} for row in window]


def load_source(directory: Path, con: sqlite3.Connection, baseline: str, *, source_order=False) -> dict:
    """Validate every input batch against the source snapshot and bind review ids.

    Returns the bound candidate items, the skipped unmatched count, the shared
    edition pair, and producer model identifiers. Source prose never leaves here.
    Historical file/batch order is preserved unless source_order=True explicitly
    requests the Chinese edition's SQL ordinal/id order before range selection.
    """
    if type(source_order) is not bool:
        raise ValueError("invalid_source_order")
    files = sorted(p for p in directory.rglob("align-*.json") if p.is_file())
    if not files:
        raise ValueError("missing_alignment_input")
    con.row_factory = sqlite3.Row
    editions: dict = {}
    dialogues_cache: dict = {}

    def edition(edition_id: str, language: str) -> dict:
        if edition_id not in editions:
            rows = con.execute(
                "SELECT id,language,book_key FROM editions WHERE id=?", (edition_id,)).fetchall()
            if len(rows) != 1:
                raise ValueError("ambiguous_or_missing_edition_id")
            editions[edition_id] = dict(rows[0])
        row = editions[edition_id]
        if row["language"] != language:
            raise ValueError("edition_language_mismatch")
        return row

    def dialogues(edition_id: str) -> list:
        if edition_id not in dialogues_cache:
            rows = [dict(r) for r in con.execute(
                "SELECT id,ordinal,text FROM dialogues WHERE edition_id=? ORDER BY ordinal,id",
                (edition_id,))]
            if not rows:
                raise ValueError("empty_edition")
            if len({r["id"] for r in rows}) != len(rows):
                raise ValueError("ambiguous_dialogue_id")
            dialogues_cache[edition_id] = rows
        return dialogues_cache[edition_id]

    def resolve(dialogue_id: str, edition_id: str, text_sha256: str) -> dict:
        rows = con.execute(
            "SELECT id,edition_id,ordinal,text FROM dialogues WHERE id=?", (dialogue_id,)).fetchall()
        if len(rows) != 1:
            raise ValueError("ambiguous_or_missing_dialogue_id")
        row = dict(rows[0])
        if row["edition_id"] != edition_id:
            raise ValueError("dialogue_edition_mismatch")
        if not isinstance(text_sha256, str) or _sha(row["text"]) != text_sha256:
            raise ValueError("changed_source_text")
        return row

    pair = None
    book_key = None
    producer_requested = None
    producer_served = None
    seen_chinese: set = set()
    seen_review: set = set()
    skipped = 0
    candidates: list = []
    artifacts: list = []
    for path in files:
        raw = path.read_bytes()
        artifacts.append({"name": path.name, "sha256": hashlib.sha256(raw).hexdigest()})
        try:
            batch = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("invalid_alignment_batch_contract")
        if not isinstance(batch, dict) or set(batch) != _BATCH_FIELDS:
            raise ValueError("invalid_alignment_batch_contract")
        if batch["schema"] != _SCHEMA:
            raise ValueError("invalid_alignment_batch_schema")
        if batch["database_sha256"] != baseline:
            raise ValueError("stale_alignment_source")
        zh, ja = batch["zh_edition_id"], batch["ja_edition_id"]
        if not isinstance(zh, str) or not isinstance(ja, str) or zh == ja:
            raise ValueError("editions_must_be_distinct")
        if pair is None:
            pair = (zh, ja)
        elif pair != (zh, ja):
            raise ValueError("inconsistent_edition_pair")
        zh_edition = edition(zh, "zh")
        ja_edition = edition(ja, "ja")
        key = zh_edition["book_key"]
        if (not isinstance(key, str) or not key or key != ja_edition["book_key"]
                or key != batch["book_key"]):
            raise ValueError("book_key_mismatch")
        if book_key is None:
            book_key = key
        elif book_key != key:
            raise ValueError("inconsistent_book_key")
        requested = batch["requested_model"]
        if not isinstance(requested, str) or not requested:
            raise ValueError("invalid_producer_model")
        if producer_requested is None:
            producer_requested = requested
            served = batch["served_model"]
            producer_served = served if isinstance(served, str) and served else requested
        elif producer_requested != requested:
            raise ValueError("inconsistent_producer_model")
        rows = batch["rows"]
        if not isinstance(rows, list):
            raise ValueError("invalid_alignment_batch_contract")
        zh_ordered = dialogues(zh)
        ja_ordered = dialogues(ja)
        ja_text = {row["id"]: row["text"] for row in ja_ordered}
        for row in rows:
            if not isinstance(row, dict) or set(row) != _ROW_FIELDS:
                raise ValueError("invalid_alignment_row_contract")
            cid = row["chinese_id"]
            if not isinstance(cid, str) or not cid:
                raise ValueError("invalid_chinese_reference")
            if cid in seen_chinese:
                raise ValueError("duplicate_chinese_id")
            seen_chinese.add(cid)
            if type(row["requires_review"]) is not int or row["requires_review"] != 1:
                raise ValueError("invalid_review_flag")
            for flag in _ZERO_FLAGS:
                if type(row[flag]) is not int or row[flag] != 0:
                    raise ValueError("invalid_review_flag")
            jids = row["japanese_ids"]
            if (not isinstance(jids, list) or any(not isinstance(j, str) for j in jids)
                    or len(jids) > 5 or len(set(jids)) != len(jids)):
                raise ValueError("invalid_japanese_reference")
            status = row["status"]
            if row["confidence"] not in {"low", "medium", "high"}:
                raise ValueError("invalid_alignment_confidence")
            if status not in {"candidate", "unmatched"}:
                raise ValueError("invalid_alignment_status")
            if (status == "candidate") != bool(jids):
                raise ValueError("status_reference_mismatch")
            resolve(cid, zh, row["chinese_text_sha256"])
            japanese = row["japanese"]
            if not isinstance(japanese, list) or len(japanese) != len(jids):
                raise ValueError("invalid_japanese_binding")
            japanese_hashes = []
            for jid, entry in zip(jids, japanese):
                if not isinstance(entry, dict) or set(entry) != _JA_FIELDS or entry["id"] != jid:
                    raise ValueError("invalid_japanese_binding")
                resolve(jid, ja, entry["text_sha256"])
                japanese_hashes.append(entry["text_sha256"])
            if status == "unmatched":
                skipped += 1
                continue
            review_id = digest({"source_sha256": baseline, "zh_edition_id": zh,
                                "ja_edition_id": ja, "chinese_id": cid,
                                "japanese_ids": jids,
                                "chinese_text_sha256": row["chinese_text_sha256"],
                                "japanese_hashes": japanese_hashes})[:16]
            if review_id in seen_review:
                raise ValueError("duplicate_review_id")
            seen_review.add(review_id)
            candidates.append({
                "review_id": review_id, "zh_edition_id": zh, "ja_edition_id": ja,
                "chinese_id": cid, "japanese_ids": list(jids),
                "chinese_text_sha256": row["chinese_text_sha256"],
                "japanese_hashes": japanese_hashes,
                "item": {"review_id": review_id,
                         "chinese": {"id": cid, "text": next(
                             r["text"] for r in zh_ordered if r["id"] == cid),
                             "context": _neighbour_context(zh_ordered, cid)},
                         "japanese": [{"id": jid, "text": ja_text[jid],
                                       "context": _neighbour_context(ja_ordered, jid),
                                       "ocr_status": "unverified"} for jid in jids]}})
    if source_order and pair is not None:
        rank = {row["id"]: index for index, row in enumerate(dialogues_cache[pair[0]])}
        candidates.sort(key=lambda item: rank[item["chinese_id"]])
    return {"candidates": candidates, "skipped": skipped, "pair": pair, "book_key": book_key,
            "producer_requested": producer_requested, "producer_served": producer_served,
            "artifacts": artifacts}


def run(args, session=None) -> dict:
    history_mode = resolve_history_mode(getattr(args, "history_mode", "text"))
    max_output_tokens = getattr(args, "max_output_tokens", None)
    output_token_budget = resolve_output_token_budget(max_output_tokens)
    output_options = {} if max_output_tokens is None else {"max_output_tokens": max_output_tokens}
    if history_mode != "text":
        output_options["history_mode"] = history_mode
    max_request_bytes = getattr(args, "max_request_bytes", None)
    if max_request_bytes is not None and (type(max_request_bytes) is not int or not 1 <= max_request_bytes <= MAX_REQUEST_BYTES):
        raise ValueError("invalid_request_byte_budget")
    incremental_references = getattr(args, "incremental_references", False)
    if type(incremental_references) is not bool:
        raise ValueError("invalid_reference_delivery")
    reference_delivery = "incremental" if incremental_references else "prefetch"
    source_order = getattr(args, "source_order", False)
    if type(source_order) is not bool:
        raise ValueError("invalid_source_order")
    if not (args.execute is True and type(args.max_calls) is int and 1 <= args.max_calls <= 100
            and type(args.batch_size) is int and 1 <= args.batch_size <= 12
            and type(args.start) is int and args.start >= 0):
        raise ValueError("invalid_dispatch_bounds")
    end_bound=getattr(args,'end',None)
    if end_bound is not None and (type(end_bound) is not int or end_bound < args.start):
        raise ValueError('invalid_dispatch_bounds')
    database = args.db.resolve(strict=True)
    if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-journal")):
        raise ValueError("unstable_database_snapshot")
    output = args.out.resolve()
    if output.exists():
        raise ValueError("alignment_review_output_exists")
    if output == database.parent or database.parent in output.parents:
        raise ValueError("alignment_review_output_must_be_external")
    directory = args.input.resolve(strict=True)
    baseline = fingerprint(database)
    con = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        loaded = load_source(directory, con, baseline, source_order=source_order)
    finally:
        con.close()
    if not _source_unchanged(database, baseline):
        raise ValueError("source_database_changed")
    zh, ja = loaded["pair"] if loaded["pair"] else (None, None)
    if loaded["producer_requested"] == args.model:
        raise ValueError("reviewer_must_differ_from_producer")
    endpoint = os.environ.get("ANTHROPIC_BASE_URL", "")
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if not endpoint or not token:
        raise ValueError("missing_runtime_authentication")

    artifact_sha256 = digest(loaded["artifacts"])
    candidates = loaded["candidates"]
    total = len(candidates)
    prefix = min(args.start, total)
    end_bound=total if end_bound is None else min(end_bound,total)
    selected = candidates[prefix:end_bound]
    selected_range_rows=len(selected)
    completed=set()
    if getattr(args,'exclude_reviewed',None):
        from ltr.alignment.index import load_observations
        proposals={item['review_id']:{'edition_id':item['zh_edition_id'],'ja_edition_id':item['ja_edition_id'],
                    'chinese_id':item['chinese_id'],'chinese_text_sha256':item['chinese_text_sha256'],
                    'japanese_json':json.dumps([{'id':identity,'text_sha256':text_hash}
                         for identity,text_hash in zip(item['japanese_ids'],item['japanese_hashes'])])}
                   for item in candidates}
        completed=set(load_observations(args.exclude_reviewed.resolve(strict=True),baseline,proposals))
        selected=[item for item in selected if item['review_id'] not in completed]
    excluded_reviewed_rows=selected_range_rows-len(selected)
    references = {j['id']: j for item in selected[:args.max_calls * args.batch_size]
                  for j in item['item']['japanese']}
    if session is None:
        session = {}
    if not isinstance(session, dict):
        raise ValueError("invalid_session")
    if not session:
        session.update(model=args.model, database_sha256=baseline,
                       system_sha256=digest(SYSTEM), session_id=str(uuid.uuid4()),
                       reference_delivery=reference_delivery, request_byte_budget=max_request_bytes,
                       output_token_budget=output_token_budget, history_mode=history_mode, history=[])
    if (session.get("model") != args.model or session.get("database_sha256") != baseline
            or session.get("system_sha256") != digest(SYSTEM)
            or not isinstance(session.get("history"), list)
            or not isinstance(session.get("session_id"), str)
            or session.get("reference_delivery", "prefetch") != reference_delivery
            or (session.get("request_byte_budget") is not None
                and type(session.get("request_byte_budget")) is not int)
            or session.get("request_byte_budget") != max_request_bytes
            or type(session.get("output_token_budget", 8000)) is not int
            or session.get("output_token_budget", 8000) != output_token_budget
            or session.get("history_mode", "text") != history_mode):
        raise ValueError("invalid_session")
    validate_history(session["history"], history_mode)
    session["history_mode"] = history_mode
    session["reference_delivery"] = reference_delivery
    session["request_byte_budget"] = max_request_bytes
    session["output_token_budget"] = output_token_budget
    session_id = session["session_id"]
    history = session["history"]
    supplied: dict = {}
    scanned = 0

    def _refresh_supplied() -> None:
        """Recover the Japanese references already present in the conversation history."""
        nonlocal scanned
        while scanned < len(history):
            message = history[scanned]
            scanned += 1
            if (not isinstance(message,dict) or set(message)!={'role','content'}
                    or message.get('role') not in {'user','assistant'}):
                raise ValueError('invalid_session')
            validate_history([message], history_mode)
            if message['role'] != 'user':
                continue
            content = message.get("content")
            try:
                payload = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                raise ValueError('invalid_session') from None
            if not isinstance(payload, dict):
                raise ValueError('invalid_session')
            refs=payload.get('japanese_reference',[])
            if not isinstance(refs,list):
                raise ValueError('invalid_session')
            for reference in refs:
                if not isinstance(reference,dict) or not isinstance(reference.get('id'),str) or not reference['id']:
                    raise ValueError('invalid_session')
                identifier=reference['id'];reference_sha=digest(reference)
                if identifier in supplied and supplied[identifier]!=reference_sha:
                    raise ValueError('conflicting_japanese_reference')
                supplied[identifier]=reference_sha

    _refresh_supplied()
    reference_hashes = {}
    for item in candidates:
        for reference in item['item']['japanese']:
            jid, reference_sha = reference['id'], digest(reference)
            if jid in reference_hashes and reference_hashes[jid] != reference_sha:
                raise ValueError("conflicting_japanese_reference")
            reference_hashes[jid] = reference_sha
    for jid, ref_sha in reference_hashes.items():
        if jid in supplied and supplied[jid] != ref_sha:
            raise ValueError("conflicting_japanese_reference")
    output.mkdir(parents=True, exist_ok=True)
    ledger = {"schema": _REVIEW_SCHEMA, "database_sha256": baseline,
              "source_artifact_sha256": artifact_sha256, "zh_edition_id": zh,
              "ja_edition_id": ja, "book_key": loaded["book_key"],
              "producer_requested_model": loaded["producer_requested"],
              "producer_served_model": loaded["producer_served"],
              "reviewer_requested_model": args.model, "session_id": session_id,
              "calls": [], "source_order": source_order, "reference_delivery": reference_delivery,
              "request_byte_budget": max_request_bytes, "next_request_bytes": None,
              "output_token_budget": output_token_budget, "history_mode": history_mode,
              "corpus_updated": False}
    counts = {"attempted_calls": 0, "valid_calls": 0, "failed_calls": 0,
              "attempted_rows": 0, "valid_rows": 0, "failed_rows": 0,
              "keep": 0, "reject": 0, "uncertain": 0, "cache_read_tokens": 0}
    served_model = args.model
    stopped = False
    consecutive_failures = 0
    stop_reason = 'range_or_budget_complete'
    number = 0
    offset = 0
    _atomic_write(output / "progress.json", ledger)
    while offset < len(selected) and not stopped:
        if counts["attempted_calls"] >= args.max_calls:
            break
        batch = selected[offset:offset + args.batch_size]
        payload = {"items": [{"review_id": item["review_id"], "chinese": item["item"]["chinese"],
                              "japanese_ids": item["japanese_ids"]} for item in batch]}
        _refresh_supplied()
        needed = ({ref['id']: ref for item in batch for ref in item['item']['japanese']}
                  if incremental_references else references)
        unsent = {jid: ref for jid, ref in needed.items() if jid not in supplied}
        if unsent:
            payload['japanese_reference'] = list(unsent.values())
        elif not history:
            payload['japanese_reference'] = []
        request_bytes = request_byte_count(args.model, payload, system=SYSTEM, history=history,
                                           session_id=session_id, **output_options)
        if max_request_bytes is not None and request_bytes > max_request_bytes:
            ledger['next_request_bytes'] = request_bytes
            stop_reason = 'request_byte_budget_exhausted'
            break
        input_sha = digest({"database": baseline, "zh_edition_id": zh, "ja_edition_id": ja,
                            "payload": payload, "history_sha256": digest(history)})
        record = {"number": number, "model": args.model,
                  "review_ids": [item["review_id"] for item in batch],
                  "input_sha256": input_sha, "history_sha256": digest(history),
                  "request_bytes": request_bytes,
                  "session_id": session_id, "status": "dispatched_outcome_unknown"}
        ledger["calls"].append(record)
        counts["attempted_calls"] += 1
        counts["attempted_rows"] += len(batch)
        _atomic_write(output / "progress.json", ledger)
        response = None
        try:
            response = request_model(endpoint, token, args.model, payload,
                                     system=SYSTEM, history=history, session_id=session_id, **output_options)
            if history_mode != "text" and safe_counts(response.get("content_shape")):
                record["content_shape"] = safe_counts(response["content_shape"])
            usage = _numeric_usage(response.get("usage"))
            record["usage"] = usage
            counts["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
            reply = response["payload"]
            try:
                reviews = validate_review(reply, [item["review_id"] for item in batch])
            except (ValueError, KeyError, TypeError):
                try:
                    reply_shape = _reply_diagnostics(reply, record["review_ids"])
                    if reply_shape is not None:
                        record["reply_shape"] = reply_shape
                except Exception:
                    pass
                raise
            if not _source_unchanged(database, baseline):
                record.update(status="failed", error_class="SourceDatabaseChanged")
                counts["failed_calls"] += 1
                counts["failed_rows"] += len(batch)
                stopped = True
                stop_reason = 'source_changed'
                _atomic_write(output / "progress.json", ledger)
                break
            by_id = {item["review_id"]: item for item in batch}
            out_rows = []
            for review in reviews:
                item = by_id[review["review_id"]]
                out_rows.append({"review_id": item["review_id"],
                                 "zh_edition_id": item["zh_edition_id"],
                                 "ja_edition_id": item["ja_edition_id"],
                                 "chinese_id": item["chinese_id"],
                                 "japanese_ids": item["japanese_ids"],
                                 "chinese_text_sha256": item["chinese_text_sha256"],
                                 "japanese_hashes": item["japanese_hashes"],
                                 "decision": review["decision"], "basis": review["basis"],
                                 "requires_review": 1, "accepted": 0, "human_confirmed": 0,
                                 "semantic_verified": 0, "reference_eligible": 0,
                                 "ocr_verified": 0})
            served_model = response.get("served_model", args.model)
            result = {"schema": _REVIEW_SCHEMA, "database_sha256": baseline,
                      "input_sha256": input_sha,
                      "source_artifact_sha256": artifact_sha256,
                      "producer_requested_model": loaded["producer_requested"],
                      "producer_served_model": loaded["producer_served"],
                      "reviewer_requested_model": args.model, "reviewer_served_model": served_model,
                      "session_id": session_id, "number": number,
                      "start": prefix + offset, "rows": out_rows}
            _atomic_write(output / f"review-{number:03d}.json", result)
            for row in out_rows:
                counts[row["decision"]] += 1
            counts["valid_calls"] += 1
            counts["valid_rows"] += len(out_rows)
            record.update(status="valid_model_review", rows=len(out_rows), usage=usage,
                          counts={k: sum(r["decision"] == k for r in out_rows)
                                  for k in ("keep", "reject", "uncertain")})
            consecutive_failures = 0
        except (ValueError, KeyError, TypeError, OSError) as exc:
            failure = failure_metadata(exc)
            if isinstance(exc, ValueError) and str(exc) in _REPLY_CODES:
                failure["error_code"] = str(exc)
            if response is None:
                counts["cache_read_tokens"] += _numeric_usage(failure.get("usage")).get("cache_read_input_tokens", 0)
            record.update(status="failed", **failure)
            counts["failed_calls"] += 1
            counts["failed_rows"] += len(batch)
            consecutive_failures += 1
            if isinstance(exc, urllib.error.HTTPError) and (exc.code in {400, 401, 403, 404, 413, 429} or is_redirect_rejection(exc)):
                record["http_status"] = exc.code
                stopped = True
                stop_reason = 'request_rejected'
            elif is_unsupported_response_content(exc):
                stopped = True
                stop_reason = 'unsupported_response_content'
            elif consecutive_failures >= 3:
                stopped = True
                stop_reason = 'consecutive_invalid_responses'
        _atomic_write(output / "progress.json", ledger)
        number += 1
        if not stopped:
            offset += len(batch)

    source_unchanged = _source_unchanged(database, baseline)
    _atomic_write(output / "progress.json", ledger)
    summary = {"schema": _REVIEW_SCHEMA, "database_sha256": baseline,
               "source_artifact_sha256": artifact_sha256, "zh_edition_id": zh,
               "ja_edition_id": ja, "book_key": loaded["book_key"],
               "producer_requested_model": loaded["producer_requested"],
               "producer_served_model": loaded["producer_served"],
               "reviewer_requested_model": args.model, "reviewer_served_model": served_model,
               "session_id": session_id, "source_order": source_order, "reference_delivery": reference_delivery,
               "request_byte_budget": max_request_bytes, "next_request_bytes": ledger['next_request_bytes'],
               "output_token_budget": output_token_budget, "history_mode": history_mode,
               "candidate_rows_total": total, "skipped_unmatched_rows": loaded["skipped"],
               "skipped_prefix_rows": prefix,
               "skipped_suffix_rows":total-end_bound,"selected_range_rows":selected_range_rows,
               "eligible_rows":len(selected),"excluded_reviewed_rows":excluded_reviewed_rows,
               "attempted_calls": counts["attempted_calls"],
               "valid_calls": counts["valid_calls"], "failed_calls": counts["failed_calls"],
               "attempted_rows": counts["attempted_rows"], "valid_rows": counts["valid_rows"],
               "failed_rows": counts["failed_rows"],
               "unattempted_rows": len(selected) - counts["attempted_rows"],
               "remaining": total - counts["valid_rows"] - excluded_reviewed_rows,
               "stop_reason": stop_reason,
               "keep": counts["keep"], "reject": counts["reject"],
               "uncertain": counts["uncertain"],
               "cache_read_input_tokens": counts["cache_read_tokens"],
               "source_unchanged": source_unchanged, "corpus_updated": False,
               "finished_at": datetime.now(timezone.utc).isoformat()}
    _atomic_write(output / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-calls", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end",type=int)
    parser.add_argument("--exclude-reviewed",type=Path)
    parser.add_argument("--source-order", action="store_true")
    parser.add_argument("--incremental-references", action="store_true")
    parser.add_argument("--max-request-bytes", type=int)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--history-mode", choices=("text", "preserve-thinking"), default="text")
    parser.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args(argv)
    if not (1 <= args.max_calls <= 100 and 1 <= args.batch_size <= 12 and args.start >= 0):
        parser.exit(1, "invalid dispatch bounds\n")
    try:
        summary = run(args)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        code = (str(exc) if isinstance(exc, ValueError) and str(exc) in _CODES
                else type(exc).__name__)
        parser.exit(1, "alignment review refused: " + code + "\n")
    print(json.dumps(summary, ensure_ascii=True))
    return 0 if summary["source_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
