# -*- coding: utf-8 -*-
"""Validate and merge identity batch slices for one edition.

Contract (fails loudly, never silently repairs):
  - every slice parses, carries the expected schema tag and batch bounds
  - candidate count per slice == end - start + 1
  - no duplicate dialogue_id across slices
  - every dialogue_id belongs to the edition
  - merged total == expected total
  - confidence_uncalibrated within [0, 1]
  - no note contains a >=6 char CJK run that literally occurs in the corpus

Writes merged-identity.json and identity-summary.json into the run directory.
Read-only with respect to the database.

Usage:
  python merge_identity_batches.py <run-dir> <db> <edition-id> <expected-total>
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path

try:  # Support both direct script execution and package-style test imports.
    from ltr.tools.corpus_overlap import corpus_blob, has_corpus_overlap
except ImportError:  # pragma: no cover - used by ``python path/to/script.py``
    from corpus_overlap import corpus_blob, has_corpus_overlap

FIELDS = {"dialogue_id", "speaker_candidate", "addressee_candidate",
          "confidence_uncalibrated", "note"}
MIN_LEAK_RUN = 6


def fail(msg: str) -> None:
    raise SystemExit(f"CONTRACT VIOLATION: {msg}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate and merge identity candidates")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("db", type=Path)
    parser.add_argument("edition")
    parser.add_argument("expected", type=int)
    args = parser.parse_args(argv)
    run_dir, db, edition, expected = args.run_dir, args.db, args.edition, args.expected
    slices = sorted(run_dir.glob("batch-*.json"))
    if not slices:
        fail(f"no batch-*.json under {run_dir}")

    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.execute("pragma query_only=ON")
    known = {r[0] for r in con.execute("select id from dialogues where edition_id=?", (edition,))}
    corpus = corpus_blob(t for (t,) in con.execute(
        "select text from paragraphs where edition_id=?", (edition,)))
    con.close()

    merged, seen, per_slice = [], set(), []
    for path in slices:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != "haruhi-identity-candidates-v1":
            fail(f"{path.name}: unexpected schema {data.get('schema')!r}")
        if data.get("edition_id") != edition:
            fail(f"{path.name}: edition mismatch {data.get('edition_id')!r}")
        start, end = data.get("batch_start"), data.get("batch_end")
        if (not isinstance(start, int) or isinstance(start, bool) or
                not isinstance(end, int) or isinstance(end, bool) or start < 0 or end < start):
            fail(f"{path.name}: invalid batch bounds")
        rows = data.get("candidates")
        if not isinstance(rows, list):
            fail(f"{path.name}: 'candidates' is not a list")
        if len(rows) != end - start + 1:
            fail(f"{path.name}: {len(rows)} candidates, expected {end - start + 1}")
        for row in rows:
            if not isinstance(row, dict):
                fail(f"{path.name}: candidate row is not an object")
            missing = FIELDS - set(row)
            if missing:
                fail(f"{path.name}: candidate missing {sorted(missing)}")
            # Whitelist, not blacklist. A batch that carries extra fields (e.g. a
            # dumped context_paragraphs list) leaks source text into the run
            # artifacts; only checking required fields present would miss it.
            extra = set(row) - FIELDS
            if extra:
                fail(f"{path.name}: candidate carries unexpected field(s) {sorted(extra)}")
            qid = row["dialogue_id"]
            if qid in seen:
                fail(f"{path.name}: duplicate dialogue_id {qid}")
            if qid not in known:
                fail(f"{path.name}: {qid} is not a dialogue of {edition}")
            seen.add(qid)
            conf = row["confidence_uncalibrated"]
            if not isinstance(conf, (int, float)) or not (0.0 <= conf <= 1.0):
                fail(f"{path.name}/{qid}: confidence out of range")
            if not isinstance(row["note"], str):
                fail(f"{path.name}/{qid}: note is not a string")
            if any(not isinstance(row[field], (str, type(None)))
                   for field in ("speaker_candidate", "addressee_candidate")):
                fail(f"{path.name}/{qid}: candidate surface has invalid type")
            if has_corpus_overlap(row["note"], corpus, MIN_LEAK_RUN):
                fail(f"{path.name}/{qid}: note matches corpus (1 item; threshold={MIN_LEAK_RUN})")
            merged.append(row)
        per_slice.append({"file": path.name, "start": start, "end": end})

    if len(merged) != expected:
        fail(f"merged {len(merged)} candidates, expected {expected}")

    names = collections.Counter(r["speaker_candidate"] for r in merged if r.get("speaker_candidate"))
    addressee = collections.Counter(r["addressee_candidate"] for r in merged if r.get("addressee_candidate"))
    summary = {
        "schema": "haruhi-identity-run-summary-v1",
        "edition_id": edition,
        "slices_count": len(per_slice),
        "attempted": len(merged),
        "resolved_speaker": sum(1 for r in merged if r.get("speaker_candidate")),
        "unresolved_speaker": sum(1 for r in merged if not r.get("speaker_candidate")),
        "resolved_addressee": sum(1 for r in merged if r.get("addressee_candidate")),
        "distinct_speaker_surface": len(names),
        "speaker_surface_counts": dict(names.most_common()),
        "distinct_addressee_surface": len(addressee),
        # NOT truncated: low-frequency addressee-only forms (e.g. a surname used once
        # when addressing someone) are exactly the ones that get missed if this list
        # is cut short, and they still need canonicalising.
        "addressee_surface_counts": dict(addressee.most_common()),
        "note_leaks_against_corpus": 0,
        "note_leak_items": [],
        "contract_checks": "all passed",
        "scope_note": ("Identity candidates only. No basis label is produced here: the audit "
                       "showed basis labels are not reproducible between raters, so the basis "
                       "is computed separately by corpus/tools/basis_detector_v2.py. No human "
                       "review has occurred; nothing here is reference-eligible."),
    }
    (run_dir / "merged-identity.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    (run_dir / "identity-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in
                      ("attempted", "resolved_speaker", "unresolved_speaker",
                       "resolved_addressee", "distinct_speaker_surface",
                       "note_leaks_against_corpus")}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
