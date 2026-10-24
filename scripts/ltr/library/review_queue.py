"""Create source-free, fingerprint-bound review queues without changing corpus DBs."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile


def fingerprint(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def queue_rows(connection: sqlite3.Connection):
    """Yield identifiers, statuses and bindings only; never select source prose."""
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    completion = {
        "corpus_completion_quote": ("quote_id", "quote_pending"),
        "corpus_completion_alignment": ("quote_id", "alignment_pending"),
        "corpus_completion_fixed_review_state": ("term_id", "fixed_term_pending"),
    }
    for table, (key, kind) in completion.items():
        if table not in tables:
            continue
        for row in connection.execute(
            f'SELECT "{key}", status FROM "{table}" WHERE requires_review=1 ORDER BY "{key}"'
        ):
            yield {"kind": kind, "item_id": row[0], "status": row[1]}
    if "attribution_candidates" in tables:
        for row in connection.execute(
            "SELECT dialogue_id, edition_id, requires_review, human_confirmed, "
            "semantic_verified, reference_eligible FROM attribution_candidates "
            "WHERE requires_review=1 OR human_confirmed=0 OR semantic_verified=0 "
            "ORDER BY edition_id,ordinal,dialogue_id"
        ):
            yield {"kind": "attribution_review", "item_id": row[0], "edition_id": row[1],
                   "status": "unverified_candidate", "human_confirmed": row[3],
                   "semantic_verified": row[4], "reference_eligible": row[5]}
    if "scan_pages" in tables:
        for row in connection.execute(
            "SELECT id,collection_id,ordinal,image_sha256,ocr_status FROM scan_pages "
            "WHERE ocr_status IS NULL OR ocr_status!='verified' ORDER BY collection_id,ordinal,id"
        ):
            yield {"kind": "ocr_review", "item_id": row[0], "collection_id": row[1],
                   "ordinal": row[2], "image_sha256": row[3], "status": row[4]}
    if {"editions", "paragraphs", "dialogues"} <= tables:
        for row in connection.execute(
            "SELECT e.id, (SELECT COUNT(*) FROM paragraphs p WHERE p.edition_id=e.id) "
            "FROM editions e WHERE e.language='zh' "
            "AND NOT EXISTS (SELECT 1 FROM dialogues d WHERE d.edition_id=e.id) "
            "AND EXISTS (SELECT 1 FROM paragraphs p WHERE p.edition_id=e.id) ORDER BY e.id"
        ):
            yield {"kind": "unquoted_edition_review", "item_id": row[0],
                   "paragraphs": row[1], "status": "extraction_gap_requires_source_review"}


def build(config_path: Path, output: Path) -> dict:
    config_path = config_path.resolve(strict=True)
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    output = output.resolve()
    if output.exists():
        raise ValueError("output already exists; choose a fresh candidate directory")
    corpora = config.get("corpora")
    if not isinstance(corpora, list) or not corpora:
        raise ValueError("configured corpora are required")
    prepared = []
    seen = set()
    for spec in corpora:
        corpus_id = spec["id"]
        if (not isinstance(corpus_id, str) or not corpus_id or
                any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in corpus_id)
                or corpus_id in seen):
            raise ValueError("invalid or duplicate corpus identifier")
        seen.add(corpus_id)
        root = Path(spec["path"])
        if not root.is_absolute():
            root = config_path.parent / root
        root = root.resolve(strict=True)
        database = (root / "dialogue.sqlite").resolve(strict=True)
        # Keep generated evidence outside each source tree, including snapshots/backups.
        if output == root or root in output.parents:
            raise ValueError("review output must be outside the source corpus")
        if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-journal")):
            raise ValueError("active SQLite sidecar present; obtain a stable read-only snapshot first")
        prepared.append((corpus_id, database, fingerprint(database)))
    summary = {"schema": "corpus-review-queue-v1",
               "generated_at": datetime.now(timezone.utc).isoformat(),
               "corpus_updated": False, "corpora": []}
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".review-queue-", dir=output.parent))
    try:
        for corpus_id, database, baseline in prepared:
            counts = Counter()
            con = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            try:
                con.execute("PRAGMA query_only=ON")
                with (staging / (corpus_id + ".jsonl")).open("w", encoding="utf-8", newline="\n") as out:
                    for item in queue_rows(con):
                        counts[item["kind"]] += 1
                        item.update(corpus=corpus_id, corpus_sha256=baseline, accepted=0)
                        out.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            finally:
                con.close()
            if (fingerprint(database) != baseline or
                    any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-journal"))):
                raise ValueError("source database changed during queue generation")
            summary["corpora"].append({"id": corpus_id, "corpus_sha256": baseline,
                                        "counts": dict(sorted(counts.items())),
                                        "queue": corpus_id + ".jsonl"})
        (staging / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # A source processed first may change while the next corpus is read.
        # Bind the whole published set again immediately before publication.
        for _, database, baseline in prepared:
            if (fingerprint(database) != baseline or
                    any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-journal"))):
                raise ValueError("source database changed before queue publication")
        # A directory move exposes the complete, bound queue together, with no partial success.
        os.rename(staging, output)
    except BaseException:
        import shutil
        shutil.rmtree(staging)
        raise
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        summary = build(args.config, args.out)
    except (ValueError, KeyError, OSError, sqlite3.Error, TypeError):
        parser.exit(1, "review queue refused: configuration, snapshot or output check failed\n")
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
