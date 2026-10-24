"""Read-only, source-bound geometry pilot and bounded reading-order catalog.

The legacy ten-page pilot remains supported alongside paginated catalogs. It never returns OCR text,
changes the formal database, or treats geometry as verified reading order.
"""
from __future__ import annotations

from ltr.core import sha256 as _sha
import math
import os
import stat
import re
from pathlib import Path

from ltr.ocr import vision_index as vision

SCHEMA = "private-haruhi-geometry-pilot-manifest-v1"
VERIFY_SCHEMA = "private-haruhi-geometry-pilot-verification-v1"
PAGE_SCHEMA = "private-haruhi-geometry-page-v1"
MAX_FILES = 10
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_RECEIPT_BYTES = 24 * 1024 * 1024
HEX = re.compile(r"[0-9a-f]{64}\Z")
PAGE_ID = re.compile(r"[A-Za-z0-9._:-]{1,200}\Z")

PAGE_KEYS = {"schema","page_id","book_key","collection_id","ordinal","source_sha256","receipt_sha256","image_sha256","algorithm_sha256","method","status","error_code","layout","raw_line_count","raw_word_count","word_order","segments","bands","raw_backward_band_steps","new_backward_band_steps","main","ruby","other","main_ruby_role_status","geometry_candidate","requires_review","accepted","human_confirmed","human_verified","semantic_verified","reference_eligible","reading_order_verified","ocr_verified"}
REQUIRED_PAGE_KEYS = PAGE_KEYS - {"error_code"}
SEGMENT_KEYS = {"band","box","column","role","source_line","word_refs"}
FLAGS = {"requires_review": 1, "accepted": 0, "human_confirmed": 0, "human_verified": 0,
         "semantic_verified": 0, "reference_eligible": 0, "reading_order_verified": 0, "ocr_verified": 0}
STATUSES = {"geometry_candidate_unverified", "unclassified_geometry_only", "empty_geometry_unclassified"}

class OCRReadingOrderIndexError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

def _need(ok, code="ocr_reading_order_invalid"):
    if not ok:
        raise OCRReadingOrderIndexError(code)

def _hash(value):
    return type(value) is str and HEX.fullmatch(value) is not None

def _integer(value, minimum=0):
    return type(value) is int and value >= minimum

def _safe_json(raw):
    try:
        return vision._json(raw)
    except Exception:
        raise OCRReadingOrderIndexError("ocr_reading_order_invalid") from None

def _static_record(c):
    _need(type(c) is dict and set(c) in (PAGE_KEYS, REQUIRED_PAGE_KEYS), "ocr_reading_order_invalid")
    _need(c.get("schema") in {PAGE_SCHEMA, CATALOG_PAGE_SCHEMA}, "ocr_reading_order_invalid")
    _need(type(c.get("page_id")) is str and PAGE_ID.fullmatch(c["page_id"]), "ocr_reading_order_invalid")
    for key in ("source_sha256", "receipt_sha256", "image_sha256", "algorithm_sha256"):
        _need(_hash(c.get(key)), "ocr_reading_order_invalid")
    _need(type(c.get("book_key")) is str and 1 <= len(c["book_key"]) <= 200 and type(c.get("collection_id")) is str and 1 <= len(c["collection_id"]) <= 200, "ocr_reading_order_invalid")
    _need(_integer(c.get("ordinal"), 1) and c.get("method") == "vertical-word-bands-v2", "ocr_reading_order_invalid")
    _need(c.get("main_ruby_role_status") == "geometry_candidate_not_semantically_verified", "ocr_reading_order_invalid")
    if c["status"] == REJECT_STATUS:
        _need(c["schema"] == CATALOG_PAGE_SCHEMA and c.get("error_code") in REJECT_CODES
              and c.get("geometry_candidate") is False)
        _need(all(c.get(key) is None for key in ("layout", "raw_line_count", "raw_word_count", "word_order",
            "segments", "bands", "raw_backward_band_steps", "new_backward_band_steps", "main", "ruby", "other")))
        for key, val in FLAGS.items():
            _need(type(c.get(key)) is int and c[key] == val)
        return
    _need(c.get("error_code") is None)
    for key in ("raw_line_count", "raw_word_count", "raw_backward_band_steps", "new_backward_band_steps"):
        _need(_integer(c.get(key)), "ocr_reading_order_invalid")
    for key, val in FLAGS.items(): _need(type(c.get(key)) is int and c[key] == val, "ocr_reading_order_invalid")
    _need(type(c.get("geometry_candidate")) is bool and c.get("status") in STATUSES, "ocr_reading_order_invalid")
    _need(type(c.get("word_order")) is list and type(c.get("segments")) is list and type(c.get("bands")) is list, "ocr_reading_order_invalid")
    _need(all(type(r) is list and len(r)==2 and _integer(r[0]) and _integer(r[1]) for r in c["word_order"]), "ocr_reading_order_invalid")
    _need(all(type(b) in (list,tuple) and len(b)==2 and all(_coord(v) for v in b) for b in c["bands"]), "ocr_reading_order_invalid")
    _need(all(type(c.get(role)) is list and all(_integer(x) for x in c[role]) for role in ("main","ruby","other")), "ocr_reading_order_invalid")
    if c["status"] == "geometry_candidate_unverified":
        _need(c["geometry_candidate"] is True and c["raw_word_count"] > 0 and c["layout"] in {"vertical_single_band","vertical_multi_band"} and bool(c["segments"]) and bool(c["bands"]), "ocr_reading_order_invalid")
    else:
        _need(c["geometry_candidate"] is False and c["layout"] == "unclassified" and c["segments"] == [] and c["bands"] == [] and c["main"] == [] and c["ruby"] == [] and c["other"] == [], "ocr_reading_order_invalid")
        _need((c["status"] == "empty_geometry_unclassified" and c["raw_word_count"] == 0) or (c["status"] == "unclassified_geometry_only" and c["raw_word_count"] > 0), "ocr_reading_order_invalid")
    for seg in c["segments"]:
        _need(type(seg) is dict and set(seg) == SEGMENT_KEYS and seg.get("role") in {"main","ruby","other"}, "ocr_reading_order_invalid")
        _need(_integer(seg.get("source_line")) and _integer(seg.get("band")) and _integer(seg.get("column")), "ocr_reading_order_invalid")
        _need(type(seg.get("word_refs")) is list and bool(seg["word_refs"]) and all(type(r) is list and len(r)==2 and _integer(r[0]) and _integer(r[1]) for r in seg["word_refs"]), "ocr_reading_order_invalid")
        _need(type(seg.get("box")) in (list,tuple) and len(seg["box"])==4 and all(_coord(x) for x in seg["box"]), "ocr_reading_order_invalid")
    if c["segments"]:
        idx=[i for role in ("main","ruby","other") for i in c[role]]
        _need(sorted(idx)==list(range(len(c["segments"]))) and len(set(idx))==len(idx), "ocr_reading_order_invalid")
        _need(all(c[role]==[i for i,s in enumerate(c["segments"]) if s["role"]==role] for role in ("main","ruby","other")), "ocr_reading_order_invalid")

