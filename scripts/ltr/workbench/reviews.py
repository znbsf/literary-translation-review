"""Corpus-external human review labels (one JSON file per record)."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from http import HTTPStatus
from pathlib import Path
from typing import Any, Mapping, Sequence

from .common import WorkbenchError, MAX_ID_CHARS, MAX_NOTE_CHARS, MAX_SPEAKER_CHARS
from .common import _as_int, _clamp, _contained


class ReviewStore:
    """Independent, corpus-external storage for human review candidates.

    One JSON document per dialogue. Writes are atomic (temp file + ``os.replace``)
    and serialised by a process lock, so a concurrent POST can never interleave
    with the read-modify-write of the same review.
    """

    SCHEMA = "corpus-workbench-review-v1"

    def __init__(self, review_dir: Path | None) -> None:
        self.root = review_dir
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.root is not None

    # -- paths ---------------------------------------------------------
    def _corpus_dir(self, corpus_id: str) -> Path:
        if self.root is None:
            raise WorkbenchError(HTTPStatus.FORBIDDEN, "reviews_disabled")
        base = self.root
        target = base / corpus_id
        resolved_base = base.resolve(strict=False)
        resolved_target = target.resolve(strict=False)
        if not _contained(resolved_target, resolved_base):
            raise WorkbenchError(HTTPStatus.FORBIDDEN, "reviews_disabled")
        return resolved_target

    @staticmethod
    def _review_key(corpus_id: str, dialogue_id: str) -> str:
        material = f"{corpus_id}\x00{dialogue_id}".encode("utf-8", "replace")
        return hashlib.sha256(material).hexdigest()[:32]

    def _review_path(self, corpus_id: str, dialogue_id: str) -> Path:
        directory = self._corpus_dir(corpus_id)
        return directory / f"{self._review_key(corpus_id, dialogue_id)}.json"

    # -- read ----------------------------------------------------------
    def load(self, corpus_id: str) -> list[dict[str, Any]]:
        """Return every stored review document for one corpus."""
        if self.root is None:
            return []
        try:
            directory = self._corpus_dir(corpus_id)
        except WorkbenchError:
            return []
        if not directory.is_dir():
            return []
        reviews: list[dict[str, Any]] = []
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return []
        for entry in entries:
            if len(reviews) >= 5000:
                break
            if entry.suffix != ".json":
                continue
            try:
                if not entry.is_file():
                    continue
                if not _contained(entry.resolve(strict=False), directory.resolve(strict=False)):
                    continue
                payload = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, Mapping) and payload.get("schema") == self.SCHEMA:
                reviews.append(dict(payload))
        reviews.sort(key=lambda item: str(item.get("created_at") or ""))
        return reviews

    def get(self, corpus_id: str, dialogue_id: str) -> dict[str, Any] | None:
        if self.root is None:
            return None
        try:
            path = self._review_path(corpus_id, dialogue_id)
            if not path.is_file():
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, WorkbenchError):
            return None
        if not isinstance(payload, Mapping) or payload.get("schema") != self.SCHEMA:
            return None
        return dict(payload)

    # -- write ---------------------------------------------------------
    def save(
        self,
        document: Mapping[str, Any],
        *,
        rebind: bool = False,
    ) -> dict[str, Any]:
        """Persist one review; reject labels bound to changed corpus data."""
        if self.root is None:
            raise WorkbenchError(HTTPStatus.FORBIDDEN, "reviews_disabled")
        corpus_id = str(document["corpus"])
        dialogue_id = str(document["dialogue_id"])
        directory = self._corpus_dir(corpus_id)
        path = self._review_path(corpus_id, dialogue_id)
        with self._lock:
            latest = self.get(corpus_id, dialogue_id)
            prepared = dict(document)
            if latest is not None:
                stale = (
                    _clamp(latest.get("corpus_fingerprint"), 72)
                    != _clamp(prepared.get("corpus_fingerprint"), 72)
                    or _clamp(latest.get("context_hash"), 120)
                    != _clamp(prepared.get("context_hash"), 120)
                )
                if stale:
                    if not rebind:
                        raise WorkbenchError(HTTPStatus.CONFLICT, "stale_review")
                    prepared["rebound_from_fingerprint"] = _clamp(
                        latest.get("corpus_fingerprint"), 72
                    )
                prepared["created_at"] = latest.get("created_at") or prepared.get(
                    "created_at"
                )
                prepared["version"] = _as_int(latest.get("version"), 0) + 1
            else:
                prepared["version"] = 1
            prepared["created_at"] = prepared.get("created_at") or prepared.get(
                "updated_at"
            )
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise WorkbenchError(
                    HTTPStatus.INTERNAL_SERVER_ERROR, "review_write_failed"
                ) from None
            if not _contained(directory.resolve(strict=False), self.root.resolve(strict=False)):
                raise WorkbenchError(HTTPStatus.FORBIDDEN, "reviews_disabled")
            handle = None
            temp_path = None
            try:
                handle = tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=str(directory),
                    prefix=".review-",
                    suffix=".tmp",
                    delete=False,
                )
                temp_path = Path(handle.name)
                json.dump(prepared, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
                handle = None
                os.replace(temp_path, path)
                temp_path = None
            except OSError:
                raise WorkbenchError(
                    HTTPStatus.INTERNAL_SERVER_ERROR, "review_write_failed"
                ) from None
            finally:
                if handle is not None:
                    try:
                        handle.close()
                    except OSError:  # pragma: no cover - defensive
                        pass
                if temp_path is not None:
                    try:
                        temp_path.unlink()
                    except OSError:  # pragma: no cover - defensive
                        pass
        return prepared


def _sanitise_review(stored: Any) -> dict[str, Any] | None:
    """Whitelist review fields; never echo configured filesystem paths."""
    if not isinstance(stored, Mapping):
        return None
    return {
        "dialogue_id": _clamp(stored.get("dialogue_id"), MAX_ID_CHARS),
        "verdict": _clamp(stored.get("verdict"), 20),
        "speaker": _clamp(stored.get("speaker"), MAX_SPEAKER_CHARS),
        "addressee": _clamp(stored.get("addressee"), MAX_SPEAKER_CHARS),
        "note": _clamp(stored.get("note"), MAX_NOTE_CHARS),
        "status": _clamp(stored.get("status"), 20) or "candidate",
        "corpus_fingerprint": _clamp(stored.get("corpus_fingerprint"), 72),
        "context_hash": _clamp(stored.get("context_hash"), 120),
        "created_at": _clamp(stored.get("created_at"), 40),
        "updated_at": _clamp(stored.get("updated_at"), 40),
        "version": _as_int(stored.get("version"), 1),
        "stale": bool(stored.get("stale", False)),
        "source": stored["source"] if stored.get("source") in ("candidates", "alignments", "identities") else "formal",
        "flags": {
            "accepted": False,
            "human_confirmed": False,
            "semantic_verified": False,
            "reference_eligible": False,
        },
    }

