# -*- coding: utf-8 -*-
"""Build a REVIEWED alias map for one volume from the series registry.

Combines, in priority order:
  1. the series registry's character variants (cross-volume canonical names);
  2. the registry's explicit resolutions for ambiguous short forms;
  3. the provisional map's mechanical containment collapses, for forms the registry
     does not cover;
and refuses to guess: anything not covered stays as its own form and is listed for
review, with group/plural references called out separately.

Usage:
  python build_reviewed_alias_map.py <provisional-map> <registry> <out> --tag <vol>
"""
from __future__ import annotations

import argparse
import json
import unicodedata
from pathlib import Path

GROUP_MARKERS = ("各位", "众人", "大家", "你们", "他们", "团员", "其他", "两人", "三人",
                 "与", "、", "们")


def norm(s: str) -> str:
    """Same comparison form the provisional generator uses: NFKC + no whitespace."""
    return unicodedata.normalize("NFKC", s).replace(" ", "").strip()


def close(mapping: dict, where: str) -> tuple[dict, list]:
    """Transitive closure of an alias map.

    A map is applied with a single dict lookup, so a value that is itself a key would
    leave an intermediate, un-normalised name in the database (e.g. 实玖琉 -> 朝比奈实玖琉
    while 朝比奈实玖琉 -> 朝比奈实玖瑠). Follow every chain to its fixed point and report
    any cycle rather than silently truncating it.
    """
    out, cycles = {}, []
    for k in mapping:
        seen, cur = [k], mapping[k]
        while cur in mapping and cur not in seen:
            seen.append(cur)
            cur = mapping[cur]
        if cur in seen:
            cycles.append({"chain": seen + [cur]})
            continue
        out[k] = cur
    return out, cycles


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("provisional")
    ap.add_argument("registry")
    ap.add_argument("out")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    out = Path(a.out)
    if out.exists() and not a.force:
        raise SystemExit(f"{out} exists; pass --force (a reviewed map is evidence)")
    prov = json.loads(Path(a.provisional).read_text(encoding="utf-8"))
    reg = json.loads(Path(a.registry).read_text(encoding="utf-8"))
    counts = {x["form"]: x["count"] for x in prov["all_forms"]}
    observed = set(counts)

    mapping, from_registry, from_registry_amb, from_containment, from_norm = {}, [], [], [], []
    by_norm = {}
    for f in observed:
        by_norm.setdefault(norm(f), []).append(f)
    for e in reg["entries"]:
        for v in e["variants"]:
            for f in by_norm.get(norm(v), []):
                if norm(f) == norm(e["canonical"]):
                    continue  # this surface already IS the canonical form
                mapping[f] = e["canonical"]
                from_registry.append({"form": f, "matched_registry_variant": v,
                                      "canonical": e["canonical"], "count": counts[f]})
    canonicals = {e["canonical"] for e in reg["entries"]}
    canon_norms = {norm(c) for c in canonicals}
    for r in reg.get("ambiguous_resolutions", []):
        f_candidates = by_norm.get(norm(r["ambiguous_form"]), [])
        for f in f_candidates:
            target = r["resolves_to"]
            if not target:
                raise SystemExit(f"registry entry for {f!r} lacks a machine-readable "
                                 f"'resolves_to' (a prose 'resolution_note' is not a "
                                 f"mapping target)")
            mapping[f] = target
            from_registry_amb.append({"form": f, "canonical": target,
                                      "count": counts[f],
                                      "note": r.get("resolution_note", ""),
                                      "rationale": r.get("rationale", "")})

    for pair in prov.get("same_after_normalisation", []):
        f, target = pair["form"], pair["canonical"]
        if f in mapping or f not in observed:
            continue
        # The target may be a normalised form rather than an observed surface, so it
        # is deliberately not required to appear in the observed set.
        mapping[f] = target
        from_norm.append(pair)

    for pair in prov.get("containment_pairs", []):
        short, long_ = pair["short"], pair["long"]
        if short in mapping or short not in observed or long_ not in observed:
            continue
        mapping[short] = long_
        from_containment.append(pair)

    mapping, cycles = close(mapping, "reviewed")

    def is_group(f: str) -> bool:
        return any(m in f for m in GROUP_MARKERS)

    review, groups, already = [], [], []
    for f in sorted(counts, key=lambda x: -counts[x]):
        if f in mapping:
            continue
        if norm(f) in canon_norms:
            already.append({"form": f, "count": counts[f],
                            "why": "already a registry canonical name (modulo NFKC "
                                   "and whitespace); no mapping needed"})
            continue
        row = {"form": f, "count": counts[f]}
        if is_group(f):
            row["why"] = "group/plural reference -- must not resolve to one character"
            groups.append(row)
        else:
            row["why"] = "not covered by registry or containment -- kept as its own form"
            review.append(row)

    payload = {
        "schema": "haruhi-alias-map-reviewed-v1",
        "tag": a.tag,
        "edition_id": prov["edition_id"],
        "inherits": {"provisional": Path(a.provisional).name,
                     "registry": Path(a.registry).name},
        "narrator_canonical": reg["entries"][0]["canonical"],
        "map": mapping,
        "reviewed_decisions": {
            "from_series_registry": {
                "entries": from_registry,
                "why": "cross-volume canonical names; without these the same character "
                       "would carry a different speaker_canonical per volume",
            },
            "ambiguous_resolutions_applied": from_registry_amb,
            "from_normalisation": {
                "entries": from_norm,
                "why": ("forms identical after NFKC + whitespace removal (e.g. a name "
                        "written with and without a full-width space)"),
            },
            "from_mechanical_containment": from_containment,
        },
        "left_unchanged": review,
        "already_canonical": already,
        "group_references_not_resolved": groups,
        "transitive_closure": {
            "applied": True,
            "why": ("a mapped target may itself be mapped; without closure the database "
                    "would hold the intermediate name because lookup is single-level"),
            "cycles": cycles,
        },
        "forms_observed": len(observed),
        "forms_mapped": len(mapping),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"observed": len(observed), "mapped": len(mapping),
                      "from_registry": len(from_registry),
                      "ambiguous_resolved": len(from_registry_amb),
                      "from_normalisation": len(from_norm),
                      "from_containment": len(from_containment),
                      "already_canonical": len(already),
                      "left_unchanged": len(review),
                      "group_refs": len(groups),
                      "cycles": len(cycles)}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
