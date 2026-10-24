"""Read facade over the configured corpora: overview, editions, dialogues, reviews."""
from __future__ import annotations

import copy
import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from http import HTTPStatus
from typing import Any, Mapping, Sequence

from ltr.library import bibliography
from ltr.library import archive as candidate_archive
from ltr.ocr import vision_index as ocr_vision_index
from ltr.library import publisher_identifiers
from ltr.library import run_snapshot
from .common import WorkbenchError, SCHEMA_ID, REVIEW_EXPORT_SCHEMA, DEFAULT_LIMIT, MAX_LIMIT, MAX_OFFSET, MAX_QUERY_CHARS, MAX_FILTER_CHARS, MAX_ID_CHARS, MAX_NOTE_CHARS, MAX_SPEAKER_CHARS, MAX_CONTEXT_PARAGRAPH_CHARS, MAX_DIALOGUE_TEXT_CHARS, CONTEXT_WINDOW, ALLOWED_VERDICTS
from .common import (_as_bool, _as_int, _bounded_int, _clamp, _clean_text, _contained, _id_list,
                     _image_content_type, _json_loads, _like_pattern, _resolved_scan_path,
                     _utc_now, _validate_id, _validate_query_text, _ID_RE)
from .config import CorpusConfig, WorkbenchConfig
from .database import (CorpusDatabase, SourceSnapshotDatabase, _EvidenceOverlay, _EvidenceStore,
                       _evidence_read, _verified_evidence)
from .ocr import OcrViews
from .reviews import ReviewStore, _sanitise_review