def _config_dir(corpus):
    raw = getattr(corpus, "ocr_reading_order_candidates", None)
    _need(raw is not None, "ocr_reading_order_not_configured")
    try:
        return vision.validate_directory_path(raw, corpus.root)
    except Exception:
        raise OCRReadingOrderIndexError("ocr_reading_order_unavailable") from None

def _flat_names(directory):
    vision._no_links(directory)
    _need(directory.is_dir(), "ocr_reading_order_unavailable")
    expected = {"manifest.json", "verification.json"} | {f"geometry-{i:02d}.json" for i in range(1, MAX_FILES+1)}
    try:
        names = set()
        with __import__("os").scandir(directory) as entries:
            for n, entry in enumerate(entries, 1):
                _need(n <= 32, "ocr_reading_order_limits_exceeded")
                vision._no_links(Path(entry.path))
                _need(entry.is_file(follow_symlinks=False), "ocr_reading_order_invalid")
                names.add(entry.name)
        _need(names == expected, "ocr_reading_order_invalid")
    except OCRReadingOrderIndexError:
        raise
    except Exception:
        raise OCRReadingOrderIndexError("ocr_reading_order_unavailable") from None
    return expected

def _read_file(directory, name, maximum=MAX_FILE_BYTES):
    _need(name in {"manifest.json", "verification.json"} | {f"geometry-{i:02d}.json" for i in range(1, MAX_FILES+1)}, "ocr_reading_order_invalid")
    try:
        raw = vision._bytes(directory / name, maximum)
    except Exception:
        raise OCRReadingOrderIndexError("ocr_reading_order_unavailable") from None
    return raw, _safe_json(raw)

def _db_page(database, page_id):
    needed = {"id","collection_id","ordinal","image_path","image_sha256","width","height","ocr_status","ocr_sha256"}
    _need(needed <= database.columns("scan_pages"), "ocr_reading_order_source_unavailable")
    _need(type(page_id) is str and PAGE_ID.fullmatch(page_id), "ocr_reading_order_invalid")
    rows = database.query("SELECT id,collection_id,ordinal,image_path,image_sha256,width,height,ocr_status,ocr_sha256 FROM scan_pages WHERE id=? LIMIT 2", [page_id])
    _need(len(rows) == 1, "ocr_reading_order_page_mismatch")
    page = dict(rows[0])
    _need(type(page["collection_id"]) is str and 1 <= len(page["collection_id"]) <= 200, "ocr_reading_order_page_mismatch")
    _need(_integer(page["ordinal"], 1) and _hash(page["image_sha256"]) and _hash(page["ocr_sha256"]), "ocr_reading_order_page_mismatch")
    _need(_integer(page["width"], 1) and _integer(page["height"], 1), "ocr_reading_order_page_mismatch")
    _need(page["ocr_status"] == "completed_unverified", "ocr_reading_order_page_mismatch")
    return page

