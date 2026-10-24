"""Generate a read-only CORPUS-MANIFEST.json for a corpus directory.

Contract:
- Opens dialogue.sqlite strictly read-only; never writes to it.
- Records only identifiers, counts, sizes and hashes. Never copies text.
- Refuses to overwrite an existing manifest unless --force is given.

Usage:
    python corpus_manifest.py <corpus-dir> [--force]
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

# Tables worth reporting per corpus family; missing ones are simply skipped.
KEY_TABLES = (
    "editions", "paragraphs", "dialogues", "entries", "alignments",
    "chapter_candidates", "edition_pair_candidates", "scan_pages",
    "scan_collections", "ocr_page_review", "ocr_reading_order_candidates",
    "ocr_recovery_candidates", "ocr_recovery_fragments",
    "corpus_completion_quote", "corpus_completion_alignment",
    "corpus_completion_fixed_term", "corpus_completion_runs",
    "attribution_candidates",
)

REFERENCE_FLAG_TABLES = (
    "entries", "corpus_completion_quote", "corpus_completion_alignment",
    "corpus_completion_fixed_term", "attribution_candidates",
)


def sha256_file(path: Path, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def table_counts(con: sqlite3.Connection) -> dict[str, int]:
    names = {r[0] for r in con.execute(
        "select name from sqlite_master where type='table'")}
    return {t: con.execute(f'select count(*) from "{t}"').fetchone()[0]
            for t in KEY_TABLES if t in names}


def reference_flag_state(con: sqlite3.Connection) -> dict[str, dict[str, int]]:
    names = {r[0] for r in con.execute(
        "select name from sqlite_master where type='table'")}
    state: dict[str, dict[str, int]] = {}
    for table in REFERENCE_FLAG_TABLES:
        if table not in names:
            continue
        cols = {r[1] for r in con.execute(f'pragma table_info("{table}")')}
        if "reference_eligible" not in cols:
            continue
        total, flagged = con.execute(
            f'select count(*), coalesce(sum(reference_eligible),0) from "{table}"'
        ).fetchone()
        state[table] = {"rows": total, "reference_eligible": flagged}
    return state


def backups(corpus: Path) -> list[dict[str, object]]:
    found = []
    for pattern in ("private/backups/*.sqlite", "backups/*.sqlite"):
        for p in sorted(corpus.glob(pattern)):
            found.append({"path": p.relative_to(corpus).as_posix(),
                          "bytes": p.stat().st_size,
                          "sha256": sha256_file(p)})
    return found


def latest_summaries(corpus: Path, limit: int = 6) -> list[dict[str, object]]:
    items = sorted(corpus.glob("completion-summary-*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out = []
    for p in items:
        try:
            data = json.loads(p.read_text("utf-8-sig"))
        except (OSError, ValueError):
            continue
        out.append({
            "file": p.name,
            "status": data.get("status") or data.get("stage"),
            "checked_at_utc": data.get("checked_at_utc") or data.get("observed_at"),
            "remaining": data.get("remaining"),
            "master_sha256": data.get("master_sha256"),
        })
    return out


def build(corpus: Path) -> dict[str, object]:
    db = corpus / "dialogue.sqlite"
    if not db.is_file():
        raise SystemExit(f"no dialogue.sqlite under {corpus}")
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.execute("pragma query_only=ON")
    try:
        integrity = con.execute("pragma quick_check").fetchone()[0]
        manifest: dict[str, object] = {
            "schema": "lightn-corpus-manifest-v1",
            "generated_at_utc": datetime.datetime.now(
                datetime.timezone.utc).isoformat(),
            "corpus_directory": corpus.name,
            "database": {
                "file": "dialogue.sqlite",
                "bytes": db.stat().st_size,
                "sha256": sha256_file(db),
                "quick_check": integrity,
            },
            "table_counts": table_counts(con),
            "reference_eligible_state": reference_flag_state(con),
            "backups": backups(corpus),
            "recent_completion_summaries": latest_summaries(corpus),
            "scope_note": (
                "Read-only inventory. No manuscript, OCR or dialogue text is "
                "included. Counts describe machine candidates only; they are "
                "not evidence of human review, literary quality or "
                "reference eligibility."
            ),
        }
    finally:
        con.close()
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus_dir")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    corpus = Path(args.corpus_dir).resolve()
    target = corpus / "CORPUS-MANIFEST.json"
    if target.exists() and not args.force:
        raise SystemExit(f"{target} already exists; pass --force to regenerate")
    manifest = build(corpus)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
