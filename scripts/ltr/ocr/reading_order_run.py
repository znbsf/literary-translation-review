"""Explicit plan/execute CLI for bounded read-only OCR geometry candidates.

The only output is an isolated geometry catalog. It never writes the source DB,
changes OCR receipts, calls a model, or stores OCR text.
"""
from __future__ import annotations

from ltr.core import sha256 as _sha
import argparse
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import warnings

from ltr.ocr import reading_order_geometry as geometry
from ltr.ocr import vision_index as vision
from ltr.workbench import CorpusConfig, CorpusDatabase

PLAN_SCHEMA="corpus-reading-order-plan-v1"
PROGRESS_SCHEMA="corpus-reading-order-progress-v1"
MANIFEST_SCHEMA="corpus-reading-order-manifest-v1"
VERIFY_SCHEMA="corpus-reading-order-verification-v1"
PAGE_SCHEMA="corpus-reading-order-page-v1"
METHOD=geometry.METHOD
FLAGS={"requires_review":1,"accepted":0,"human_confirmed":0,"human_verified":0,
       "semantic_verified":0,"reference_eligible":0,"reading_order_verified":0,"ocr_verified":0}
MAX_PAGES=4096
MAX_SELECTOR_BYTES=2*1024*1024
MAX_RECEIPT_BYTES=24*1024*1024
MAX_IMAGE_BYTES=vision.MAX_IMAGE_BYTES
MAX_PAGE_OUTPUT_BYTES=2*1024*1024
MAX_MANIFEST_BYTES=4*1024*1024
MAX_TOTAL_BYTES=8*1024*1024*1024
MAX_DIMENSION=geometry.MAX_DIMENSION
HEX=re.compile(r"[0-9a-f]{64}\Z")
SAFE_ID=re.compile(r"[A-Za-z0-9._:-]{1,200}\Z")
SAFE_COLLECTION=re.compile(r"[A-Za-z0-9._-]{1,200}\Z")
ERROR_CODES={"receipt_unavailable","receipt_invalid","receipt_binding_mismatch","geometry_invalid","geometry_limits_exceeded"}

class RunError(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)

class InputRejected(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)

def _need(ok,code="geometry_run_invalid"):
    if not ok: raise RunError(code)

def _is_hash(v): return type(v) is str and HEX.fullmatch(v) is not None
def _int(v,lo=0): return type(v) is int and v>=lo
def _json_bytes(obj):
    return (json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)+"\n").encode("utf-8")

def _unique(pairs):
    out={}
    for k,v in pairs:
        if k in out: raise ValueError("duplicate_key")
        out[k]=v
    return out

def _json(raw):
    try: return json.loads(raw.decode("utf-8"),object_pairs_hook=_unique,parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
    except Exception: raise RunError("geometry_json_invalid") from None

def _no_links(path):
    for p in (Path(path),*Path(path).parents):
        info=p.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0)&0x400:
            raise RunError("geometry_path_unsafe")

def _read(path,limit):
    path=Path(path)
    try:
        raw=vision._bytes(path,limit)
    except Exception:
        raise RunError("geometry_input_unavailable") from None
    return raw

def _write_exclusive(path,raw):
    path=Path(path); _no_links(path.parent)
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_BINARY",0)|getattr(os,"O_NOFOLLOW",0)
    try:
        fd=os.open(path,flags,0o600)
        with os.fdopen(fd,"wb") as f: f.write(raw); f.flush(); os.fsync(f.fileno())
    except Exception:
        raise RunError("geometry_output_failed") from None

def _file_identity(path):
    st=Path(path).lstat()
    _need(stat.S_ISREG(st.st_mode),"geometry_progress_invalid")
    return (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns)

def _replace_progress(path,raw,expected_identity,expected_sha):
    path=Path(path); _no_links(path.parent)
    _need(_file_identity(path)==expected_identity,"geometry_progress_changed")
    _need(_sha(_read(path,MAX_MANIFEST_BYTES))==expected_sha,"geometry_progress_changed")
    tmp=path.with_name("progress.tmp")
    _need(not os.path.lexists(tmp),"geometry_output_unsafe")
    _write_exclusive(tmp,raw)
    _no_links(path)
    _need(_file_identity(path)==expected_identity and _sha(_read(path,MAX_MANIFEST_BYTES))==expected_sha,"geometry_progress_changed")
    os.replace(tmp,path); _no_links(path)
    return _file_identity(path),_sha(raw)

