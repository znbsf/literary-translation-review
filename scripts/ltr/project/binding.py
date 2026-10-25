"""Immutable corpus bindings and recoverable project publication; zero model calls.

project.json is the publication point. A pending record can be recovered whether
the process stopped before or after that atomic replacement. Existing corpora
are always opened read-only; new corpora must be children of the private project.
"""
from __future__ import annotations

from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from ltr.library import corpus as corpus_tools
from ltr.library.source_lineage import readonly
from .storage import (StorageError, private_root, digest, write_json, atomic_write,
                      project_lock, read_project, validate_project)

FORMAT = "translation-project-v2"
PENDING = "project-publication.pending.json"
RECEIPT = "project-binding-receipt.json"


def stored_project(value: dict) -> dict:
    result = copy.deepcopy(value)
    if result.get("format") == FORMAT:
        result.pop("source", None)
    return result


def _read_json(path: Path, limit: int = 200_000_000) -> dict:
    private_root(path.parent)
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)() or path.stat().st_size > limit:
        raise StorageError("invalid_binding_artifact")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, TypeError):
        raise StorageError("invalid_binding_artifact") from None


def _db_path(path: Path) -> Path:
    path = private_root(Path(path))
    return path / "dialogue.sqlite" if path.is_dir() else path


def _source_rows(database: Path, edition: str, selection: list) -> tuple[list, str]:
    path = corpus_tools._stable_path(database)
    before = corpus_tools._sha256(path)
    source = []
    with readonly(path) as con:
        con.execute("BEGIN")
        for item in selection:
            if (type(item) is not dict or set(item) != {"id", "sha256"}
                    or not isinstance(item["id"], str) or not isinstance(item["sha256"], str)):
                raise StorageError("invalid_source_selection")
            row = con.execute("SELECT id,text,locator_json FROM paragraphs WHERE edition_id=? AND id=?",
                              (edition, item["id"])).fetchone()
            if row is None:
                raise StorageError("bound_paragraph_missing")
            try:
                locator = json.loads(row[2])
                fields = locator["_project_source_fields"]
                if type(fields) is not dict or "id" in fields or "text" in fields:
                    raise ValueError
                paragraph = {"id": row[0], "text": row[1], **fields}
            except (ValueError, KeyError, TypeError):
                raise StorageError("bound_paragraph_metadata_missing") from None
            if digest(paragraph) != item["sha256"]:
                raise StorageError("bound_paragraph_changed")
            source.append(paragraph)
        con.rollback()
    corpus_tools._stable_path(path)
    if before != corpus_tools._sha256(path):
        raise StorageError("bound_corpus_changed")
    corpus_tools._stable_path(path)
    return source, before


def hydrate(root: Path, value: dict, *, allow_missing_units=False) -> dict:
    try:
        binding = value["corpus_binding"]
        if (binding["schema"] != "project-corpus-binding-v1" or not binding["selection"]
                or not isinstance(binding["edition_id"], str)):
            raise StorageError("invalid_corpus_binding")
        relative = Path(binding["database"])
        source, sha = _source_rows(root / relative, binding["edition_id"], binding["selection"])
        if sha != binding["database_sha256"]:
            raise StorageError("bound_corpus_changed")
        result = copy.deepcopy(value)
        result["source"] = source
        validate_project(result, allow_missing_units=allow_missing_units)
        return result
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, StorageError):
            raise
        raise StorageError("invalid_corpus_binding") from None


def _bind_value(root: Path, project: dict, database: Path, edition: str) -> dict:
    import os
    selection = [{"id": p["id"], "sha256": digest(p)} for p in project["source"]]
    source, sha = _source_rows(database, edition, selection)
    if digest(source) != project["source_digest"]:
        raise StorageError("bound_source_mismatch")
    result = stored_project(project)
    result.pop("source", None)
    result["format"] = FORMAT
    result["corpus_binding"] = {
        "schema": "project-corpus-binding-v1",
        "database": os.path.relpath(database, root),
        "database_sha256": sha, "edition_id": edition, "selection": selection,
    }
    return result


def _check_snapshot(root: Path, project: dict) -> None:
    if digest(_read_json(root / "source-snapshot.json")) != project["source_snapshot_digest"]:
        raise StorageError("source_snapshot_changed")


