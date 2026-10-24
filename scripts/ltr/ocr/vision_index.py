"""Bounded read-only projection of external OCR vision candidate files.

Private OCR text is returned only by explicit candidate detail requests. No
candidate becomes official OCR, a review decision, or a reference source.
"""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import re
import stat

from ltr.ocr import vision_candidate as contract

MAX_FILES = 200
MAX_DIRECTORY_ENTRIES = 2048
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 64 * 1024 * 1024
MAX_BASELINE_PARAGRAPHS = 500000
MAX_BASELINE_ROWS = 100
MAX_BASELINE_ROW_CHARS = 1000
MAX_BASELINE_TOTAL_CHARS = 50000
MAX_BASELINE_LOCATOR_CHARS = 16384
MAX_BASELINE_EDITIONS = 10000
MAX_BASELINE_METADATA_CHARS = 16384
ID_PATTERN = re.compile(r"candidate-[A-Za-z0-9._-]{1,120}\.json")


class OCRVisionIndexError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise OCRVisionIndexError(code)


def _no_links(path):
    # Windows junctions/reparse points must not bypass a lexical root check.
    for current in (path, *path.parents):
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise OCRVisionIndexError("ocr_vision_path_unsafe")


def validate_directory_path(path, corpus_root):
    lexical = Path(path).absolute()
    # Missing external directories are a runtime availability issue, not a
    # reason to create them. Check existing ancestors without following links.
    for current in (lexical, *lexical.parents):
        if os.path.lexists(current):
            _no_links(current)
            break
    resolved = lexical.resolve()
    root = Path(corpus_root).resolve()
    _require(resolved != root and root not in resolved.parents, "ocr_vision_directory_must_be_external")
    return lexical


def _files(directory):
    _require(directory.exists(), "ocr_vision_directory_unavailable")
    _no_links(directory)
    _require(directory.is_dir(), "ocr_vision_directory_unavailable")
    paths = []
    with os.scandir(directory) as entries:
        for number, entry in enumerate(entries, 1):
            _require(number <= MAX_DIRECTORY_ENTRIES, "ocr_vision_limits_exceeded")
            path = Path(entry.path)
            _no_links(path)
            # This surface is deliberately flat. Nested directories cannot hide
            # additional candidate trees or alternate file-resolution routes.
            _require(entry.is_file(follow_symlinks=False), "ocr_vision_path_unsafe")
            if entry.name.startswith("candidate-") and entry.name.endswith(".json"):
                _require(ID_PATTERN.fullmatch(entry.name), "ocr_vision_path_unsafe")
                paths.append(path)
                _require(len(paths) <= MAX_FILES, "ocr_vision_limits_exceeded")
    return sorted(paths, key=lambda path: path.name)


def _bytes(path, maximum):
    _no_links(path)
    before = path.lstat()
    _require(stat.S_ISREG(before.st_mode), "ocr_vision_path_unsafe")
    _require(before.st_size <= maximum, "ocr_vision_limits_exceeded")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        _require((before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino), "ocr_vision_candidates_changed")
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    _require(len(raw) <= maximum, "ocr_vision_limits_exceeded")
    _no_links(path)
    final = path.lstat()
    _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
             (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) ==
             (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns), "ocr_vision_candidates_changed")
    return raw


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "ocr_vision_candidate_invalid")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique)


def validated_image_path(corpus, raw):
    from ltr.workbench import _resolved_scan_path
    resolved = _resolved_scan_path(corpus, raw)
    raw_path = Path(raw)
    if raw_path.is_absolute():
        lexical = raw_path
    elif raw_path.parts[:2] == ("private", "scans"):
        lexical = corpus.root / raw_path
    else:
        lexical = corpus.scan_root / raw_path
    _no_links(lexical)
    return resolved