def _load_legacy(corpus, database):
    try:
        directory = _config_dir(corpus)
        names_before = _flat_names(directory)
        database.require()
        source = database.source_sha256()
        _need(_hash(source), "ocr_reading_order_source_unavailable")
        mraw, manifest = _read_file(directory, "manifest.json")
        vraw, verify = _read_file(directory, "verification.json")
        _need(type(manifest) is dict and set(manifest) == {"schema","source_sha256","inventory_sha256","algorithm_sha256","helper_sha256","candidates"} and manifest.get("schema") == SCHEMA, "ocr_reading_order_invalid")
        _need(type(verify) is dict and set(verify) == {"schema","verified_at_utc","source_sha256","inventory_sha256","algorithm_sha256","helper_sha256","manifest_sha256","scope_pages","candidate_pages","unclassified_pages","rejected_pages","requires_review","accepted","human_confirmed","human_verified","semantic_verified","reference_eligible","reading_order_verified","ocr_verified","source_unchanged","inputs_unchanged","algorithm_unchanged","qualification_flags_unchanged","corpus_updated","model_calls"} and verify.get("schema") == VERIFY_SCHEMA and type(verify.get("verified_at_utc")) is str, "ocr_reading_order_invalid")
        _need(manifest.get("source_sha256") == source and verify.get("source_sha256") == source, "ocr_reading_order_source_mismatch")
        inv, alg, helper = manifest.get("inventory_sha256"), manifest.get("algorithm_sha256"), manifest.get("helper_sha256")
        _need(_hash(inv) and _hash(alg) and _hash(helper), "ocr_reading_order_invalid")
        _need(verify.get("inventory_sha256") == inv and verify.get("algorithm_sha256") == alg and verify.get("helper_sha256") == helper, "ocr_reading_order_invalid")
        _need(verify.get("manifest_sha256") == _sha(mraw), "ocr_reading_order_invalid")
        _need(verify.get("source_unchanged") is True and verify.get("inputs_unchanged") is True and verify.get("algorithm_unchanged") is True and verify.get("qualification_flags_unchanged") is True and verify.get("corpus_updated") is False and verify.get("model_calls") == 0, "ocr_reading_order_invalid")
        for key, val in FLAGS.items(): _need(type(verify.get(key)) is int and verify[key] == val, "ocr_reading_order_invalid")
        rows = manifest.get("candidates")
        _need(type(rows) is list and len(rows) == MAX_FILES, "ocr_reading_order_invalid")
        items, seen = [], set()
        total = len(mraw) + len(vraw)
        for n, row in enumerate(rows, 1):
            _need(type(row) is dict and set(row) == {"page_id","file","sha256","status","geometry_candidate"}, "ocr_reading_order_invalid")
            name = f"geometry-{n:02d}.json"
            _need(row["file"] == name and _hash(row["sha256"]) and type(row["geometry_candidate"]) is bool and row["status"] in STATUSES, "ocr_reading_order_invalid")
            raw, candidate = _read_file(directory, name)
            total += len(raw); _need(total <= MAX_TOTAL_BYTES, "ocr_reading_order_limits_exceeded")
            candidate_sha = _sha(raw)
            _need(candidate_sha == row["sha256"], "ocr_reading_order_hash_mismatch")
            _need(type(candidate) is dict and set(candidate) in (PAGE_KEYS, REQUIRED_PAGE_KEYS), "ocr_reading_order_invalid")
            _need(candidate.get("schema") == PAGE_SCHEMA)
            _static_record(candidate)
            _need(candidate.get("page_id") == row["page_id"] and candidate.get("status") == row["status"] and candidate.get("geometry_candidate") is row["geometry_candidate"], "ocr_reading_order_invalid")
            _need(candidate.get("source_sha256") == source, "ocr_reading_order_source_mismatch")
            _need(candidate.get("algorithm_sha256") == alg, "ocr_reading_order_invalid")
            _need(candidate["page_id"] not in seen, "ocr_reading_order_invalid"); seen.add(candidate["page_id"])
            page = _db_page(database, candidate["page_id"])
            _need(candidate.get("collection_id") == page["collection_id"] and candidate.get("ordinal") == page["ordinal"] and candidate.get("image_sha256") == page["image_sha256"] and candidate.get("receipt_sha256") == page["ocr_sha256"], "ocr_reading_order_page_mismatch")
            items.append({"id": candidate["page_id"], "candidate_sha256": candidate_sha, "page": page, "candidate": candidate, "file": name})
        candidate_count=sum(x["candidate"]["geometry_candidate"] for x in items)
        unclassified_count=sum(x["candidate"]["status"] != "geometry_candidate_unverified" for x in items)
        for key, expected in (("scope_pages", MAX_FILES), ("candidate_pages", candidate_count), ("unclassified_pages", unclassified_count), ("rejected_pages", 0)):
            _need(type(verify.get(key)) is int and verify[key] == expected, "ocr_reading_order_invalid")
        _need(database.source_sha256(refresh=True) == source, "ocr_reading_order_source_mismatch")
        _need(_flat_names(directory) == names_before, "ocr_reading_order_invalid")
        for name, expected in [("manifest.json", _sha(mraw)), ("verification.json", _sha(vraw))] + [(item["file"], item["candidate_sha256"]) for item in items]:
            check_raw, _ = _read_file(directory, name)
            _need(_sha(check_raw) == expected, "ocr_reading_order_hash_mismatch")
        _need(database.source_sha256(refresh=True) == source, "ocr_reading_order_source_mismatch")
        return directory, source, _sha(mraw), inv, alg, helper, items
    except OCRReadingOrderIndexError:
        raise
    except Exception:
        raise OCRReadingOrderIndexError("ocr_reading_order_unavailable") from None

