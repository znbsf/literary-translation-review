#!/usr/bin/env python3
"""Validate a single-source work-understanding artifact without emitting text."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

DIMS = set("events characters relationships chronology causality knowledge narrative world_rules foreshadowing continuity themes space_objects versions".split())
REL_TYPES = set("causes enables prevents precedes contradicts supports foreshadows resolves knows believes relationship_change variant_of".split())
KINDS = {"fact", "character_statement", "interpretation"}
FLAGS = ("accepted", "semantic_verified", "reference_eligible", "ocr_verified", "human_verified", "human_confirmed")
STATES = {"partial", "not_started", "candidate_covered", "not_applicable"}

def validate(data, root, manifest=None):
    errors = []
    def err(code): errors.append(code)
    def required(obj, keys, code):
        if not isinstance(obj, dict) or any(k not in obj for k in keys):
            err(code); return False
        return True
    if not required(data, ("schema", "work_id", "source", "reading", "records", "relations", "coverage", "questions"), "top_fields"):
        return errors, {}
    if data["schema"] != "work-understanding-records-v1": err("schema")
    src = data["source"]
    lines = []
    if required(src, ("edition_id", "sha256", "path", "language", "locator_kind"), "source_fields"):
        if src["locator_kind"] != "source_txt_line_1based_inclusive": err("locator_kind")
        try:
            base = Path(root).resolve(strict=True)
            path = (base / src["path"]).resolve(strict=True)
            if not path.is_relative_to(base) or not path.is_file(): raise ValueError()
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != src["sha256"]: err("source_hash")
            lines = raw.decode("utf-8-sig").splitlines()
        except (OSError, ValueError, TypeError, UnicodeError): err("source_read")
    if manifest is not None:
        try:
            m = json.loads(Path(manifest).read_text(encoding="utf-8"))
            entries = m if isinstance(m, list) else m.get("sources", [])
            matches = [x for x in entries if x.get("path") == src.get("path") and x.get("sha256") == src.get("sha256") and x.get("edition_id") == src.get("edition_id") and x.get("language") == src.get("language") and x.get("work_id") == data.get("work_id")]
            if not matches: err("manifest_source")
            elif "line_count" in matches[0] and matches[0]["line_count"] != len(lines): err("manifest_line_count")
        except (OSError, ValueError, TypeError, AttributeError): err("manifest_read")
    def span(x, evidence=False):
        try:
            a,b = (x["start_line"],x["end_line"]) if evidence else x
            if type(a) is not int or type(b) is not int or a < 1 or b < a or b > len(lines): raise ValueError()
            if evidence and not any(line.strip() for line in lines[a-1:b]): raise ValueError()
            if evidence and "anchor" in x:
                anchor=x["anchor"]
                if not isinstance(anchor,str) or not anchor.strip() or anchor not in "\n".join(lines[a-1:b]):
                    err("evidence_anchor")
            return a,b
        except (TypeError, ValueError, KeyError): err("evidence_range" if evidence else "reading_range"); return None
    reading=data["reading"]
    ranges=[]
    if required(reading, ("machine_read_ranges", "human_confirmed", "limitations"), "reading_fields"):
        if reading["human_confirmed"] is not False: err("human_confirmation")
        if not isinstance(reading["machine_read_ranges"], list): err("reading_ranges")
        else:
            ranges=[r for x in reading["machine_read_ranges"] if (r := span(x))]
            ranges.sort()
            if any(a[1] >= b[0] for a,b in zip(ranges,ranges[1:])): err("reading_overlap")
    ids={}
    def evidence(obj):
        e=obj.get("evidence")
        if not isinstance(e,list) or not e: err("evidence_missing"); return False
        return all(span(x,True) is not None for x in e)
    records=data["records"]
    if not isinstance(records,list): err("records_type"); records=[]
    for rec in records:
        if not required(rec, ("id","dimension","kind","statement","evidence","entities","story_time","disclosed_at","uncertainty","status"), "record_fields"): continue
        key=rec["id"]
        if not isinstance(key,str) or not key or key in ids: err("duplicate_id")
        else: ids[key]=rec
        if rec["dimension"] not in DIMS: err("record_dimension")
        if rec["kind"] not in KINDS: err("record_kind")
        if not isinstance(rec["statement"],str) or not rec["statement"].strip(): err("statement")
        if not isinstance(rec["entities"],list): err("entities")
        disclosed=rec["disclosed_at"]
        if disclosed is not None and (type(disclosed) is not int or not 1 <= disclosed <= len(lines)): err("disclosed_at")
        if rec["status"] != "candidate" or any(rec.get(f,False) not in (False, 0) for f in FLAGS): err("candidate_confirmation")
        evidence(rec)
    relations=data["relations"]
    if not isinstance(relations,list): err("relations_type"); relations=[]
    for rel in relations:
        if not required(rel,("id","from","to","type","statement","evidence","kind","status"),"relation_fields"): continue
        key=rel["id"]
        if not isinstance(key,str) or not key or key in ids: err("duplicate_id")
        else: ids[key]=rel
        record_ids = {r.get("id") for r in records if isinstance(r,dict) and isinstance(r.get("id"),str)}
        if any(not isinstance(rel[k],str) or rel[k] not in record_ids for k in ("from","to")): err("relation_endpoint")
        if rel["type"] not in REL_TYPES: err("relation_type")
        if rel["kind"] not in KINDS: err("relation_kind")
        if not isinstance(rel["statement"],str) or not rel["statement"].strip(): err("statement")
        if rel["status"] != "candidate" or any(rel.get(f,False) not in (False, 0) for f in FLAGS): err("candidate_confirmation")
        evidence(rel)
        if rel["type"] in {"causes","enables","prevents"}:
            for endpoint in (rel["from"],rel["to"]):
                if isinstance(endpoint,str) and endpoint in ids and not ids[endpoint].get("evidence"): err("causal_endpoint_evidence")
    coverage=data["coverage"]
    if not isinstance(coverage,list): err("coverage_type"); coverage=[]
    seen=set()
    for c in coverage:
        if not required(c,("dimension","status","record_ids","gaps"),"coverage_fields"): continue
        dim=c["dimension"]
        if dim not in DIMS or dim in seen: err("coverage_dimension")
        seen.add(dim)
        if c["status"] not in STATES: err("coverage_status")
        if not isinstance(c["record_ids"],list): err("coverage_records"); continue
        if any(k not in ids or ids[k].get("dimension") != dim for k in c["record_ids"]): err("coverage_records")
        if c["status"] == "candidate_covered" and not c["record_ids"]: err("coverage_empty")
        if c["status"] == "not_applicable" and not c.get("reason"): err("coverage_reason")
        if c["status"] == "not_started" and c["record_ids"]: err("coverage_conflict")
    if seen != DIMS: err("coverage_missing")
    if not isinstance(data["questions"],list): err("questions_type")
    covered=0
    last=0
    for a,b in ranges:
        covered += max(0,b-max(a,last+1)+1)
        last=max(last,b)
    return errors, {"source_lines":len(lines),"machine_read_lines":covered,"machine_uncovered_lines":max(0,len(lines)-covered),"records":len(records),"relations":len(relations)}

def main():
    p=argparse.ArgumentParser(); p.add_argument("input"); p.add_argument("--source-root",required=True); p.add_argument("--manifest"); args=p.parse_args()
    try:
        data=json.loads(Path(args.input).read_text(encoding="utf-8")); errors,counts=validate(data,args.source_root,args.manifest)
    except (OSError,ValueError,TypeError,KeyError,AttributeError): errors,counts=["input_invalid"],{}
    summary={}
    for e in errors: summary[e]=summary.get(e,0)+1
    print(json.dumps({"ok":not errors,"counts":counts,"errors":summary},ensure_ascii=False)); return int(bool(errors))
if __name__ == "__main__": sys.exit(main())
