"""External JSON configuration: corpora, their optional evidence sources and the review directory.

Every evidence path must lie outside every corpus directory; relative paths are
resolved against the config file. Unknown keys are ignored, retired keys fail
loudly so stale configs are noticed.
"""
from __future__ import annotations

import json
import os
import re
from http import HTTPStatus
from pathlib import Path
from typing import Any, Mapping, Sequence

from ltr.library import source_lineage
from .common import DB_FILENAME, SCAN_SUBDIR, WorkbenchError, _clamp, _clean_text, _contained, _validate_id

_CORPUS_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Optional evidence sources: config key -> "file" or "dir".
EVIDENCE_PATHS = {
    "glossary": "file",
    "candidate_index": "file",
    "alignment_index": "file",
    "attribution_index": "file",
    "bibliography": "file",
    "run_snapshot": "file",
    "ocr_vision_candidates": "dir",
    "ocr_reading_order_candidates": "dir",
    "ocr_regions": "file",
}
RETIRED_KEYS = ("ocr_region_transcriptions", "ocr_region_status_candidates", "ocr_region_status_paged_catalog",
                "ocr_region_status_paged_catalog_root_sha256", "ocr_region_status_paged_catalog_source_epoch")


def _fail(code: str) -> WorkbenchError:
    return WorkbenchError(HTTPStatus.BAD_REQUEST, code)


class CorpusConfig:
    """Validated view of one configured corpus directory."""

    def __init__(self, corpus_id: str, name: str, root: Path, *, source_snapshot_db: Path | None = None,
                 source_snapshot_sha256: str | None = None, **evidence: Path | None) -> None:
        unknown = set(evidence) - set(EVIDENCE_PATHS)
        if unknown:
            raise TypeError("unknown evidence keys: " + ", ".join(sorted(unknown)))
        self.id = corpus_id
        self.name = name
        self.root = root
        for key in EVIDENCE_PATHS:
            setattr(self, key, evidence.get(key))
        self.source_snapshot_db = source_snapshot_db
        self.source_snapshot_sha256 = source_snapshot_sha256
        self._db_path_override: Path | None = None
        if (source_snapshot_db is None) != (source_snapshot_sha256 is None):
            raise _fail("source_snapshot_config_invalid")
        if source_snapshot_db is not None:
            try:
                source_lineage.validate_snapshot_path(self.db_path, source_snapshot_db, source_snapshot_sha256)
            except source_lineage.LineageError as exc:
                raise _fail(str(exc)) from None

    @property
    def db_path(self) -> Path:
        return self._db_path_override or self.root / DB_FILENAME

    @property
    def scan_root(self) -> Path:
        return self.root / SCAN_SUBDIR


class WorkbenchConfig:
    """Configuration loaded from an external JSON file."""

    def __init__(self, corpora: Sequence[CorpusConfig], review_dir: Path | None, source: Path | None = None) -> None:
        self.corpora = list(corpora)
        self.review_dir = review_dir
        self.source = source
        self._by_id = {corpus.id: corpus for corpus in self.corpora}

    @property
    def reviews_enabled(self) -> bool:
        return self.review_dir is not None

    def corpus(self, corpus_id: Any) -> CorpusConfig:
        corpus = self._by_id.get(_validate_id(corpus_id, corpus=True))
        if corpus is None:
            raise WorkbenchError(HTTPStatus.NOT_FOUND, "unknown_corpus")
        return corpus

    def get(self, corpus_id: str) -> CorpusConfig | None:
        return self._by_id.get(corpus_id)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "WorkbenchConfig":
        source = Path(path).resolve()
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except OSError:
            raise _fail("config_unreadable") from None
        except ValueError:
            raise _fail("config_invalid") from None
        if not isinstance(payload, Mapping) or not isinstance(payload.get("corpora"), list):
            raise _fail("config_invalid")

        def resolve(raw: Any) -> Path:
            if not isinstance(raw, str) or not raw.strip():
                raise _fail("config_invalid")
            value = Path(raw).expanduser()
            return value if value.is_absolute() else source.parent / value

        corpora: list[CorpusConfig] = []
        for entry in payload["corpora"]:
            if not isinstance(entry, Mapping):
                raise _fail("config_invalid")
            corpus_id = _clean_text(entry.get("id"))
            if not _CORPUS_ID_RE.match(corpus_id) or any(c.id == corpus_id for c in corpora):
                raise _fail("config_invalid")
            if any(key in entry for key in RETIRED_KEYS):
                raise _fail("ocr_region_config_retired")
            root = resolve(entry.get("path")).resolve(strict=False)
            evidence: dict[str, Path] = {}
            for key, kind in EVIDENCE_PATHS.items():
                if entry.get(key) is None:
                    continue
                value = resolve(entry[key])
                if kind == "dir":
                    from ltr.ocr import vision_index as ocr_vision_index
                    try:
                        value = ocr_vision_index.validate_directory_path(value, root)
                    except (ocr_vision_index.OCRVisionIndexError, OSError, ValueError):
                        raise _fail(key + "_config_invalid") from None
                elif key not in ("run_snapshot", "glossary"):
                    value = value.resolve(strict=False)
                else:
                    value = value.absolute()  # keep lexical ancestors so the reader can refuse links
                evidence[key] = value
            snapshot = None
            if "source_snapshot_db" in entry or "source_snapshot_sha256" in entry:
                if not isinstance(entry.get("source_snapshot_sha256"), str):
                    raise _fail("source_snapshot_config_invalid")
                snapshot = resolve(entry.get("source_snapshot_db")).absolute()
            corpora.append(CorpusConfig(corpus_id, _clamp(entry.get("name") or corpus_id, 80) or corpus_id, root,
                                        source_snapshot_db=snapshot,
                                        source_snapshot_sha256=entry.get("source_snapshot_sha256"), **evidence))
        if not corpora:
            raise _fail("config_invalid")
        for corpus in corpora:
            for key in EVIDENCE_PATHS:
                value = getattr(corpus, key)
                if value is not None and any(_contained(value.resolve(strict=False), c.root) for c in corpora):
                    raise _fail(key + "_must_be_external")

        review_dir = None
        if payload.get("review_dir") is not None:
            review_dir = resolve(payload["review_dir"]).resolve(strict=False)
            if any(_contained(review_dir, c.root) for c in corpora):
                raise _fail("reviews_must_be_external")
        return cls(corpora, review_dir, source=source)
