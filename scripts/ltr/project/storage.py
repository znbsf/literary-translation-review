"""Private single-user storage; no raw-content logging.

Atomic replacement follows the small io.py primitive from v2.5-longform,
with fsync and finally cleanup. This is not encryption or a multi-file transaction.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from contextlib import contextmanager


class StorageError(ValueError):
    pass


def private_root(path: Path) -> Path:
    path = path.absolute()
    for parent in (path, *path.parents):
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise StorageError("linked_private_root")
        if (parent / ".git").exists():
            raise StorageError("private_root_inside_git")
    return path.resolve()


def atomic_write(path: Path, text: str | bytes) -> None:
    root = private_root(path.parent)
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise StorageError("linked_output")
    root.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        mode = "wb" if isinstance(text, bytes) else "w"
        options = {} if isinstance(text, bytes) else {"encoding": "utf-8"}
        with tempfile.NamedTemporaryFile(mode, dir=root, delete=False, **options) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_json(path: Path, value: dict) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_private_json(path: Path):
    """Read workflow JSON after enforcing the private-root boundary."""
    private_root(path)
    return json.loads(path.read_text(encoding="utf-8"))


def read_project(root: Path) -> dict:
    root = private_root(root)
    path = root / "project.json"
    if path.is_symlink() or path.stat().st_size > 20_000_000:
        raise StorageError("invalid_project_file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("format") == "translation-project-v2":
        from .binding import hydrate
        value = hydrate(root, value)
    elif value.get("format") != "translation-project-v1":
        raise StorageError("unknown_project_format")
    validate_project(value)
    snapshot = root / "source-snapshot.json"
    if snapshot.is_symlink() or snapshot.stat().st_size > 200_000_000:
        raise StorageError("invalid_source_snapshot")
    if digest(json.loads(snapshot.read_text(encoding="utf-8"))) != value.get("source_snapshot_digest"):
        raise StorageError("source_snapshot_changed")
    return value


def validate_project(project: dict) -> None:
    try:
        if type(project["revision"]) is not int or project["revision"] < 1:
            raise StorageError("invalid_project_revision")
        source, units = project["source"], project["units"]
        if not source or digest(source) != project["source_digest"]:
            raise StorageError("source_changed")
        ids = [p["id"] for p in source]
        unit_ids = [u["id"] for u in units]
        if len(ids) != len(set(ids)) or len(unit_ids) != len(set(unit_ids)) or set(ids) != set(unit_ids):
            raise StorageError("coverage_mismatch")
        if any(not isinstance(p["text"], str) for p in source):
            raise StorageError("invalid_source")
        for u in units:
            if (not isinstance(u["target"], str) or u["status"] not in {"draft", "accepted", "needs_review"}
                    or type(u["revision"]) is not int or u["revision"] < 1
                    or (u["status"] == "accepted" and not u["target"].strip())):
                raise StorageError("invalid_unit")
    except (KeyError, TypeError):
        raise StorageError("invalid_project") from None


@contextmanager
def project_lock(root: Path):
    root = private_root(root)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".writer.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise StorageError("project_locked") from None
    try:
        os.close(descriptor)
        yield
    finally:
        lock.unlink()


def digest(value: object) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def create_project(root: Path, imported: dict) -> dict:
    with project_lock(root):
        return _create_project(root, imported)


def _create_project(root: Path, imported: dict) -> dict:
    root = private_root(root)
    if (root / "project.json").exists():
        raise StorageError("project_already_exists")
    project = build_project(imported)
    write_json(root / "source-snapshot.json", imported)
    write_json(root / "project.json", project)
    return project


def build_project(imported: dict) -> dict:
    """Build a validated value without publishing files."""
    source = []
    for item in imported["items"]:
        for paragraph in item["paragraphs"]:
            source.append(dict(paragraph))
    if not source:
        raise StorageError("empty_selection")
    project = {
        "format": "translation-project-v1", "revision": 1,
        "book_hash": imported["book_hash"], "source_language": imported["language"],
        "target_language": "zh-Hans", "source": source,
        "source_digest": digest(source), "source_snapshot_digest": digest(imported),
        "context": [], "issues": [],
        "units": [{"id": p["id"], "target": "", "revision": 1,
                   "status": "draft", "dependencies": []} for p in source],
        "model_calls": 0,
    }
    validate_project(project)
    return project


def apply_edits(root: Path, edit: dict) -> dict:
    with project_lock(root):
        return _apply_edits(root, edit)


def _apply_edits(root: Path, edit: dict) -> dict:
    from .binding import PENDING, stored_project
    if (root / PENDING).exists():
        raise StorageError("project_recovery_required")
    project = read_project(root)
    if (edit.get("book_hash") != project["book_hash"] or
            edit.get("source_digest") != project["source_digest"] or
            digest(project["source"]) != project["source_digest"]):
        raise StorageError("source_changed")
    if type(edit.get("base_revision")) is not int or edit["base_revision"] != project["revision"]:
        raise StorageError("stale_edit")
    updates = edit.get("units")
    if not isinstance(updates, list) or len(updates) != len(project["units"]):
        raise StorageError("coverage_mismatch")
    by_id = {u.get("id"): u for u in updates if isinstance(u, dict)}
    if len(by_id) != len(updates) or set(by_id) != {u["id"] for u in project["units"]}:
        raise StorageError("coverage_mismatch")
    changed = set()
    for unit in project["units"]:
        update = by_id[unit["id"]]
        text, status = update.get("target"), update.get("status")
        if not isinstance(text, str) or len(text) > 100_000 or status not in {"draft", "accepted"}:
            raise StorageError("invalid_edit")
        if status == "accepted" and not text.strip():
            raise StorageError("empty_accepted_translation")
        if text != unit["target"]:
            changed.add(unit["id"])
            unit["revision"] += 1
            # A changed text must receive confirmation in a later explicit action.
            status = "draft"
        unit.update(target=text, status=status)
    for issue in project["issues"]:
        if issue.get("unit_id") in changed:
            issue["status"] = "stale"
    previous_revision = project["revision"]
    project["revision"] += 1
    validate_project(project)
    write_json(root / "history" / f"revision-{previous_revision}.json", stored_project(read_project(root)))
    write_json(root / "project.json", stored_project(project))
    return project