def read_candidates(corpus, database):
    """Validate a complete frozen directory; return private candidates in memory."""
    from ltr.workbench import WorkbenchError
    _require(corpus.ocr_vision_candidates is not None, "ocr_vision_not_configured")
    try:
        directory = validate_directory_path(corpus.ocr_vision_candidates, corpus.root)
        paths = _files(directory)
        database.require()
        baseline = database.source_sha256()
        needed = {"id", "image_path", "image_sha256", "width", "height"}
        _require(needed <= database.columns("scan_pages"), "ocr_vision_scan_metadata_unavailable")
        snapshots, image_snapshots, page_cache, items = [], {}, {}, []
        total = image_total = 0
        for path in paths:
            raw = _bytes(path, MAX_FILE_BYTES)
            total += len(raw)
            _require(total <= MAX_TOTAL_BYTES, "ocr_vision_limits_exceeded")
            sha = hashlib.sha256(raw).hexdigest()
            snapshots.append((path.name, sha))
            candidate = _json(raw)
            _require(type(candidate) is dict, "ocr_vision_candidate_invalid")
            _require(candidate.get("source_sha256") == baseline, "ocr_vision_source_mismatch")
            page_id = candidate.get("page_id")
            _require(type(page_id) is str and re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", page_id), "ocr_vision_page_mismatch")
            if page_id not in page_cache:
                ordinal = database._pick("scan_pages", "ordinal")
                rows = database.query('SELECT id,image_path,image_sha256,width,height,' + ordinal +
                                      ' AS ordinal FROM scan_pages WHERE id=?', [page_id])
                _require(len(rows) == 1, "ocr_vision_page_mismatch")
                page = dict(rows[0])
                image = validated_image_path(corpus, page["image_path"])
                image_raw = _bytes(image, MAX_IMAGE_BYTES)
                image_total += len(image_raw)
                _require(image_total <= MAX_TOTAL_IMAGE_BYTES, "ocr_vision_limits_exceeded")
                _require(hashlib.sha256(image_raw).hexdigest() == page['image_sha256'], "ocr_vision_image_mismatch")
                image_snapshots[image] = page['image_sha256']
                page_cache[page_id] = page
            page = page_cache[page_id]
            manifest = {'schema': contract.INPUT_SCHEMA, 'source_sha256': baseline, 'page_id': page_id,
                        'image_sha256': page['image_sha256'], 'width': page['width'], 'height': page['height']}
            _require(candidate.get('image_sha256') == page['image_sha256'], 'ocr_vision_image_mismatch')
            validated = contract.validate_candidate(candidate, manifest)
            items.append({'id': path.name, 'candidate_sha256': sha, 'scan_ordinal': page['ordinal'],
                          'metadata': contract.summarize(validated, manifest), 'candidate': validated})
        _require([p.name for p in _files(directory)] == [name for name, _ in snapshots], 'ocr_vision_candidates_changed')
        for name, sha in snapshots:
            _require(hashlib.sha256(_bytes(directory / name, MAX_FILE_BYTES)).hexdigest() == sha, 'ocr_vision_candidates_changed')
        for image, sha in image_snapshots.items():
            _require(hashlib.sha256(_bytes(image, MAX_IMAGE_BYTES)).hexdigest() == sha, 'ocr_vision_image_mismatch')
        database.require()
        _require(database.source_sha256() == baseline, 'ocr_vision_source_mismatch')
        return items
    except OCRVisionIndexError:
        raise
    except contract.OCRVisionCandidateError:
        raise OCRVisionIndexError('ocr_vision_candidate_invalid') from None
    except WorkbenchError as exc:
        code = 'ocr_vision_image_unavailable' if exc.code == 'scan_unavailable' else 'ocr_vision_source_unavailable'
        raise OCRVisionIndexError(code) from None
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise OCRVisionIndexError('ocr_vision_candidates_unreadable') from None


def metadata(item):
    return {key: item[key] for key in ('id', 'candidate_sha256', 'scan_ordinal', 'metadata')}


def _baseline_collection_edition(database, page_id):
    """Resolve one explicit collection mapping; never guess by ID or language."""
    unavailable = "ocr_vision_baseline_edition_mapping_unavailable"
    invalid = "ocr_vision_baseline_edition_mapping_invalid"
    limits = "ocr_vision_baseline_edition_mapping_limit_exceeded"
    conn = database.connect()
    with closing(conn.execute("SELECT typeof(collection_id) AS value_type,length(collection_id) AS value_chars,"
                              "CASE WHEN typeof(collection_id)='text' THEN substr(collection_id,1,201) "
                              "ELSE collection_id END AS collection_id FROM scan_pages WHERE id=? LIMIT 2", (page_id,))) as cursor:
        page, duplicate = cursor.fetchone(), cursor.fetchone()
    _require(page is not None and duplicate is None, unavailable)
    _require(page["value_type"] in {"text", "integer"}
             and (page["value_type"] != "text" or 0 < page["value_chars"] <= 200), invalid)
    collection = page["collection_id"]

    def collection_value(value):
        return ((type(value) is str and 0 < len(value) <= 200)
                or (type(value) is int and -(1 << 63) <= value < (1 << 63)))

    _require(collection_value(collection), invalid)
    with closing(conn.execute("SELECT count(*) FROM (SELECT 1 FROM editions LIMIT ?)",
                              (MAX_BASELINE_EDITIONS + 1,))) as cursor:
        _require(cursor.fetchone()[0] <= MAX_BASELINE_EDITIONS, limits)

    def reject(_value):
        raise OCRVisionIndexError(invalid)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, invalid)
            result[key] = value
        return result

    matched, inspected = [], 0
    sql = ("SELECT typeof(id) AS id_type,length(id) AS id_chars,substr(id,1,201) AS id,"
           "typeof(metadata_json) AS metadata_type,length(metadata_json) AS metadata_chars,"
           "substr(metadata_json,1,?) AS metadata_json FROM editions LIMIT ?")
    with closing(conn.execute(sql, (MAX_BASELINE_METADATA_CHARS + 1, MAX_BASELINE_EDITIONS + 1))) as cursor:
        for row in cursor:
            inspected += 1
            _require(inspected <= MAX_BASELINE_EDITIONS, limits)
            # NULL means absent metadata, not an implicit collection mapping.
            if row["metadata_type"] == "null":
                continue
            _require(row["metadata_type"] == "text" and 0 < row["metadata_chars"] <= MAX_BASELINE_METADATA_CHARS, invalid)
            try:
                metadata = json.loads(row["metadata_json"], object_pairs_hook=unique, parse_constant=reject)
            except (ValueError, TypeError, RecursionError):
                raise OCRVisionIndexError(invalid) from None
            _require(type(metadata) is dict, invalid)
            if "collection_id" not in metadata:
                continue
            value = metadata["collection_id"]
            _require(collection_value(value), invalid)
            if type(value) is not type(collection) or value != collection:
                continue
            _require(row["id_type"] == "text" and 0 < row["id_chars"] <= 200
                     and re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", row["id"]), invalid)
            matched.append(row["id"])
            _require(len(matched) <= 1, "ocr_vision_baseline_edition_mapping_ambiguous")
    _require(len(matched) == 1, unavailable)
    with closing(conn.execute("SELECT 1 FROM editions WHERE id=? LIMIT 2", (matched[0],))) as cursor:
        first, duplicate = cursor.fetchone(), cursor.fetchone()
    _require(first is not None and duplicate is None, "ocr_vision_baseline_edition_mapping_ambiguous")
    return matched[0]


def read_baseline(database, page_id):
    """Project bounded legacy OCR rows by locator, never by paragraph ID prefix.

    The caller must validate the candidate/page/image before and after this read.
    Text is private local-UI output only. Its displayed-prefix hash is explicitly
    distinct from a full source-text hash; the source DB binding covers the latter.
    """
    from ltr.workbench import WorkbenchError, _locator_page_ids
    import sqlite3

    result = {"available": False, "error": None, "rows": [], "total_rows": None,
              "matched_rows": None, "scanned_rows": None, "scan_complete": False,
              "locator_coverage_complete": False, "invalid_locator_count": None,
              "truncated": False, "paragraph_scope": None,
              "ordering": "edition_id_then_original_ordinal",
              "text_hash_scope": "displayed_text",
              "limits": {"paragraphs": MAX_BASELINE_PARAGRAPHS, "rows": MAX_BASELINE_ROWS,
                         "row_chars": MAX_BASELINE_ROW_CHARS, "total_chars": MAX_BASELINE_TOTAL_CHARS,
                         "editions": MAX_BASELINE_EDITIONS, "edition_metadata_chars": MAX_BASELINE_METADATA_CHARS},
              "scope_note": "Legacy OCR engine rows in original edition ordinal order; unverified by humans. Scan ordinal is not a printed page number.",
              **contract.FLAGS}

    def unavailable(code):
        result.update(available=False, error=code, rows=[], total_rows=None)
        return result

    try:
        if not database.has("paragraphs"):
            return unavailable("ocr_vision_baseline_paragraphs_unavailable")
        if not {"edition_id", "id", "ordinal", "text", "locator_json"} <= database.columns("paragraphs"):
            return unavailable("ocr_vision_baseline_locator_unavailable")
        conn = database.connect()
        edition = None
        scope = "all_editions_locator"
        if "edition_id" in database.columns("scan_pages"):
            with closing(conn.execute("SELECT edition_id FROM scan_pages WHERE id=? LIMIT 2", (page_id,))) as cursor:
                first, second = cursor.fetchone(), cursor.fetchone()
            if first is None or second is not None:
                return unavailable("ocr_vision_baseline_page_unavailable")
            edition = first[0]
            if type(edition) is not str or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", edition):
                return unavailable("ocr_vision_baseline_page_unavailable")
            scope = "page_edition"
        elif ("collection_id" in database.columns("scan_pages")
              and {"id", "metadata_json"} <= database.columns("editions")):
            try:
                edition = _baseline_collection_edition(database, page_id)
            except OCRVisionIndexError as exc:
                return unavailable(exc.code)
            scope = "collection_edition"
        where, params = (" WHERE edition_id=?", (edition,)) if edition is not None else ("", ())
        result["paragraph_scope"] = scope
        # Stop at LIMIT+1; never COUNT an unbounded table or load all source text.
        with closing(conn.execute("SELECT count(*) FROM (SELECT 1 FROM paragraphs" + where + " LIMIT ?)",
                                  (*params, MAX_BASELINE_PARAGRAPHS + 1))) as cursor:
            count = cursor.fetchone()[0]
        if count > MAX_BASELINE_PARAGRAPHS:
            return unavailable("ocr_vision_baseline_scan_limit_exceeded")
        selected, invalid, matched, scanned = [], 0, 0, 0
        sql = ("SELECT id,edition_id,ordinal,typeof(locator_json) AS locator_type,"
               "length(locator_json) AS locator_chars,substr(locator_json,1,?) AS locator_json "
               "FROM paragraphs" + where + " ORDER BY edition_id,ordinal,id LIMIT ?")
        with closing(conn.execute(sql, (MAX_BASELINE_LOCATOR_CHARS + 1, *params, MAX_BASELINE_PARAGRAPHS + 1))) as cursor:
            for row in cursor:
                scanned += 1
                if scanned > MAX_BASELINE_PARAGRAPHS:
                    return unavailable("ocr_vision_baseline_scan_limit_exceeded")
                if row["locator_type"] != "text" or not 0 < row["locator_chars"] <= MAX_BASELINE_LOCATOR_CHARS:
                    invalid += 1
                    continue
                try:
                    locator = _json(row["locator_json"].encode("utf-8"))
                    ids = _locator_page_ids(locator)
                except (ValueError, TypeError, RecursionError, UnicodeError):
                    ids = []
                if not ids:
                    invalid += 1
                    continue
                if page_id not in ids:
                    continue
                matched += 1
                if len(selected) < MAX_BASELINE_ROWS:
                    selected.append((row["edition_id"], row["id"], row["ordinal"]))
        result.update(scanned_rows=scanned, scan_complete=True, invalid_locator_count=invalid,
                      matched_rows=matched, locator_coverage_complete=invalid == 0)
        if scanned and invalid == scanned:
            return unavailable("ocr_vision_baseline_locator_unavailable")
        rows, chars, truncated = [], 0, matched > MAX_BASELINE_ROWS
        for edition_id, paragraph_id, ordinal in selected:
            if (type(edition_id) is not str or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", edition_id)
                    or type(paragraph_id) not in (str, int)
                    or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", str(paragraph_id))
                    or type(ordinal) is not int):
                return unavailable("ocr_vision_baseline_row_invalid")
            remaining = MAX_BASELINE_TOTAL_CHARS - chars
            if remaining <= 0:
                truncated = True
                break
            take = min(MAX_BASELINE_ROW_CHARS, remaining)
            # IDs may be local to an edition. Even if a legacy table has no
            # uniqueness constraint, never pick an arbitrary same-key row.
            with closing(conn.execute("SELECT ordinal,typeof(text) AS text_type,length(text) AS text_chars,"
                                      "substr(text,1,?) AS text FROM paragraphs WHERE edition_id IS ? AND id IS ? LIMIT 2",
                                      (take, edition_id, paragraph_id))) as cursor:
                text_row, duplicate = cursor.fetchone(), cursor.fetchone()
            if duplicate is not None:
                return unavailable("ocr_vision_baseline_row_ambiguous")
            if text_row is None or text_row["ordinal"] != ordinal or text_row["text_type"] != "text":
                return unavailable("ocr_vision_baseline_row_invalid")
            text = text_row["text"]
            clipped = text_row["text_chars"] > len(text)
            rows.append({"edition_id": edition_id, "id": str(paragraph_id), "ordinal": ordinal,
                         "text": text, "display_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                         "original_text_chars": text_row["text_chars"], "text_truncated": clipped})
            chars += len(text)
            truncated = truncated or clipped
        result.update(available=True, rows=rows, total_rows=matched if not invalid else None,
                      truncated=truncated)
        return result
    except (sqlite3.Error, WorkbenchError, OSError, ValueError, TypeError, KeyError, UnicodeError):
        return unavailable("ocr_vision_baseline_unavailable")


def overview(corpus, database):
    if corpus.ocr_vision_candidates is None:
        return {'available': False, 'error': None}
    try:
        items = read_candidates(corpus, database)
        return {'available': True, 'candidate_count': len(items),
                'page_count': len({item['candidate']['page_id'] for item in items}), **contract.FLAGS}
    except OCRVisionIndexError as exc:
        return {'available': False, 'error': exc.code}
