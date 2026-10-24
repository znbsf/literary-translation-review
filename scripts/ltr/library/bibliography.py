"""Read-only projection of an *external* publisher bibliography catalog.

The catalog is a bounded, publisher-facing facts document (schema
``corpus-publisher-bibliography-v1``). It is deliberately independent of any
local corpus: this module never opens a corpus database, never writes any file
and never performs a network request. It only reads one bounded JSON document,
validates it strictly and returns an explicit allowlist of fields.

Rejection is always reported as a *static* sanitised code (never a raw parser
message and never a machine path) so the workbench can degrade gracefully while
keeping every other corpus source working.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA = "corpus-publisher-bibliography-v1"

MAX_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 200

TOP_KEYS = frozenset({"schema", "verified_at_utc", "entries"})
ENTRY_REQUIRED_KEYS = frozenset(
    {
        "work_id",
        "title_ja",
        "category",
        "edition_variant",
        "release_date",
        "isbn13",
        "publisher_url",
        "field_sources",
        "uncertainties",
    }
)
ENTRY_OPTIONAL_KEYS = frozenset({"related_publications"})
# Preserve the existing module-level contract for callers/tests that inspect
# the required legacy entry shape.
ENTRY_KEYS = ENTRY_REQUIRED_KEYS
RELATION_KEYS = frozenset({"work_id", "relation", "note", "source_url"})
RELATIONS = frozenset({"reprints_from", "includes_bonus"})
# Only these bibliographic field names may carry a per-field source link.
FIELD_SOURCE_KEYS = frozenset(
    {
        "title_ja",
        "category",
        "edition_variant",
        "release_date",
        "isbn13",
        "publisher_url",
    }
)
# Exact publisher hosts allowed for catalog URLs; nothing else is accepted.
ALLOWED_HOSTS = frozenset(
    {
        "www.kadokawa.co.jp", "store.kadokawa.co.jp", "promo.kadokawa.co.jp",
        "group.kadokawa.co.jp", "www.kadokawa.com.tw", "www.kodansha.co.jp",
    }
)

MAX_WORK_ID_CHARS = 120
MAX_TITLE_CHARS = 300
MAX_CATEGORY_CHARS = 80
MAX_VARIANT_CHARS = 120
MAX_URL_CHARS = 400
MAX_UNCERTAINTIES = 20
MAX_UNCERTAINTY_CHARS = 300
MAX_RELATED_PUBLICATIONS = 20
MAX_RELATION_NOTE_CHARS = 160

_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_ISBN_RE = re.compile(r"^[0-9]{13}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class BibliographyError(ValueError):
    """Static, sanitised bibliography rejection code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _clean(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _bounded_str(value: Any, limit: int, code: str) -> str:
    if not isinstance(value, str):
        raise BibliographyError(code)
    text = _clean(value)
    if not text or len(text) > limit or _CONTROL_RE.search(text):
        raise BibliographyError(code)
    return text


def _nullable_str(value: Any, limit: int, code: str) -> str | None:
    if value is None:
        return None
    return _bounded_str(value, limit, code)


def _date(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise BibliographyError("bibliography_date_invalid")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise BibliographyError("bibliography_date_invalid") from None
    return value


def _isbn(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _ISBN_RE.match(value):
        raise BibliographyError("bibliography_isbn_invalid")
    total = sum(
        int(char) * (1 if index % 2 == 0 else 3)
        for index, char in enumerate(value)
    )
    if total % 10 != 0:
        raise BibliographyError("bibliography_isbn_invalid")
    return value


def _url(value: Any, code: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BibliographyError(code)
    text = value.strip()
    if not text or len(text) > MAX_URL_CHARS or _CONTROL_RE.search(text):
        raise BibliographyError(code)
    try:
        parts = urlsplit(text)
    except ValueError:
        raise BibliographyError(code) from None
    if parts.scheme != "https":
        raise BibliographyError(code)
    if parts.username is not None or parts.password is not None:
        raise BibliographyError(code)
    if "@" in (parts.netloc or ""):
        raise BibliographyError(code)
    if (parts.hostname or "").lower() not in ALLOWED_HOSTS:
        raise BibliographyError(code)
    try:
        port = parts.port
    except ValueError:
        raise BibliographyError(code) from None
    if port is not None:
        raise BibliographyError(code)
    return text


def _field_sources(value: Any) -> dict[str, str | None]:
    if not isinstance(value, Mapping):
        raise BibliographyError("bibliography_field_sources_invalid")
    resolved: dict[str, str | None] = {}
    for key, item in value.items():
        if not isinstance(key, str) or key not in FIELD_SOURCE_KEYS:
            raise BibliographyError("bibliography_field_sources_invalid")
        resolved[key] = _url(item, "bibliography_field_sources_invalid")
    return resolved


def _uncertainties(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise BibliographyError("bibliography_uncertainties_invalid")
    if len(value) > MAX_UNCERTAINTIES:
        raise BibliographyError("bibliography_uncertainties_invalid")
    resolved: list[str] = []
    for item in value:
        resolved.append(
            _bounded_str(item, MAX_UNCERTAINTY_CHARS, "bibliography_uncertainties_invalid")
        )
    return resolved


def _relation_note(value: Any) -> str:
    text = _bounded_str(value, MAX_RELATION_NOTE_CHARS,
                        "bibliography_related_publications_invalid")
    # Notes are short display text, not markup, paths, or embedded links.
    for char in text:
        category = unicodedata.category(char)
        if category[0] in {"L", "M", "N", "P"} or category == "Zs":
            continue
        raise BibliographyError("bibliography_related_publications_invalid")
    if re.search(r"(?i)(?:https?|javascript|data)\s*:", text):
        raise BibliographyError("bibliography_related_publications_invalid")
    return text


def _related_publications(value: Any, own_work_id: str) -> list[dict[str, str]]:
    code = "bibliography_related_publications_invalid"
    if not isinstance(value, list) or len(value) > MAX_RELATED_PUBLICATIONS:
        raise BibliographyError(code)
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != RELATION_KEYS:
            raise BibliographyError(code)
        work_id = _bounded_str(item["work_id"], MAX_WORK_ID_CHARS, code)
        relation = item["relation"]
        if not isinstance(relation, str) or relation not in RELATIONS or work_id == own_work_id:
            raise BibliographyError(code)
        note = _relation_note(item["note"])
        source_url = _url(item["source_url"], code)
        if source_url is None:
            raise BibliographyError(code)
        identity = (work_id, relation, note, source_url)
        if identity in seen:
            raise BibliographyError(code)
        seen.add(identity)
        result.append({"work_id": work_id, "relation": relation,
                       "note": note, "source_url": source_url})
    return result


def _entry(value: Any, seen_ids: set[str], seen_isbn: set[str]) -> dict[str, Any]:
    if (not isinstance(value, Mapping)
            or not ENTRY_REQUIRED_KEYS <= set(value)
            or not set(value) <= ENTRY_REQUIRED_KEYS | ENTRY_OPTIONAL_KEYS):
        raise BibliographyError("bibliography_entry_invalid")
    work_id = _bounded_str(value["work_id"], MAX_WORK_ID_CHARS, "bibliography_entry_invalid")
    if work_id in seen_ids:
        raise BibliographyError("bibliography_duplicate_work_id")
    seen_ids.add(work_id)
    isbn = _isbn(value["isbn13"])
    if isbn is not None:
        if isbn in seen_isbn:
            raise BibliographyError("bibliography_duplicate_isbn")
        seen_isbn.add(isbn)
    entry = {
        "work_id": work_id,
        "title_ja": _nullable_str(value["title_ja"], MAX_TITLE_CHARS, "bibliography_entry_invalid"),
        "category": _nullable_str(value["category"], MAX_CATEGORY_CHARS, "bibliography_entry_invalid"),
        "edition_variant": _nullable_str(
            value["edition_variant"], MAX_VARIANT_CHARS, "bibliography_entry_invalid"
        ),
        "release_date": _date(value["release_date"]),
        "isbn13": isbn,
        "publisher_url": _url(value["publisher_url"], "bibliography_url_invalid"),
        "field_sources": _field_sources(value["field_sources"]),
        "uncertainties": _uncertainties(value["uncertainties"]),
    }
    if "related_publications" in value:
        entry["related_publications"] = _related_publications(
            value["related_publications"], work_id
        )
    return entry


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise BibliographyError("bibliography_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise BibliographyError("bibliography_timestamp_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise BibliographyError("bibliography_timestamp_invalid")
    return value


def _read_bytes(path: Any) -> bytes:
    if not isinstance(path, (str, Path)):
        raise BibliographyError("bibliography_invalid_path")
    target = Path(path)
    try:
        size = target.stat().st_size
    except FileNotFoundError:
        raise BibliographyError("bibliography_missing") from None
    except OSError:
        raise BibliographyError("bibliography_unreadable") from None
    if size > MAX_BYTES:
        raise BibliographyError("bibliography_too_large")
    try:
        with target.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        raise BibliographyError("bibliography_missing") from None
    except OSError:
        raise BibliographyError("bibliography_unreadable") from None
    if len(raw) > MAX_BYTES:
        raise BibliographyError("bibliography_too_large")
    return raw


def load(path: Any) -> dict[str, Any]:
    """Read and strictly validate one catalog; raise ``BibliographyError``."""
    raw = _read_bytes(path)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise BibliographyError("bibliography_invalid_json") from None
    if not isinstance(payload, Mapping) or set(payload) != TOP_KEYS:
        raise BibliographyError("bibliography_schema_mismatch")
    if payload.get("schema") != SCHEMA:
        raise BibliographyError("bibliography_schema_mismatch")
    verified_at_utc = _timestamp(payload.get("verified_at_utc"))
    entries_raw = payload.get("entries")
    if not isinstance(entries_raw, list):
        raise BibliographyError("bibliography_entries_invalid")
    if len(entries_raw) > MAX_ENTRIES:
        raise BibliographyError("bibliography_too_many_entries")
    seen_ids: set[str] = set()
    seen_isbn: set[str] = set()
    entries = [_entry(entry, seen_ids, seen_isbn) for entry in entries_raw]
    for entry in entries:
        for related in entry.get("related_publications", []):
            if related["work_id"] not in seen_ids:
                raise BibliographyError("bibliography_related_publications_invalid")
    return {"verified_at_utc": verified_at_utc, "entries": entries}


def _unavailable(error: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "total": 0,
        "verified_at_utc": None,
        "entries": [],
        "corpus_updated": False,
        "local_edition_mapping_verified": False,
    }
    if error is not None:
        result["error"] = error
    return result


def overview(path: Any) -> dict[str, Any]:
    """Sanitised overview projection for an optional configured catalog.

    A missing or malformed catalog is reported as ``available: False`` with a
    static error code; it is never presented as a complete, empty catalog. An
    unconfigured catalog (``path is None``) is reported as ``available: False``
    without an error. No exception escapes and no local corpus is touched.
    """
    if path is None:
        return _unavailable(None)
    try:
        catalog = load(path)
    except BibliographyError as exc:
        return _unavailable(exc.code)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return _unavailable("bibliography_invalid")
    return {
        "available": True,
        "total": len(catalog["entries"]),
        "verified_at_utc": catalog["verified_at_utc"],
        "entries": catalog["entries"],
        "corpus_updated": False,
        "local_edition_mapping_verified": False,
    }


__all__ = ["SCHEMA", "BibliographyError", "load", "overview"]
