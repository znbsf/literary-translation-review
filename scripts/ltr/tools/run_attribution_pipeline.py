# -*- coding: utf-8 -*-
"""Drive steps 2-4 of the attribution pipeline for one or more runs.

Per run directory: merge+validate the identity batches, derive the referent
vocabulary from the merged identities, run the deterministic basis detector, and
generate a provisional alias map plus a human-readable review list.

The alias map it produces is PROVISIONAL: cross-volume canonical names and narrator
nicknames are reviewed decisions and are applied afterwards, by hand, into a
`*-reviewed.json` map (see docs/ATTRIBUTION-PIPELINE.md step 4).

The expected candidate count per edition is read from the database rather than
passed in, so it cannot drift from reality.

Usage:
  python run_attribution_pipeline.py --db DB --run RUNDIR:EDITION [--run RUNDIR:EDITION ...]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent


def run(cmd: list[str], label: str) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    ok = r.returncode == 0
    print("  [%s] %s" % ("OK " if ok else "FAIL", label))
    for line in (r.stdout or "").strip().splitlines():
        print("       " + line)
    if not ok:
        for line in (r.stderr or "").strip().splitlines():
            print("       ! " + line)
        raise SystemExit(f"{label} failed")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--run", action="append", required=True, help="run_dir:edition_id")
    a = ap.parse_args()

    con = sqlite3.connect(f"file:{Path(a.db).as_posix()}?mode=ro", uri=True)
    counts = {e: con.execute(
        "select count(*) from dialogues where edition_id=?", (e,)).fetchone()[0]
        for _, e in (s.rsplit(":", 1) for s in a.run)}
    con.close()

    report = []
    for spec in a.run:
        run_dir, edition = spec.rsplit(":", 1)
        run_dir = Path(run_dir)
        expected = counts[edition]
        print("\n== %s (edition %s, expected %d)" % (run_dir.name, edition, expected))

        run([sys.executable, str(TOOLS / "merge_identity_batches.py"),
             str(run_dir), a.db, edition, str(expected)],
            "merge + contract validation")

        merged = json.loads((run_dir / "merged-identity.json").read_text(encoding="utf-8"))
        names = sorted({r["speaker_candidate"] for r in merged if r.get("speaker_candidate")})
        vocab = run_dir / "referent-vocabulary.json"
        vocab.write_text(json.dumps(names, ensure_ascii=False, indent=1), encoding="utf-8")
        print("  [OK ] referent vocabulary: %d forms" % len(names))

        run([sys.executable, str(TOOLS / "basis_detector_v2.py"),
             a.db, edition, str(vocab), str(run_dir / "basis-detector-v2.json")],
            "deterministic basis detector")

        # determinism assertion: rerun and compare bytes
        import hashlib
        p = run_dir / "basis-detector-v2.json"
        first = hashlib.sha256(p.read_bytes()).hexdigest()
        repeated = subprocess.run([sys.executable, str(TOOLS / "basis_detector_v2.py"),
                                   a.db, edition, str(vocab), str(p)],
                                  capture_output=True, text=True, encoding="utf-8")
        if repeated.returncode != 0:
            raise SystemExit("basis detector determinism rerun failed")
        same = hashlib.sha256(p.read_bytes()).hexdigest() == first
        print("  [%s] detector determinism (byte-identical rerun)" % ("OK " if same else "FAIL"))
        if not same:
            raise SystemExit("basis detector is not deterministic")

        tag = run_dir.name.replace("identity-run-20261018-", "")
        run([sys.executable, str(TOOLS / "build_alias_map.py"), str(run_dir),
             str(run_dir / f"alias-map-{tag}.json"), "--tag", tag, "--force"],
            "provisional alias map")

        summary = json.loads((run_dir / "identity-summary.json").read_text(encoding="utf-8"))
        det = json.loads(p.read_text(encoding="utf-8"))["counts"]
        report.append({
            "run": run_dir.name, "edition": edition,
            "candidates": summary["attempted"],
            "resolved_speaker": summary["resolved_speaker"],
            "unresolved_speaker": summary["unresolved_speaker"],
            "note_leaks": summary["note_leaks_against_corpus"],
            "distinct_speaker_surface": summary["distinct_speaker_surface"],
            "basis": {k: det[k] for k in ("literal_named", "pronoun_named", "inferred")},
            "provisional_alias_map": f"alias-map-{tag}.json",
        })

    print("\n== 汇总")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    for spec, row in zip(a.run, report):
        d = Path(spec.rsplit(":", 1)[0])
        (d / "pipeline-summary.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\nwrote pipeline-summary.json into each run directory")
    return 0


if __name__ == "__main__":
    sys.exit(main())