CATALOG_SCHEMA = "corpus-reading-order-manifest-v1"
CATALOG_VERIFY_SCHEMA = "corpus-reading-order-verification-v1"
CATALOG_PAGE_SCHEMA = "corpus-reading-order-page-v1"
MAX_PAGES = 4096
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_LIST_ITEMS = 100
REJECT_STATUS = "geometry_input_rejected"
REJECT_CODES = {"page_metadata_mismatch", "receipt_unavailable", "receipt_invalid",
                "receipt_binding_mismatch", "geometry_invalid", "geometry_limits_exceeded", "image_metadata_invalid"}
COUNT_KEYS = {"page_count", "classified_count", "unclassified_count", "rejected_count", "unattempted_count"}
CATALOG_KEYS = {"schema", "source_sha256", "plan_sha256", "algorithm_sha256", "helper_module_sha256", "flags", "pages"} | COUNT_KEYS
CATALOG_ROW_KEYS = {"page_id", "file", "candidate_sha256", "collection_id", "ordinal", "book_key",
    "image_sha256", "ocr_sha256", "width", "height", "status", "geometry_candidate",
    "raw_word_count", "raw_line_count", "segment_count", "error_code"}
CATALOG_VERIFY_KEYS = {"schema", "verified_at_utc", "source_sha256", "plan_sha256", "manifest_sha256",
    "algorithm_sha256", "helper_module_sha256", "plan_file_sha256", "progress_file_sha256", "flags",
    "source_unchanged", "inputs_unchanged", "outputs_unchanged", "corpus_updated", "model_calls", "complete"} | COUNT_KEYS


def _flags(flags):
    _need(type(flags) is dict and set(flags) == set(FLAGS)
          and all(type(flags[k]) is int and flags[k] == v for k, v in FLAGS.items()))


def _leaf_stat(path):
    info = path.lstat()
    _need(stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
          "ocr_reading_order_invalid")
    return info


def _catalog_bytes(directory, name, maximum):
    # The root's ancestors are checked once by _config_dir. Leaf identity and
    # no-follow opens protect each admitted file without walking 4096 ancestors.
    path = directory / name
    before = _leaf_stat(path)
    _need(before.st_size <= maximum, "ocr_reading_order_limits_exceeded")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        _need((opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino))
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    final = _leaf_stat(path)
    signature = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns)
    _need(signature(before) == signature(after) == signature(final)
          and len(raw) <= maximum, "ocr_reading_order_hash_mismatch")
    return raw


def _catalog_names(directory, expected):
    root = directory.lstat()
    _need(stat.S_ISDIR(root.st_mode) and not getattr(root, "st_file_attributes", 0) & 0x400)
    names = set()
    with os.scandir(directory) as entries:
        for number, entry in enumerate(entries, 1):
            _need(number <= MAX_PAGES + 4, "ocr_reading_order_limits_exceeded")
            info = entry.stat(follow_symlinks=False)
            _need(stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400)
            _need(entry.name in expected and entry.name not in names)
            maximum = MAX_MANIFEST_BYTES if entry.name in {"manifest.json", "verification.json", "plan.json", "progress.json"} else MAX_FILE_BYTES
            _need(info.st_size <= maximum, "ocr_reading_order_limits_exceeded")
            names.add(entry.name)
    _need(names == expected)
    return root.st_dev, root.st_ino


def _catalog_db_pages(database, rows):
    needed = {"id", "collection_id", "ordinal", "image_path", "image_sha256", "width", "height", "ocr_status", "ocr_sha256"}
    _need(needed <= database.columns("scan_pages") and {"id", "book_key"} <= database.columns("scan_collections"),
          "ocr_reading_order_source_unavailable")
    ids = [row["page_id"] for row in rows]
    placeholders = ",".join("?" for _ in ids)
    found = database.query("SELECT p.id,p.collection_id,p.ordinal,p.image_path,p.image_sha256,p.width,p.height,p.ocr_status,p.ocr_sha256,c.book_key "
        "FROM scan_pages p JOIN scan_collections c ON c.id=p.collection_id WHERE p.id IN (" + placeholders + ") LIMIT ?", [*ids, MAX_PAGES + 1])
    _need(len(found) == len(rows), "ocr_reading_order_page_mismatch")
    pages = {}
    for record in found:
        page = dict(record)
        _need(page["id"] not in pages, "ocr_reading_order_page_mismatch")
        pages[page["id"]] = page
    for row in rows:
        page = pages.get(row["page_id"])
        _need(page is not None and page["ocr_status"] == "completed_unverified"
              and all(page[key] == row[key] and type(page[key]) is type(row[key])
                      for key in ("collection_id", "ordinal", "book_key", "image_sha256", "ocr_sha256", "width", "height")),
              "ocr_reading_order_page_mismatch")
    return pages