def _root_db(db_path):
    path=Path(db_path).absolute()
    _no_links(path)
    _need(path.name=="dialogue.sqlite","geometry_database_invalid")
    root=path.parent
    corpus=CorpusConfig("geometry-cli","geometry-cli",root)
    return corpus,CorpusDatabase(corpus)

def _module_pins():
    return _sha(Path(__file__).read_bytes()),_sha(Path(geometry.__file__).read_bytes())

def _safe_out(out,root,create=False):
    path=Path(out).absolute()
    try: resolved=vision.validate_directory_path(path,root)
    except Exception: raise RunError("geometry_output_unsafe") from None
    if create:
        _need(not os.path.lexists(resolved),"geometry_output_exists")
        try: resolved.mkdir(parents=False,exist_ok=False)
        except Exception: raise RunError("geometry_output_failed") from None
    try:
        _no_links(resolved); _need(resolved.is_dir(),"geometry_output_unavailable")
    except RunError: raise
    except Exception: raise RunError("geometry_output_unavailable") from None
    return resolved

def _selector(path,kind):
    raw=_read(path,MAX_SELECTOR_BYTES); obj=_json(raw)
    value=None
    if type(obj) is list: value=obj
    elif type(obj) is dict:
        keys={"page_ids","pilot_page_ids","ids","remaining"} if kind=="include" else {"page_ids","excluded_page_ids","excluded_processed","ids"}
        found=[k for k in keys if k in obj]
        _need(len(found)==1,"geometry_selector_invalid")
        value=obj[found[0]]
        if found[0] in {"remaining","excluded_processed"}:
            _need(type(value) is list and all(type(row) is dict and type(row.get("page_id")) is str for row in value),"geometry_selector_invalid")
            value=[row["page_id"] for row in value]
    _need(type(value) is list and (1<=len(value) if kind=="include" else 0<=len(value)) and len(value)<=MAX_PAGES,"geometry_selector_invalid")
    _need(all(type(x) is str and SAFE_ID.fullmatch(x) for x in value) and len(value)==len(set(value)),"geometry_selector_invalid")
    return value,_sha(raw)

def _metadata(database,page_id,corpus):
    needed={"id","collection_id","ordinal","image_path","image_sha256","width","height","ocr_status","ocr_sha256"}
    _need(needed<=database.columns("scan_pages"),"geometry_source_schema_invalid")
    _need({"id","book_key"}<=database.columns("scan_collections"),"geometry_source_schema_invalid")
    rows=database.query("SELECT p.id,p.collection_id,p.ordinal,p.image_path,p.image_sha256,p.width,p.height,p.ocr_status,p.ocr_sha256,c.book_key FROM scan_pages p JOIN scan_collections c ON c.id=p.collection_id WHERE p.id=? LIMIT 2",[page_id])
    _need(len(rows)==1,"geometry_page_metadata_mismatch")
    r=dict(rows[0])
    _need(type(r["id"]) is str and r["id"]==page_id and type(r["collection_id"]) is str and SAFE_COLLECTION.fullmatch(r["collection_id"]),"geometry_page_metadata_mismatch")
    _need(_int(r["ordinal"],1) and type(r["book_key"]) is str and 1<=len(r["book_key"])<=200,"geometry_page_metadata_mismatch")
    _need(_int(r["width"],1) and r["width"]<=MAX_DIMENSION and _int(r["height"],1) and r["height"]<=MAX_DIMENSION,"geometry_page_metadata_mismatch")
    _need(_is_hash(r["image_sha256"]) and _is_hash(r["ocr_sha256"]),"geometry_page_metadata_mismatch")
    _need(r["ocr_status"] == "completed_unverified","geometry_page_metadata_mismatch")
    try: vision.validated_image_path(corpus,r["image_path"])
    except Exception: raise RunError("geometry_image_metadata_invalid") from None
    return {"page_id":r["id"],"collection_id":r["collection_id"],"ordinal":r["ordinal"],"book_key":r["book_key"],"image_path":r["image_path"],"image_sha256":r["image_sha256"],"ocr_sha256":r["ocr_sha256"],"width":r["width"],"height":r["height"]}

