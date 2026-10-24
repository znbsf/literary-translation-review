# -*- coding: utf-8 -*-
"""Propose an alias map for one edition's identity run.

Automates what can be decided mechanically and *flags what cannot*:

  mechanical  if observed form A is a strict prefix of observed form B, map A -> B
              (longest observed form wins). This covers cases like a short surname
              and the full name both being used.
  pinned      first-person narrator forms are canonicalised to 我.
  review      everything else is passed through, and the full observed name list is
              emitted with counts so the main agent can spot a narrator nickname
              (a form used for the first-person narrator that is not a pronoun) --
              which is NOT mechanically decidable and must be a reviewed decision.

Writes alias-map-<tag>.json next to the run directory outputs. Refuses to overwrite
unless --force is passed, because a reviewed map is evidence.

Usage:
  python build_alias_map.py <run-dir> <alias-map-out> [--tag volNN] [--force]
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

NARRATOR = {"我", "我们", "叙述者", "主人公"}


def norm(s: str) -> str:
    """Comparison form for a name: NFKC (full-width digits/letters -> ASCII,
    ideographic space -> space) with all internal whitespace removed.

    Names in this corpus occur both spaced and unspaced (a full-width space between
    surname and given name) and with full-width digits, so raw equality and raw
    containment silently miss genuine equivalences.
    """
    return unicodedata.normalize("NFKC", s).replace(" ", "").strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Propose aliases for human review")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--tag", default="run")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    run_dir, out, force, tag = args.run_dir, args.out, args.force, args.tag
    if out.exists() and not force:
        raise SystemExit(f"{out} exists; pass --force to regenerate (a reviewed map is evidence)")

    summary = json.loads((run_dir / "identity-summary.json").read_text(encoding="utf-8"))
    # BOTH sides. Counting only speaker surfaces was a real gap: a form used solely
    # when addressing someone (阿虚, 实玖瑠, 三味, ...) never entered the map, so it was
    # never canonicalised and survived as its own canonical on the addressee side.
    spk = summary.get("speaker_surface_counts", {})
    addr = summary.get("addressee_surface_counts", {})
    counts = dict(spk)
    for f, n in addr.items():
        counts[f] = counts.get(f, 0) + n
    sides = {f: ("both" if f in spk and f in addr else "speaker" if f in spk else "addressee")
             for f in counts}
    forms = list(counts)

    mapping, pairs, ambiguous, same_norm = {}, [], [], []
    forms_by_norm = defaultdict(list)
    for f in forms:
        forms_by_norm[norm(f)].append(f)

    # 1) forms that differ only by whitespace/full-width width: same name.
    #    The canonical is the NORMALISED form (the map key), so `speaker_canonical`
    #    never carries a stray space or full-width digit, while `speaker_surface`
    #    keeps the original. Single-member groups need no mapping at all.
    primaries = []
    for key, members in forms_by_norm.items():
        primary = max(members, key=lambda m: (counts[m], len(m)))
        primaries.append(primary)
        if len(members) < 2:
            continue
        for m in members:
            if m == key:
                continue  # already the normalised form
            mapping[m] = key
            same_norm.append({"form": m, "canonical": key,
                              "form_count": counts[m],
                              "sibling_surfaces": [x for x in members if x != m],
                              "why": "identical to a sibling form after NFKC + whitespace removal"})

    # 2) containment on the comparison form, among the group primaries.
    pnorm = {p: norm(p) for p in primaries}
    for a in primaries:
        na = pnorm[a]
        if len(na) < 2:
            continue  # single characters are too weak a signal to merge on
        containers = [b for b in primaries if a != b and na in pnorm[b] and len(na) < len(pnorm[b])]
        if not containers:
            continue
        longest = max(len(pnorm[b]) for b in containers)
        winners = sorted(b for b in containers if len(pnorm[b]) == longest)
        if len(winners) == 1:
            mapping[a] = winners[0]
            pairs.append({"short": a, "long": winners[0],
                          "short_count": counts[a], "long_count": counts[winners[0]]})
        else:
            # More than one equally long candidate: refusing to guess is the point.
            ambiguous.append({"short": a, "candidates": winners,
                              "short_count": counts[a]})
    for f in forms:
        if f in NARRATOR:
            mapping[f] = "我"

    # Transitive closure: lookup at write time is a single dict get, so any value that
    # is itself a key would leave an intermediate name in the database.
    cycles = []
    closed = {}
    for k in mapping:
        seen, cur = [k], mapping[k]
        while cur in mapping and cur not in seen:
            seen.append(cur)
            cur = mapping[cur]
        if cur in seen:
            cycles.append(seen + [cur])
            continue
        closed[k] = cur
    mapping = closed

    review = [
        {"form": f, "count": counts[f], "sides": sides[f],
         "proposed": mapping.get(f, f),
         "note": ("narrator form pinned to 我" if f in NARRATOR else
                  "prefix-collapsed" if f in mapping else
                  "REVIEW: kept as-is -- confirm this is not a narrator nickname "
                  "and not an alias of another observed form")}
        for f in sorted(counts, key=lambda x: -counts[x])
    ]

    payload = {
        "schema": "haruhi-alias-map-v1",
        "edition_id": summary["edition_id"],
        "tag": tag,
        "basis": ("Mechanically proposes prefix collapses and pins narrator pronouns; "
                  "every remaining form is explicitly flagged REVIEW rather than "
                  "silently assumed to be a distinct character."),
        "narrator_canonical": "我",
        "map": mapping,
        "containment_pairs": pairs,
        "same_after_normalisation": same_norm,
        "ambiguous_not_merged": ambiguous,
        "alias_cycles_dropped": cycles,
        "normalisation": ("comparison uses NFKC plus whitespace removal, because names "
                          "occur both spaced and unspaced and with full-width digits; "
                          "surfaces themselves are never rewritten"),
        "forms_observed": len(forms),
        "review_required": [r for r in review if r["note"].startswith("REVIEW")],
        "all_forms": review,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"observed_forms": len(forms),
                      "containment_collapses": len(pairs),
                      "same_after_normalisation": len(same_norm),
                      "ambiguous_not_merged": len(ambiguous),
                      "narrator_pinned": sum(1 for f in forms if f in NARRATOR),
                      "review_required": len(payload["review_required"])},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