def _catalog_row(row, number):
    _need(type(row) is dict and set(row) == CATALOG_ROW_KEYS)
    _need(type(row["page_id"]) is str and PAGE_ID.fullmatch(row["page_id"])
          and row["file"] == f"geometry-{number:06d}.json")
    for key in ("candidate_sha256", "image_sha256", "ocr_sha256"):
        _need(_hash(row[key]))
    _need(type(row["collection_id"]) is str and PAGE_ID.fullmatch(row["collection_id"])
          and type(row["book_key"]) is str and 1 <= len(row["book_key"]) <= 200)
    _need(_integer(row["ordinal"], 1) and _integer(row["width"], 1) and _integer(row["height"], 1))
    _need(type(row["geometry_candidate"]) is bool and row["status"] in STATUSES | {REJECT_STATUS})
    if row["status"] == REJECT_STATUS:
        _need(row["geometry_candidate"] is False and row["error_code"] in REJECT_CODES
              and all(row[k] is None for k in ("raw_word_count", "raw_line_count", "segment_count")))
    else:
        _need(row["error_code"] is None and _integer(row["raw_word_count"]) and row["raw_word_count"] <= 100000
              and _integer(row["raw_line_count"]) and row["raw_line_count"] <= 20000
              and _integer(row["segment_count"]) and row["segment_count"] <= 100000)
        classified = row["status"] == "geometry_candidate_unverified"
        _need(row["geometry_candidate"] is classified and (row["segment_count"] > 0 if classified else row["segment_count"] == 0))
        _need((row["raw_word_count"] > 0) if row["status"] != "empty_geometry_unclassified" else row["raw_word_count"] == 0)


def _catalog_guard(directory, database, source, shared):
    _need(_catalog_names(directory, shared["names"]) == shared["root_identity"])
    for name, digest in shared["hashes"].items():
        _need(_sha(_catalog_bytes(directory, name, MAX_MANIFEST_BYTES)) == digest, "ocr_reading_order_hash_mismatch")
    _need(database.source_sha256(refresh=True) == source, "ocr_reading_order_source_mismatch")


def _load_catalog(corpus, database, directory, mraw, manifest):
    _need(type(manifest) is dict and set(manifest) == CATALOG_KEYS and manifest.get("schema") == CATALOG_SCHEMA)
    rows = manifest.get("pages")
    _need(type(rows) is list and 1 <= len(rows) <= MAX_PAGES, "ocr_reading_order_limits_exceeded")
    _flags(manifest["flags"])
    for key in ("source_sha256", "plan_sha256", "algorithm_sha256", "helper_module_sha256"):
        _need(_hash(manifest[key]))
    for number, row in enumerate(rows, 1):
        _catalog_row(row, number)
    _need(len({row["page_id"] for row in rows}) == len(rows))
    expected = {"manifest.json", "verification.json", "plan.json", "progress.json"} | {row["file"] for row in rows}
    identity = _catalog_names(directory, expected)
    vraw = _catalog_bytes(directory, "verification.json", MAX_MANIFEST_BYTES)
    verify = _safe_json(vraw)
    _need(type(verify) is dict and set(verify) == CATALOG_VERIFY_KEYS and verify["schema"] == CATALOG_VERIFY_SCHEMA)
    _need(type(verify["verified_at_utc"]) is str and 1 <= len(verify["verified_at_utc"]) <= 80)
    _flags(verify["flags"])
    for key in ("source_sha256", "plan_sha256", "algorithm_sha256", "helper_module_sha256"):
        _need(verify[key] == manifest[key])
    _need(verify["manifest_sha256"] == _sha(mraw))
    _need(all(verify[k] is True for k in ("source_unchanged", "inputs_unchanged", "outputs_unchanged", "complete"))
          and verify["corpus_updated"] is False and type(verify["model_calls"]) is int and verify["model_calls"] == 0)
    counts = {"page_count":len(rows), "classified_count":sum(row["geometry_candidate"] for row in rows),
              "rejected_count":sum(row["status"] == REJECT_STATUS for row in rows), "unattempted_count":0}
    counts["unclassified_count"] = len(rows) - counts["classified_count"] - counts["rejected_count"]
    _need(all(type(manifest[k]) is int and type(verify[k]) is int and manifest[k] == verify[k] == v for k,v in counts.items()))
    hashes = {"manifest.json":_sha(mraw), "verification.json":_sha(vraw)}
    for name, key in (("plan.json", "plan_file_sha256"), ("progress.json", "progress_file_sha256")):
        _need(_hash(verify[key]))
        hashes[name] = _sha(_catalog_bytes(directory, name, MAX_MANIFEST_BYTES))
        _need(hashes[name] == verify[key], "ocr_reading_order_hash_mismatch")
    database.require()
    source = database.source_sha256()
    _need(source == manifest["source_sha256"], "ocr_reading_order_source_mismatch")
    pages = _catalog_db_pages(database, rows)
    shared = {"hashes":hashes, "names":expected, "root_identity":identity}
    items = [{"id":row["page_id"], "candidate_sha256":row["candidate_sha256"], "page":pages[row["page_id"]],
              "candidate":row, "file":row["file"], "catalog":shared} for row in rows]
    _catalog_guard(directory, database, source, shared)
    return directory, source, _sha(mraw), manifest["plan_sha256"], manifest["algorithm_sha256"], manifest["helper_module_sha256"], items


def _load(corpus, database):
    try:
        directory = _config_dir(corpus)
        mraw = _catalog_bytes(directory, "manifest.json", MAX_MANIFEST_BYTES)
        manifest = _safe_json(mraw)
        if type(manifest) is dict and manifest.get("schema") == CATALOG_SCHEMA:
            return _load_catalog(corpus, database, directory, mraw, manifest)
        return _load_legacy(corpus, database)
    except OCRReadingOrderIndexError:
        raise
    except Exception:
        raise OCRReadingOrderIndexError("ocr_reading_order_unavailable") from None


