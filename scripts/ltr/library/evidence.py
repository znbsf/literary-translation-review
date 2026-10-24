"""Read-only evidence queries shared by the CLI and local workbench.

Search hits never become alignments. Explicit human+semantic+reference flags
are required for verified relationships; model observations remain candidates.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from ltr.library.corpus import _stable_path, _sha256
from ltr.workbench.common import WorkbenchError, _validate_query_text, _validate_id
from ltr.workbench.database import _verified_evidence

SCHEMA = "ltr-evidence-v1"
SOURCE_KINDS = frozenset({"existing_translation", "japanese_source", "source_epub",
    "translation", "historical_translation", "reference_translation", "source_text",
    "scan_ocr", "derived_ocr"})
LEVELS = ("verified", "candidate", "clue")
FLAGS = ("human_confirmed", "semantic_verified", "reference_eligible")


def _json(value, default):
    try:
        return json.loads(value) if value else default
    except (ValueError, TypeError):
        raise WorkbenchError(409, "evidence_json_invalid") from None


def _bounded(value, default, maximum):
    if value in (None, ""):
        return default
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise WorkbenchError(400, "evidence_parameter_invalid")
    number = int(value)
    if number > maximum:
        raise WorkbenchError(400, "evidence_parameter_invalid")
    return number


def _flag(value):
    return type(value) in (int, bool) and value == 1


def _verified(record):
    evidence = _json(record.get("evidence_json"), {})
    extra = evidence.get("qualification", evidence) if isinstance(evidence, dict) else {}
    if not isinstance(extra, dict):
        return False
    flags = {key: record.get(key, extra.get(key)) for key in (*FLAGS, "requires_review")}
    pending = flags["requires_review"]
    review_clear = pending is None or (type(pending) in (int, bool) and pending == 0)
    return all(_flag(flags[key]) for key in FLAGS) and review_clear


def _public(value, limit=200):
    text = str(value or "")
    # Titles and status metadata must not disclose local paths or endpoints.
    if re.search(r"(?i)[a-z]:[\\/]|\\\\|(?:https?://)|/Users/|/home/", text):
        return "[hidden metadata]"
    return "".join(c for c in text if c >= " " or c == "\n")[:limit]


class EvidenceReader:
    def __init__(self, store, params):
        self.store = store
        self.params = params
        self.corpus_id = _validate_id(params.get("corpus"), corpus=True)
        self.mode = params.get("mode", "line")
        if self.mode not in ("line", "speaker", "term"):
            raise WorkbenchError(400, "evidence_mode_invalid")
        self.limit = max(1, _bounded(params.get("limit"), 20, 100))
        self.offset = _bounded(params.get("offset"), 0, 10000)
        self.excerpt = _bounded(params.get("excerpt_chars"), 160, 400)
        raw = params.get("with_text", False)
        if raw not in (False, True, "0", "1", "false", "true", ""):
            raise WorkbenchError(400, "evidence_parameter_invalid")
        self.with_text = raw in (True, "1", "true")
        self.book = _validate_query_text(params.get("book"), limit=200)
        self.lang = _validate_query_text(params.get("lang"), limit=30)
        self.config, self.formal = store.database(self.corpus_id)
        _, self.source = store.evidence_database(self.corpus_id)
        self.indexes = {}
        self.relation_cache = {}
        self.stack = ExitStack()

    def execute(self):
        with self.stack, _verified_evidence(self.source):
            before = self.formal.source_sha256(refresh=True)
            self.source_sha = self.source.source_sha256()
            self.editions = {r["id"]: dict(r) for r in self.source.query("SELECT * FROM editions")}
            if self.mode == "line":
                result = self.line()
            elif self.mode == "speaker":
                result = self.speaker()
            else:
                result = self.term()
            if self.formal.source_sha256(refresh=True) != before:
                raise WorkbenchError(409, "evidence_formal_changed")
            return {"schema": SCHEMA, "mode": self.mode, "corpus": self.corpus_id,
                    "source_sha256": self.source_sha, "formal_sha256": before,
                    "excerpt_chars": self.excerpt, "with_text": self.with_text,
                    "excluded_generated_drafts": True, **result}

    def allowed(self, edition_id):
        edition = self.editions.get(edition_id)
        return bool(edition and edition.get("origin_kind") in SOURCE_KINDS)

    def scoped(self, edition_id):
        edition = self.editions.get(edition_id, {})
        return self.allowed(edition_id) and (not self.book or edition.get("book_key") == self.book)

    def index(self, kind):
        if kind not in self.indexes:
            path = getattr(self.config, {"alignments": "alignment_index",
                "identities": "attribution_index", "candidates": "candidate_index"}[kind])
            if path is None:
                self.indexes[kind] = None
            else:
                _stable_path(path)
                sha = _sha256(path)
                overlay = self.store.overlay(self.corpus_id, kind)._overlay
                con = self.stack.enter_context(overlay.index())
                # Check actual index bytes again on exit, as well as source SHA
                # and individual source-bound rows through the existing reader.
                @contextmanager
                def unchanged():
                    try:
                        yield
                    finally:
                        _stable_path(path)
                        if _sha256(path) != sha:
                            raise WorkbenchError(409, "evidence_index_changed")
                self.stack.enter_context(unchanged())
                self.indexes[kind] = (con, overlay)
        return self.indexes[kind]

    def dialogue(self, identity):
        rows = self.source.query("SELECT * FROM dialogues WHERE id=?", [identity])
        if len(rows) != 1:
            raise WorkbenchError(404, "evidence_dialogue_not_found")
        return dict(rows[0])

    def subject(self, identity):
        _validate_id(identity)
        for prefix, kind, column in (("alignment:", "alignments", "chinese_id"),
                                      ("identity:", "identities", "dialogue_id")):
            if identity.startswith(prefix):
                index = self.index(kind)
                if index is None:
                    raise WorkbenchError(404, "evidence_index_unavailable")
                row = index[1]._row(identity)
                return self.dialogue(row[column])
        if identity.startswith("candidate:"):
            index = self.index("candidates")
            if index is None:
                raise WorkbenchError(404, "evidence_index_unavailable")
            row = dict(index[1]._row(identity))
            row.update(id=identity, start_para=row["ordinal"], end_para=row["ordinal"],
                       interlocutor=row.get("addressee"), attribution_status="candidate")
            return row
        return self.dialogue(identity)

    def _text(self, text, *, position=0):
        maximum = 100000 if self.with_text else self.excerpt
        start = 0 if self.with_text else max(0, position - maximum // 3)
        value = text[start:start + maximum]
        return {"excerpt": value, "excerpt_start": start, "text_total_chars": len(text),
                "text_truncated": start > 0 or len(value) < len(text),
                **({"text": value} if self.with_text else {})}

    def bindings(self, row):
        start, end = row.get("start_para"), row.get("end_para")
        if type(start) is not int or type(end) is not int or end < start:
            return [], "unresolved"
        if end - start > 100:
            endpoints = self.source.query(
                "SELECT id,ordinal,text FROM paragraphs WHERE edition_id=? AND ordinal IN (?,?) ORDER BY ordinal",
                [row["edition_id"], start, end])
            if len(endpoints) != 2:
                raise WorkbenchError(409, "evidence_paragraph_endpoint_missing")
            return [{"edition_id": row["edition_id"], "paragraph_id": p["id"], "ordinal": p["ordinal"],
                     "start": None, "end": None,
                     "paragraph_sha256": hashlib.sha256(p["text"].encode("utf-8")).hexdigest()}
                    for p in endpoints], "endpoint_range"
        paragraphs = [dict(p) for p in self.source.query(
            "SELECT id,ordinal,text FROM paragraphs WHERE edition_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal",
            [row["edition_id"], start, end])]
        text = "\n".join(p["text"] for p in paragraphs)
        needle = row.get("text") or ""
        position = text.find(needle) if needle else -1
        exact = position >= 0 and text.find(needle, position + 1) < 0
        result, base = [], 0
        for p in paragraphs:
            left, right = max(0, position - base), min(len(p["text"]), position + len(needle) - base)
            if not exact or left < right:
                result.append({"edition_id": row["edition_id"], "paragraph_id": p["id"],
                    "ordinal": p["ordinal"], "start": left if exact else None,
                    "end": right if exact else None,
                    "paragraph_sha256": hashlib.sha256(p["text"].encode("utf-8")).hexdigest()})
            base += len(p["text"]) + 1
        return result, "exact" if exact else "paragraph_range"

    def record(self, row):
        edition = self.editions[row["edition_id"]]
        bindings, status = self.bindings(row)
        return {"id": row["id"], "edition_id": row["edition_id"],
                "edition_title": _public(edition.get("title")), "book_key": _public(edition.get("book_key")),
                "language": _public(edition.get("language"), 30), "origin_kind": edition.get("origin_kind"),
                "ordinal": row.get("ordinal"), "speaker": _public(row.get("speaker"), 80),
                "addressee": _public(row.get("interlocutor"), 80),
                "speaker_status": _public(row.get("attribution_status"), 80),
                "bindings": bindings, "offset_status": status,
                "paragraph_range": {"start": row.get("start_para"), "end": row.get("end_para")},
                **self._text(row.get("text") or "")}

    def _formal_binding(self, row):
        originals = self.formal.query("SELECT edition_id,text,start_para,end_para FROM dialogues WHERE id=?", [row["id"]])
        if len(originals) != 1 or any(originals[0][k] != row[k] for k in ("edition_id", "text", "start_para", "end_para")):
            raise WorkbenchError(409, "evidence_formal_source_mismatch")

    def _relation(self, identity, origin, chinese, japanese, level, *, status="", observations=()):
        if not self.allowed(chinese["edition_id"]) or any(not self.allowed(r["edition_id"]) for r in japanese):
            return None
        if not self.scoped(chinese["edition_id"]):
            return None
        # Only known same-work tuples are correspondences across editions.
        book = self.editions[chinese["edition_id"]].get("book_key")
        if any(self.editions[r["edition_id"]].get("book_key") != book for r in japanese):
            raise WorkbenchError(409, "evidence_alignment_book_mismatch")
        opinions = []
        for item in observations:
            if item.get("decision") not in {"keep", "reject", "uncertain"}:
                raise WorkbenchError(409, "evidence_observation_invalid")
            opinions.append({"decision": item["decision"], "basis": _public(item.get("basis"), 80)})
        return {"id": identity, "origin": origin, "level": level,
                "relation": "aligned" if level == "verified" else "proposed_alignment" if japanese else "unmatched",
                "status": _public(status, 80), "record": self.record(chinese),
                "references": [self.record(r) for r in japanese], "review_observations": opinions}

    def relations(self, identity, capacity):
        if identity in self.relation_cache:
            return self.relation_cache[identity][:capacity]
        items = []
        if self.formal.has("alignments"):
            rows = self.formal.query("""SELECT * FROM alignments a WHERE chinese_id=? OR EXISTS(
                SELECT 1 FROM json_each(a.japanese_ids_json) j WHERE j.value=?)
                ORDER BY chinese_id,japanese_ids_json LIMIT ?""", [identity, identity, 1001])
            if len(rows) > 1000:
                raise WorkbenchError(400, "evidence_relation_limit")
            for raw in rows:
                row = dict(raw)
                ids = _json(row.get("japanese_ids_json"), [])
                if not isinstance(ids, list) or len(ids) > 32 or any(not isinstance(i, str) for i in ids):
                    raise WorkbenchError(409, "evidence_alignment_invalid")
                chinese = self.dialogue(row["chinese_id"])
                japanese = [self.dialogue(i) for i in ids]
                if not self.allowed(chinese["edition_id"]) or any(not self.allowed(r["edition_id"]) for r in japanese):
                    continue
                for member in [chinese, *japanese]:
                    self._formal_binding(member)
                item = self._relation("formal:" + row["chinese_id"], "formal_alignment",
                    chinese, japanese, "verified" if _verified(row) else "candidate", status=row.get("status"))
                if item:
                    items.append(item)
        index = self.index("alignments")
        if index:
            con, overlay = index
            rows = con.execute("""SELECT * FROM candidates c WHERE chinese_id=? OR EXISTS(
                SELECT 1 FROM json_each(c.japanese_json) j WHERE json_extract(j.value,'$.id')=?)
                ORDER BY edition_id,ordinal,id LIMIT ?""", [identity, identity, 1001]).fetchall()
            if len(rows) > 1000:
                raise WorkbenchError(400, "evidence_relation_limit")
            for row in rows:
                overlay._validate(row)
                chinese = self.dialogue(row["chinese_id"])
                japanese = [self.dialogue(r["id"]) for r in _json(row["japanese_json"], [])]
                opinions = _json(row["observations_json"], [])
                item = self._relation("alignment:" + row["id"], "alignment_index", chinese, japanese,
                    "candidate", status=row["status"], observations=opinions)
                if item:
                    items.append(item)
        items.sort(key=lambda r: (LEVELS.index(r["level"]), r["record"]["edition_id"], r["id"]))
        self.relation_cache[identity] = items
        return items[:capacity]

    def line(self):
        identity = self.params.get("dialogue") or self.params.get("id")
        row = self.subject(identity)
        if not self.scoped(row["edition_id"]):
            raise WorkbenchError(404, "evidence_source_excluded")
        capacity = self.offset + self.limit + 1
        items = self.relations(row["id"], capacity)
        all_items = self.relation_cache.get(row["id"], items)
        return {"subject": self.record(row), "items": items[self.offset:self.offset + self.limit],
                "offset": self.offset, "limit": self.limit, "has_more": len(all_items) > self.offset + self.limit,
                "no_correspondence_found": not any(r["references"] for r in all_items),
                "has_verified_correspondence": any(r["level"] == "verified" and r["references"] for r in all_items)}

    def aliases(self, name):
        names = {name}
        table = "attribution_candidates"
        if self.source.has(table):
            columns = self.source.columns(table)
            for _ in range(4):
                before = len(names)
                for fields in (("speaker_surface", "speaker_canonical"), ("addressee_surface", "addressee_canonical")):
                    if not set(fields) <= columns:
                        continue
                    marks = ",".join("?" for _ in names)
                    allowed = [eid for eid in self.editions if self.scoped(eid)]
                    if not allowed:
                        continue
                    scope_marks = ",".join("?" for _ in allowed)
                    if "edition_id" in columns:
                        scope = f"edition_id IN ({scope_marks})"
                    elif "dialogue_id" in columns:
                        scope = f"dialogue_id IN (SELECT id FROM dialogues WHERE edition_id IN ({scope_marks}))"
                    else:
                        continue
                    pairs = self.source.query(f"SELECT DISTINCT {fields[0]},{fields[1]} FROM {table} "
                        f"WHERE ({fields[0]} IN ({marks}) OR {fields[1]} IN ({marks})) AND {scope}",
                        [*sorted(names), *sorted(names), *allowed])
                    names.update(v for r in pairs for v in r if isinstance(v, str) and v.strip() and v != "unknown")
                if len(names) > 32:
                    raise WorkbenchError(400, "evidence_alias_limit")
                if len(names) == before:
                    break
        return sorted(names)

    def _speaker_rows(self, names, recipients, capacity):
        def condition(prefix, field, values):
            return f"{prefix}.{field} IN ({','.join('?' for _ in values)})", list(values)
        where, values = condition("d", "speaker", names)
        if recipients:
            other, extra = condition("d", "interlocutor", recipients)
            where = f"({where} AND {other})"
            values += extra
        columns = self.source.columns("attribution_candidates")
        needed = {"dialogue_id", "speaker_surface", "speaker_canonical", "addressee_surface", "addressee_canonical"}
        if needed <= columns:
            marks = ",".join("?" for _ in names)
            candidate = f"(a.speaker_surface IN ({marks}) OR a.speaker_canonical IN ({marks}))"
            if "edition_id" in columns:
                candidate += " AND a.edition_id=d.edition_id"
            values += [*names, *names]
            if recipients:
                marks = ",".join("?" for _ in recipients)
                candidate += f" AND (a.addressee_surface IN ({marks}) OR a.addressee_canonical IN ({marks}))"
                values += [*recipients, *recipients]
            where = f"({where} OR EXISTS(SELECT 1 FROM attribution_candidates a WHERE a.dialogue_id=d.id AND {candidate}))"
        allowed = [eid for eid in self.editions if self.scoped(eid)]
        if not allowed:
            return []
        marks = ",".join("?" for _ in allowed)
        return self.source.query(f"SELECT d.* FROM dialogues d WHERE {where} AND d.edition_id IN ({marks}) "
            "ORDER BY d.edition_id,d.ordinal,d.id LIMIT ?", [*values, *allowed, capacity])

    def speaker(self):
        name = _validate_query_text(self.params.get("name"), limit=80)
        if not name:
            raise WorkbenchError(400, "evidence_name_required")
        recipient = _validate_query_text(self.params.get("addressee"), limit=80)
        names = self.aliases(name)
        recipients = self.aliases(recipient) if recipient else []
        capacity = self.offset + self.limit + 1
        items = []
        for raw in self._speaker_rows(names, recipients, capacity):
            row = dict(raw)
            claims = []
            if row.get("speaker") in names and (not recipients or row.get("interlocutor") in recipients):
                claims.append({"speaker": _public(row["speaker"], 80), "addressee": _public(row.get("interlocutor"), 80),
                               "origin": "formal_attribution", "level": "verified" if _verified(row) else "candidate"})
            if self.source.has("attribution_candidates"):
                for raw_claim in self.source.query("SELECT * FROM attribution_candidates WHERE dialogue_id=?", [row["id"]]):
                    claim = dict(raw_claim)
                    if claim.get("edition_id", row["edition_id"]) != row["edition_id"]:
                        raise WorkbenchError(409, "evidence_attribution_edition_mismatch")
                    speaker = claim.get("speaker_canonical") or claim.get("speaker_surface")
                    addressee = claim.get("addressee_canonical") or claim.get("addressee_surface")
                    if speaker in names and (not recipients or addressee in recipients):
                        claims.append({"speaker": _public(speaker, 80), "addressee": _public(addressee, 80),
                            "origin": "attribution_candidates", "level": "verified" if _verified(claim) else "candidate"})
            if claims:
                items.append({"id": "speaker:" + row["id"], "level": "verified" if any(c["level"] == "verified" for c in claims) else "candidate",
                              "relation": "speaker_attribution", "origin": "formal_attribution",
                              "record": self.record(row), "claims": claims, "references": []})
        index = self.index("identities")
        if index:
            con, overlay = index
            marks = ",".join("?" for _ in names)
            where = f"EXISTS(SELECT 1 FROM json_each(r.observations_json) o WHERE json_extract(o.value,'$.speaker') IN ({marks})"
            values = list(names)
            if recipients:
                marks = ",".join("?" for _ in recipients)
                where += f" AND json_extract(o.value,'$.addressee') IN ({marks})"
                values += recipients
            where += ")"
            allowed = [eid for eid in self.editions if self.scoped(eid)]
            if not allowed:
                allowed = [""]
            where += " AND edition_id IN (" + ",".join("?" for _ in allowed) + ")"
            rows = con.execute("SELECT * FROM records r WHERE " + where + " ORDER BY edition_id,ordinal,id LIMIT ?",
                               [*values, *allowed, capacity])
            for row in rows:
                if not self.scoped(row["edition_id"]):
                    continue
                overlay._validate(row)
                original = self.dialogue(row["dialogue_id"])
                claims = [{"speaker": _public(o["speaker"], 80), "addressee": _public(o.get("addressee"), 80),
                           "origin": "attribution_index", "level": "candidate"}
                          for o in _json(row["observations_json"], [])
                          if o.get("speaker") in names and (not recipients or o.get("addressee") in recipients)]
                items.append({"id": "identity:" + row["id"], "level": "candidate", "relation": "speaker_attribution",
                              "origin": "attribution_index", "record": self.record(original), "claims": claims, "references": []})
        items.sort(key=lambda r: (LEVELS.index(r["level"]), r["record"]["edition_id"], r["record"]["ordinal"], r["id"]))
        return {"items": items[self.offset:self.offset + self.limit], "offset": self.offset, "limit": self.limit,
                "has_more": len(items) > self.offset + self.limit, "aliases": [_public(n, 80) for n in names],
                "aliases_are_search_keys": True}

    def term(self):
        term = _validate_query_text(self.params.get("text"), limit=120)
        if not term:
            raise WorkbenchError(400, "evidence_term_required")
        allowed = [eid for eid, e in self.editions.items()
                   if self.scoped(eid) and (not self.lang or e.get("language") == self.lang)]
        if not allowed:
            return {"items": [], "offset": self.offset, "limit": self.limit, "has_more": False}
        marks = ",".join("?" for _ in allowed)
        rows = self.source.query(f"SELECT * FROM paragraphs WHERE instr(text,?)>0 AND edition_id IN ({marks}) "
            "ORDER BY edition_id,ordinal,id LIMIT ? OFFSET ?", [term, *allowed, self.limit + 1, self.offset])
        items = []
        for raw in rows[:self.limit]:
            p = dict(raw); position = p["text"].find(term)
            edition = self.editions[p["edition_id"]]
            record = {"kind": "paragraph", "id": p["id"], "edition_id": p["edition_id"], "edition_title": _public(edition.get("title")),
                      "book_key": _public(edition.get("book_key")), "language": _public(edition.get("language"), 30),
                      "origin_kind": edition.get("origin_kind"), "ordinal": p["ordinal"],
                      "speaker": "", "speaker_status": "not_attributed",
                      "bindings": [{"edition_id": p["edition_id"], "paragraph_id": p["id"], "ordinal": p["ordinal"],
                                    "start": position, "end": position + len(term),
                                    "paragraph_sha256": hashlib.sha256(p["text"].encode("utf-8")).hexdigest()}],
                      "offset_status": "exact", **self._text(p["text"], position=position)}
            dialogues = self.source.query("""SELECT id FROM dialogues WHERE edition_id=?
                AND start_para<=? AND end_para>=? AND instr(text,?)>0 ORDER BY ordinal,id LIMIT 6""",
                [p["edition_id"], p["ordinal"], p["ordinal"], term])
            relations, seen = [], set()
            for d in dialogues[:5]:
                for relation in self.relations(d["id"], 6):
                    if relation["id"] not in seen and len(relations) < 5:
                        relations.append(relation); seen.add(relation["id"])
            items.append({"id": "term:" + p["edition_id"] + ":" + p["id"], "level": "clue",
                          "relation": "retrieval", "origin": "paragraph_search", "record": record,
                          "references": [], "correspondences": relations,
                          "correspondences_may_have_more": len(dialogues) > 5 or len(relations) == 5})
        return {"items": items, "offset": self.offset, "limit": self.limit, "has_more": len(rows) > self.limit}


def query_evidence(store, params):
    try:
        return EvidenceReader(store, params).execute()
    except WorkbenchError:
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError, OSError):
        raise WorkbenchError(409, "evidence_source_invalid") from None


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        print('{"status":"rejected","code":"invalid_arguments"}')
        raise SystemExit(2)


def main():
    parser = QuietParser(prog="ltr evidence", description="Read-only line/speaker/term evidence; zero model calls")
    commands = parser.add_subparsers(dest="mode", required=True)
    for mode in ("line", "speaker", "term"):
        command = commands.add_parser(mode)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--corpus", required=True)
        command.add_argument("--limit", type=int, default=20)
        command.add_argument("--offset", type=int, default=0)
        command.add_argument("--excerpt-chars", type=int, default=160)
        command.add_argument("--with-text", action="store_true")
        command.add_argument("--output", type=Path)
        command.add_argument("--book")
        if mode == "line":
            command.add_argument("--dialogue", required=True)
        elif mode == "speaker":
            command.add_argument("--name", required=True)
            command.add_argument("--addressee")
        else:
            command.add_argument("--text", required=True)
            command.add_argument("--lang")
    args = parser.parse_args()
    store = None
    try:
        from ltr.workbench.config import WorkbenchConfig
        from ltr.workbench.store import WorkbenchStore
        from ltr.project.storage import private_root, write_json
        config = WorkbenchConfig.load(args.config)
        if args.output:
            target = private_root(args.output.parent) / args.output.name
            if target.is_symlink() or target.exists():
                raise WorkbenchError(400, "evidence_output_exists")
            if any(target.resolve().is_relative_to(c.root.resolve()) for c in config.corpora):
                raise WorkbenchError(400, "evidence_output_inside_corpus")
        store = WorkbenchStore(config)
        result = query_evidence(store, vars(args))
        if args.output:
            write_json(target, result)
            print(json.dumps({"status": "saved", "items": len(result["items"]), "model_calls": 0}))
        else:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except WorkbenchError as exc:
        print(json.dumps({"status": "rejected", "code": exc.code}))
    except Exception:
        print('{"status":"rejected","code":"evidence_operation_failed"}')
    finally:
        if store:
            store.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