def _project_sha(root: Path) -> str | None:
    path = root / "project.json"
    if not path.exists():
        return None
    _read_json(path, 20_000_000)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _recover_locked(root: Path) -> dict:
    pending = _read_json(root / PENDING, 20_000_000)
    if pending.get("schema") != "project-publication-v1":
        raise StorageError("invalid_project_publication")
    if pending.get("phase") == "building":
        pending = _finish_build(root, pending)
    value = pending["project"]
    if value.get("format") != FORMAT:
        raise StorageError("invalid_project_publication")
    current_sha = _project_sha(root)
    current = _read_json(root / "project.json", 20_000_000) if current_sha else None
    if current != value and current_sha != pending["previous_project_sha256"]:
        raise StorageError("project_publication_conflict")
    checked = hydrate(root, value)
    _check_snapshot(root, value)
    # A single file replacement publishes a reference to an already validated,
    # immutable database. The old project remains readable until this point.
    write_json(root / "project.json", value)
    write_json(root / RECEIPT, {
        "schema": "project-binding-receipt-v1", "status": "published",
        "operation": pending["operation"],
        "previous_project_sha256": pending["previous_project_sha256"],
        "project_sha256": _project_sha(root),
        "source_digest": value["source_digest"],
        "database_sha256": value["corpus_binding"]["database_sha256"],
        "retained_staging": pending.get("retained_staging", []),
        "paragraphs": len(value["corpus_binding"]["selection"]), "model_calls": 0,
    })
    (root / PENDING).unlink()
    return checked


def _finish_build(root: Path, pending: dict) -> dict:
    if _project_sha(root) != pending["previous_project_sha256"]:
        raise StorageError("project_publication_conflict")
    project = pending["source_project"]
    validate_project(project)
    _check_snapshot(root, project)
    name = pending["destination"]
    if type(name) is not str or Path(name).name != name or name in {"history", "migration", ".", ".."}:
        raise StorageError("new_corpus_directory_invalid")
    destination = private_root(root / name)
    if not destination.exists():
        # Preserve interrupted staging files. Only a new, uniquely named private
        # directory is built; recovery never rewrites an ambiguous old database.
        abandoned = list(pending.get("retained_staging", []))
        if pending.get("staging") and (root / pending["staging"]).exists():
            abandoned.append(pending["staging"])
        staging_name = ".corpus-building-" + uuid.uuid4().hex
        pending.update(staging=staging_name, retained_staging=abandoned)
        write_json(root / PENDING, pending)
        _make_corpus(root, project, root / staging_name)
        if destination.exists():
            raise StorageError("corpus_already_exists")
        (root / staging_name).rename(destination)
    # If interruption happened just after rename, this only reads the completed
    # destination and verifies exact paragraph bindings before publication.
    database = destination / "dialogue.sqlite"
    edition = "source-" + project["book_hash"]
    with readonly(corpus_tools._stable_path(database)) as con:
        if (con.execute("SELECT COUNT(*) FROM editions").fetchone()[0] != 1
                or con.execute("SELECT COUNT(*) FROM paragraphs").fetchone()[0] != len(project["source"])
                or con.execute("SELECT COUNT(*) FROM alignments").fetchone()[0] != 0):
            raise StorageError("new_corpus_content_mismatch")
    value = _bind_value(root, project, database, edition)
    ready = {"schema": "project-publication-v1", "operation": pending["operation"],
             "phase": "ready", "previous_project_sha256": pending["previous_project_sha256"],
             "project": value, "retained_staging": pending.get("retained_staging", [])}
    write_json(root / PENDING, ready)
    return ready


def _begin_build(root: Path, project: dict, destination: Path,
                 previous: str | None, operation: str) -> dict:
    write_json(root / PENDING, {"schema": "project-publication-v1", "phase": "building",
                               "source_project": project, "destination": destination.name,
                               "previous_project_sha256": previous, "operation": operation})
    return _recover_locked(root)


def _publish(root: Path, value: dict, previous: str | None, operation: str) -> dict:
    if (root / PENDING).exists():
        raise StorageError("project_recovery_required")
    write_json(root / PENDING, {"schema": "project-publication-v1", "operation": operation,
                               "previous_project_sha256": previous, "project": value})
    return _recover_locked(root)


def recover_project(root: Path) -> dict:
    root = private_root(root)
    with project_lock(root):
        if not (root / PENDING).exists():
            # Successful repeated recovery is a read-only no-op.
            project = read_project(root)
            if project.get("format") != FORMAT or not (root / RECEIPT).exists():
                raise StorageError("project_recovery_not_available")
            receipt = _read_json(root / RECEIPT)
            if receipt.get("project_sha256") != _project_sha(root):
                raise StorageError("project_publication_conflict")
            return project
        return _recover_locked(root)


def _new_corpus_path(root: Path, destination: Path) -> Path:
    path = private_root(destination)
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise StorageError("new_corpus_must_be_inside_project") from None
    if len(relative.parts) != 1 or relative.name in {"history", "migration"}:
        raise StorageError("new_corpus_directory_invalid")
    if path.exists():
        raise StorageError("corpus_already_exists")
    return path