def _coord(v):
    return type(v) in (int, float) and math.isfinite(v)

def _ref(value, lines):
    _need(type(value) in (list, tuple) and len(value) == 2 and _integer(value[0]) and _integer(value[1]), "ocr_reading_order_invalid")
    li, wi = value
    _need(li < len(lines) and wi < len(lines[li]["words"]), "ocr_reading_order_invalid")
    return [li, wi]

def _receipt_geometry(corpus, page, candidate):
    # Construct the only allowed receipt path from database identity fields.
    _need(re.fullmatch(r"[A-Za-z0-9._-]{1,200}", page["collection_id"]) is not None, "ocr_reading_order_receipt_invalid")
    receipt_path = Path(corpus.root) / "private" / "ocr" / f"{page['collection_id']}_{page['ordinal']:04d}.json"
    try:
        raw = vision._bytes(receipt_path, MAX_RECEIPT_BYTES)
    except Exception:
        raise OCRReadingOrderIndexError("ocr_reading_order_receipt_unavailable") from None
    _need(_sha(raw) == page["ocr_sha256"] == candidate["receipt_sha256"], "ocr_reading_order_receipt_mismatch")
    receipt = _safe_json(raw)
    _need(type(receipt) is dict and receipt.get("schema") == "haruhi-page-ocr-v1" and receipt.get("status") == "completed", "ocr_reading_order_receipt_invalid")
    _need(receipt.get("page_id") == page["id"] and receipt.get("image_sha256") == page["image_sha256"] and receipt.get("width") == page["width"] and receipt.get("height") == page["height"], "ocr_reading_order_receipt_mismatch")
    raw_lines = receipt.get("lines")
    _need(type(raw_lines) is list and len(raw_lines) <= 20000, "ocr_reading_order_receipt_invalid")
    lines=[]; total=0
    for line in raw_lines:
        _need(type(line) is dict and type(line.get("words")) is list, "ocr_reading_order_receipt_invalid")
        words=[]
        for word in line["words"]:
            total+=1; _need(total <= 100000, "ocr_reading_order_limits_exceeded")
            _need(type(word) is dict and all(k in word for k in ("x","y","w","h")), "ocr_reading_order_receipt_invalid")
            x,y,w,h=(word[k] for k in ("x","y","w","h"))
            _need(all(_coord(v) for v in (x,y,w,h)) and w>0 and h>0 and x>=0 and y>=0 and x+w<=page["width"] and y+h<=page["height"], "ocr_reading_order_receipt_invalid")
            words.append({"x":x,"y":y,"w":w,"h":h})
        lines.append({"words":words})
    return raw, lines, total

