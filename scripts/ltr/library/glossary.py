"""Private wording proposals, explicit decisions and immutable version snapshots."""
from __future__ import annotations

from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from .corpus import QuietParser, _stable_path, _sha256
from .source_lineage import readonly

SCHEMA = "ltr-glossary-v1"
STATUSES = ("proposed", "confirmed", "superseded", "rejected")
SOURCE_KINDS = ("japanese_source", "source_epub", "source_text")
DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES('schema','ltr-glossary-v1');
CREATE TABLE entries(
 id TEXT PRIMARY KEY NOT NULL, source_form TEXT NOT NULL CHECK(length(trim(source_form))>0),
 target_form TEXT NOT NULL, kind TEXT NOT NULL, conditions TEXT NOT NULL,
 variants_json TEXT NOT NULL CHECK(json_valid(variants_json) AND json_type(variants_json)='array'),
 sources_json TEXT NOT NULL CHECK(json_valid(sources_json) AND json_type(sources_json)='array'),
 status TEXT NOT NULL CHECK(status IN ('proposed','confirmed','superseded','rejected')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 superseded_by TEXT REFERENCES entries(id), decision_note TEXT NOT NULL,
 CHECK(status!='confirmed' OR length(trim(target_form))>0),
 CHECK((status='superseded')=(superseded_by IS NOT NULL))
);
CREATE INDEX entries_source ON entries(source_form);
CREATE TABLE versions(
 version TEXT PRIMARY KEY, locked_at TEXT NOT NULL, entry_count INTEGER NOT NULL,
 sha256 TEXT NOT NULL, note TEXT NOT NULL, history_json TEXT NOT NULL
);
CREATE TABLE version_entries(
 version TEXT NOT NULL REFERENCES versions(version), id TEXT NOT NULL,
 snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)), PRIMARY KEY(version,id)
);
CREATE TRIGGER versions_no_update BEFORE UPDATE ON versions BEGIN SELECT RAISE(ABORT,'locked'); END;
CREATE TRIGGER versions_no_delete BEFORE DELETE ON versions BEGIN SELECT RAISE(ABORT,'locked'); END;
CREATE TRIGGER snapshots_no_update BEFORE UPDATE ON version_entries BEGIN SELECT RAISE(ABORT,'locked'); END;
CREATE TRIGGER snapshots_no_delete BEFORE DELETE ON version_entries BEGIN SELECT RAISE(ABORT,'locked'); END;
CREATE TRIGGER snapshots_no_append BEFORE INSERT ON version_entries
 WHEN (SELECT count(*) FROM version_entries WHERE version=NEW.version) >=
      (SELECT entry_count FROM versions WHERE version=NEW.version)
 BEGIN SELECT RAISE(ABORT,'locked'); END;