def _image_size_hash(corpus,image_path,expected_sha,width,height):
    try:
        path=vision.validated_image_path(corpus,image_path)
        raw=_read(path,MAX_IMAGE_BYTES)
        _need(_sha(raw)==expected_sha,"geometry_image_hash_mismatch")
        from PIL import Image
        with warnings.catch_warnings():
            warnings.simplefilter("error",Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                dimensions=image.size; kind=image.format
                image.verify()
        _need(kind in {"PNG","JPEG","GIF","WEBP"},"geometry_image_format_invalid")
        _need(dimensions==(width,height),"geometry_image_dimensions_mismatch")
        return len(raw),_sha(raw)
    except RunError: raise
    except ImportError: raise RunError("geometry_image_dependency_unavailable") from None
    except Exception: raise RunError("geometry_image_invalid") from None

def _receipt_path(root,row):
    _need(SAFE_COLLECTION.fullmatch(row["collection_id"]) is not None,"geometry_receipt_path_invalid")
    return Path(root)/"private"/"ocr"/f"{row['collection_id']}_{row['ordinal']:04d}.json"

def _receipt_size_hash(root,row):
    path=_receipt_path(root,row)
    raw=_read(path,MAX_RECEIPT_BYTES)
    return len(raw),_sha(raw)

def _page_identity(row):
    return {k:row[k] for k in ("page_id","collection_id","ordinal","book_key","image_sha256","ocr_sha256","width","height")}

def build_plan(db_path,out,page_ids_path,exclude_page_ids_path=None,max_pages=MAX_PAGES,max_total_input_bytes=0,max_total_output_bytes=0):
    _need(_int(max_pages,1) and max_pages<=MAX_PAGES and _int(max_total_input_bytes,1) and max_total_input_bytes<=MAX_TOTAL_BYTES and _int(max_total_output_bytes,1) and max_total_output_bytes<=MAX_TOTAL_BYTES,"geometry_limits_invalid")
    corpus,database=_root_db(db_path)
    out_path=_safe_out(out,corpus.root,create=True)
    try:
        database.require(); source=database.source_sha256(refresh=True)
        ids,ids_hash=_selector(page_ids_path,"include")
        excludes,exclude_hash=([],None)
        if exclude_page_ids_path:
            excludes,exclude_hash=_selector(exclude_page_ids_path,"exclude")
        _need(len(ids)<=max_pages and not (set(ids)&set(excludes)),"geometry_selector_invalid")
        _need({"scan_pages","scan_collections"}<=database.tables(),"geometry_source_schema_invalid")
        pages=[]; input_total=0
        for page_id in ids:
            row=_metadata(database,page_id,corpus)
            size,receipt_sha=_receipt_size_hash(corpus.root,row)
            image_size,image_sha=_image_size_hash(corpus,row["image_path"],row["image_sha256"],row["width"],row["height"])
            input_total+=size+image_size; _need(input_total<=max_total_input_bytes,"geometry_input_budget_exceeded")
            pages.append({**_page_identity(row),"receipt_bytes":size,"image_bytes":image_size})
            _need(receipt_sha==row["ocr_sha256"],"geometry_receipt_hash_mismatch")
            _need(image_sha==row["image_sha256"],"geometry_image_hash_mismatch")
        _need(database.source_sha256(refresh=True)==source,"geometry_source_changed")
        _need(_selector(page_ids_path,"include")[1]==ids_hash,"geometry_selector_changed")
        if exclude_page_ids_path: _need(_selector(exclude_page_ids_path,"exclude")[1]==exclude_hash,"geometry_selector_changed")
        helper_sha,algorithm_sha=_module_pins()
        plan={"schema":PLAN_SCHEMA,"source_sha256":source,"page_ids_sha256":ids_hash,"exclude_page_ids_sha256":exclude_hash,
              "algorithm_sha256":algorithm_sha,"helper_module_sha256":helper_sha,"max_pages":max_pages,
              "max_total_input_bytes":max_total_input_bytes,"max_total_output_bytes":max_total_output_bytes,
              "pages":pages}
        raw=_json_bytes(plan); _need(len(raw)<=MAX_MANIFEST_BYTES and len(raw)<=max_total_output_bytes,"geometry_output_budget_exceeded")
        progress={"schema":PROGRESS_SCHEMA,"status":"planned","source_sha256":source,"plan_sha256":_sha(raw),
                  "page_count":len(pages),"processed":0,"classified":0,"unclassified":0,"rejected":0,
                  "unattempted":len(pages),"model_calls":0,"corpus_updated":False,"error_code":None,"results":[]}
        praw=_json_bytes(progress); _need(len(praw)<=MAX_MANIFEST_BYTES and len(raw)+len(praw)<=max_total_output_bytes,"geometry_output_budget_exceeded")
        _write_exclusive(out_path/"plan.json",raw)
        _write_exclusive(out_path/"progress.json",praw)
        _need(_file_identity(out_path/"progress.json")[2]==len(praw),"geometry_progress_changed")
        return {"status":"planned","page_count":len(pages),"source_sha256":source,"plan_sha256":_sha(raw)}
    finally:
        database.close()

def _validate_plan(plan,plan_raw,ids_hash,exclude_hash,limits):
    _need(type(plan) is dict and set(plan)=={"schema","source_sha256","page_ids_sha256","exclude_page_ids_sha256","algorithm_sha256","helper_module_sha256","max_pages","max_total_input_bytes","max_total_output_bytes","pages"} and plan.get("schema")==PLAN_SCHEMA,"geometry_plan_invalid")
    _need(_is_hash(plan["source_sha256"]) and _is_hash(plan["algorithm_sha256"]) and _is_hash(plan["helper_module_sha256"]),"geometry_plan_invalid")
    _need(plan["page_ids_sha256"]==ids_hash and plan["exclude_page_ids_sha256"]==exclude_hash,"geometry_plan_mismatch")
    _need((plan["max_pages"],plan["max_total_input_bytes"],plan["max_total_output_bytes"])==limits,"geometry_plan_mismatch")
    _need(type(plan["pages"]) is list and 1<=len(plan["pages"])<=MAX_PAGES and len(plan["pages"])<=plan["max_pages"],"geometry_plan_invalid")
    seen=set();total=0
    for row in plan["pages"]:
        expected={"page_id","collection_id","ordinal","book_key","image_sha256","ocr_sha256","width","height","receipt_bytes","image_bytes"}
        _need(type(row) is dict and set(row)==expected and SAFE_ID.fullmatch(row["page_id"]) and row["page_id"] not in seen,"geometry_plan_invalid")
        seen.add(row["page_id"]); _need(SAFE_COLLECTION.fullmatch(row["collection_id"]) and _int(row["ordinal"],1) and _is_hash(row["image_sha256"]) and _is_hash(row["ocr_sha256"]),"geometry_plan_invalid")
        _need(_int(row["width"],1) and row["width"]<=MAX_DIMENSION and _int(row["height"],1) and row["height"]<=MAX_DIMENSION and _int(row["receipt_bytes"],1) and row["receipt_bytes"]<=MAX_RECEIPT_BYTES and _int(row["image_bytes"],1) and row["image_bytes"]<=MAX_IMAGE_BYTES,"geometry_plan_invalid")
        total+=row["receipt_bytes"]+row["image_bytes"]
    _need(total<=plan["max_total_input_bytes"],"geometry_input_budget_exceeded")
    return total

def _project_receipt(raw,row):
    def rejected(code): raise InputRejected(code)
    try: obj=json.loads(raw.decode("utf-8"),object_pairs_hook=_unique,parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
    except Exception: rejected("receipt_invalid")
    if type(obj) is not dict or obj.get("schema")!="haruhi-page-ocr-v1" or obj.get("status")!="completed":
        rejected("receipt_invalid")
    if obj.get("page_id")!=row["page_id"] or obj.get("image_sha256")!=row["image_sha256"] or obj.get("width")!=row["width"] or obj.get("height")!=row["height"]:
        rejected("receipt_binding_mismatch")
    lines=obj.get("lines")
    if type(lines) is not list or len(lines)>geometry.MAX_LINES: rejected("receipt_invalid")
    geometry_lines=[]; total_words=0
    for line in lines:
        if type(line) is not dict or type(line.get("words")) is not list: rejected("receipt_invalid")
        projected=[]
        for word in line["words"]:
            total_words+=1
            if total_words>geometry.MAX_WORDS: rejected("geometry_limits_exceeded")
            if type(word) is not dict or not all(k in word for k in ("x","y","w","h")): rejected("receipt_invalid")
            projected.append({k:word[k] for k in ("x","y","w","h")})
        geometry_lines.append({"words":projected})
    return geometry_lines

def _candidate(row,source,algorithm_sha,geometry_result=None,error_code=None):
    rejected=error_code is not None
    if rejected:
        if error_code not in ERROR_CODES: raise RunError("geometry_reject_code_invalid")
        status="geometry_input_rejected"; layout=None; line_count=word_count=None
        word_order=segments=bands=raw_steps=new_steps=main=ruby=other=None; geom=False
    elif geometry_result["layout"]=="unclassified":
        status="empty_geometry_unclassified" if geometry_result["raw_word_count"]==0 else "unclassified_geometry_only"
        layout="unclassified"; line_count=geometry_result["raw_line_count"];word_count=geometry_result["raw_word_count"]
        word_order=geometry_result["word_order"];segments=[];bands=[];raw_steps=geometry_result["raw_backward_band_steps"];new_steps=geometry_result["new_backward_band_steps"]
        main=[];ruby=[];other=[];geom=False
    else:
        status="geometry_candidate_unverified"; layout=geometry_result["layout"]; line_count=geometry_result["raw_line_count"];word_count=geometry_result["raw_word_count"]
        word_order=geometry_result["word_order"];segments=geometry_result["segments"];bands=geometry_result["bands"]
        raw_steps=geometry_result["raw_backward_band_steps"];new_steps=geometry_result["new_backward_band_steps"]
        main=geometry_result["main"];ruby=geometry_result["ruby"];other=geometry_result["other"];geom=True
    return {"schema":PAGE_SCHEMA,"page_id":row["page_id"],"book_key":row["book_key"],"collection_id":row["collection_id"],"ordinal":row["ordinal"],
            "source_sha256":source,"receipt_sha256":row["ocr_sha256"],"image_sha256":row["image_sha256"],"algorithm_sha256":algorithm_sha,
            "method":METHOD,"status":status,"error_code":error_code,"layout":layout,"raw_line_count":line_count,"raw_word_count":word_count,
            "word_order":word_order,"segments":segments,"bands":bands,"raw_backward_band_steps":raw_steps,"new_backward_band_steps":new_steps,
            "main":main,"ruby":ruby,"other":other,"main_ruby_role_status":"geometry_candidate_not_semantically_verified",
            "geometry_candidate":geom,**FLAGS}

def _progress_payload(source_sha,plan_sha,pages,results,status,code=None):
    counts={"processed":len(results),"classified":sum(x["status"]=="geometry_candidate_unverified" for x in results),
            "unclassified":sum(x["status"] in {"unclassified_geometry_only","empty_geometry_unclassified"} for x in results),
            "rejected":sum(x["status"]=="geometry_input_rejected" for x in results)}
    data={"schema":PROGRESS_SCHEMA,"status":status,"source_sha256":source_sha,
          "plan_sha256":plan_sha,"page_count":len(pages),**counts,"unattempted":len(pages)-len(results),"model_calls":0,"corpus_updated":False,
          "error_code":code,"results":results}
    return data

def _progress_update(out,source_sha,plan_sha,pages,results,status,state,code=None):
    data=_progress_payload(source_sha,plan_sha,pages,results,status,code)
    raw=_json_bytes(data)
    _need(len(raw)<=MAX_MANIFEST_BYTES,"geometry_output_budget_exceeded")
    next_state=_replace_progress(out/"progress.json",raw,*state)
    return data,next_state

def _recheck_inputs(corpus,database,plan):
    for row in plan["pages"]:
        now=_metadata(database,row["page_id"],corpus)
        _need(_page_identity(now)=={k:row[k] for k in _page_identity(now)},"geometry_page_metadata_mismatch")
        raw=_read(_receipt_path(corpus.root,row),MAX_RECEIPT_BYTES)
        _need(len(raw)==row["receipt_bytes"] and _sha(raw)==row["ocr_sha256"],"geometry_receipt_hash_mismatch")
        image_row=_metadata(database,row["page_id"],corpus)
        image_size,image_sha=_image_size_hash(corpus,image_row["image_path"],row["image_sha256"],row["width"],row["height"])
        _need(image_size==row["image_bytes"] and image_sha==row["image_sha256"],"geometry_image_hash_mismatch")

def execute_plan(db_path,out,page_ids_path,exclude_page_ids_path=None,max_pages=MAX_PAGES,max_total_input_bytes=0,max_total_output_bytes=0):
    corpus,database=_root_db(db_path); out_path=_safe_out(out,corpus.root,create=False)
    limits=(max_pages,max_total_input_bytes,max_total_output_bytes)
    results=[]; plan=None; plan_sha=None; progress_state=None
    try:
        database.require()
        plan_raw=_read(out_path/"plan.json",MAX_MANIFEST_BYTES)
        plan=_json(plan_raw); plan_sha=_sha(plan_raw)
        ids,ids_hash=_selector(page_ids_path,"include")
        excludes,exclude_hash=([],None)
        if exclude_page_ids_path: excludes,exclude_hash=_selector(exclude_page_ids_path,"exclude")
        _need(len(ids)<=max_pages and not(set(ids)&set(excludes)),"geometry_selector_invalid")
        _validate_plan(plan,plan_raw,ids_hash,exclude_hash,limits)
        _need([row["page_id"] for row in plan["pages"]]==ids,"geometry_plan_mismatch")
        source=database.source_sha256(refresh=True)
        _need(source==plan["source_sha256"],"geometry_source_changed")
        helper_sha,algorithm_sha=_module_pins()
        _need(helper_sha==plan["helper_module_sha256"] and algorithm_sha==plan["algorithm_sha256"],"geometry_code_changed")
        progress_raw=_read(out_path/"progress.json",MAX_MANIFEST_BYTES); progress=_json(progress_raw)
        expected_progress=_json_bytes(_progress_payload(source,plan_sha,plan["pages"],[],"planned"))
        _need(progress.get("schema")==PROGRESS_SCHEMA and progress.get("status")=="planned" and progress.get("plan_sha256")==plan_sha and progress.get("processed")==0 and progress_raw==expected_progress,"geometry_progress_invalid")
        progress_path=out_path/"progress.json"
        progress_state=(_file_identity(progress_path),_sha(progress_raw))
        expected_names={"plan.json","progress.json"}
        _need(set(p.name for p in out_path.iterdir())==expected_names,"geometry_output_not_empty")
        input_used=0; output_used=len(plan_raw)+len(progress_raw)
        for index,row in enumerate(plan["pages"]):
            if index%100==0:
                _need(database.source_sha256(refresh=True)==source,"geometry_source_changed")
            current=_metadata(database,row["page_id"],corpus)
            _need(_page_identity(current)=={k:row[k] for k in _page_identity(current)},"geometry_page_metadata_mismatch")
            image_size,image_sha=_image_size_hash(corpus,current["image_path"],row["image_sha256"],row["width"],row["height"])
            input_used+=image_size
            _need(input_used<=max_total_input_bytes and image_size==row["image_bytes"] and image_sha==row["image_sha256"],"geometry_image_hash_mismatch")
            receipt_path=_receipt_path(corpus.root,row)
            raw=_read(receipt_path,MAX_RECEIPT_BYTES)
            input_used+=len(raw)
            _need(input_used<=max_total_input_bytes,"geometry_input_budget_exceeded")
            _need(len(raw)==row["receipt_bytes"] and _sha(raw)==row["ocr_sha256"],"geometry_receipt_hash_mismatch")
            candidate_result=None; error=None
            try:
                lines=_project_receipt(raw,row)
                geometry_result=geometry.word_order(lines,row["width"],row["height"])
                candidate_result=_candidate(row,source,algorithm_sha,geometry_result)
            except InputRejected as exc:
                error=exc.code
            except geometry.GeometryError as exc:
                error="geometry_limits_exceeded" if exc.code=="geometry_limits_exceeded" else "geometry_invalid"
            if error:
                candidate_result=_candidate(row,source,algorithm_sha,error_code=error)
            raw_after=_read(receipt_path,MAX_RECEIPT_BYTES)
            _need(_sha(raw_after)==_sha(raw),"geometry_receipt_changed")
            _need(_page_identity(_metadata(database,row["page_id"],corpus))==_page_identity(current),"geometry_page_metadata_mismatch")
            out_raw=_json_bytes(candidate_result)
            _need(len(out_raw)<=MAX_PAGE_OUTPUT_BYTES and output_used+len(out_raw)<=max_total_output_bytes,"geometry_output_budget_exceeded")
            filename=f"geometry-{index+1:06d}.json"
            item={"page_id":row["page_id"],"file":filename,"candidate_sha256":_sha(out_raw),"status":candidate_result["status"],"geometry_candidate":candidate_result["geometry_candidate"],"raw_word_count":candidate_result["raw_word_count"],"raw_line_count":candidate_result["raw_line_count"],"segment_count":len(candidate_result["segments"]) if type(candidate_result["segments"]) is list else None,"error_code":candidate_result["error_code"]}
            next_results=results+[item]
            next_progress=_json_bytes(_progress_payload(source,plan_sha,plan["pages"],next_results,"running"))
            old_progress_size=progress_state[0][2]
            _need(len(next_progress)<=MAX_MANIFEST_BYTES and output_used-old_progress_size+len(out_raw)+len(next_progress)<=max_total_output_bytes,"geometry_output_budget_exceeded")
            _write_exclusive(out_path/filename,out_raw)
            results.append(item)
            progress_state=_replace_progress(out_path/"progress.json",next_progress,*progress_state)
            output_used=output_used-old_progress_size+len(out_raw)+len(next_progress)
        _need(database.source_sha256(refresh=True)==source,"geometry_source_changed")
        _need(_module_pins()==(helper_sha,algorithm_sha),"geometry_code_changed")
        _need(_selector(page_ids_path,"include")[1]==ids_hash,"geometry_selector_changed")
        if exclude_page_ids_path: _need(_selector(exclude_page_ids_path,"exclude")[1]==exclude_hash,"geometry_selector_changed")
        _recheck_inputs(corpus,database,plan)
        _need(len(results)==len(plan["pages"]),"geometry_run_incomplete")
        # Revalidate the exact flat output set and every generated file hash.
        expected_names={"plan.json","progress.json"}|{x["file"] for x in results}
        _need({p.name for p in out_path.iterdir()}==expected_names,"geometry_output_changed")
        for item in results:
            raw=_read(out_path/item["file"],MAX_PAGE_OUTPUT_BYTES)
            _need(_sha(raw)==item["candidate_sha256"],"geometry_output_changed")
        progress_data,progress_state=_progress_update(out_path,source,plan_sha,plan["pages"],results,"complete",progress_state)
        progress_raw=_read(out_path/"progress.json",MAX_MANIFEST_BYTES)
        progress_sha=_sha(progress_raw)
        output_used=sum(p.stat().st_size for p in out_path.iterdir() if p.is_file())
        counts={"page_count":len(results),"classified_count":sum(x["status"]=="geometry_candidate_unverified" for x in results),
                "unclassified_count":sum(x["status"] in {"unclassified_geometry_only","empty_geometry_unclassified"} for x in results),
                "rejected_count":sum(x["status"]=="geometry_input_rejected" for x in results),"unattempted_count":0}
        manifest={"schema":MANIFEST_SCHEMA,"source_sha256":source,"plan_sha256":plan_sha,"algorithm_sha256":algorithm_sha,
                  "helper_module_sha256":helper_sha,**counts,"flags":FLAGS,
                  "pages":[{**x,"collection_id":plan["pages"][i]["collection_id"],"ordinal":plan["pages"][i]["ordinal"],
                             "book_key":plan["pages"][i]["book_key"],"image_sha256":plan["pages"][i]["image_sha256"],
                             "ocr_sha256":plan["pages"][i]["ocr_sha256"],"width":plan["pages"][i]["width"],
                             "height":plan["pages"][i]["height"]} for i,x in enumerate(results)]}
        mraw=_json_bytes(manifest); manifest_sha=_sha(mraw)
        _need(len(mraw)<=MAX_MANIFEST_BYTES,"geometry_output_budget_exceeded")
        verify={"schema":VERIFY_SCHEMA,"verified_at_utc":datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z"),
                "source_sha256":source,"plan_sha256":plan_sha,"manifest_sha256":manifest_sha,"algorithm_sha256":algorithm_sha,
                "helper_module_sha256":helper_sha,"plan_file_sha256":plan_sha,"progress_file_sha256":progress_sha,
                **counts,"flags":FLAGS,"source_unchanged":True,"inputs_unchanged":True,"outputs_unchanged":True,
                "corpus_updated":False,"model_calls":0,"complete":True}
        vraw=_json_bytes(verify)
        _need(len(vraw)<=MAX_MANIFEST_BYTES,"geometry_output_budget_exceeded")
        _need(output_used+len(mraw)+len(vraw)<=max_total_output_bytes,"geometry_output_budget_exceeded")
        _need(database.source_sha256(refresh=True)==source,"geometry_source_changed")
        _recheck_inputs(corpus,database,plan)
        for item in results:
            _need(_sha(_read(out_path/item["file"],MAX_PAGE_OUTPUT_BYTES))==item["candidate_sha256"],"geometry_output_changed")
        _need(_sha(_read(out_path/"plan.json",MAX_MANIFEST_BYTES))==plan_sha and _file_identity(out_path/"progress.json")==progress_state[0] and _sha(_read(out_path/"progress.json",MAX_MANIFEST_BYTES))==progress_sha,"geometry_output_changed")
        _need(database.source_sha256(refresh=True)==source,"geometry_source_changed")
        _write_exclusive(out_path/"manifest.json",mraw)
        _write_exclusive(out_path/"verification.json",vraw)
        _need(_sha(_read(out_path/"manifest.json",MAX_MANIFEST_BYTES))==manifest_sha,"geometry_output_changed")
        _need(_sha(_read(out_path/"verification.json",MAX_MANIFEST_BYTES))==_sha(vraw),"geometry_output_changed")
        return {"status":"complete","page_count":len(results),**counts,"source_sha256":source,"manifest_sha256":manifest_sha}
    except Exception as exc:
        code=exc.code if isinstance(exc,RunError) else "geometry_run_failed"
        if plan is not None and plan_sha is not None:
            try:
                if progress_state is not None: _progress_update(out_path,plan["source_sha256"],plan_sha,plan["pages"],results,"stopped",progress_state,code)
            except Exception: pass
        raise RunError(code) from None
    finally:
        database.close()

def _args(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",required=True); ap.add_argument("--out",required=True)
    ap.add_argument("--page-ids",required=True); ap.add_argument("--exclude-page-ids")
    ap.add_argument("--max-pages",type=int,required=True)
    ap.add_argument("--max-total-input-bytes",type=int,required=True)
    ap.add_argument("--max-total-output-bytes",type=int,required=True)
    ap.add_argument("--execute",action="store_true")
    return ap.parse_args(argv)

def main(argv=None):
    args=_args(argv)
    try:
        if args.execute:
            result=execute_plan(args.db,args.out,args.page_ids,args.exclude_page_ids,args.max_pages,args.max_total_input_bytes,args.max_total_output_bytes)
        else:
            result=build_plan(args.db,args.out,args.page_ids,args.exclude_page_ids,args.max_pages,args.max_total_input_bytes,args.max_total_output_bytes)
        sys.stdout.write(json.dumps(result,sort_keys=True)+"\n")
        return 0
    except RunError as exc:
        sys.stderr.write(exc.code+"\n")
        return 2
    except Exception:
        sys.stderr.write("geometry_run_failed\n")
        return 2

if __name__=="__main__": raise SystemExit(main())