def _validate_geometry(candidate, lines, width, height, source_word_count):
    for k,v in FLAGS.items(): _need(type(candidate.get(k)) is int and candidate[k] == v, "ocr_reading_order_invalid")
    _need(type(candidate["geometry_candidate"]) is bool and candidate["status"] in STATUSES, "ocr_reading_order_invalid")
    _need(type(candidate["word_order"]) is list and type(candidate["segments"]) is list, "ocr_reading_order_invalid")
    allrefs=[[li,wi] for li,line in enumerate(lines) for wi in range(len(line["words"]))]
    order=[_ref(x,lines) for x in candidate["word_order"]]
    _need(len(order)==source_word_count==len(allrefs) and len({tuple(x) for x in order})==len(allrefs) and set(map(tuple,order))==set(map(tuple,allrefs)), "ocr_reading_order_invalid")
    _need(candidate["raw_word_count"]==source_word_count and _integer(candidate["raw_line_count"]) and candidate["raw_line_count"]==len(lines), "ocr_reading_order_invalid")
    for key in ("raw_backward_band_steps", "new_backward_band_steps"):
        _need(_integer(candidate.get(key)), "ocr_reading_order_invalid")
    _need(type(candidate.get("book_key")) is str and 1 <= len(candidate["book_key"]) <= 200, "ocr_reading_order_invalid")
    _need(candidate.get("method") == "vertical-word-bands-v2", "ocr_reading_order_invalid")
    status=candidate["status"]
    if status != "geometry_candidate_unverified":
        _need(order==allrefs, "ocr_reading_order_invalid")
        _need(candidate["geometry_candidate"] is False and candidate["layout"]=="unclassified" and candidate["segments"]==[] and candidate["bands"]==[] and candidate["main"]==[] and candidate["ruby"]==[] and candidate["other"]==[] and ((status=="empty_geometry_unclassified" and source_word_count==0) or (status=="unclassified_geometry_only" and source_word_count>0)), "ocr_reading_order_invalid")
        return
    _need(candidate["geometry_candidate"] is True and source_word_count>0 and candidate["layout"] in {"vertical_single_band","vertical_multi_band"}, "ocr_reading_order_invalid")
    bands=candidate["bands"]
    _need(type(bands) is list and 1<=len(bands)<=20000, "ocr_reading_order_invalid")
    for band in bands:
        _need(type(band) in (list,tuple) and len(band)==2 and all(_coord(v) for v in band) and 0<=band[0]<=band[1]<=height, "ocr_reading_order_invalid")
    _need(len(candidate["segments"])<=100000, "ocr_reading_order_limits_exceeded")
    flat=[]; partitions={"main":[],"ruby":[],"other":[]}
    for i,seg in enumerate(candidate["segments"]):
        _need(type(seg) is dict and set(seg)==SEGMENT_KEYS and seg.get("role") in partitions, "ocr_reading_order_invalid")
        line_index=seg.get("source_line"); _need(_integer(line_index) and line_index<len(lines), "ocr_reading_order_invalid")
        refs=[_ref(r,lines) for r in seg["word_refs"]]
        _need(refs and all(r[0]==line_index for r in refs), "ocr_reading_order_invalid")
        sorted_refs=sorted(refs,key=lambda r:(lines[r[0]]["words"][r[1]]["y"],r[1]))
        _need(refs==sorted_refs, "ocr_reading_order_invalid")
        boxes=[]
        for li,wi in refs:
            w=lines[li]["words"][wi]; boxes.append((w["x"],w["y"],w["x"]+w["w"],w["y"]+w["h"]))
        box=[min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)]
        _need(type(seg["box"]) in (list,tuple) and len(seg["box"])==4 and all(_coord(x) for x in seg["box"]) and tuple(seg["box"])==tuple(box), "ocr_reading_order_invalid")
        _need(_integer(seg.get("band")) and seg["band"]<len(bands) and _integer(seg.get("column")), "ocr_reading_order_invalid")
        # The producer assigns every word to the nearest band midpoint;
        # low-occupancy headers can lie outside the band's occupied interval.
        for li, wi in refs:
            word = lines[li]["words"][wi]
            nearest = min(range(len(bands)), key=lambda k: abs(
                word["y"] + word["h"] / 2 - sum(bands[k]) / 2))
            _need(nearest == seg["band"], "ocr_reading_order_invalid")
        flat.extend(refs); partitions[seg["role"]].append(i)
    _need(flat==order, "ocr_reading_order_invalid")
    _need(candidate["segments"] == sorted(candidate["segments"], key=lambda s:
          (s["band"], s["column"], s["box"][1])), "ocr_reading_order_invalid")
    for role, indices in partitions.items():
        got=candidate[role]; _need(type(got) is list and all(_integer(i) and i<len(candidate["segments"]) for i in got) and got==indices, "ocr_reading_order_invalid")
    _need(sorted(sum(partitions.values(),[]))==list(range(len(candidate["segments"]))) and len(sum(partitions.values(),[]))==len(set(sum(partitions.values(),[]))), "ocr_reading_order_invalid")

def metadata(item):
    c = item["candidate"]
    result = {"id":item["id"], "candidate_sha256":item["candidate_sha256"], "page_id":c["page_id"],
        "scan_ordinal":item["page"]["ordinal"], "status":c["status"], "geometry_candidate":c["geometry_candidate"],
        "raw_word_count":c["raw_word_count"], "segment_count":c["segment_count"] if "catalog" in item else len(c["segments"])}
    if "catalog" in item:
        result.update(collection_id=c["collection_id"], book_key=c["book_key"], raw_line_count=c["raw_line_count"], error_code=c["error_code"])
    return result


def overview(corpus, database):
    if getattr(corpus, "ocr_reading_order_candidates", None) is None:
        return {"available":False, "error":None}
    try:
        *_, items = _load(corpus, database)
        classified = sum(i["candidate"]["geometry_candidate"] for i in items)
        rejected = sum(i["candidate"]["status"] == REJECT_STATUS for i in items)
        result = {"available":True, "total":len(items), "candidate_count":classified, "classified_count":classified,
                  "unclassified_count":len(items)-classified-rejected}
        if items and "catalog" in items[0]:
            groups = {}
            for item in items:
                c = item["candidate"]
                group = groups.setdefault(c["collection_id"], {"collection_id":c["collection_id"], "book_key":c["book_key"],
                    "page_count":0, "classified_count":0, "unclassified_count":0, "rejected_count":0})
                _need(group["book_key"] == c["book_key"])
                group["page_count"] += 1
                key = "rejected_count" if c["status"] == REJECT_STATUS else "classified_count" if c["geometry_candidate"] else "unclassified_count"
                group[key] += 1
            result.update(rejected_count=rejected, metadata_only=True, detail_validation_count=None,
                          collections=list(groups.values()))
        return result
    except OCRReadingOrderIndexError as exc:
        return {"available":False, "error":exc.code}


def list_candidates(corpus, database, *, offset=0, limit=MAX_LIST_ITEMS, collection_id=None):
    empty = {"available":False, "error":None, "total":0, "items":[]}
    if getattr(corpus, "ocr_reading_order_candidates", None) is None:
        return empty
    try:
        _need(_integer(offset) and type(limit) is int and 1 <= limit <= MAX_LIST_ITEMS,
              "ocr_reading_order_invalid_pagination")
        _need(collection_id is None or type(collection_id) is str and PAGE_ID.fullmatch(collection_id),
              "ocr_reading_order_invalid_collection")
        *_, items = _load(corpus, database)
        catalog = bool(items and "catalog" in items[0])
        if collection_id is not None:
            items = [item for item in items if item["candidate"]["collection_id"] == collection_id]
        result = {"available":True, "total":len(items), "items":[metadata(i) for i in items[offset:offset+limit]]}
        if catalog:
            result.update(offset=offset, limit=limit, metadata_only=True)
        return result
    except OCRReadingOrderIndexError as exc:
        return {**empty, "error":exc.code}


