"""OCR evidence views: vision candidates, reading order, reading units and region candidates."""
from __future__ import annotations

import re
import sqlite3
from http import HTTPStatus
from typing import Any, Mapping, Sequence

from ltr.ocr import reading_order_index as ocr_reading_order_index
from ltr.ocr import reading_units as ocr_reading_units
from ltr.ocr import regions as ocr_regions
from ltr.ocr import vision_index as ocr_vision_index
from .common import WorkbenchError, MAX_OFFSET
from .common import _bounded_int, _validate_id
from .database import _evidence_read, _verified_evidence

EVIDENCE_KEYS = ("ocr_vision_candidates", "ocr_reading_order_candidates", "ocr_regions", "run_snapshot",
                 "evidence_source")


class OcrViews:
    """Mixed into WorkbenchStore."""

    def _evidence_overview(self, corpus):
        try:
            evidence, db = self.evidence_database(corpus.id)
            with _verified_evidence(db):
                return {
                    "ocr_vision_candidates": ocr_vision_index.overview(evidence, db),
                    "ocr_reading_order_candidates": ocr_reading_order_index.overview(evidence, db),
                    "ocr_regions": self._regions_overview(evidence, db),
                    "run_snapshot": self._run_snapshot(evidence, db),
                    "evidence_source": {"available": True,
                        "binding": "source_snapshot" if corpus.source_snapshot_db is not None else "live_corpus",
                        "source_sha256": db.source_sha256()},
                }
        except WorkbenchError as exc:
            return {key: {"available": False, "error": exc.code} for key in EVIDENCE_KEYS}

    # -- region candidates -------------------------------------------------
    @staticmethod
    def _region_store(corpus) -> ocr_regions.RegionStore:
        if corpus.ocr_regions is None:
            raise WorkbenchError(HTTPStatus.NOT_FOUND, "ocr_regions_not_configured")
        try:
            return ocr_regions.RegionStore(corpus.ocr_regions)
        except (ocr_regions.RegionError, sqlite3.Error):
            raise WorkbenchError(HTTPStatus.CONFLICT, "ocr_regions_unavailable") from None

    def _regions_overview(self, corpus, db):
        if corpus.ocr_regions is None:
            return {"available": False, "error": None}
        try:
            with self._region_store(corpus) as store:
                return {"available": True, **store.overview()}
        except WorkbenchError as exc:
            return {"available": False, "error": exc.code}

    @_evidence_read
    def ocr_regions(self, params: Mapping[str, Any]) -> dict[str, Any]:
        corpus, _ = self.evidence_database(params.get("corpus"))
        offset = _bounded_int(params.get("offset"), 0, 1_000_000)
        limit = _bounded_int(params.get("limit"), 20, ocr_regions.MAX_LIST)
        page_id = _validate_id(params["page_id"]) if params.get("page_id") else None
        if limit < 1:
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, "invalid_limit")
        with self._region_store(corpus) as store:
            return store.list(offset=offset, limit=limit, page_id=page_id)

    @_evidence_read
    def ocr_region(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """One candidate, re-checked against the page image and source it claims."""
        corpus, db = self.evidence_database(params.get("corpus"))
        digest = params.get("candidate_sha256")
        if type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, "invalid_parameter")
        try:
            with self._region_store(corpus) as store:
                result = store.read(digest)
        except ocr_regions.RegionError as exc:
            raise WorkbenchError(HTTPStatus.CONFLICT, exc.code) from None
        candidate = result["candidate"]
        rows = db.query("SELECT image_sha256, width, height FROM scan_pages WHERE id=?", [candidate["page_id"]])
        image_bound = bool(rows) and (rows[0]["image_sha256"], rows[0]["width"], rows[0]["height"]) == (
            candidate["image_sha256"], candidate["width"], candidate["height"])
        return {**result, "image_bound": image_bound,
                "source_bound": candidate["source_sha256"] == db.source_sha256()}

    @_evidence_read
    def ocr_reading_order_candidates(self, params: Mapping[str, Any]) -> dict[str, Any]:
        corpus, db = self.evidence_database(params.get("corpus"))
        offset = _bounded_int(params.get("offset"), 0, MAX_OFFSET, name="offset")
        limit = _bounded_int(params.get("limit"), 20, 100, name="limit")
        if limit < 1:
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, "invalid_limit")
        collection_id = params.get("collection_id")
        if collection_id is not None:
            collection_id = _validate_id(collection_id)
        try:
            result = ocr_reading_order_index.list_candidates(
                corpus, db, offset=offset, limit=limit, collection_id=collection_id)
        except ocr_reading_order_index.OCRReadingOrderIndexError as exc:
            raise WorkbenchError(HTTPStatus.CONFLICT, exc.code) from None
        return {**result, "offset":offset, "limit":limit}

    @_evidence_read
    def ocr_reading_order_candidate(self, params: Mapping[str, Any]) -> dict[str, Any]:
        corpus, db = self.evidence_database(params.get("corpus"))
        try:
            result = ocr_reading_order_index.read_candidate(corpus, db, params.get("id"), params.get("candidate_sha256"))
            if result.get("available") is False:
                raise WorkbenchError(HTTPStatus.CONFLICT, result["error"])
            return result
        except ocr_reading_order_index.OCRReadingOrderIndexError as exc:
            raise WorkbenchError(HTTPStatus.CONFLICT, exc.code) from None

    @_evidence_read
    def ocr_reading_units(self, params: Mapping[str, Any]) -> dict[str, Any]:
        corpus, db = self.evidence_database(params.get("corpus"))
        result = ocr_reading_units.read_reading_units(
            corpus, db, params.get("id"), params.get("candidate_sha256"))
        if result.get("available") is not True:
            raise WorkbenchError(HTTPStatus.CONFLICT, result["error"])
        return result


    @_evidence_read
    def ocr_reading_order_image(self, params: Mapping[str, Any]) -> tuple[bytes, str]:
        expected = params.get("image_sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, "ocr_reading_order_image_hash_required")
        before = self.ocr_reading_order_candidate(params)
        candidate = before["candidate"]
        if expected != candidate["image_sha256"]:
            raise WorkbenchError(HTTPStatus.CONFLICT, "ocr_reading_order_image_mismatch")
        data = self._evidence_scan_image({"corpus": params.get("corpus"), "page": candidate["page_id"], "image_sha256": expected})
        after = self.ocr_reading_order_candidate(params)
        if before != after:
            raise WorkbenchError(HTTPStatus.CONFLICT, "ocr_reading_order_candidates_changed")
        return data

    @_evidence_read
    def ocr_vision_candidates(self, params: Mapping[str, Any]) -> dict[str, Any]:
        corpus, db = self.evidence_database(params.get("corpus"))
        if corpus.ocr_vision_candidates is None:
            return {"available": False, "error": None}
        try:
            items = ocr_vision_index.read_candidates(corpus, db)
        except ocr_vision_index.OCRVisionIndexError as exc:
            return {"available": False, "error": exc.code}
        offset = _bounded_int(params.get("offset"), 0, 100000, name="offset")
        limit = _bounded_int(params.get("limit"), 20, 50, name="limit")
        if limit < 1:
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, "invalid_limit")
        return {"available": True, "total": len(items), "offset": offset, "limit": limit,
                "items": [ocr_vision_index.metadata(item) for item in items[offset:offset + limit]]}

    @_evidence_read
    def ocr_vision_candidate(self, params: Mapping[str, Any]) -> dict[str, Any]:
        candidate_id = params.get("id")
        if not isinstance(candidate_id, str) or not ocr_vision_index.ID_PATTERN.fullmatch(candidate_id):
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, "ocr_vision_id_invalid")
        corpus, db = self.evidence_database(params.get("corpus"))
        try:
            items = ocr_vision_index.read_candidates(corpus, db)
        except ocr_vision_index.OCRVisionIndexError as exc:
            raise WorkbenchError(HTTPStatus.CONFLICT, exc.code) from None
        for item in items:
            if item["id"] == candidate_id:
                expected = params.get("candidate_sha256")
                if expected is not None and expected != item["candidate_sha256"]:
                    raise WorkbenchError(HTTPStatus.CONFLICT, "ocr_vision_candidates_changed")
                return {"available": True, **item}
        raise WorkbenchError(HTTPStatus.NOT_FOUND, "ocr_vision_candidate_unavailable")

    @_evidence_read
    def ocr_vision_image(self, params: Mapping[str, Any]) -> tuple[bytes, str]:
        expected = params.get("image_sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, "ocr_vision_image_hash_required")
        item = self.ocr_vision_candidate(params)
        if item["candidate"]["image_sha256"] != expected:
            raise WorkbenchError(HTTPStatus.CONFLICT, "ocr_vision_image_mismatch")
        data = self._evidence_scan_image({"corpus": params.get("corpus"), "page": item["candidate"]["page_id"],
                                "image_sha256": expected})
        _, db = self.evidence_database(params.get("corpus"))
        if db.source_sha256() != item["candidate"]["source_sha256"]:
            raise WorkbenchError(HTTPStatus.CONFLICT, "ocr_vision_source_mismatch")
        return data

    @_evidence_read
    def ocr_vision_baseline(self, params: Mapping[str, Any]) -> dict[str, Any]:
        expected = params.get("candidate_sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, "ocr_vision_candidate_hash_required")
        # Reuse complete candidate/source/image validation before exposing any
        # legacy text, then revalidate all bindings after the bounded scan.
        item = self.ocr_vision_candidate(params)
        _, db = self.evidence_database(params.get("corpus"))
        before = db.fingerprint()
        source = item["candidate"]["source_sha256"]
        if db.source_sha256() != source:
            raise WorkbenchError(HTTPStatus.CONFLICT, "ocr_vision_source_mismatch")
        result = ocr_vision_index.read_baseline(db, item["candidate"]["page_id"])
        current = self.ocr_vision_candidate(params)
        if (db.fingerprint() != before or db.source_sha256() != source
                or current["candidate"]["page_id"] != item["candidate"]["page_id"]):
            raise WorkbenchError(HTTPStatus.CONFLICT, "ocr_vision_source_mismatch")
        return {"id": item["id"], "candidate_sha256": expected,
                "page_id": item["candidate"]["page_id"], "source_sha256": source,
                **result}

