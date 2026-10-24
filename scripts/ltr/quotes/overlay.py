"""Read-only candidate sidecar projection for the local workbench."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3

from ltr.quotes.index import read_index, summary
from ltr.quotes.batch import unique_source_span_start


class CandidateOverlay:
    def __init__(self, store, corpus_id):
        self.store = store
        self.corpus, self.database = store.database(corpus_id)

    @contextmanager
    def index(self):
        from ltr.workbench import WorkbenchError
        if self.corpus.candidate_index is None:
            raise WorkbenchError(404, "candidate_index_unavailable")
        try:
            with read_index(self.corpus.candidate_index, self.database.source_sha256()) as con:
                yield con
        except (ValueError, OSError, sqlite3.Error):
            raise WorkbenchError(409, "candidate_index_stale_or_invalid") from None

    def overview(self):
        with self.index() as con:
            return dict(summary(con), available=True)

    def edition_counts(self):
        with self.index() as con:
            return dict(con.execute("SELECT edition_id,COUNT(*) FROM candidates GROUP BY edition_id"))

    @staticmethod
    def _identity(value):
        from ltr.workbench import WorkbenchError
        if not isinstance(value, str) or not re.fullmatch(r"candidate:[0-9a-f]{16}", value):
            raise WorkbenchError(400, "invalid_candidate_id")
        return value.split(":", 1)[1]

    def _edition(self, identity):
        from ltr.workbench import WorkbenchError
        rows = self.database.query("SELECT id,title,language FROM editions WHERE id=?", [identity])
        if len(rows) != 1:
            raise WorkbenchError(409, "candidate_edition_mismatch")
        return rows[0]

    def _validate(self, row):
        from ltr.workbench import WorkbenchError
        rows = self.database.query(
            "SELECT ordinal,text FROM paragraphs WHERE edition_id=? AND id=?",
            [row["edition_id"], row["paragraph_id"]])
        if len(rows) != 1:
            raise WorkbenchError(409, "candidate_source_mismatch")
        paragraph = rows[0]
        if (paragraph["ordinal"] != row["ordinal"]
                or not 0 <= row["start"] < row["end"] <= len(paragraph["text"])
                or paragraph["text"][row["start"]:row["end"]] != row["text"]
                or unique_source_span_start(paragraph["text"], row["text"]) != row["start"]
                or hashlib.sha256(paragraph["text"].encode()).hexdigest() != row["paragraph_sha256"]
                or row["requires_review"] != 1
                or any(row[k] != 0 for k in ("human_confirmed", "semantic_verified", "accepted", "reference_eligible"))):
            raise WorkbenchError(409, "candidate_source_mismatch")

    def _item(self, row):
        from ltr.workbench import _clamp,MAX_DIALOGUE_TEXT_CHARS
        self._validate(row)
        edition = self._edition(row["edition_id"])
        return {"id": "candidate:" + row["id"], "edition_id": row["edition_id"],
                "edition_title": edition["title"], "language": edition["language"],
                "ordinal": row["ordinal"], "text": _clamp(row['text'],MAX_DIALOGUE_TEXT_CHARS),
                'text_truncated':len(row['text'])>MAX_DIALOGUE_TEXT_CHARS,'text_total_chars':len(row['text']),
                "speaker": row["speaker"] or "", "addressee": row["addressee"] or "",
                "attribution_status": "candidate", "basis_detector": "unquoted_source_span",
                "reference_eligible": False, "has_alignment": False, "source": "candidates",
                "candidate_kind": row["kind"], "model_review_status": row["model_review_status"]}

    def _row(self, identity):
        from ltr.workbench import WorkbenchError
        with self.index() as con:
            row = con.execute("SELECT * FROM candidates WHERE id=?", [self._identity(identity)]).fetchone()
        if row is None:
            raise WorkbenchError(404, "candidate_not_found")
        self._validate(row)
        return row

    def list(self, params):
        from ltr.workbench import _bounded_int, _validate_query_text, _like_pattern
        offset = _bounded_int(params.get("offset"), 0, 1_000_000)
        limit = max(1, _bounded_int(params.get("limit"), 30, 100))
        query = _validate_query_text(params.get("q"), limit=120)
        edition = _validate_query_text(params.get("edition"), limit=200)
        speaker = _validate_query_text(params.get("speaker"), limit=80)
        status = _validate_query_text(params.get("status"), limit=80)
        where, bindings = [], []
        for column, value in (("edition_id", edition), ("speaker", speaker)):
            if value:
                where.append(column + "=?")
                bindings.append(value)
        if query:
            where.append("text LIKE ? ESCAPE '\\'")
            bindings.append(_like_pattern(query))
        if status and status != "candidate":
            where.append("model_review_status=?")
            bindings.append(status)
        sql = "SELECT * FROM candidates" + (" WHERE " + " AND ".join(where) if where else "")
        with self.index() as con:
            rows = con.execute(sql + " ORDER BY edition_id,ordinal,start,id LIMIT ? OFFSET ?",
                               [*bindings, limit + 1, offset]).fetchall()
        return {"items": [self._item(r) for r in rows[:limit]], "offset": offset,
                "limit": limit, "has_more": len(rows) > limit, "source": "candidates"}

    def detail(self, identity):
        from ltr.workbench import _sanitise_review, _clamp
        row = self._row(identity)
        context = self.database.query(
            "SELECT ordinal,text FROM paragraphs WHERE edition_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal LIMIT 5",
            [row["edition_id"], max(0, row["ordinal"] - 2), row["ordinal"] + 2])
        review = self.store.reviews.get(self.corpus.id, identity)
        if review and (review.get("context_hash") != row["payload_sha256"]
                       or review.get("corpus_fingerprint") != self.database.fingerprint()):
            review = dict(review, stale=True)
        observations = json.loads(row["observations_json"])
        return {"item": self._item(row),
                "context_anchor": {"start_para":row['ordinal'],"end_para":row['ordinal'],"mode":"continuous_window"},
                "context": [{"ordinal": r["ordinal"], "text": _clamp(r["text"], 400)} for r in context],
                "attribution": {"speaker": row["speaker"], "addressee": row["addressee"],
                    "status": "candidate", "basis_detector": "unquoted_source_span",
                    "flags": {"requires_review": True, "human_confirmed": False,
                              "semantic_verified": False, "reference_eligible": False},
                    "provenance": [{"paragraph_id": row["paragraph_id"], "start": row["start"],
                                    "end": row["end"], "paragraph_sha256": row["paragraph_sha256"]}]},
                "alignment": None, "entry": {"reference_eligible": False,
                    "attribution_status": "candidate", "alignment_status": "unmatched"},
                "scan_pages": [], "review": _sanitise_review(review),
                "candidate": {"model": row["model"], "review_status": row["model_review_status"],
                    "review_observations": observations, "source_sha256": self.database.source_sha256()}}

    def review_target(self, identity):
        row = self._row(identity)
        return {"id": identity, "edition_id": row["edition_id"],
                "context_hash": row["payload_sha256"]}, self._edition(row["edition_id"])["title"]
