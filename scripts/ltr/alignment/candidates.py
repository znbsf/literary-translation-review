"""Private zh->ja OCR dialogue alignment candidates. Never mutates a corpus DB.

Source text lives only in memory, the authorized model payload and private
candidate outputs. Console, progress ledgers and errors carry identifiers, hashes
and error classes only. Candidates are never human-verified alignments.
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

from ltr.quotes.batch import is_unsupported_response_content, is_redirect_rejection, digest, request_model, failure_metadata
from ltr.library.review_queue import fingerprint


SYSTEM = """你是私有文学语料的对话对齐助手。reference 给出日语 OCR 对话清单，含
精确 id、ordinal、text 与 ocr_status（一律为 unverified）。chinese 给出待对齐的中文
对话。请仅为每个中文条目挑选日语对话 id，依据语义与顺序，但 OCR 文本可能残缺或错误，
上下文不足时不得强行匹配，应输出 unmatched。禁止改写、翻译或编造任何原文。
只处理 chinese 内条目，reference 仅作参照。输出唯一 JSON 对象
{"alignments":[{"chinese_id":"给定中文id","japanese_ids":["给定日文id"],"status":"candidate|unmatched","confidence":"low|medium|high"}]}。
chinese_id 必须来自 chinese；japanese_ids 只能选 reference 内的 id，最多 5 个且不重复；
status=candidate 时日文列表非空，status=unmatched 时列表为空。不要添加字段或说明。
所有输出只是待复核候选，不是人工确认结果。"""

_STATUS = {"candidate", "unmatched"}
_CONFIDENCE = {"low", "medium", "high"}
_FIELDS = {"chinese_id", "japanese_ids", "status", "confidence"}


def validate_alignment(payload: object, zh_ids: list[str], ja_ids: list[str]) -> list[dict]:
    """Validate one model alignment reply against exact batch and edition ids."""
    if not isinstance(payload, dict) or set(payload) != {"alignments"}:
        raise ValueError("invalid_alignment_contract")
    rows = payload["alignments"]
    if not isinstance(rows, list):
        raise ValueError("invalid_alignment_contract")
    if not isinstance(zh_ids, list) or len(set(zh_ids)) != len(zh_ids):
        raise ValueError("duplicate_chinese_target_id")
    ja_set = set(ja_ids)
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != _FIELDS:
            raise ValueError("invalid_alignment_contract")
        cid, jids = row["chinese_id"], row["japanese_ids"]
        if not isinstance(cid, str) or cid not in zh_ids or cid in seen:
            raise ValueError("invalid_chinese_reference")
        if not isinstance(jids, list) or any(not isinstance(j, str) for j in jids):
            raise ValueError("invalid_japanese_reference")
        if len(jids) > 5 or len(set(jids)) != len(jids) or any(j not in ja_set for j in jids):
            raise ValueError("invalid_japanese_reference")
        if row["status"] not in _STATUS or row["confidence"] not in _CONFIDENCE:
            raise ValueError("invalid_alignment_enum")
        if (row["status"] == "candidate") != bool(jids):
            raise ValueError("status_reference_mismatch")
        seen.add(cid)
        result.append({"chinese_id": cid, "japanese_ids": list(jids),
                       "status": row["status"], "confidence": row["confidence"],
                       "requires_review": 1, "accepted": 0, "human_confirmed": 0,
                       "semantic_verified": 0, "reference_eligible": 0, "ocr_verified": 0})
    if seen != set(zh_ids):
        raise ValueError("incomplete_alignment_coverage")
    return result


def _atomic_write(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _edition(con: sqlite3.Connection, edition_id: str, language: str) -> dict:
    rows = con.execute(
        "SELECT id,title,language,book_key FROM editions WHERE id=?", (edition_id,)).fetchall()
    if len(rows) != 1:
        raise ValueError("ambiguous_or_missing_edition_id")
    row = dict(rows[0])
    if row["language"] != language:
        raise ValueError("edition_language_mismatch")
    return row


def _dialogues(con: sqlite3.Connection, edition_id: str) -> list[dict]:
    rows = [dict(r) for r in con.execute(
        "SELECT id,ordinal,text FROM dialogues WHERE edition_id=? ORDER BY ordinal,id", (edition_id,))]
    if not rows:
        raise ValueError("empty_edition")
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("ambiguous_dialogue_id")
    return rows


def _source_unchanged(database: Path, baseline: str) -> bool:
    return (not any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-journal"))
            and fingerprint(database) == baseline)


def run(args) -> dict:
    if not (args.execute is True and type(args.max_calls) is int and 1 <= args.max_calls <= 100
            and type(args.batch_size) is int and 1 <= args.batch_size <= 12
            and type(args.start) is int and args.start >= 0):
        raise ValueError("invalid_dispatch_bounds")
    database = args.db.resolve(strict=True)
    if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-journal")):
        raise ValueError("unstable_database_snapshot")
    if args.zh_edition == args.ja_edition:
        raise ValueError("editions_must_be_distinct")
    output = args.out.resolve()
    if output.exists():
        raise ValueError("alignment_output_exists")
    if output == database.parent or database.parent in output.parents:
        raise ValueError("alignment_output_must_be_external")
    baseline = fingerprint(database)
    con = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        zh_edition = _edition(con, args.zh_edition, "zh")
        ja_edition = _edition(con, args.ja_edition, "ja")
        book_key = zh_edition["book_key"]
        if not isinstance(book_key, str) or not book_key or book_key != ja_edition["book_key"]:
            raise ValueError("book_key_mismatch")
        zh_rows = _dialogues(con, args.zh_edition)
        ja_rows = _dialogues(con, args.ja_edition)
        for row in [*zh_rows, *ja_rows]:
            if con.execute("SELECT COUNT(*) FROM dialogues WHERE id=?", (row["id"],)).fetchone()[0] != 1:
                raise ValueError("ambiguous_dialogue_id")
        completed = set()
        if getattr(args, 'exclude_completed', None):
            from ltr.alignment.index import load_proposals
            existing = load_proposals(con, args.exclude_completed.resolve(strict=True), baseline)
            if any(r['edition_id'] != args.zh_edition or r['ja_edition_id'] != args.ja_edition for r in existing):
                raise ValueError('completed_edition_mismatch')
            completed = {r['chinese_id'] for r in existing}
    finally:
        con.close()
    if not _source_unchanged(database, baseline):
        raise ValueError("source_database_changed")

    endpoint = os.environ.get("ANTHROPIC_BASE_URL", "")
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if not endpoint or not token:
        raise ValueError("missing_runtime_authentication")

    ja_text = {r["id"]: r["text"] for r in ja_rows}
    ja_ids = list(ja_text)
    reference = [{"id": r["id"], "ordinal": r["ordinal"], "text": r["text"],
                  "ocr_status": "unverified"} for r in ja_rows]
    session_id = str(uuid.uuid4())

    output.mkdir(parents=True, exist_ok=True)
    ledger = {"schema": "haruhi-alignment-candidates-v1", "database_sha256": baseline,
              "zh_edition_id": args.zh_edition, "ja_edition_id": args.ja_edition,
              "book_key": book_key, "requested_model": args.model, "session_id": session_id,
              "calls": [], "corpus_updated": False}
    counts = {"total": len(zh_rows), "attempted": 0, "valid": 0, "matched": 0,
              "unmatched": 0, "failed": 0, "covered": 0, "attempted_rows": 0, "cache_read_tokens": 0}
    history: list = []
    stopped = False
    consecutive_failures = 0
    stop_reason = 'range_or_budget_complete'
    eligible = [r for r in zh_rows[args.start:] if r['id'] not in completed]
    excluded = len(zh_rows[args.start:]) - len(eligible)
    start = 0
    while start < len(eligible) and not stopped:
        if counts["attempted"] >= args.max_calls:
            break
        end = min(start + args.batch_size, len(eligible))
        batch = eligible[start:end]
        batch_ids = [r["id"] for r in batch]
        chinese = [{"id": r["id"], "ordinal": r["ordinal"], "text": r["text"]} for r in batch]
        payload = {"chinese": chinese} if history else {"reference": reference, "chinese": chinese}
        input_sha = digest({"database": baseline, "zh": args.zh_edition, "ja": args.ja_edition,
                            "book_key": book_key, "payload": payload})
        record = {"start": start + args.start, "end": end + args.start, "input_sha256": input_sha,
                  "target_ids": batch_ids,
                  "history_sha256": digest(history), "status": "dispatched_outcome_unknown"}
        ledger["calls"].append(record)
        counts["attempted"] += 1
        counts["attempted_rows"] += len(batch)
        _atomic_write(output / "progress.json", ledger)
        try:
            response = request_model(endpoint, token, args.model, payload,
                                     system=SYSTEM, history=history, session_id=session_id)
            rows = validate_alignment(response["payload"], batch_ids, ja_ids)
            if not _source_unchanged(database, baseline):
                record.update(status="failed", error_class="SourceDatabaseChanged")
                counts["failed"] += 1
                stopped = True
                stop_reason = 'source_changed'
                _atomic_write(output / "progress.json", ledger)
                break
            for row in rows:
                original = next(r for r in batch if r["id"] == row["chinese_id"])
                row["chinese_text_sha256"] = hashlib.sha256(original["text"].encode()).hexdigest()
                row["japanese"] = [{"id": j, "text_sha256": hashlib.sha256(ja_text[j].encode()).hexdigest()} for j in row["japanese_ids"]]
            result = {"schema": "haruhi-alignment-candidates-v1", "database_sha256": baseline,
                      "zh_edition_id": args.zh_edition, "ja_edition_id": args.ja_edition,
                      "book_key": book_key, "requested_model": args.model,
                      "served_model": response.get("served_model", args.model),
                      "input_sha256": input_sha, "session_id": session_id, "rows": rows}
            _atomic_write(output / f"align-{start+args.start:05d}-{end+args.start-1:05d}.json", result)
            matched = sum(r["status"] == "candidate" for r in rows)
            unmatched = sum(r["status"] == "unmatched" for r in rows)
            counts["matched"] += matched
            counts["unmatched"] += unmatched
            counts["covered"] += len(batch)
            counts["valid"] += 1
            usage = response.get("usage") or {}
            if isinstance(usage, dict):
                counts["cache_read_tokens"] += int(usage.get("cache_read_input_tokens", 0) or 0)
            record.update(status="valid_alignments", rows=len(rows), matched=matched,
                          unmatched=unmatched, usage=usage)
            consecutive_failures = 0
        except (ValueError, KeyError, TypeError, OSError) as exc:
            record.update(status="failed", **failure_metadata(exc))
            counts["failed"] += 1
            if is_unsupported_response_content(exc):
                counts["cache_read_tokens"] += exc.usage.get("cache_read_input_tokens", 0)
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
        if not stopped:
            # Advance past the batch; no automatic retry, budget stays bounded.
            start = end

    source_unchanged = _source_unchanged(database, baseline)
    summary = {"schema": "haruhi-alignment-candidates-v1", "database_sha256": baseline,
               "zh_edition_id": args.zh_edition, "ja_edition_id": args.ja_edition,
               "book_key": book_key, "requested_model": args.model, "session_id": session_id,
               "total": counts["total"], "attempted": counts["attempted"],
               "valid": counts["valid"], "matched": counts["matched"],
               "unmatched": counts["unmatched"], "failed": counts["failed"],
               "attempted_calls": counts["attempted"], "valid_calls": counts["valid"],
               "failed_calls": counts["failed"], "attempted_rows": counts["attempted_rows"],
               "valid_rows": counts["covered"], "failed_rows": counts["attempted_rows"] - counts["covered"],
               "unattempted_rows": len(eligible) - counts["attempted_rows"],
               "skipped_prefix_rows": min(args.start, counts["total"]),
               "skipped_completed_rows": excluded,
               "remaining": counts["total"] - counts["covered"] - excluded,
               "stop_reason": stop_reason,
               "cache_read_tokens": counts["cache_read_tokens"],
               "source_unchanged": source_unchanged, "corpus_updated": False,
               "finished_at": datetime.now(timezone.utc).isoformat()}
    _atomic_write(output / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--zh-edition", required=True)
    parser.add_argument("--ja-edition", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-calls", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--exclude-completed", type=Path)
    parser.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args(argv)
    if not (1 <= args.max_calls <= 100 and 1 <= args.batch_size <= 12 and args.start >= 0):
        parser.exit(1, "invalid dispatch bounds\n")
    try:
        summary = run(args)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
        parser.exit(1, "alignment run refused: input, binding or runtime check failed\n")
    print(json.dumps(summary, ensure_ascii=True))
    return 0 if summary["source_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