def _catalog_receipt_bytes(corpus, page, *, optional=False):
    _need(re.fullmatch(r"[A-Za-z0-9._-]{1,200}", page["collection_id"]) is not None,
          "ocr_reading_order_receipt_invalid")
    path = Path(corpus.root) / "private" / "ocr" / f"{page['collection_id']}_{page['ordinal']:04d}.json"
    try:
        raw = vision._bytes(path, MAX_RECEIPT_BYTES)
    except Exception:
        if optional:
            return None
        raise OCRReadingOrderIndexError("ocr_reading_order_receipt_unavailable") from None
    _need(_sha(raw) == page["ocr_sha256"], "ocr_reading_order_receipt_mismatch")
    return raw


def _catalog_detail(corpus, database, directory, source, item, algorithm):
    row, page = item["candidate"], item["page"]
    raw = _catalog_bytes(directory, item["file"], MAX_FILE_BYTES)
    _need(_sha(raw) == item["candidate_sha256"], "ocr_reading_order_hash_mismatch")
    candidate = _safe_json(raw)
    _need(type(candidate) is dict and candidate.get("schema") == CATALOG_PAGE_SCHEMA)
    _static_record(candidate)
    _need(candidate["source_sha256"] == source and candidate["algorithm_sha256"] == algorithm,
          "ocr_reading_order_source_mismatch")
    for key in ("page_id", "collection_id", "ordinal", "book_key", "image_sha256", "status", "geometry_candidate", "raw_word_count", "raw_line_count", "error_code"):
        _need(candidate.get(key) == row[key] and type(candidate.get(key)) is type(row[key]), "ocr_reading_order_page_mismatch")
    _need(candidate["receipt_sha256"] == row["ocr_sha256"], "ocr_reading_order_receipt_mismatch")
    rejected = candidate["status"] == REJECT_STATUS
    _need(row["segment_count"] == (None if rejected else len(candidate["segments"])))
    if rejected:
        receipt_raw = _catalog_receipt_bytes(corpus, page, optional=True)
    else:
        receipt_raw, lines, count = _receipt_geometry(corpus, page, candidate)
        _validate_geometry(candidate, lines, page["width"], page["height"], count)
    _catalog_guard(directory, database, source, item["catalog"])
    _need(_sha(_catalog_bytes(directory, item["file"], MAX_FILE_BYTES)) == item["candidate_sha256"], "ocr_reading_order_hash_mismatch")
    receipt_again = _catalog_receipt_bytes(corpus, page, optional=rejected)
    _need((receipt_raw is None) == (receipt_again is None)
          and (receipt_raw is None or _sha(receipt_raw) == _sha(receipt_again)), "ocr_reading_order_receipt_mismatch")
    _need(database.source_sha256(refresh=True) == source, "ocr_reading_order_source_mismatch")
    return candidate, not rejected


def read_candidate(corpus, database, identity, expected_sha):
    try:
        directory, source, manifest_sha, inventory, algorithm, helper, items = _load(corpus, database)
        _need(type(identity) is str and type(expected_sha) is str and _hash(expected_sha))
        matches = [i for i in items if i["id"] == identity]
        _need(len(matches) == 1 and matches[0]["candidate_sha256"] == expected_sha, "ocr_reading_order_hash_mismatch")
        item = matches[0]; page = item["page"]
        if "catalog" in item:
            candidate, verified = _catalog_detail(corpus, database, directory, source, item, algorithm)
            return {"id":identity, "candidate_sha256":expected_sha, "width":page["width"], "height":page["height"],
                "manifest_sha256":manifest_sha, "plan_sha256":inventory, "algorithm_sha256":algorithm,
                "helper_module_sha256":helper, "candidate":candidate, "image_bound":True,
                "receipt_geometry_validated":verified}
        candidate = item["candidate"]
        receipt_raw, lines, count = _receipt_geometry(corpus, page, candidate)
        _validate_geometry(candidate, lines, page["width"], page["height"], count)
        _need(database.source_sha256(refresh=True) == source, "ocr_reading_order_source_mismatch")
        raw, _ = _read_file(directory, item["file"])
        _need(_sha(raw) == expected_sha, "ocr_reading_order_hash_mismatch")
        receipt_path = Path(corpus.root)/"private"/"ocr"/f"{page['collection_id']}_{page['ordinal']:04d}.json"
        _need(_sha(vision._bytes(receipt_path, MAX_RECEIPT_BYTES)) == _sha(receipt_raw), "ocr_reading_order_receipt_mismatch")
        return {"id":identity, "candidate_sha256":expected_sha, "width":page["width"], "height":page["height"],
            "inventory_sha256":inventory, "manifest_sha256":manifest_sha, "algorithm_sha256":algorithm,
            "helper_sha256":helper, "candidate":candidate}
    except OCRReadingOrderIndexError as exc:
        return {"available":False, "error":exc.code}
    except Exception:
        return {"available":False, "error":"ocr_reading_order_unavailable"}
