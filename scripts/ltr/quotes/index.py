"""Build and read a private, source-bound candidate sidecar index.

The source corpus is always read-only. The disposable index contains candidate
spans and machine-review observations; human labels live separately.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from ltr.quotes.batch import digest, unique_source_span_start
from ltr.library.review_queue import fingerprint

SCHEMA = "corpus-candidate-index-v1"
FLAGS = {"requires_review": 1, "human_confirmed": 0, "semantic_verified": 0,
         "accepted": 0, "reference_eligible": 0}
KINDS = {"spoken", "thought", "written", "uncertain"}
DECISIONS = {"keep", "reject", "uncertain"}


def candidate_id(edition_id: str, row: dict, source: str) -> str:
    return digest({"edition_id": edition_id, "paragraph_id": row["paragraph_id"],
                   "start": row["start"], "end": row["end"], "source": source})[:16]


def _stable_source(database: Path):
    if not database.is_file() or any(Path(str(database) + suffix).exists()
                                   for suffix in ("-wal", "-journal")):
        raise ValueError("stable_source_required")


def load_candidates(con: sqlite3.Connection, directory: Path, source: str) -> list[dict]:
    candidates = []
    seen = set()
    for file in sorted(directory.rglob("batch-*.json")):
        raw = file.read_bytes()
        data = json.loads(raw)
        if data.get("schema") != "unquoted-dialogue-candidates-v1" or data.get("database_sha256") != source:
            raise ValueError("candidate_source_mismatch")
        edition = data["edition_id"]
        edition_rows = con.execute("SELECT id FROM editions WHERE id=?", (edition,)).fetchall()
        if len(edition_rows) != 1:
            raise ValueError("candidate_edition_mismatch")
        for row in data["candidates"]:
            if any(type(row.get(k)) is not int or row[k] != v for k, v in FLAGS.items()):
                raise ValueError("candidate_flags_invalid")
            paragraphs = con.execute(
                "SELECT ordinal,text FROM paragraphs WHERE edition_id=? AND id=?",
                (edition, row["paragraph_id"])).fetchall()
            if len(paragraphs) != 1:
                raise ValueError("candidate_paragraph_ambiguous")
            paragraph = paragraphs[0]
            text = paragraph["text"]
            start, end = row["start"], row["end"]
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                    or text[start:end] != row["text"] or unique_source_span_start(text, row["text"]) != start
                    or paragraph["ordinal"] != row["ordinal"]
                    or hashlib.sha256(text.encode()).hexdigest() != row["paragraph_sha256"]):
                raise ValueError("candidate_span_mismatch")
            if row["kind"] not in KINDS:
                raise ValueError("candidate_kind_invalid")
            for field in ("speaker", "addressee"):
                if row[field] is not None and (not isinstance(row[field], str) or len(row[field]) > 80):
                    raise ValueError("candidate_identity_invalid")
            identity = candidate_id(edition, row, source)
            if identity in seen:
                raise ValueError("duplicate_candidate_identity")
            seen.add(identity)
            item = dict(row, id=identity, edition_id=edition, model=data["model"],
                        served_model=data.get("served_model"),
                        artifact_sha256=hashlib.sha256(raw).hexdigest())
            item["payload_sha256"] = digest({k: item[k] for k in (
                "id", "edition_id", "paragraph_id", "ordinal", "start", "end", "text",
                "speaker", "addressee", "kind", "paragraph_sha256", "model")})
            candidates.append(item)
    return candidates


def load_reviews(directory: Path | None, source: str, known: set[str]) -> dict[str, list[dict]]:
    reviews = defaultdict(list)
    seen = set()
    if directory is None:
        return reviews
    for file in sorted(directory.rglob("review-*.json")):
        raw = file.read_bytes()
        data = json.loads(raw)
        if data.get("schema") != "unquoted-candidate-model-review-v1":
            continue
        if data.get("database_sha256") != source:
            raise ValueError("review_source_mismatch")
        for row in data["reviews"]:
            identity = str(row["candidate_id"])[:16]
            if identity not in known:
                raise ValueError("review_unknown_candidate")
            if row["decision"] not in DECISIONS or row["kind"] not in KINDS:
                raise ValueError("review_contract_invalid")
            if any(row.get(flag) != 0 for flag in ("accepted", "human_confirmed", "semantic_verified", "reference_eligible")):
                raise ValueError("review_flags_invalid")
            observation = {"model": data["model"], "decision": row["decision"],
                           "basis": row["basis"], "kind": row["kind"],
                           "artifact_sha256": hashlib.sha256(raw).hexdigest()}
            key = (identity, digest(observation))
            if key not in seen:
                seen.add(key)
                reviews[identity].append(observation)
    return reviews


@contextmanager
def read_index(path: Path, source: str):
    if not path.is_file():
        raise ValueError("candidate_index_missing")
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        metadata = dict(con.execute("SELECT key,value FROM metadata"))
        if metadata.get("schema") != SCHEMA or metadata.get("source_sha256") != source:
            raise ValueError("candidate_index_stale")
        yield con
    finally:
        con.close()


def build(database: Path, directory: Path, output: Path, reviews: Path | None = None,
          *, refresh: bool = False) -> dict:
    database = database.resolve(strict=True)
    output = output.resolve()
    if output == database or database.parent in output.parents:
        raise ValueError("index_output_must_be_external")
    _stable_source(database)
    source = fingerprint(database)
    if output.exists():
        if not refresh or output.samefile(database):
            raise ValueError("index_output_exists")
        with read_index(output, source):
            pass
    con = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        candidates = load_candidates(con, directory, source)
    finally:
        con.close()
    if not candidates:
        raise ValueError("candidate_index_empty")
    observations = load_reviews(reviews, source, {r["id"] for r in candidates})
    counts = Counter()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".candidate-index-", suffix=".sqlite", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    destination = sqlite3.connect(temporary)
    try:
        destination.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE candidates(
                id TEXT PRIMARY KEY, edition_id TEXT NOT NULL, paragraph_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
                text TEXT NOT NULL, speaker TEXT, addressee TEXT, kind TEXT NOT NULL,
                model TEXT NOT NULL, served_model TEXT, paragraph_sha256 TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, artifact_sha256 TEXT NOT NULL,
                model_review_status TEXT NOT NULL, observations_json TEXT NOT NULL,
                requires_review INTEGER NOT NULL CHECK(requires_review=1),
                human_confirmed INTEGER NOT NULL CHECK(human_confirmed=0),
                semantic_verified INTEGER NOT NULL CHECK(semantic_verified=0),
                accepted INTEGER NOT NULL CHECK(accepted=0),
                reference_eligible INTEGER NOT NULL CHECK(reference_eligible=0));
            CREATE INDEX candidates_edition ON candidates(edition_id,ordinal);
        """)
        for row in candidates:
            machine_reviews = observations.get(row["id"], [])
            decisions = {r["decision"] for r in machine_reviews}
            status = "unreviewed" if not decisions else next(iter(decisions)) if len(decisions) == 1 else "conflict"
            counts[status] += 1
            row.update(model_review_status=status, observations_json=json.dumps(machine_reviews, ensure_ascii=False))
            columns = [r[1] for r in destination.execute("PRAGMA table_info(candidates)")]
            destination.execute("INSERT INTO candidates VALUES (" + ",".join("?" for _ in columns) + ")",
                                [row[c] for c in columns])
        metadata = {"schema": SCHEMA, "source_sha256": source,
                    "created_at": datetime.now(timezone.utc).isoformat()}
        destination.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
        destination.commit()
        if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("candidate_index_integrity_failed")
        destination.close()
        _stable_source(database)
        if fingerprint(database) != source:
            raise ValueError("source_changed_during_indexing")
        os.replace(temporary, output)
    finally:
        destination.close()
        if temporary.exists():
            temporary.unlink()
    return {"schema": SCHEMA, "source_sha256": source, "total": len(candidates),
            "reviewed": len(candidates) - counts["unreviewed"], "review_status": dict(counts),
            "source_unchanged": True, "corpus_updated": False}


def summary(con: sqlite3.Connection) -> dict:
    counts = dict(con.execute("SELECT model_review_status,COUNT(*) FROM candidates GROUP BY model_review_status"))
    total = sum(counts.values())
    return {"total": total, "reviewed": total - counts.get("unreviewed", 0), "review_status": counts}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = build(args.db, args.input, args.out, args.reviews, refresh=args.refresh)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
        parser.exit(1, "candidate index refused: source, review or output validation failed\n")
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