def _make_corpus(root: Path, project: dict, destination: Path) -> tuple[Path, str]:
    """Only called after the destination has been checked absent under a lock."""
    corpus_tools.init_corpus(destination)
    database = destination / "dialogue.sqlite"
    edition = "source-" + project["book_hash"]
    with closing(sqlite3.connect(database)) as con, con:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("""INSERT INTO editions(id,book_key,title,language,origin_kind,source_sha256,metadata_json)
                       VALUES(?,?,?,?,?,?,?)""",
                    (edition, project["book_hash"], "Imported source", project["source_language"],
                     "source_epub", project["book_hash"],
                     json.dumps({"project_source_digest": project["source_digest"]})))
        for ordinal, paragraph in enumerate(project["source"]):
            locator = {**paragraph.get("locator", {}),
                       "_project_source_fields": {k: v for k, v in paragraph.items() if k not in {"id", "text"}}}
            con.execute("INSERT INTO paragraphs VALUES(?,?,?,?,?)",
                        (edition, ordinal, paragraph["id"], paragraph["text"],
                         json.dumps(locator, ensure_ascii=False)))
        _import_dialogues(con, edition, project["source"])
    result = corpus_tools.check_corpus(database)
    if result["status"] != "pass":
        raise StorageError("new_corpus_validation_failed")
    return database, edition


def _import_dialogues(con: sqlite3.Connection, edition: str, source: list) -> None:
    """Deterministic quote spans, without speaker or semantic qualification.

    Supports nested Japanese corner quotes across paragraphs. Closed and
    unclosed spans remain unresolved; ASCII quote guessing is not performed.
    """
    stack, records = [], []
    pairs = {"「": "」", "『": "』"}
    for ordinal, paragraph in enumerate(source):
        for offset, char in enumerate(paragraph["text"]):
            for active in stack:
                active["text"].append(char)
            if char in pairs:
                stack.append({"start_para": ordinal, "start": offset, "opening": char,
                              "text": [char]})
            elif stack and char == pairs[stack[-1]["opening"]]:
                span = stack.pop()
                records.append({**span, "end_para": ordinal, "end": offset + 1, "closed": 1})
        for active in stack:
            active["text"].append("\n")
    for span in stack:
        records.append({**span, "end_para": len(source) - 1, "end": len(source[-1]["text"]), "closed": 0})
    records.sort(key=lambda r: (r["start_para"], r["start"], r["end_para"], r["end"]))
    for ordinal, span in enumerate(records):
        text = "".join(span["text"]).rstrip("\n")
        locator = {k: span[k] for k in ("start_para", "start", "end_para", "end")}
        con.execute("""INSERT INTO dialogues(id,edition_id,ordinal,text,start_para,end_para,
                       opening,closed,context_hash,attribution_status,evidence_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (edition + ":q" + str(ordinal), edition, ordinal, text, span["start_para"],
                     span["end_para"], span["opening"], span["closed"],
                     hashlib.sha256(text.encode("utf-8")).hexdigest(), "unresolved",
                     json.dumps({"kind": "quote_span", "locator": locator})))


def create_bound_project(root: Path, imported: dict, destination: Path) -> dict:
    from .storage import build_project
    root = private_root(root)
    destination = _new_corpus_path(root, destination)
    # Refuse even an empty existing project directory; never overwrite a project.
    try:
        root.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise StorageError("project_already_exists") from None
    with project_lock(root):
        project = build_project(imported)
        write_json(root / "source-snapshot.json", imported)
        return _begin_build(root, project, destination, None, "import")


def migrate_project(root: Path, destination: Path) -> dict:
    root = private_root(root)
    with project_lock(root):
        if (root / PENDING).exists():
            raise StorageError("project_recovery_required")
        project = read_project(root)
        if project["format"] != "translation-project-v1":
            raise StorageError("project_already_bound")
        destination = _new_corpus_path(root, destination)
        previous = _project_sha(root)
        original = (root / "project.json").read_bytes()
        atomic_write(root / "migration" / (previous + ".json"), original)
        return _begin_build(root, project, destination, previous, "migrate")


def bind_project(root: Path, database: Path, edition: str) -> dict:
    """Bind to existing compatible paragraphs; never write to that database.

    IDs, complete paragraph metadata and content must match exactly. There is
    no fuzzy matching, implicit mapping, or source replacement.
    """
    root, database = private_root(root), _db_path(database)
    with project_lock(root):
        if (root / PENDING).exists():
            raise StorageError("project_recovery_required")
        project = read_project(root)
        if project["format"] != "translation-project-v1":
            raise StorageError("project_already_bound")
        previous = _project_sha(root)
        value = _bind_value(root, project, database, edition)
        atomic_write(root / "migration" / (previous + ".json"),
                     (root / "project.json").read_bytes())
        return _publish(root, value, previous, "bind")