class WorkbenchStore(OcrViews):
    """Sanitised read/write facade over the configured corpora."""

    def __init__(self, config: WorkbenchConfig) -> None:
        self.config = config
        self.reviews = ReviewStore(config.review_dir)
        self._databases: dict[str, CorpusDatabase] = {}
        self._evidence_databases: dict[str, SourceSnapshotDatabase] = {}
        self._lock = threading.Lock()

    @staticmethod
    def record_source(params: Mapping[str, Any]) -> str:
        source = params.get("source", "formal")
        if source not in ("formal", "candidates", "alignments", "identities"):
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, "invalid_record_source")
        return source

    def overlay(self, corpus_id, source="candidates"):
        view = _EvidenceStore(self)
        if source == "identities":
            from ltr.attribution.overlay import AttributionOverlay
            overlay = AttributionOverlay(view, corpus_id)
        elif source == "alignments":
            from ltr.alignment.overlay import AlignmentOverlay
            overlay = AlignmentOverlay(view, corpus_id)
        else:
            from ltr.quotes.overlay import CandidateOverlay
            overlay = CandidateOverlay(view, corpus_id)
        return _EvidenceOverlay(overlay)

    def completion_alignment(self, corpus_id):
        from ltr.alignment.completion import CompletionAlignmentView
        return CompletionAlignmentView(self, corpus_id)

    @contextmanager
    def review_source(self, params):
        source = self.record_source(params)
        _, db = (self.database(params.get("corpus")) if source == "formal"
                 else self.evidence_database(params.get("corpus")))
        with _verified_evidence(db):
            yield db

    # -- infrastructure ------------------------------------------------
    def database(self, corpus_id: Any) -> tuple[CorpusConfig, CorpusDatabase]:
        corpus = self.config.corpus(corpus_id)
        with self._lock:
            db = self._databases.get(corpus.id)
            if db is None:
                db = CorpusDatabase(corpus)
                self._databases[corpus.id] = db
        db.require()
        return corpus, db

    def evidence_database(self, corpus_id):
        corpus = self.config.corpus(corpus_id)
        if corpus.source_snapshot_db is None:
            return self.database(corpus_id)
        with self._lock:
            db = self._evidence_databases.get(corpus.id)
            if db is None:
                # Only the explicitly validated sibling file can override db_path.
                evidence = copy.copy(corpus)
                evidence._db_path_override = corpus.source_snapshot_db
                db = SourceSnapshotDatabase(evidence, corpus.db_path, corpus.source_snapshot_sha256)
                self._evidence_databases[corpus.id] = db
        db.require()
        return db.corpus, db

    def close(self) -> None:
        with self._lock:
            for db in self._evidence_databases.values():
                db.close()
            self._evidence_databases.clear()
            for db in self._databases.values():
                db.close()
            self._databases.clear()

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _count(db: CorpusDatabase, table: str) -> int:
        if not db.has(table):
            return 0
        return db.scalar(f'SELECT COUNT(*) FROM "{table}"')

    @staticmethod
    def _dialogue_exprs(db: CorpusDatabase) -> dict[str, str]:
        table = "dialogues"
        columns = db.columns(table)
        pick = db._pick

        def column(name: str) -> str:
            return f"d.{pick(table, name)}" if name in columns else "NULL"

        return {
            "id": "d.\"id\"" if "id" in columns else "NULL",
            "edition_id": column("edition_id"),
            "ordinal": column("ordinal"),
            "text": column("text"),
            "speaker": column("speaker"),
            "attribution_status": column("attribution_status"),
            "interlocutor": column("interlocutor"),
            "start_para": column("start_para"),
            "end_para": column("end_para"),
            "context_hash": column("context_hash"),
        }

    def _alignment_rows(self, db: CorpusDatabase, dialogue_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not db.has("alignments") or not dialogue_ids:
            return {}
        ids = list(dict.fromkeys(dialogue_ids))[:MAX_LIMIT + 1]
        marks = ",".join("?" for _ in ids)
        rows = db.query(f"SELECT chinese_id,japanese_ids_json,status,confidence,method FROM alignments WHERE chinese_id IN ({marks})", ids)
        eligibility = self._entry_eligibility(db, ids)
        return {str(r["chinese_id"]): {"chinese_id": str(r["chinese_id"]),
                "japanese": _id_list(r["japanese_ids_json"]), "status": r["status"],
                "confidence": r["confidence"], "method": r["method"],
                "reference_eligible": eligibility.get(str(r["chinese_id"]), False)} for r in rows}

    def _entry_eligibility(
        self, db: CorpusDatabase, entry_ids: Sequence[str]
    ) -> dict[str, bool]:
        ids = [eid for eid in dict.fromkeys(entry_ids) if eid][: MAX_LIMIT + 1]
        if not ids or not db.has("entries"):
            return {}
        columns = db.columns("entries")
        if "id" not in columns:
            return {}
        eligible = '"reference_eligible"' if "reference_eligible" in columns else "NULL"
        clause = ",".join("?" * len(ids))
        try:
            rows = db.connect().execute(
                f'SELECT "id" AS id, {eligible} AS eligible FROM entries '
                f'WHERE "id" IN ({clause})',
                ids,
            ).fetchall()
        except sqlite3.Error:
            return {}
        return {_clean_text(row["id"]): _as_bool(row["eligible"]) for row in rows}

    def _candidates(
        self, db: CorpusDatabase, dialogue_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        if not db.has("attribution_candidates") or not dialogue_ids:
            return {}
        columns = db.columns("attribution_candidates")
        if "dialogue_id" not in columns:
            return {}
        conn = db.connect()
        wanted = [cid for cid in dict.fromkeys(dialogue_ids)][:MAX_LIMIT + 1]
        clause = ",".join("?" * len(wanted))
        order = '"created_at" DESC' if "created_at" in columns else '"dialogue_id"'
        try:
            rows = conn.execute(
                f"SELECT * FROM attribution_candidates "
                f"WHERE dialogue_id IN ({clause}) ORDER BY {order}",
                wanted,
            ).fetchall()
        except sqlite3.Error:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = _clean_text(row["dialogue_id"])
            if not key or key in result:
                continue
            result[key] = {name: row[name] for name in row.keys()}
        return result

    @staticmethod
    def _candidate_status(candidate: Mapping[str, Any]) -> str:
        if _as_bool(candidate.get("human_confirmed")):
            return "human_confirmed"
        if _as_bool(candidate.get("semantic_verified")):
            return "semantic_verified"
        return "candidate"

    @staticmethod
    def _candidate_speaker(candidate: Mapping[str, Any]) -> str:
        return _clamp(
            _clean_text(candidate.get("speaker_canonical"))
            or _clean_text(candidate.get("speaker_surface")),
            120,
        )

    @staticmethod
    def _candidate_addressee(candidate: Mapping[str, Any]) -> str:
        return _clamp(
            _clean_text(candidate.get("addressee_canonical"))
            or _clean_text(candidate.get("addressee_surface")),
            120,
        )

    def _build_item(
        self,
        row: sqlite3.Row,
        candidate: Mapping[str, Any] | None,
        alignment: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        base_speaker = _clamp(row["speaker"], 120)
        base_addressee = _clamp(row["interlocutor"], 120)
        base_status = _clamp(row["attribution_status"], 40)
        basis = ""
        if candidate is not None:
            # A candidate row replaces the legacy base attribution wholesale: an
            # unknown candidate speaker must never fall back to the older base
            # speaker value.
            speaker = self._candidate_speaker(candidate)
            addressee = self._candidate_addressee(candidate)
            status = self._candidate_status(candidate)
            basis = _clamp(candidate.get("basis_detector"), 60)
        else:
            speaker = base_speaker
            addressee = base_addressee
            status = base_status
        return {
            "id": _clean_text(row["id"]),
            "edition_id": _clean_text(row["edition_id"]),
            "edition_title": _clamp(row["edition_title"], 200),
            "language": _clamp(row["language"], 40),
            "ordinal": _as_int(row["ordinal"]),
            "text": _clamp(row["text"], MAX_DIALOGUE_TEXT_CHARS),
            "text_truncated":len(row['text'] or '')>MAX_DIALOGUE_TEXT_CHARS,
            "text_total_chars":len(row['text'] or ''),
            "speaker": speaker,
            "addressee": addressee,
            "attribution_status": status,
            "basis_detector": basis,
            "reference_eligible": bool(
                alignment is not None and alignment.get("reference_eligible")
            ),
            "has_alignment": alignment is not None,
            "source": "formal",
        }

    # -- overview ------------------------------------------------------
    def overview(self) -> dict[str, Any]:
        corpora: list[dict[str, Any]] = []
        for corpus in self.config.corpora:
            try:
                corpora.append(self._corpus_overview(corpus))
            except (WorkbenchError, sqlite3.Error, OSError):
                corpora.append(self._unavailable_overview(corpus))
        return {
            "schema": SCHEMA_ID,
            "generated_at": _utc_now(),
            "corpora": corpora,
            "reviews_enabled": self.config.reviews_enabled,
        }

    def _publisher_identifiers(self, corpus: CorpusConfig) -> dict[str, Any]:
        if corpus.publisher_identifiers is None:
            return {"available": False, "error": None}
        try:
            evidence, db = self.evidence_database(corpus.id)
            with _verified_evidence(db):
                return publisher_identifiers.overview(evidence.publisher_identifiers, evidence.db_path, evidence.bibliography)
        except WorkbenchError as exc:
            return {"available": False, "error": exc.code}

    def _run_snapshot(self, corpus: CorpusConfig, db: CorpusDatabase) -> dict[str, Any]:
        if corpus.run_snapshot is None:
            return {"available": False}
        try:
            resolved = corpus.run_snapshot.resolve(strict=False)
            if any(_contained(resolved, item.root.resolve()) for item in self.config.corpora):
                return {"available": False, "error": "run_snapshot_must_be_external"}
            # A same-size replacement can preserve mtime. This source-bound
            # snapshot surface therefore bypasses the ordinary overview cache.
            expected = db.source_sha256(refresh=True)
            result = run_snapshot.read_run_snapshot(corpus.run_snapshot, expected)
            db.require()
            if db.source_sha256(refresh=True) != expected:
                return {"available": False, "error": "run_snapshot_source_mismatch"}
            return result
        except run_snapshot.RunSnapshotError as exc:
            return {"available": False, "error": exc.code}
        except WorkbenchError:
            return {"available": False, "error": "run_snapshot_source_unavailable"}
        except (OSError, RuntimeError, ValueError):
            return {"available": False, "error": "run_snapshot_unavailable"}

    def _machine_archive(self, db):
        try:
            db.require()
            result = candidate_archive.summary(db.connect())
            db.require()
            return result
        except (candidate_archive.ArchiveError, WorkbenchError, sqlite3.Error):
            return {"available": False, "error": "candidate_archive_summary_invalid"}

    def _unavailable_overview(self, corpus: CorpusConfig) -> dict[str, Any]:
        """Zero-valued view of a corpus whose database is absent or unreadable.

        A missing database is never created and never treated as a real corpus
        with data; the zero counts here are explicitly not measurements.
        """
        return {
            "id": corpus.id,
            "name": corpus.name,
            "available": False,
            "machine_archive": {"available": False, "error": "database_unavailable"},
            "counts": {
                "editions": 0,
                "paragraphs": 0,
                "dialogues": 0,
                "entries": 0,
                "alignments": 0,
            },
            "languages": [],
            "reference": {"total": 0, "eligible": 0},
            "alignment_status": [],
            "attribution": {
                "total": 0,
                "speaker_known": 0,
                "human_confirmed": 0,
                "semantic_verified": 0,
                "reference_eligible": 0,
            },
            "ocr": {"total": 0, "unverified": 0},
            "capabilities": {"attribution": False, "scans": False,
                             "completion_alignment_review": False,
                             "ocr_regions": False},
            "completion_alignment_review": {"available": False},
            "pending_alignments": {
                "available": False, "error": "database_unavailable",
                "source_coverage": {"available": False, "error": "database_unavailable"},
            },
            "publisher_bibliography": bibliography.overview(corpus.bibliography),
            "publisher_identifiers": self._publisher_identifiers(corpus),
            "ocr_vision_candidates": {"available": False, "error": "ocr_vision_source_unavailable" if corpus.ocr_vision_candidates is not None else None},
            "ocr_reading_order_candidates": {"available": False, "error": "ocr_reading_order_source_unavailable" if corpus.ocr_reading_order_candidates is not None else None},
            "ocr_regions": {"available": False, "error": "ocr_regions_source_unavailable" if corpus.ocr_regions is not None else None},
            "run_snapshot": {"available": False, "error": "run_snapshot_source_unavailable" if corpus.run_snapshot is not None else None},
            "review_count": len(self.reviews.load(corpus.id)),
        }

    def _corpus_overview(self, corpus: CorpusConfig) -> dict[str, Any]:
        try:
            _, evidence_db = self.evidence_database(corpus.id)
        except WorkbenchError:
            return self._corpus_overview_body(corpus)
        try:
            with _verified_evidence(evidence_db):
                return self._corpus_overview_body(corpus)
        except WorkbenchError:
            if isinstance(evidence_db, SourceSnapshotDatabase) and evidence_db._invalid_code is not None:
                return self._corpus_overview_body(corpus)
            raise

    def _corpus_overview_body(self, corpus: CorpusConfig) -> dict[str, Any]:
        _, db = self.database(corpus.id)
        counts = {
            "editions": self._count(db, "editions"),
            "paragraphs": self._count(db, "paragraphs"),
            "dialogues": self._count(db, "dialogues"),
            "entries": self._count(db, "entries"),
            "alignments": self._count(db, "alignments"),
        }

        languages: list[dict[str, Any]] = []
        if db.has("editions") and db.has_column("editions", "language"):
            rows = db.query(
                'SELECT COALESCE(language, \'\') AS lang, COUNT(*) AS n '
                "FROM editions GROUP BY lang ORDER BY n DESC, lang ASC LIMIT 50"
            )
            for row in rows:
                languages.append(
                    {"language": _clamp(row["lang"], 40), "count": _as_int(row["n"])}
                )

        reference = {"total": counts["entries"], "eligible": 0}
        if db.has("entries") and db.has_column("entries", "reference_eligible"):
            reference["eligible"] = db.scalar(
                'SELECT COUNT(*) FROM entries WHERE "reference_eligible" NOT IN (0, \'\', \'false\', \'False\')'
            )

        alignment_status: list[dict[str, Any]] = []
        if db.has("alignments") and db.has_column("alignments", "status"):
            rows = db.query(
                'SELECT COALESCE("status", \'\') AS st, COUNT(*) AS n '
                'FROM alignments GROUP BY st ORDER BY n DESC, st ASC LIMIT 50'
            )
            for row in rows:
                alignment_status.append(
                    {"status": _clamp(row["st"], 40), "count": _as_int(row["n"])}
                )

        attribution = {
            "total": self._count(db, "attribution_candidates") if db.has("attribution_candidates") else counts["dialogues"],
            "speaker_known": 0,
            "human_confirmed": 0,
            "semantic_verified": 0,
            "reference_eligible": 0,
        }
        if db.has("dialogues") and db.has_column("dialogues", "speaker"):
            pending_filter = " AND COALESCE(attribution_status,'') NOT IN ('unresolved','not_dialogue','quoted_excluded')" if db.has_column("dialogues", "attribution_status") else ""
            attribution["speaker_known"] = db.scalar(
                "SELECT COUNT(*) FROM dialogues "
                "WHERE speaker IS NOT NULL AND TRIM(speaker) <> ''" + pending_filter
            )
        if db.has("attribution_candidates"):
            for flag in ("human_confirmed", "semantic_verified", "reference_eligible"):
                if db.has_column("attribution_candidates", flag):
                    attribution[flag] = db.scalar('SELECT COUNT(*) FROM attribution_candidates WHERE "' + flag + '"=1')
            if db.has_column("attribution_candidates", "speaker_canonical"):
                attribution["speaker_known"] = db.scalar("SELECT COUNT(*) FROM attribution_candidates WHERE speaker_canonical IS NOT NULL AND TRIM(speaker_canonical) != ''")

        ocr = {"total": 0, "unverified": 0}
        if db.has("scan_pages"):
            ocr["total"] = self._count(db, "scan_pages")
            if db.has_column("scan_pages", "ocr_status"):
                ocr["unverified"] = db.scalar(
                    "SELECT COUNT(*) FROM scan_pages "
                    "WHERE ocr_status IS NULL OR TRIM(ocr_status) = '' "
                    "OR LOWER(ocr_status) = 'completed_unverified' "
                    "OR LOWER(ocr_status) = 'pending' "
                    "OR LOWER(ocr_status) = 'failed'"
                )

        pending = {"total": 0, "reviewed": 0, "review_status": {}, "available": False}
        if corpus.candidate_index is not None:
            try:
                pending = self.overlay(corpus.id).overview()
            except WorkbenchError as exc:
                pending["error"] = exc.code
        pending_alignments = {"total": 0, "reviewed": 0, "review_status": {}, "available": False,
                              "source_coverage": {"available": False, "error": None}}
        if corpus.alignment_index is not None:
            try:
                pending_alignments = self.overlay(corpus.id, "alignments").overview()
            except WorkbenchError as exc:
                pending_alignments["error"] = exc.code
                pending_alignments["source_coverage"] = {"available": False, "error": exc.code}
        pending_identities = {"total":0,"with_speaker":0,"statuses":{},"observations":0,"available":False}
        if corpus.attribution_index is not None:
            try:
                pending_identities=self.overlay(corpus.id,"identities").overview()
            except WorkbenchError as exc:
                pending_identities["error"]=exc.code
        completion_alignment_review = self.completion_alignment(corpus.id).overview()
        evidence_overview = self._evidence_overview(corpus)
        return {
            "id": corpus.id,
            "name": corpus.name,
            "available": True,
            "counts": counts,
            "machine_archive": self._machine_archive(db),
            "languages": languages,
            "reference": reference,
            "alignment_status": alignment_status,
            "attribution": attribution,
            "ocr": ocr,
            "capabilities": {
                "attribution": db.has("attribution_candidates"),
                "scans": db.has("scan_pages"),
                "candidates": pending["available"],
                "alignment_candidates": pending_alignments["available"],
                "identities":pending_identities["available"],
                "completion_alignment_review": completion_alignment_review["available"],
                "ocr_regions": corpus.ocr_regions is not None,
            },
            "completion_alignment_review": completion_alignment_review,
            "pending_candidates": pending,
            "pending_alignments": pending_alignments,
            "pending_identities":pending_identities,
            "publisher_bibliography": bibliography.overview(corpus.bibliography),
            "publisher_identifiers": self._publisher_identifiers(corpus),
            **evidence_overview,
            "review_count": len(self.reviews.load(corpus.id)),
        }

    # -- editions ------------------------------------------------------
    def editions(self, corpus_id: Any) -> dict[str, Any]:
        _, db = self.database(corpus_id)
        if not db.has("editions"):
            return {"items": []}
        columns = db.columns("editions")
        pick = db._pick

        def column(name: str) -> str:
            return pick("editions", name) if name in columns else "NULL"

        if db.has("dialogues") and "id" in db.columns("dialogues") and "edition_id" in db.columns("dialogues"):
            dialogue_count = (
                '(SELECT COUNT(*) FROM dialogues d WHERE d."edition_id" = e."id")'
            )
        else:
            dialogue_count = "0"
        id_expr = '"id"' if "id" in columns else "NULL"
        sql = (
            f"SELECT {id_expr} AS id, {column('book_key')} AS book_key, "
            f"{column('title')} AS title, {column('language')} AS language, "
            f"{column('origin_kind')} AS origin_kind, {dialogue_count} AS dialogues "
            "FROM editions e ORDER BY title ASC, id ASC LIMIT 500"
        )
        items: list[dict[str, Any]] = []
        for row in db.query(sql):
            items.append(
                {
                    "id": _clean_text(row["id"]),
                    "book_key": _clamp(row["book_key"], 120),
                    "title": _clamp(row["title"], 200),
                    "language": _clamp(row["language"], 40),
                    "origin_kind": _clamp(row["origin_kind"], 40),
                    "dialogues": _as_int(row["dialogues"]),
                }
            )
        corpus = self.config.corpus(corpus_id)
        candidate_counts = {}
        if corpus.candidate_index is not None:
            try:
                candidate_counts = self.overlay(corpus_id).edition_counts()
            except WorkbenchError:
                candidate_counts = None
        for item in items:
            item["candidate_count"] = candidate_counts.get(item["id"], 0) if candidate_counts is not None else None
        alignment_counts = {}
        if corpus.alignment_index is not None:
            try:
                alignment_counts = self.overlay(corpus_id, "alignments").edition_counts()
            except WorkbenchError:
                alignment_counts = None
        for item in items:
            item["alignment_candidate_count"] = alignment_counts.get(item["id"], 0) if alignment_counts is not None else None
        identity_counts={}
        if corpus.attribution_index is not None:
            try:
                identity_counts=self.overlay(corpus_id,"identities").edition_counts()
            except WorkbenchError:
                identity_counts=None
        for item in items:
            item["identity_candidate_count"]=identity_counts.get(item['id'],0) if identity_counts is not None else None
        return {"items": items}

    # -- dialogues list ------------------------------------------------
    def dialogues(self, params: Mapping[str, Any]) -> dict[str, Any]:
        source = self.record_source(params)
        from ltr.alignment.completion import validate_pending_filter
        pending_alignment = validate_pending_filter(params)
        if source != "formal":
            if pending_alignment:
                raise WorkbenchError(HTTPStatus.BAD_REQUEST, "pending_alignment_source_unsupported")
            return self.overlay(params.get("corpus"), source).list(params)
        corpus_id = params.get("corpus")
        _, db = self.database(corpus_id)
        if pending_alignment and not self.completion_alignment(corpus_id).available():
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, "pending_alignment_filter_unavailable")
        edition = _validate_query_text(params.get("edition"), limit=MAX_ID_CHARS)
        q_raw = _validate_query_text(params.get("q"), limit=MAX_QUERY_CHARS)
        speaker = _validate_query_text(params.get("speaker"), limit=MAX_FILTER_CHARS)
        status = _validate_query_text(params.get("status"), limit=MAX_FILTER_CHARS)
        offset = _bounded_int(params.get("offset"), 0, MAX_OFFSET, name="offset")
        limit = _bounded_int(params.get("limit"), DEFAULT_LIMIT, MAX_LIMIT, name="limit")
        if limit < 1:
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, "invalid_parameter")

        if not db.has("dialogues") or "id" not in db.columns("dialogues"):
            return {"items": [], "offset": offset, "limit": limit, "has_more": False}

        columns = db.columns("dialogues")
        exprs = self._dialogue_exprs(db)
        has_editions = db.has("editions")
        editions_columns = db.columns("editions") if has_editions else set()
        if has_editions and "id" in editions_columns:
            join = 'LEFT JOIN editions e ON e."id" = d."edition_id"'
            title_expr = 'e."title"' if "title" in editions_columns else "NULL"
            language_expr = 'e."language"' if "language" in editions_columns else "NULL"
        else:
            join = ""
            title_expr = "NULL"
            language_expr = "NULL"

        where: list[str] = []
        args: list[Any] = []
        if edition:
            if "edition_id" not in columns:
                return {"items": [], "offset": offset, "limit": limit, "has_more": False}
            where.append('d."edition_id" = ?')
            args.append(edition)
        if q_raw:
            text_columns = [name for name in ("text", "speaker") if name in columns]
            if not text_columns:
                return {"items": [], "offset": offset, "limit": limit, "has_more": False}
            clauses = [f'd."{name}" LIKE ? ESCAPE \'\\\'' for name in text_columns]
            where.append("(" + " OR ".join(clauses) + ")")
            args.extend(_like_pattern(q_raw) for _ in text_columns)
        speaker_expr = exprs['speaker']
        status_expr = exprs['attribution_status']
        if db.has("attribution_candidates"):
            join += ' LEFT JOIN attribution_candidates ac ON ac.dialogue_id=d.id'
            speaker_expr = "CASE WHEN ac.dialogue_id IS NOT NULL THEN COALESCE(NULLIF(ac.speaker_canonical,''),ac.speaker_surface) ELSE " + speaker_expr + " END"
            status_expr = "CASE WHEN ac.dialogue_id IS NULL THEN " + status_expr + " WHEN ac.human_confirmed=1 THEN 'human_confirmed' WHEN ac.semantic_verified=1 THEN 'semantic_verified' ELSE 'candidate' END"
        if speaker:
            where.append('(' + speaker_expr + ') = ? COLLATE NOCASE')
            args.append(speaker)
        if status:
            where.append('(' + status_expr + ') = ? COLLATE NOCASE')
            args.append(status)
        if pending_alignment:
            where.append('EXISTS (SELECT 1 FROM corpus_completion_alignment ca '
                         'WHERE ca.quote_id=d.id AND ca.target_edition_id=d.edition_id '
                         'AND ca.requires_review=1)')

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        order_parts = []
        if "ordinal" in columns:
            order_parts.append('d."ordinal" ASC')
        order_parts.append('d."id" ASC')
        sql = (
            f"SELECT {exprs['id']} AS id, {exprs['edition_id']} AS edition_id, "
            f"{exprs['ordinal']} AS ordinal, {exprs['text']} AS text, "
            f"{exprs['speaker']} AS speaker, {exprs['interlocutor']} AS interlocutor, "
            f"{exprs['attribution_status']} AS attribution_status, "
            f"{title_expr} AS edition_title, {language_expr} AS language "
            f"FROM dialogues d {join}{where_sql} "
            f"ORDER BY {', '.join(order_parts)} LIMIT ? OFFSET ?"
        )
        args.extend([limit + 1, offset])
        rows = db.query(sql, args)
        has_more = len(rows) > limit
        rows = rows[:limit]

        ids = [_clean_text(row["id"]) for row in rows]
        alignments = self._alignment_rows(db, ids)
        candidates = self._candidates(db, ids)
        items = [
            self._build_item(row, candidates.get(_clean_text(row["id"])), alignments.get(_clean_text(row["id"])))
            for row in rows
        ]
        return {
            "items": items,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
        }

    # -- dialogue detail -----------------------------------------------
    def dialogue(self, params: Mapping[str, Any]) -> dict[str, Any]:
        source = self.record_source(params)
        if source != "formal":
            return self.overlay(params.get("corpus"), source).detail(params.get("id"))
        corpus_id = params.get("corpus")
        _, db = self.database(corpus_id)
        dialogue_id = _validate_id(params.get("id"))
        if not db.has("dialogues") or "id" not in db.columns("dialogues"):
            raise WorkbenchError(HTTPStatus.NOT_FOUND, "dialogue_not_found")

        columns = db.columns("dialogues")
        exprs = self._dialogue_exprs(db)
        has_editions = db.has("editions") and "id" in db.columns("editions")
        if has_editions:
            editions_columns = db.columns("editions")
            join = 'LEFT JOIN editions e ON e."id" = d."edition_id"'
            title_expr = 'e."title"' if "title" in editions_columns else "NULL"
            language_expr = 'e."language"' if "language" in editions_columns else "NULL"
        else:
            join = ""
            title_expr = "NULL"
            language_expr = "NULL"

        sql = (
            f"SELECT {exprs['id']} AS id, {exprs['edition_id']} AS edition_id, "
            f"{exprs['ordinal']} AS ordinal, {exprs['text']} AS text, "
            f"{exprs['speaker']} AS speaker, {exprs['interlocutor']} AS interlocutor, "
            f"{exprs['attribution_status']} AS attribution_status, "
            f"{exprs['start_para']} AS start_para, {exprs['end_para']} AS end_para, "
            f"{exprs['context_hash']} AS context_hash, "
            f"{title_expr} AS edition_title, {language_expr} AS language "
            f"FROM dialogues d {join} WHERE d.\"id\" = ? LIMIT 1"
        )
        rows = db.query(sql, [dialogue_id])
        if not rows:
            raise WorkbenchError(HTTPStatus.NOT_FOUND, "dialogue_not_found")
        row = rows[0]

        alignments = self._alignment_rows(db, [dialogue_id])
        candidates = self._candidates(db, [dialogue_id])
        alignment = alignments.get(dialogue_id)
        item = self._build_item(row, candidates.get(dialogue_id), alignment)

        context = self._context(db, row)
        attribution = self._attribution_detail(db, row, dialogue_id, candidates, alignment)
        entry = self._entry_detail(db, alignment or {"chinese_id": dialogue_id})
        scan_pages = self._scan_pages(db, row)
        review = self.reviews.get(self.config.corpus(corpus_id).id, dialogue_id)
        if review and review.get("corpus_fingerprint") != db.fingerprint():
            review = dict(review, stale=True)

        completion_view = self.completion_alignment(corpus_id)
        completion = (completion_view.detail(dialogue_id, row['edition_id'])
                      if completion_view.available() else None)

        return {
            "item": item,
            "context": context,
            "context_anchor": self._context_anchor(row),
            "attribution": attribution,
            "alignment": self._alignment_detail(db, alignment),
            "completion_alignment": completion,
            "entry": entry,
            "scan_pages": scan_pages,
            "review": _sanitise_review(review),
        }

    @staticmethod
    def _context_anchor(row):
        if row['start_para'] is None or row['end_para'] is None:
            return None
        start,end=sorted((int(row['start_para']),int(row['end_para'])))
        return {'start_para':start,'end_para':end,
                'mode':'endpoint_windows' if end-start+2*CONTEXT_WINDOW+1>30 else 'continuous_window'}

    def _context(self, db: CorpusDatabase, row: sqlite3.Row) -> list[dict[str, Any]]:
        if not db.has("paragraphs") or row["start_para"] is None or row["end_para"] is None:
            return []
        start, end = sorted((int(row["start_para"]), int(row["end_para"])))
        if self._context_anchor(row)['mode']=='endpoint_windows':
            rows = db.query('SELECT ordinal,text FROM paragraphs WHERE edition_id=? AND '
                            '(ordinal BETWEEN ? AND ? OR ordinal BETWEEN ? AND ?) ORDER BY ordinal LIMIT 10',
                            [row['edition_id'],max(0,start-CONTEXT_WINDOW),start+CONTEXT_WINDOW,
                             max(0,end-CONTEXT_WINDOW),end+CONTEXT_WINDOW])
        else:
            rows = db.query("SELECT ordinal,text FROM paragraphs WHERE edition_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal LIMIT 30",
                            [row["edition_id"], max(0,start-CONTEXT_WINDOW), end+CONTEXT_WINDOW])
        return [{"ordinal": int(r["ordinal"]), "text": _clamp(r["text"], MAX_CONTEXT_PARAGRAPH_CHARS)} for r in rows]

    def _attribution_detail(
        self,
        db: CorpusDatabase,
        row: sqlite3.Row,
        dialogue_id: str,
        candidates: Mapping[str, Mapping[str, Any]],
        alignment: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        candidate = candidates.get(dialogue_id)
        if candidate is None:
            return None
        evidence: list[str] = []
        for key in (
            "basis_detector",
            "run_id",
            "run_model_surface",
            "detector_evidence_json",
        ):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                if key == "detector_evidence_json":
                    parsed = _json_loads(value)
                    if isinstance(parsed, Mapping):
                        for sub_key in sorted(parsed)[:10]:
                            sub_value = parsed[sub_key]
                            if isinstance(sub_value, (str, int, float, bool)):
                                evidence.append(
                                    f"{_clamp(sub_key, 40)}={_clamp(sub_value, 80)}"
                                )
                    continue
                evidence.append(f"{_clamp(key, 40)}={_clamp(value, 80)}")
        return {
            "speaker": self._candidate_speaker(candidate),
            "addressee": self._candidate_addressee(candidate),
            "basis_detector": _clamp(candidate.get("basis_detector"), 60),
            "status": self._candidate_status(candidate),
            "flags": {
                "requires_review": _as_bool(candidate.get("requires_review")),
                "human_confirmed": _as_bool(candidate.get("human_confirmed")),
                "semantic_verified": _as_bool(candidate.get("semantic_verified")),
                "reference_eligible": _as_bool(candidate.get("reference_eligible")),
            },
            "provenance": evidence[:10],
            "base": {
                "speaker": _clamp(row["speaker"], 120),
                "addressee": _clamp(row["interlocutor"], 120),
                "attribution_status": _clamp(row["attribution_status"], 40),
            },
            "alignment_status": _clamp(
                alignment.get("status") if alignment else "", 40
            ),
        }

    def _alignment_detail(
        self, db: CorpusDatabase, alignment: Mapping[str, Any] | None
    ) -> dict[str, Any] | None:
        if alignment is None:
            return None
        japanese: list[dict[str, Any]] = []
        if db.has("dialogues") and "id" in db.columns("dialogues"):
            ids = list(alignment.get("japanese") or [])[:50]
            if ids:
                columns = db.columns("dialogues")
                text_expr = '"text"' if "text" in columns else "NULL"
                ordinal_expr = '"ordinal"' if "ordinal" in columns else "NULL"
                clause = ",".join("?" * len(ids))
                rows = db.query(
                    f'SELECT "id" AS id, {text_expr} AS text, '
                    f'{ordinal_expr} AS ordinal FROM dialogues WHERE "id" IN ({clause})',
                    ids,
                )
                by_id = {_clean_text(item["id"]): item for item in rows}
                for cid in ids:
                    item = by_id.get(cid)
                    japanese.append(
                        {
                            "id": cid,
                            "text": _clamp(item["text"], MAX_CONTEXT_PARAGRAPH_CHARS)
                            if item is not None
                            else "",
                            "ordinal": _as_int(item["ordinal"]) if item is not None else 0,
                        }
                    )
        confidence = alignment.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            confidence_value: Any = None
            if isinstance(confidence, str) and confidence.strip():
                try:
                    confidence_value = round(float(confidence), 4)
                except ValueError:
                    confidence_value = confidence if confidence in {"high", "medium", "low", "unverified", "model_proposal", "provenance_only"} else None
        else:
            confidence_value = round(float(confidence), 4)
        return {
            "status": _clamp(alignment.get("status"), 40),
            "method": _clamp(alignment.get("method"), 60),
            "confidence": confidence_value,
            "japanese": japanese,
        }

    def _entry_detail(
        self, db: CorpusDatabase, alignment: Mapping[str, Any] | None
    ) -> dict[str, Any] | None:
        if alignment is None or not db.has("entries"):
            return None
        columns = db.columns("entries")
        if "id" not in columns:
            return None
        chinese_id = _clean_text(alignment.get("chinese_id"))
        if not chinese_id:
            return None
        eligible = '"reference_eligible"' if "reference_eligible" in columns else "NULL"
        status = '"attribution_status"' if "attribution_status" in columns else "NULL"
        alignment_status = '"alignment_status"' if "alignment_status" in columns else "NULL"
        rows = db.query(
            f'SELECT {eligible} AS eligible, {status} AS attribution_status, '
            f'{alignment_status} AS alignment_status FROM entries WHERE "id" = ? LIMIT 1',
            [chinese_id],
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "reference_eligible": _as_bool(row["eligible"]),
            "attribution_status": _clamp(row["attribution_status"], 40),
            "alignment_status": _clamp(row["alignment_status"], 40),
        }

    def _scan_pages(
        self, db: CorpusDatabase, row: sqlite3.Row
    ) -> list[dict[str, Any]]:
        if not db.has("scan_pages"):
            return []
        columns = db.columns("scan_pages")
        if "id" not in columns:
            return []
        page_ids = self._page_ids_from_locator(db, row)
        if not page_ids:
            return []
        id_expr = '"id"'
        ordinal_expr = '"ordinal"' if "ordinal" in columns else "NULL"
        status_expr = '"ocr_status"' if "ocr_status" in columns else "NULL"
        path_expr = '"image_path"' if "image_path" in columns else "NULL"
        clause = ",".join("?" * len(page_ids))
        rows = db.query(
            f"SELECT {id_expr} AS id, {ordinal_expr} AS ordinal, "
            f"{status_expr} AS ocr_status, {path_expr} AS image_path "
            f"FROM scan_pages WHERE {id_expr} IN ({clause})",
            page_ids,
        )
        root = db.corpus.scan_root
        pages: list[dict[str, Any]] = []
        for page in rows[:10]:
            image_available = False
            try:
                candidate = _resolved_scan_path(db.corpus, page["image_path"])
                image_available = candidate.is_file()
            except (WorkbenchError, OSError):
                image_available = False
            pages.append(
                {
                    "id": _clean_text(page["id"]),
                    "ordinal": _as_int(page["ordinal"]),
                    "ocr_status": _clamp(page["ocr_status"], 40),
                    "image_available": bool(image_available),
                }
            )
        pages.sort(key=lambda item: item["ordinal"])
        return pages

    def _page_ids_from_locator(
        self, db: CorpusDatabase, row: sqlite3.Row
    ) -> list[str]:
        """Resolve scan page anchors from ``paragraphs.locator_json`` only."""
        if not db.has("paragraphs"):
            return []
        columns = db.columns("paragraphs")
        if "locator_json" not in columns or "ordinal" not in columns:
            return []
        start = row["start_para"]
        if start is None:
            return []
        try:
            start_num = int(start)
        except (TypeError, ValueError):
            return []
        edition_filter = ""
        params: list[Any] = [start_num]
        if "edition_id" in columns and row["edition_id"] is not None:
            edition_filter = ' AND "edition_id" = ?'
            params.append(row["edition_id"])
        params.append(CONTEXT_WINDOW + 1)
        rows = db.query(
            'SELECT "locator_json" AS locator_json FROM paragraphs '
            f'WHERE "ordinal" >= ? AND "ordinal" IS NOT NULL{edition_filter} '
            'ORDER BY "ordinal" ASC LIMIT ?',
            params,
        )
        page_ids: list[str] = []
        for item in rows:
            locator = _json_loads(item["locator_json"])
            ids = _locator_page_ids(locator)
            for page_id in ids:
                if page_id not in page_ids:
                    page_ids.append(page_id)
            if len(page_ids) >= 3:
                break
        return page_ids[:3]

    # -- scan image ----------------------------------------------------
    def _evidence_scan_image(self, params):
        if self.config.corpus(params.get("corpus")).source_snapshot_db is None:
            return self.scan_image(params)
        return self.scan_image(params, evidence=True)

    def scan_image(self, params: Mapping[str, Any], *, evidence: bool = False) -> tuple[bytes, str]:
        corpus_id = params.get("corpus")
        corpus, db = (self.evidence_database(corpus_id) if evidence else self.database(corpus_id))
        page_id = _validate_id(params.get("page"))
        if not db.has("scan_pages"):
            raise WorkbenchError(HTTPStatus.NOT_FOUND, "scan_unavailable")
        columns = db.columns("scan_pages")
        if "id" not in columns or "image_path" not in columns:
            raise WorkbenchError(HTTPStatus.NOT_FOUND, "scan_unavailable")
        rows = db.query(
            'SELECT "image_path" AS image_path,' + db._pick('scan_pages', 'image_sha256') + ' AS image_sha256 FROM scan_pages WHERE "id" = ? LIMIT 1',
            [page_id],
        )
        if not rows:
            raise WorkbenchError(HTTPStatus.NOT_FOUND, "scan_unavailable")
        expected = params.get("image_sha256")
        try:
            resolved_target = (ocr_vision_index.validated_image_path(corpus, rows[0]["image_path"])
                               if expected is not None else _resolved_scan_path(corpus, rows[0]["image_path"]))
            if not resolved_target.is_file():
                raise WorkbenchError(HTTPStatus.NOT_FOUND, "scan_unavailable")
            if resolved_target.stat().st_size > 24 * 1024 * 1024:
                raise WorkbenchError(HTTPStatus.NOT_FOUND, "scan_unavailable")
            data = (ocr_vision_index._bytes(resolved_target, 24 * 1024 * 1024)
                    if expected is not None else resolved_target.read_bytes())
        except WorkbenchError:
            raise
        except ocr_vision_index.OCRVisionIndexError as exc:
            raise WorkbenchError(HTTPStatus.CONFLICT, exc.code) from None
        except OSError:
            raise WorkbenchError(HTTPStatus.NOT_FOUND, "scan_unavailable") from None
        expected = params.get("image_sha256")
        if expected is not None and (rows[0]["image_sha256"] != expected or hashlib.sha256(data).hexdigest() != expected):
            raise WorkbenchError(HTTPStatus.CONFLICT, "ocr_vision_image_mismatch")
        db.require()
        return data, _image_content_type(resolved_target)

    # -- reviews -------------------------------------------------------
    def save_review(self, body: Mapping[str, Any]) -> dict[str, Any]:
        with self.review_source(body):
            return self._save_review_body(body)

    def _save_review_body(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if not self.config.reviews_enabled:
            raise WorkbenchError(HTTPStatus.FORBIDDEN, "reviews_disabled")
        corpus_id = _validate_id(body.get("corpus"), corpus=True)
        corpus, db = self.database(corpus_id)
        dialogue_id = _validate_id(body.get("dialogue_id"))
        verdict = _clean_text(body.get("verdict")).lower()
        if verdict not in ALLOWED_VERDICTS:
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, "invalid_verdict")
        speaker = _validate_query_text(body.get("speaker"), limit=MAX_SPEAKER_CHARS)
        addressee = _validate_query_text(body.get("addressee"), limit=MAX_SPEAKER_CHARS)
        note = _validate_query_text(body.get("note"), limit=MAX_NOTE_CHARS)

        source = self.record_source(body)
        if source != "formal":
            _, db = self.evidence_database(corpus_id)
        fingerprint = db.fingerprint()
        if source != "formal":
            row, edition_title = self.overlay(corpus_id, source).review_target(dialogue_id)
        else:
            row, edition_title = self._review_target(db, dialogue_id)
        context_hash = _clamp(row["context_hash"], 120)
        updated_at = _utc_now()
        document = {
            "schema": ReviewStore.SCHEMA,
            "corpus": corpus.id,
            "corpus_fingerprint": fingerprint,
            "dialogue_id": dialogue_id,
            "edition_id": _clean_text(row["edition_id"]),
            "edition_title": _clamp(edition_title, 200),
            "context_hash": context_hash,
            "verdict": verdict,
            "speaker": speaker,
            "addressee": addressee,
            "note": note,
            "status": "candidate",
            "accepted": False,
            "human_confirmed": False,
            "semantic_verified": False,
            "reference_eligible": False,
            "labels_accepted": False,
            "source_binding": "source_snapshot" if source != "formal" and corpus.source_snapshot_db is not None else "live_corpus",
            "source": source,
            "created_at": updated_at,
            "updated_at": updated_at,
        }
        db.require()
        stored = self.reviews.save(document)
        return {
            "saved": True,
            "review": _sanitise_review(stored) or {},
            "corpus_updated": False,
        }

    def _review_target(
        self, db: CorpusDatabase, dialogue_id: str
    ) -> tuple[Mapping[str, Any], str]:
        if not db.has("dialogues") or "id" not in db.columns("dialogues"):
            raise WorkbenchError(HTTPStatus.NOT_FOUND, "dialogue_not_found")
        columns = db.columns("dialogues")
        pick = db._pick

        def column(name: str) -> str:
            return pick("dialogues", name) if name in columns else "NULL"

        title_expr = "NULL"
        join = ""
        if db.has("editions") and "id" in db.columns("editions"):
            title_expr = 'e."title"' if "title" in db.columns("editions") else "NULL"
            join = 'LEFT JOIN editions e ON e."id" = d."edition_id"'
        rows = db.query(
            f"SELECT d.\"id\" AS id, {column('edition_id')} AS edition_id, "
            f"{column('context_hash')} AS context_hash, "
            f"{title_expr} AS edition_title FROM dialogues d {join} "
            'WHERE d."id" = ? LIMIT 1',
            [dialogue_id],
        )
        if not rows:
            raise WorkbenchError(HTTPStatus.NOT_FOUND, "dialogue_not_found")
        row = rows[0]
        return {
            "id": _clean_text(row["id"]),
            "edition_id": _clean_text(row["edition_id"]),
            "context_hash": _clamp(row["context_hash"], 120),
        }, _clamp(row["edition_title"], 200)

    def review_export(self, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            _, evidence_db = self.evidence_database(params.get("corpus"))
        except WorkbenchError:
            return self._review_export_body(params)
        try:
            with _verified_evidence(evidence_db):
                return self._review_export_body(params)
        except WorkbenchError:
            if isinstance(evidence_db, SourceSnapshotDatabase) and evidence_db._invalid_code is not None:
                return self._review_export_body(params)
            raise

    def _review_export_body(self, params: Mapping[str, Any]) -> dict[str, Any]:
        corpus_id = _validate_id(params.get("corpus"), corpus=True)
        corpus = self.config.get(corpus_id)
        if corpus is None:
            raise WorkbenchError(HTTPStatus.NOT_FOUND, "unknown_corpus")
        reviews: list[dict[str, Any]] = []
        if self.config.reviews_enabled:
            _, database = self.database(corpus.id)
            current_fingerprint = database.fingerprint()
            for stored in self.reviews.load(corpus.id):
                stale = stored.get("corpus_fingerprint") != current_fingerprint
                if stored.get("source") in ("candidates", "alignments", "identities"):
                    try:
                        _, evidence_db = self.evidence_database(corpus.id)
                        stale = stored.get("corpus_fingerprint") != evidence_db.fingerprint()
                        target, _ = self.overlay(corpus.id, stored["source"]).review_target(stored.get("dialogue_id"))
                        stale = stale or stored.get("context_hash") != target["context_hash"]
                        evidence_db.require()
                    except WorkbenchError:
                        stale = True
                stored = dict(stored, stale=stale)
                sanitised = _sanitise_review(stored)
                if sanitised is not None:
                    reviews.append(sanitised)
        return {
            "schema": REVIEW_EXPORT_SCHEMA,
            "corpus": corpus.id,
            "reviews": reviews,
        }


def _locator_page_ids(locator: Any) -> list[str]:
    """Extract page anchors (``page_id`` / ``image_id``) from a locator object."""
    ids: list[str] = []

    def visit(node: Any, depth: int) -> None:
        if depth > 4 or len(ids) >= 3:
            return
        if isinstance(node, Mapping):
            for key in sorted(node):
                if str(key).lower() in {"page_id", "image_id"}:
                    text = _clean_text(node[key])
                    if text and _ID_RE.match(text) and text not in ids:
                        ids.append(text)
                else:
                    visit(node[key], depth + 1)
        elif isinstance(node, list):
            for entry in node[:20]:
                visit(entry, depth + 1)

    visit(locator, 0)
    return ids[:3]
