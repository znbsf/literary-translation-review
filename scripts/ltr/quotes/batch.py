"""Bounded private candidate extraction. Never modifies a corpus database.

Source text exists only in memory, authorized model payloads and private candidate
outputs. Console and ledger report identifiers, hashes, counts and error classes.
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
import urllib.request
import uuid

from ltr.response_history import resolve_history_mode, validate_history, assistant_content, safe_counts, TOP_LEVEL_FIELDS
from ltr.library.review_queue import fingerprint


SYSTEM = """你是私有文学语料整理员。输入是未经引号标注的中文段落。
逐段找出直接说话、人物心语、书面引用的原样片段；不要把一般叙述当成说话。
只处理 targets 内段落，context 仅帮助理解。允许没有候选，禁止为了覆盖率编造。
输出唯一 JSON 对象 {"candidates":[{"paragraph_id":"输入ID","text":"原文连续子串",
"speaker":null,"addressee":null,"kind":"spoken|thought|written|uncertain"}]}。
speaker/addressee 必须有上下文依据，否则 null。不要添加说明、代码围栏或额外字段。
text 必须逐字照录且在该段落唯一出现；保留标点、空格和换行，不改写或纠错。
所有输出只是待复核候选，不是人工确认结果。"""


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def unique_source_span_start(source: str, text: str) -> int | None:
    """Return the sole exact start, checking overlapping occurrences too."""
    if not isinstance(source, str) or not isinstance(text, str) or not text:
        return None
    start = source.find(text)
    if start < 0 or source.find(text, start + 1) != -1:
        return None
    return start


def validate(payload: object, targets: list[dict]) -> list[dict]:
    if not isinstance(payload, dict) or set(payload) != {"candidates"}:
        raise ValueError("invalid_candidate_contract")
    rows = payload["candidates"]
    if not isinstance(rows, list) or len(rows) > 200:
        raise ValueError("invalid_candidate_contract")
    sources = {r["id"]: r for r in targets}
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"paragraph_id", "text", "speaker", "addressee", "kind"}:
            raise ValueError("invalid_candidate_contract")
        source = sources.get(row["paragraph_id"])
        text = row["text"]
        if source is None or not isinstance(text, str) or not 1 <= len(text) <= 2000:
            raise ValueError("invalid_source_binding")
        start = unique_source_span_start(source["text"], text)
        if start is None:
            raise ValueError("non_unique_or_changed_source_span")
        if row["kind"] not in {"spoken", "thought", "written", "uncertain"}:
            raise ValueError("invalid_candidate_kind")
        for field in ("speaker", "addressee"):
            if row[field] is not None and (not isinstance(row[field], str) or len(row[field]) > 80):
                raise ValueError("invalid_identity")
        key = (source["id"], start, start + len(text))
        if key in seen:
            raise ValueError("duplicate_span")
        seen.add(key)
        result.append(dict(row, ordinal=source["ordinal"], start=start, end=start + len(text),
                           paragraph_sha256=hashlib.sha256(source["text"].encode("utf-8")).hexdigest(),
                           requires_review=1, human_confirmed=0, semantic_verified=0,
                           accepted=0, reference_eligible=0))
    return result


class ModelResponseError(ValueError):
    """Source-free response diagnostics; never retain response text."""
    def __init__(self, code, *, text_length=None, usage=None, content_shape=None):
        super().__init__(code)
        self.code = code
        self.text_length = text_length
        self.content_shape = safe_counts(content_shape)
        self.usage = {k: v for k, v in (usage or {}).items()
                      if k in {'input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'}
                      and type(v) is int and v >= 0} if isinstance(usage, dict) else {}


def failure_metadata(exc):
    result = {'error_class': type(exc).__name__}
    safe = getattr(exc, '_corpus_request_metadata', None)
    if isinstance(safe, dict):
        metrics = safe.get('request_metrics', {})
        result['request_metrics'] = {k:v for k,v in metrics.items()
            if k in {'request_bytes','history_messages','input_messages'}
            and type(v) is int and v >= 0} if isinstance(metrics,dict) else {}
        if safe.get('provider_error_hint') in {
                'context_limit','output_budget','model_unavailable','rate_limit','quota','unspecified'}:
            result['provider_error_hint'] = safe['provider_error_hint']
    if is_redirect_rejection(exc):
        result['error_code'] = 'http_redirect_rejected'
    if isinstance(exc, ModelResponseError):
        result.update(error_code=exc.code, usage=exc.usage)
        if exc.content_shape: result['content_shape'] = exc.content_shape
        if exc.text_length is not None: result['response_chars'] = exc.text_length
    return result


def _http_error_hint(exc):
    """Inspect a bounded error envelope in memory; return only a static hint."""
    try:
        raw = exc.read(8193)
        if len(raw) > 8192:
            return 'unspecified'
        data = json.loads(raw)
        error = data.get('error',data) if isinstance(data,dict) else None
        if not isinstance(error,dict):
            return 'unspecified'
        message = error.get('message','')
        message = message.lower() if isinstance(message,str) else ''
        code = error.get('code',error.get('type',''))
        if code in {'context_length_exceeded','max_context_length_exceeded'} or any(
                p in message for p in ('context length','context window','prompt is too long',
                                       'too many input tokens','input length exceeds')):
            return 'context_limit'
        if code in {'model_not_found','model_not_supported'} or (
                'model' in message and any(p in message for p in ('not supported','not found','does not exist'))):
            return 'model_unavailable'
        if code=='insufficient_quota' or ('quota' in message and any(
                p in message for p in ('exceeded','exhausted'))):
            return 'quota'
        if code=='rate_limit_error' or 'rate limit' in message:
            return 'rate_limit'
        if 'max_tokens' in message and any(p in message for p in ('limit','maximum','range')):
            return 'output_budget'
    except (ValueError,TypeError,OSError,AttributeError,RecursionError):
        pass
    return 'unspecified'


def parse_model_json(text):
    """Accept a JSON document or one exact Markdown JSON envelope, nothing else."""
    value = text.strip()
    lines = value.splitlines()
    if len(lines) >= 3 and lines[0] in ('```json', '```') and lines[-1] == '```':
        value = '\n'.join(lines[1:-1])
    return json.loads(value)


def _request_message(payload):
    return {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}


def resolve_output_token_budget(max_output_tokens=None, history_mode="text") -> int:
    """Resolve an explicit output ceiling without guessing provider capacity."""
    if max_output_tokens is None:
        return 8000
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 131072:
        raise ValueError("invalid_output_token_budget")
    return max_output_tokens


def encode_request(model: str, payload: dict, *, system: str = SYSTEM,
                   history: list | None = None, session_id: str | None = None,
                   max_output_tokens=None, history_mode="text") -> bytes:
    """Pure HTTP body encoder; callers must never log or persist the result."""
    output_token_budget = resolve_output_token_budget(max_output_tokens)
    resolve_history_mode(history_mode)
    validate_history(history if history is not None else [], history_mode)
    message = _request_message(payload)
    body = {"model": model, "max_tokens": output_token_budget, "system": system,
            "messages": [*(history or []), message]}
    if session_id:
        body["metadata"] = {"user_id": json.dumps({"device_id": "corpus-runtime", "account_uuid": "", "session_id": session_id})}
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def request_byte_count(model: str, payload: dict, *, system: str = SYSTEM,
                       history: list | None = None, session_id: str | None = None,
                       max_output_tokens=None, history_mode="text") -> int:
    """Count exactly the encoded HTTP body, including history and metadata."""
    return len(encode_request(model, payload, system=system, history=history, session_id=session_id,
                              max_output_tokens=max_output_tokens, history_mode=history_mode))


def is_redirect_rejection(exc):
    return isinstance(exc, urllib.error.HTTPError) and type(exc.code) is int and 300 <= exc.code < 400


def is_unsupported_response_content(exc):
    return isinstance(exc, ModelResponseError) and exc.code == "unsupported_response_content"


def _assistant_content(data, history_mode):
    if TOP_LEVEL_FIELDS.intersection(data):
        raise ModelResponseError("unsupported_response_content", usage=data.get("usage"))
    try:
        return assistant_content(data.get("content"), history_mode)
    except ValueError:
        raise ModelResponseError("unsupported_response_content", usage=data.get("usage")) from None


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        response.close()
        raise urllib.error.HTTPError(request.full_url, code, "redirect_rejected", headers, None)


def request_model(endpoint: str, token: str, model: str, payload: dict, *, system: str = SYSTEM,
                  history: list | None = None, session_id: str | None = None,
                  max_output_tokens=None, history_mode="text") -> dict:
    encoded = encode_request(model, payload, system=system, history=history, session_id=session_id,
                             max_output_tokens=max_output_tokens, history_mode=history_mode)
    message = _request_message(payload)
    history_messages = len(history or [])
    headers = {"Content-Type": "application/json", "x-api-key": token,
               "anthropic-version": "2023-06-01"}
    if session_id:
        headers["X-Session-Id"] = session_id
    request = urllib.request.Request(endpoint.rstrip("/") + "/v1/messages",
        data=encoded,
        headers=headers)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects()).open(request, timeout=120) as response:
            try:
                data = json.load(response)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ModelResponseError('invalid_transport_json') from None
    except OSError as exc:
        safe = {'request_metrics': {'request_bytes':len(request.data),
                'history_messages':history_messages,'input_messages':history_messages + 1}}
        if isinstance(exc,urllib.error.HTTPError):
            safe['provider_error_hint'] = _http_error_hint(exc)
        exc._corpus_request_metadata = safe
        raise
    if not isinstance(data, dict):
        raise ModelResponseError('invalid_transport_contract')
    text, assistant, counts = _assistant_content(data, history_mode)
    shape = counts if history_mode == "preserve-thinking" else None
    if data.get("stop_reason") != "end_turn":
        raise ModelResponseError('incomplete_model_response', usage=data.get('usage'), content_shape=shape)
    if history is not None:
        history.extend([message, {"role": "assistant", "content": assistant}])
    try:
        parsed = parse_model_json(text)
    except json.JSONDecodeError:
        raise ModelResponseError('invalid_model_json', text_length=len(text), usage=data.get('usage'), content_shape=shape) from None
    result = {"payload": parsed, "usage": data.get("usage", {}), "served_model": data.get("model", model)}
    if shape is not None:
        result["content_shape"] = shape
    return result


def run(args) -> dict:
    database = args.db.resolve(strict=True)
    output = args.out.resolve()
    if output == database.parent or database.parent in output.parents:
        raise ValueError("candidate_output_must_be_external")
    if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-journal")):
        raise ValueError("unstable_database_snapshot")
    baseline = fingerprint(database)
    con = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        if con.execute("SELECT COUNT(*) FROM dialogues WHERE edition_id=?", (args.edition,)).fetchone()[0]:
            raise ValueError("edition_already_has_extracted_dialogues")
        paragraphs = [dict(r) for r in con.execute(
            "SELECT id,ordinal,text FROM paragraphs WHERE edition_id=? ORDER BY ordinal", (args.edition,))]
    finally:
        con.close()
    if not paragraphs:
        raise ValueError("empty_edition")
    endpoint = os.environ.get("ANTHROPIC_BASE_URL", "")
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if not endpoint or not token:
        raise ValueError("missing_runtime_authentication")
    output.mkdir(parents=True, exist_ok=True)
    ledger = {"schema": "unquoted-dialogue-candidate-run-v1", "edition_id": args.edition,
              "database_sha256": baseline, "paragraphs_total": len(paragraphs), "calls": [],
              "batches": [], "corpus_updated": False}
    def save_progress():
        temporary = output / "progress.tmp"
        temporary.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, output / "progress.json")
    call_count = 0
    histories = {model: [] for model in args.models}
    sessions = {model: str(uuid.uuid4()) for model in args.models}
    ledger["sessions"] = sessions
    ledger["conversation_mode"] = "serial_per_model_in_memory_no_prompt_log"
    starts = args.batch_starts if args.batch_starts is not None else list(range(args.start, min(len(paragraphs), args.start + args.batch_size * args.max_batches), args.batch_size))
    if len(starts) > args.max_batches or len(set(starts)) != len(starts) or any(start < 0 or start >= len(paragraphs) for start in starts):
        raise ValueError("invalid_dispatch_scope")
    ledger["batch_starts"] = starts
    for start in starts:
        end = min(start + args.batch_size, len(paragraphs))
        targets = paragraphs[start:end]
        payload = {"targets": targets, "context": paragraphs[max(0,start-2):start] + paragraphs[end:end+2]}
        input_sha = digest({"database": baseline, "edition": args.edition, "payload": payload,
                            "system": hashlib.sha256(SYSTEM.encode()).hexdigest()})
        destination = output / f"batch-{start:05d}-{end-1:05d}.json"
        if destination.exists():
            previous = json.loads(destination.read_text(encoding="utf-8"))
            if previous.get("input_sha256") != input_sha:
                raise ValueError("existing_batch_binding_mismatch")
            ledger["batches"].append({"start": start, "end": end, "status": "existing_bound_batch",
                                       "candidates": len(previous["candidates"])})
            continue
        success = False
        for model in args.models:
            if call_count >= args.max_calls:
                break
            call_count += 1
            record = {"model": model, "start": start, "end": end, "input_sha256": input_sha,
                      "status": "dispatched_outcome_unknown", "session_id": sessions[model],
                      "history_sha256": digest(histories[model])}
            record["request_sha256"] = digest({"input_sha256": input_sha, "model": model,
                                               "history_sha256": record["history_sha256"]})
            ledger["calls"].append(record)
            save_progress()
            try:
                response = request_model(endpoint, token, model, payload,
                                         history=histories[model], session_id=sessions[model])
                candidates = validate(response["payload"], targets)
                if fingerprint(database) != baseline:
                    raise ValueError("source_database_changed")
                result = {"schema": "unquoted-dialogue-candidates-v1", "edition_id": args.edition,
                          "database_sha256": baseline, "input_sha256": input_sha,
                          "start": start, "end": end, "model": model, "served_model": response["served_model"],
                          "candidates": candidates, "requires_review": 1, "accepted": 0,
                          "human_confirmed": 0, "semantic_verified": 0, "reference_eligible": 0}
                with destination.open("x", encoding="utf-8") as file:
                    json.dump(result, file, ensure_ascii=False, indent=2)
                record.update(status="valid_candidates", candidates=len(candidates), usage=response["usage"])
                success = True
            except (ValueError, KeyError, TypeError, OSError) as exc:
                record.update(status="failed", **failure_metadata(exc))
                if isinstance(exc, urllib.error.HTTPError):
                    record["http_status"] = exc.code
                if is_redirect_rejection(exc):
                    ledger["stop_reason"] = "request_rejected"
                elif is_unsupported_response_content(exc):
                    ledger["stop_reason"] = "unsupported_response_content"
            if success or ledger.get("stop_reason") in {"request_rejected", "unsupported_response_content"}:
                break
        ledger["batches"].append({"start": start, "end": end, "status": "valid" if success else "unresolved"})
        # Preserve finite dispatch accounting after every batch; no prompts or source text.
        if ledger.get("stop_reason") in {"request_rejected", "unsupported_response_content"}:
            ledger["unattempted_batch_starts"] = starts[len(ledger["batches"]):]
        save_progress()
        if call_count >= args.max_calls or ledger.get("stop_reason") in {"request_rejected", "unsupported_response_content"}:
            break
    ledger["finished_at"] = datetime.now(timezone.utc).isoformat()
    ledger["source_unchanged"] = fingerprint(database) == baseline
    (output / f"summary-{args.start:05d}.json").write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    result = {"paragraphs_total": len(paragraphs), "batches": ledger["batches"],
            "calls": call_count, "valid_calls": sum(r['status']=='valid_candidates' for r in ledger['calls']),
            "candidates": sum(r.get('candidates',0) for r in ledger['calls']),
            "source_unchanged": ledger["source_unchanged"], "corpus_updated": False}
    if ledger.get("stop_reason") in {"request_rejected", "unsupported_response_content"}:
        result.update(stop_reason=ledger["stop_reason"], failed_calls=sum(r['status'] == 'failed' for r in ledger['calls']),
                      unattempted_batch_starts=ledger["unattempted_batch_starts"])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--edition", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--batch-size", type=int, default=24)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--max-batches", type=int, default=4)
    parser.add_argument("--max-calls", type=int, default=8)
    parser.add_argument("--batch-starts", type=int, nargs="+")
    parser.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args(argv)
    if not (1 <= args.batch_size <= 32 and args.start >= 0 and 1 <= args.max_batches <= 100 and 1 <= args.max_calls <= 100):
        parser.exit(1, "invalid dispatch bounds\n")
    try:
        summary = run(args)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
        parser.exit(1, "candidate run refused: input, binding or runtime check failed\n")
    print(json.dumps(summary, ensure_ascii=True))
    return 0 if summary['source_unchanged'] else 1


if __name__ == "__main__":
    raise SystemExit(main())