"""


class GlossaryError(ValueError):
    """Fixed codes only; never include user input or SQLite diagnostics."""


def _need(ok, code):
    if not ok:
        raise GlossaryError(code)


def _text(value, required=False):
    _need(isinstance(value, str) and len(value) <= 100000
          and (not required or bool(value.strip())), "glossary_text_invalid")
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def external_path(path, roots=()):
    """Reject lexical links, hard links, Git ancestors and known corpus roots."""
    path = Path(path).absolute()
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        _need(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
              "glossary_path_linked")
        _need(not (part / ".git").exists(), "glossary_inside_git")
        if part != path:
            _need(not (part / "dialogue.sqlite").exists(), "glossary_inside_corpus")
    path = path.resolve()
    _need(not any(path.is_relative_to(Path(root).resolve()) for root in roots), "glossary_inside_corpus")
    if path.exists():
        _need(path.is_file() and path.stat().st_nlink == 1, "glossary_path_invalid")
    return path


@contextmanager
def connect(path, *, write=False, roots=()):
    path = external_path(path, roots)
    _stable_path(path)
    # mode=rw must not create a missing file. Read paths never acquire a writer.
    with closing(sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True)) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        if not write:
            con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            marker = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            _need(marker is not None and marker[0] == SCHEMA, "glossary_schema_invalid")
            saved = con.execute("SELECT value FROM meta WHERE key='corpus_roots'").fetchone()
            if saved:
                external_path(path, json.loads(saved[0]))
            yield con
            if write:
                con.commit()
        finally:
            con.rollback()


def init_store(path):
    path = external_path(path)
    try:
        with path.open("xb"):
            pass
    except FileExistsError:
        raise GlossaryError("glossary_already_exists") from None
    # Failed initialization is retained for inspection, never overwritten on retry.
    with closing(sqlite3.connect(path)) as con:
        con.executescript(DDL)
    return {"status": "initialized", "schema": SCHEMA, "model_calls": 0}


def _entry(con, identity):
    row = con.execute("SELECT * FROM entries WHERE id=?", (identity,)).fetchone()
    _need(row is not None, "glossary_entry_not_found")
    return dict(row)


def _add(con, source, target="", kind="unclassified", conditions="", variants=None, sources=None):
    for value in (source, kind):
        _text(value, True)
    _text(target); _text(conditions)
    variants = [] if variants is None else variants
    sources = [] if sources is None else sources
    _need(isinstance(variants, list) and isinstance(sources, list), "glossary_metadata_invalid")
    identity, now = uuid.uuid4().hex, _now()
    con.execute("INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (identity, source, target, kind, conditions, _json(variants), _json(sources),
                 "proposed", now, now, None, ""))
    return _entry(con, identity)


def add(path, source, target="", kind="unclassified", conditions=""):
    with connect(path, write=True) as con:
        return _add(con, source, target, kind, conditions)


def confirm(path, identity, target, note=""):
    _text(target, True); _text(note)
    with connect(path, write=True) as con:
        row = _entry(con, identity)
        _need(row["status"] in ("proposed", "confirmed"), "glossary_decision_terminal")
        # Changing a confirmed spelling requires a new proposal and supersede,
        # so the original decision remains available to regression checks.
        _need(row["status"] != "confirmed" or row["target_form"] == target,
              "glossary_supersede_required")
        con.execute("UPDATE entries SET target_form=?,status='confirmed',decision_note=?,updated_at=? WHERE id=?",
                    (target, note, _now(), identity))
        return _entry(con, identity)


def decide(path, identity, *, by=None, note):
    _text(note, True)
    with connect(path, write=True) as con:
        row = _entry(con, identity)
        _need(row["status"] in ("proposed", "confirmed"), "glossary_decision_terminal")
        if by is not None:
            replacement = _entry(con, by)
            _need(identity != by and replacement["status"] == "confirmed", "glossary_replacement_invalid")
            _need(row["source_form"] == replacement["source_form"], "glossary_replacement_source_mismatch")
        con.execute("UPDATE entries SET status=?,superseded_by=?,decision_note=?,updated_at=? WHERE id=?",
                    ("superseded" if by else "rejected", by, note, _now(), identity))
        return _entry(con, identity)


def _rows(con, status=None):
    return [dict(r) for r in con.execute("SELECT * FROM entries" + (" WHERE status=?" if status else "")
                                       + " ORDER BY source_form,id", (status,) if status else ())]


def lock(path, version, note=""):
    _text(version, True); _text(note)
    with connect(path, write=True) as con:
        _need(con.execute("SELECT 1 FROM versions WHERE version=?", (version,)).fetchone() is None,
              "glossary_version_exists")
        entries = _rows(con, "confirmed")
        _need(entries, "glossary_empty_lock")
        history = _rows(con, "superseded") + _rows(con, "rejected")
        sha = _digest({"entries": entries, "history": history})
        con.execute("INSERT INTO versions VALUES(?,?,?,?,?,?)", (version, _now(), len(entries), sha, note, _json(history)))
        con.executemany("INSERT INTO version_entries VALUES(?,?,?)", [(version, e["id"], _json(e)) for e in entries])
        return _export(con, version)


def _export(con, version):
    row = con.execute("SELECT * FROM versions WHERE version=?", (version,)).fetchone()
    _need(row is not None, "glossary_version_not_found")
    entries = [json.loads(r[0]) for r in con.execute(
        "SELECT snapshot_json FROM version_entries WHERE version=?", (version,))]
    entries.sort(key=lambda r: (r["source_form"], r["id"]))
    history = json.loads(row["history_json"])
    _need(len(entries) == row["entry_count"] and _digest({"entries": entries, "history": history}) == row["sha256"],
          "glossary_snapshot_mismatch")
    return {"schema": SCHEMA, **{k: row[k] for k in ("version", "locked_at", "entry_count", "sha256", "note")},
            "entries": entries, "history": history,
            "confirmed_glossary": {"version": version, "locked": True, "sha256": row["sha256"],
                                   "entry_count": len(entries), "complete_for_scope": False}}


def export(path, version):
    with connect(path) as con:
        return _export(con, version)


def list_entries(path, status=None, version=None):
    _need(status is None or status in STATUSES, "glossary_status_invalid")
    with connect(path) as con:
        rows = _export(con, version)["entries"] if version is not None else _rows(con)
        return {"items": [r for r in rows if status is None or r["status"] == status]}


def markdown(data):
    def cell(value):
        return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;").replace("\r", "").replace("\n", "<br>")
    lines = ["# 锁定词表 " + cell(data["version"]), "", "条目数：" + str(data["entry_count"]),
             "", "SHA-256：" + data["sha256"], "", "范围完整性尚待人工确认（complete_for_scope=false）。", "",
             "| 原文 | 工作译法 | 类型 | 适用条件 | 变体 | 出处 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for entry in data["entries"]:
        lines.append("| " + " | ".join(cell(entry[k]) for k in
                     ("source_form", "target_form", "kind", "conditions", "variants_json", "sources_json")) + " |")
    return "\n".join(lines)


def _extract(database, book, minimum):
    path = _stable_path(database)
    before = _sha256(path)
    candidates = {}
    brackets = re.compile(r"『([^『』\n]{1,80})』|【([^【】\n]{1,80})】|〈([^〈〉\n]{1,80})〉|《([^《》\n]{1,80})》")
    phrases = re.compile(r"[ァ-ヺー]{2,40}|[一-鿿々]{2,20}")
    with readonly(path) as con:
        con.execute("BEGIN")
        for edition, paragraph, text in con.execute("""SELECT p.edition_id,p.id,p.text FROM paragraphs p
            JOIN editions e ON e.id=p.edition_id WHERE e.origin_kind IN (?,?,?)
            AND (? IS NULL OR e.book_key=?) ORDER BY p.edition_id,p.ordinal,p.id""", (*SOURCE_KINDS, book, book)):
            hits = {}
            for match in brackets.finditer(text):
                group = next(i for i in range(1, 5) if match.group(i) is not None)
                hits[(match.start(group), match.end(group))] = (match.group(group), True)
            for match in phrases.finditer(text):
                hits.setdefault(match.span(), (match.group(), False))
            for (start, end), (term, special) in hits.items():
                if not term.strip():  # e.g. 『　』 used as a visual blank
                    continue
                item = candidates.setdefault(term, {"source_form": term, "sources": [], "special": False})
                item["special"] |= special
                item["sources"].append({"edition_id": edition, "paragraph_id": paragraph,
                    "start": start, "end": end, "paragraph_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "source_sha256": before, "method": "bracket" if special else "repeated_phrase"})
    _stable_path(path)
    _need(before == _sha256(path), "glossary_source_changed")
    results = [r for r in candidates.values() if r["special"] or len(r["sources"]) >= minimum]
    # --limit keeps the strongest proposals: bracketed terms first, then the most repeated.
    results.sort(key=lambda r: (not r["special"], -len(r["sources"]), r["source_form"]))
    for item in results:
        for source in item["sources"]:
            source["occurrence_count"] = len(item["sources"])
    return results, before


def _import_array(item, key):
    value = item[key] if key in item else json.loads(item.get(key + "_json", "[]"))
    _need(isinstance(value, list) and all(isinstance(v, dict) for v in value), "glossary_import_invalid")
    return value


def propose(path, config, corpus_id, *, book=None, limit=100, minimum=2, from_json=None):
    _need(type(limit) is int and 1 <= limit <= 10000 and type(minimum) is int and minimum >= 2,
          "glossary_limits_invalid")
    corpus = config.corpus(corpus_id)
    roots = [c.root for c in config.corpora]
    external_path(path, roots)
    # The existing facade validates configured source snapshots before use.
    from ltr.workbench.store import WorkbenchStore
    from ltr.workbench.database import _verified_evidence
    store = WorkbenchStore(config)
    try:
        _, formal = store.database(corpus_id)
        formal_sha = formal.source_sha256(refresh=True)
        source_config, source = store.evidence_database(corpus_id)
        with _verified_evidence(source):
            proposals, sha = _extract(source_config.db_path, book, minimum)
        _need(formal.source_sha256(refresh=True) == formal_sha, "glossary_source_changed")
    finally:
        store.close()
    if from_json is not None:
        raw = Path(from_json).read_bytes()
        imported = json.loads(raw)
        if isinstance(imported, dict):
            imported = imported.get("entries")
        _need(isinstance(imported, list), "glossary_import_invalid")
        extracted = {p["source_form"]: p for p in proposals}
        imported_rows = []
        for index, item in enumerate(imported):
            _need(isinstance(item, dict), "glossary_import_invalid")
            _text(item.get("source_form"), True)
            # Ignore imported status, id and decision: import is always a proposal.
            sources = _import_array(item, "sources")
            variants = _import_array(item, "variants")
            for key in ("target_form", "kind", "conditions"):
                if key in item:
                    _text(item[key], key == "kind")
            # Preserve imported wording/metadata when the extractor also found
            # the same source; attach fresh occurrences without confirming it.
            fresh = extracted.get(item["source_form"], {}).get("sources", [])
            imported_rows.append({**item, "variants": variants, "sources": [*sources, *fresh,
                {"method": "import", "row": index, "import_sha256": hashlib.sha256(raw).hexdigest(),
                 "occurrence_count": 0}], "special": False})
        proposals = imported_rows + proposals
    added = []
    with connect(path, write=True, roots=roots) as con:
        saved = con.execute("SELECT value FROM meta WHERE key='corpus_roots'").fetchone()
        known = set(json.loads(saved[0]) if saved else []) | {str(r.resolve()) for r in roots}
        con.execute("INSERT OR REPLACE INTO meta VALUES('corpus_roots',?)", (_json(sorted(known)),))
        seen = {r[0] for r in con.execute("SELECT source_form FROM entries")}
        for item in proposals:
            if item["source_form"] in seen:
                continue
            sources = item["sources"]
            added.append(_add(con, item["source_form"], item.get("target_form", ""),
                              item.get("kind", "unclassified"), item.get("conditions", ""),
                              item.get("variants", []), sources))
            seen.add(item["source_form"])
            if len(added) >= limit:
                break
    return {"status": "proposed", "added": len(added), "ids": [r["id"] for r in added],
            "source_sha256": sha, "formal_sha256": formal_sha, "model_calls": 0}


def project_pairs(root):
    from ltr.project.storage import read_project
    project = read_project(Path(root))  # v2 hydrates and verifies bound SQLite rows.
    targets = {u["id"]: u["target"] for u in project["units"]}
    return [{"source": p["text"], "target": targets[p["id"]], "locator": p["id"]} for p in project["source"]]


def check_pairs(data, pairs):
    _need(isinstance(pairs, list) and bool(pairs), "glossary_pairs_invalid")
    for pair in pairs:
        _need(isinstance(pair, dict) and isinstance(pair.get("target"), str)
              and (pair.get("source") is None or isinstance(pair["source"], str))
              and isinstance(pair.get("locator", ""), str), "glossary_pairs_invalid")
    issues, observations = [], {}
    bound = sum(p.get("source") is not None for p in pairs)
    old = data["history"]
    ordinary = {"ordinary", "common", "unclassified", "普通词", "普通同形词", "普通引用", "未分类"}
    for entry in data["entries"]:
        term, target = entry["source_form"], entry["target_form"]
        variants = json.loads(entry["variants_json"])
        allowed = {v["target_form"] for v in variants if isinstance(v, dict)
                   and v.get("allowed") is True and isinstance(v.get("target_form"), str) and v["target_form"]}
        alternatives = {v["target_form"] for v in variants if isinstance(v, dict)
                        and isinstance(v.get("target_form"), str) and v["target_form"]} | {target}
        retired = {r["target_form"] for r in old if r["status"] == "superseded"
                   and r["source_form"] == term and r["target_form"]
                   and r["target_form"] not in allowed | {target}}
        alternatives |= retired
        seen = observations.setdefault(term, set())
        for index, pair in enumerate(pairs):
            source, text = pair.get("source"), pair["target"]
            if source is not None and term not in source:
                continue
            clue = source is None or entry["kind"] in ordinary or bool(entry["conditions"])
            base = {"entry_id": entry["id"], "locator": pair.get("locator", ""), "pair_index": index,
                    "level": "clue" if clue else "finding", "requires_review": True}
            hits = {word for word in alternatives if word in text}
            if source is not None:
                seen.update(hits)
                if not any(word in text for word in allowed | {target}):
                    issues.append({**base, "code": "confirmed_target_missing"})
            elif hits:
                issues.append({**base, "code": "target_string_clue"})
            if retired & hits:
                issues.append({**base, "code": "superseded_target_present"})
    # A replacement can itself be rejected before this lock. Its superseded
    # ancestors still remain relevant even without a current confirmed term.
    confirmed_sources = {e["source_form"] for e in data["entries"]}
    for entry in old:
        if entry["status"] != "superseded" or entry["source_form"] in confirmed_sources or not entry["target_form"]:
            continue
        for index, pair in enumerate(pairs):
            source = pair.get("source")
            if (source is None or entry["source_form"] in source) and entry["target_form"] in pair["target"]:
                clue = source is None or entry["kind"] in ordinary or bool(entry["conditions"])
                issues.append({"code": "superseded_target_present", "entry_id": entry["id"],
                    "pair_index": index, "locator": pair.get("locator", ""),
                    "level": "clue" if clue else "finding", "requires_review": True})
    for term, forms in observations.items():
        if len(forms) > 1:
            issues.append({"code": "multiple_target_forms", "source_form": term,
                           "target_forms": sorted(forms), "level": "clue", "requires_review": True})
    return {"schema": "ltr-glossary-check-v1", "version": data["version"], "sha256": data["sha256"],
            "pair_count": len(pairs), "bound_pair_count": bound, "source_coverage_checked": bound == len(pairs),
            "message": "已完成绑定段落的字面源词覆盖检查；语义与同形词仍待复核。" if bound == len(pairs)
                       else "未完成源词覆盖检查；无源译绑定的段落只报告字符串线索。",
            "issues": issues, "semantic_verified": False, "model_calls": 0}


def query_glossary(store, params):
    from ltr.workbench.common import WorkbenchError
    corpus = store.config.corpus(params.get("corpus"))
    if corpus.glossary is None:
        return {"available": False, "message": "未配置词表；请在库外建立词表并设置 glossary。"}
    try:
        with connect(corpus.glossary, roots=[c.root for c in store.config.corpora]) as con:
            versions = [dict(r) for r in con.execute(
                "SELECT version,locked_at,entry_count,sha256,note FROM versions ORDER BY rowid DESC")]
            version = params.get("version") or (versions[0]["version"] if versions else None)
            selected = _export(con, version) if version else None
            rows = _rows(con)
            groups = {}
            for row in rows:
                if row["status"] == "confirmed":
                    groups.setdefault(row["source_form"], []).append(row["id"])
            conflicts = [{"code": "multiple_confirmed", "source_form": term, "ids": ids}
                         for term, ids in groups.items() if len(ids) > 1]
            conflicts += [{"code": "superseded_link", "id": r["id"], "superseded_by": r["superseded_by"],
                           "decision_note": r["decision_note"]} for r in rows if r["status"] == "superseded"]
            return {"available": True, "versions": versions, "selected": selected,
                    "entries": selected["entries"] if selected else [],
                    "proposals": [r for r in rows if r["status"] == "proposed"], "conflicts": conflicts}
    except GlossaryError as exc:
        raise WorkbenchError(409, str(exc)) from None
    except Exception:
        raise WorkbenchError(409, "glossary_unavailable") from None


def main():
    parser = QuietParser(prog="ltr glossary", description="固定表达提案、人工裁定、版本锁定与只读核对；零模型调用")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "propose", "add", "confirm", "supersede", "reject", "lock", "export", "check", "list"):
        command = commands.add_parser(name)
        command.add_argument("--store", type=Path, required=True)
        if name == "propose":
            command.add_argument("--config", type=Path, required=True)
            command.add_argument("--corpus", required=True)
            command.add_argument("--book")
            command.add_argument("--limit", type=int, default=100)
            command.add_argument("--min-count", type=int, default=2)
            command.add_argument("--from-json", type=Path)
        if name == "add":
            command.add_argument("--source", required=True)
            command.add_argument("--target", default="")
            command.add_argument("--kind", required=True)
            command.add_argument("--conditions", default="")
        if name in ("confirm", "supersede", "reject"):
            command.add_argument("--id", required=True)
        if name == "confirm":
            command.add_argument("--target", required=True)
        if name == "supersede":
            command.add_argument("--by", required=True)
        if name in ("confirm", "supersede", "reject", "lock"):
            command.add_argument("--note", required=name in ("supersede", "reject"), default="")
        if name in ("lock", "export", "check", "list"):
            command.add_argument("--version", required=name != "list")
        if name == "export":
            command.add_argument("--format", choices=("json", "md"), default="json")
        if name == "list":
            command.add_argument("--status", choices=STATUSES)
        if name == "check":
            group = command.add_mutually_exclusive_group(required=True)
            group.add_argument("--project", type=Path)
            group.add_argument("--pairs", type=Path)
    args = parser.parse_args()
    try:
        name, path = args.command, args.store
        if name == "init":
            result = init_store(path)
        elif name == "add":
            result = add(path, args.source, args.target, args.kind, args.conditions)
        elif name == "confirm":
            result = confirm(path, args.id, args.target, args.note)
        elif name in ("supersede", "reject"):
            result = decide(path, args.id, by=getattr(args, "by", None), note=args.note)
        elif name == "lock":
            result = lock(path, args.version, args.note)
        elif name == "export":
            result = export(path, args.version)
            if args.format == "md":
                print(markdown(result))
                return 0
        elif name == "list":
            result = list_entries(path, args.status, args.version)
        elif name == "propose":
            from ltr.workbench.config import WorkbenchConfig
            result = propose(path, WorkbenchConfig.load(args.config), args.corpus, book=args.book,
                             limit=args.limit, minimum=args.min_count, from_json=args.from_json)
        else:
            pairs = project_pairs(args.project) if args.project else json.loads(args.pairs.read_text(encoding="utf-8"))
            result = check_pairs(export(path, args.version), pairs)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except GlossaryError as exc:
        print(json.dumps({"status": "rejected", "code": str(exc)}))
    except Exception:
        print('{"status":"rejected","code":"glossary_operation_failed"}')
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
