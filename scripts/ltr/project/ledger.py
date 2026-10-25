"""Private whole-book understanding ledger; declarations are never inferred."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from .storage import atomic_write, private_root, project_lock, read_project, write_json

SCHEMA = "ltr-ledger-v1"
# The keys are the required_dimensions contract in check_manifest.validate.
DIMENSIONS = (
    "relationships_and_history", "character_arcs", "plot_knowledge",
    "fixed_wording", "addressee_and_register", "narrative_style",
    "wordplay_imagery_foreshadowing", "notes_versions_sources",
)
KINDS = ("fact", "narration", "interpretation")
STATUSES = ("decided", "pending", "withdrawn")


class LedgerError(ValueError):
    """Only static, content-free error codes."""


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _now():
    return datetime.now(timezone.utc).isoformat()


def _path(path):
    path = private_root(Path(path))
    if any((parent / "dialogue.sqlite").exists() for parent in path.parents):
        raise LedgerError("ledger_inside_corpus")
    if path.name in {"project.json", "source-snapshot.json", ".writer.lock"}:
        raise LedgerError("ledger_reserved_path")
    return path


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LedgerError("ledger_duplicate_json_key")
        result[key] = value
    return result


def _load(path):
    data = json.loads(_path(path).read_bytes().decode("utf-8"), object_pairs_hook=_pairs)
    if (not isinstance(data, dict) or data.get("schema") != SCHEMA
            or not isinstance(data.get("scope"), dict)
            or not isinstance(data.get("entries"), list)):
        raise LedgerError("ledger_invalid")
    scope = data["scope"]
    ids = scope.get("paragraph_ids")
    if (not isinstance(ids, list) or not ids or any(not _text(i) for i in ids)
            or len(set(ids)) != len(ids) or type(scope.get("complete_book_read")) is not bool):
        raise LedgerError("ledger_scope_invalid")
    sha = data.get("project_sha256")
    if sha not in (None, "") and (not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{64}", sha)):
        raise LedgerError("ledger_project_digest_invalid")
    return data


def _entry_errors(entry, ids):
    if not isinstance(entry, dict):
        return ["ledger_entry_invalid"]
    if entry.get("status") == "withdrawn":
        return []
    errors = []
    for field in ("id", "observation", "created_at", "updated_at"):
        if not _text(entry.get(field)):
            errors.append("ledger_entry_invalid")
    if entry.get("dimension") not in DIMENSIONS:
        errors.append("ledger_dimension_invalid")
    if entry.get("kind") not in KINDS:
        errors.append("ledger_kind_invalid")
    if entry.get("status") not in STATUSES:
        errors.append("ledger_status_invalid")
    if entry.get("status") == "decided" and not _text(entry.get("translation_decision")):
        errors.append("ledger_decision_required")
    locator = entry.get("source_locator")
    if not _text(locator) or locator not in ids:
        errors.append("ledger_locator_out_of_scope")
    span = entry.get("source_range")
    if span is not None:
        if (not isinstance(span, list) or len(span) != 2
                or any(not _text(i) or i not in ids for i in span)):
            errors.append("ledger_range_out_of_scope")
        elif ids.index(span[0]) > ids.index(span[1]):
            errors.append("ledger_range_reversed")
        elif locator in ids and not ids.index(span[0]) <= ids.index(locator) <= ids.index(span[1]):
            errors.append("ledger_locator_outside_range")
    return errors


def _structural_errors(data):
    errors, seen = [], set()
    for entry in data["entries"]:
        errors.extend(_entry_errors(entry, data["scope"]["paragraph_ids"]))
        if isinstance(entry, dict) and entry.get("status") != "withdrawn" and _text(entry.get("id")):
            if entry["id"] in seen:
                errors.append("ledger_duplicate_id")
            seen.add(entry["id"])
    confirmation = data["scope"].get("read_confirmation")
    if data["scope"]["complete_book_read"] and (
            not isinstance(confirmation, dict) or not _text(confirmation.get("by"))
            or not _text(confirmation.get("at")) or not isinstance(confirmation.get("note"), str)):
        errors.append("ledger_read_confirmation_invalid")
    return sorted(set(errors))


def _validate(data):
    errors = _structural_errors(data)
    if errors:
        raise LedgerError(errors[0])


def _project(root):
    root = private_root(Path(root))
    before = private_root(root / "project.json").read_bytes()
    value = read_project(root)
    if before != (root / "project.json").read_bytes():
        raise LedgerError("ledger_project_changed_during_read")
    return value, hashlib.sha256(before).hexdigest()


def init_ledger(path, project):
    path = _path(path)
    value, sha = _project(project)
    binding = value.get("corpus_binding")
    if binding and path.is_relative_to((Path(project) / binding["database"]).resolve().parent):
        raise LedgerError("ledger_inside_corpus")
    with project_lock(path.parent):
        if path.exists():
            raise LedgerError("ledger_already_exists")
        data = {"schema": SCHEMA, "project_sha256": sha,
                "scope": {"paragraph_ids": [p["id"] for p in value["source"]],
                          "complete_book_read": False, "read_confirmation": None}, "entries": []}
        write_json(path, data)
    return data


def change_ledger(path, mode, **fields):
    path = _path(path)
    with project_lock(path.parent):
        data = _load(path)
        stamp = _now()
        if mode == "confirm-read":
            if not _text(fields.get("by")) or not isinstance(fields.get("note", ""), str):
                raise LedgerError("ledger_confirmation_required")
            data["scope"].update(complete_book_read=True,
                read_confirmation={"by": fields["by"], "at": stamp, "note": fields.get("note", "")})
        elif mode == "add":
            used = {e.get("id") for e in data["entries"] if isinstance(e, dict) and _text(e.get("id"))}
            number = 1
            while "d" + str(number) in used:
                number += 1
            entry = {"id": "d" + str(number), "created_at": stamp, "updated_at": stamp,
                     "status": "pending", "translation_decision": "", "source_range": None}
            _update_entry(entry, fields)
            data["entries"].append(entry)
        elif mode in ("decide", "edit", "remove"):
            matches = [e for e in data["entries"] if isinstance(e, dict) and e.get("id") == fields.get("id")]
            if len(matches) != 1:
                raise LedgerError("ledger_entry_not_found")
            entry = matches[0]
            if entry.get("status") == "withdrawn":
                raise LedgerError("ledger_entry_withdrawn")
            if mode == "remove":
                entry["status"] = "withdrawn"
            else:
                if mode == "decide" and not _text(fields.get("decision")):
                    raise LedgerError("ledger_decision_required")
                _update_entry(entry, fields)
            entry["updated_at"] = stamp
        else:
            raise LedgerError("ledger_operation_invalid")
        _validate(data)
        write_json(path, data)
    return data


def _update_entry(entry, fields):
    for argument, key in (("dimension", "dimension"), ("kind", "kind"),
                          ("locator", "source_locator"), ("range", "source_range"),
                          ("observation", "observation")):
        if fields.get(argument) is not None:
            entry[key] = fields[argument]
    if fields.get("clear_range"):
        entry["source_range"] = None
    if fields.get("decision") is not None:
        if not _text(fields["decision"]):
            raise LedgerError("ledger_decision_required")
        entry.update(translation_decision=fields["decision"], status="decided")
    if fields.get("pending"):
        entry.update(translation_decision="", status="pending")


def check_ledger(path, project=None):
    data = _load(path)
    errors = _structural_errors(data)
    active = [e for e in data["entries"] if isinstance(e, dict) and e.get("status") != "withdrawn"]
    missing = [d for d in DIMENSIONS if not any(e.get("dimension") == d for e in active)]
    pending = sum(e.get("status") == "pending" for e in active)
    if missing:
        errors.append("ledger_dimensions_missing")
    if pending:
        errors.append("ledger_pending_entries")
    if not data["scope"]["complete_book_read"]:
        errors.append("ledger_book_read_unconfirmed")
    if project is not None:
        value, sha = _project(project)
        if sha != data.get("project_sha256"):
            errors.append("ledger_project_changed")
        if data["scope"]["paragraph_ids"] != [p["id"] for p in value["source"]]:
            errors.append("ledger_project_scope_changed")
    return {"status": "fail" if errors else "pass", "errors": sorted(set(errors)),
            "missing_dimensions": missing, "pending_count": pending}


def understanding(data):
    _validate(data)
    active = [e for e in data["entries"] if e["status"] != "withdrawn"]
    return {"complete_book_read": data["scope"]["complete_book_read"],
            "dimensions": [d for d in DIMENSIONS if any(e["dimension"] == d for e in active)],
            "decisions": [{key: e[key] for key in ("id", "dimension", "source_locator", "translation_decision")}
                          for e in active if e["status"] == "decided"]}


def _replace_understanding(raw, value, field="whole_book_understanding"):
    """Locate a top-level JSON value without reserializing any other bytes."""
    text = raw.decode("utf-8")
    document = json.loads(text, object_pairs_hook=_pairs)
    if not isinstance(document, dict) or document.get("schema") != "literary-review-manifest-v2":
        raise LedgerError("ledger_manifest_invalid")
    decoder = json.JSONDecoder()
    position = text.index("{") + 1
    replacement = json.dumps(value, ensure_ascii=False, indent=2)
    while True:
        while text[position].isspace():
            position += 1
        if text[position] == "}":
            # Preserve existing whitespace, including when adding an absent field.
            prefix = "," if document else ""
            return (text[:position] + prefix + json.dumps(field) + ': ' + replacement + text[position:]).encode("utf-8")
        key, position = decoder.raw_decode(text, position)
        while text[position].isspace() or text[position] == ":":
            position += 1
        start = position
        _, position = decoder.raw_decode(text, position)
        if key == field:
            return (text[:start] + replacement + text[position:]).encode("utf-8")
        while text[position].isspace():
            position += 1
        if text[position] == ",":
            position += 1


def export_ledger(path, manifest, write=False):
    path, manifest = _path(path), _path(manifest)
    if path == manifest:
        raise LedgerError("ledger_manifest_conflict")
    result = understanding(_load(path))
    if write:
        with project_lock(manifest.parent):
            raw = manifest.read_bytes()
            atomic_write(manifest, _replace_understanding(raw, result))
    return result


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        print('{"status":"rejected","code":"invalid_arguments"}')
        raise SystemExit(2)


def main(argv=None):
    parser = QuietParser(prog="ltr ledger", description="八维理解台账；全书已读只能由人声明")
    commands = parser.add_subparsers(dest="mode", required=True)
    for mode in ("init", "add", "decide", "edit", "remove", "confirm-read", "check", "export", "list"):
        command = commands.add_parser(mode)
        command.add_argument("--ledger", type=Path, required=True)
        if mode in ("init", "check"):
            command.add_argument("--project", type=Path, required=mode == "init")
        if mode in ("add", "edit", "list"):
            command.add_argument("--dimension", choices=DIMENSIONS, required=mode == "add")
        if mode in ("add", "edit"):
            command.add_argument("--kind", choices=KINDS, required=mode == "add")
            command.add_argument("--locator", required=mode == "add")
            span = command.add_mutually_exclusive_group()
            span.add_argument("--range", nargs=2)
            if mode == "edit":
                span.add_argument("--clear-range", action="store_true")
            command.add_argument("--observation", required=mode == "add")
        if mode in ("add", "edit", "decide"):
            decision = command.add_mutually_exclusive_group(required=mode == "decide")
            decision.add_argument("--decision")
            if mode == "edit":
                decision.add_argument("--pending", action="store_true")
        if mode in ("edit", "decide", "remove"):
            command.add_argument("--id", required=True)
        if mode == "confirm-read":
            command.add_argument("--by", required=True, help="人工声明者")
            command.add_argument("--note", default="")
        if mode == "export":
            command.add_argument("--manifest", type=Path, required=True)
            command.add_argument("--write", action="store_true")
        if mode == "list":
            command.add_argument("--status", choices=STATUSES)
    args = parser.parse_args(argv)
    try:
        if args.mode == "init":
            init_ledger(args.ledger, args.project)
            result = {"status": "created"}
        elif args.mode == "check":
            result = check_ledger(args.ledger, args.project)
        elif args.mode == "export":
            result = export_ledger(args.ledger, args.manifest, args.write)
        elif args.mode == "list":
            data = _load(args.ledger)
            _validate(data)
            result = {"entries": [e for e in data["entries"]
                if (args.dimension is None or e.get("dimension") == args.dimension)
                and (args.status is None or e.get("status") == args.status)]}
        else:
            fields = {k: v for k, v in vars(args).items() if k not in ("mode", "ledger")}
            change_ledger(args.ledger, args.mode, **fields)
            result = {"status": "saved"}
        print(json.dumps(result, ensure_ascii=False))
        return 1 if args.mode == "check" and result["errors"] else 0
    except LedgerError as exc:
        print(json.dumps({"status": "rejected", "code": str(exc)}))
    except Exception:
        print('{"status":"rejected","code":"ledger_operation_failed"}')
    return 1 if args.mode == "check" else 2


if __name__ == "__main__":
    raise SystemExit(main())
