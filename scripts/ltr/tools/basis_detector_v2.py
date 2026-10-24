# -*- coding: utf-8 -*-
"""Deterministic speaker-basis detector v2 (referent-anchored).

v1 tried to harvest referent forms by requiring adjacency to a speech verb and
found only 3 usable forms for the whole volume: in this prose an attribution
typically reads "NAME + adverbial modifier + speech verb" (e.g. a name followed
by an adverbial phrase and then the verb), so the name is NOT adjacent to the
verb. That observation is itself a finding — it explains why rubric N1
("name + speech verb in the same or adjacent paragraph") was so hard to apply
consistently.

v2 takes machine identity candidates as the referent vocabulary. Earlier
model agreement was observed on a limited sample only; it does not establish
full-corpus identity accuracy. The detector uses those names and asks a purely mechanical
question: does that referent, or the first-person narrator, appear shortly before
a speech verb in the quote's own or an adjacent paragraph?

Classes:
  literal_named  an in-volume character name precedes a speech verb within LIMIT chars
  pronoun_named  the first-person narrator (我) does
  inferred       neither does

Everything is a pure function of (database content, name list). Run twice → identical bytes.
Read-only; no database writes.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

STRONG_VERBS = ("说道", "问道", "答道", "应道", "喊道", "叫道", "念道", "叹道",
                "笑道", "骂道", "低语", "嘀咕", "嘟囔", "回答", "补充", "插嘴",
                "开口", "自语", "反问", "催促", "解释道", "开口道", "接着道",
                "继续说道", "低声说", "说", "问", "答", "道", "喊", "叫")
SENTENCE_END = "。！？…\n」』\""
LIMIT = 12
FIRST_PERSON = "我"


def build(paragraphs: dict[int, str], names: list[str]):
    pats = [(re.compile("(" + re.escape(n) + ")"), n, "literal_named") for n in names]
    pats.append((re.compile("(" + FIRST_PERSON + ")"), FIRST_PERSON, "pronoun_named"))
    return pats


def detect(dialogue: dict, paragraphs: dict[int, str], pats, distances: list[int]) -> dict:
    quote = dialogue["text"] or ""
    lo = max(0, min(dialogue["start_para"], dialogue["end_para"]) - 1)
    hi = max(dialogue["start_para"], dialogue["end_para"]) + 1
    rank = {"inferred": 0, "pronoun_named": 1, "literal_named": 2}
    best, evidence = None, []
    for ordinal in range(lo, hi + 1):
        raw = paragraphs.get(ordinal)
        if raw is None:
            continue
        text = raw.replace(quote, "") if quote else raw
        for pat, referent, cls in pats:
            for m in pat.finditer(text):
                tail = text[m.end():m.end() + LIMIT]
                cut = min((tail.find(c) for c in SENTENCE_END if tail.find(c) != -1), default=len(tail))
                tail = tail[:cut]
                verb = next((v for v in STRONG_VERBS if v in tail), None)
                if verb is None:
                    continue
                dist = tail.find(verb) + len(verb)
                distances.append(dist)
                evidence.append({"ordinal": ordinal, "referent": referent, "verb": verb,
                                 "distance_chars": dist,
                                 "same_paragraph_as_quote": ordinal == dialogue["start_para"]})
                if best is None or rank[cls] > rank[best]:
                    best = cls
    basis = best or "inferred"
    if basis == "inferred":
        evidence = []
    return {"basis_detector": basis, "evidence": evidence}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only referent-basis detection")
    parser.add_argument("db", type=Path)
    parser.add_argument("edition")
    parser.add_argument("names_file", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    db, edition, names_file, out = args.db, args.edition, args.names_file, args.out
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("pragma query_only=ON")
    paragraphs = {r["ordinal"]: r["text"] for r in con.execute(
        "select ordinal, text from paragraphs where edition_id=?", (edition,))}
    dialogues = [dict(r) for r in con.execute(
        "select id, ordinal, text, start_para, end_para from dialogues "
        "where edition_id=? order by ordinal", (edition,))]

    raw_names = json.loads(names_file.read_text(encoding="utf-8"))
    names = sorted({n for n in raw_names if n and n not in ("我", "叙述者")})
    pats = build(paragraphs, names)
    distances: list[int] = []
    results, dist = [], {"literal_named": 0, "pronoun_named": 0, "inferred": 0}
    for d in dialogues:
        r = detect(d, paragraphs, pats, distances)
        dist[r["basis_detector"]] += 1
        results.append({"dialogue_id": d["id"], "ordinal": d["ordinal"], **r})

    hist = {}
    for d in distances:
        hist[d] = hist.get(d, 0) + 1
    payload = {
        "schema": "haruhi-basis-detector-v2",
        "edition_id": edition,
        "determinism": "pure function of (database content, referent list); no model, no randomness",
        "referent_vocabulary_size": len(names),
        "referent_vocabulary": names,
        "distance_limit_chars": LIMIT,
        "counts": {"total": len(results), **dist},
        "referent_to_verb_distance_histogram": dict(sorted(hist.items())),
        "note": ("Evidence stores only the referent, the matched verb, the paragraph ordinal "
                 "and a character distance; all from closed vocabularies. No source sentence "
                 "is stored. The matched verbs are function words, not prose."),
        "results": results,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"total": len(results), **dist,
                      "referents": len(names),
                      "max_observed_adverb_gap_chars": (max(hist) if hist else 0)},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
