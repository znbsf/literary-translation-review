"""Read-only, source-bound projection of *external* publisher ISBN occurrences.

The candidate document (schema ``corpus-publisher-id-candidates-v1``) is an
external, publisher-facing facts file produced elsewhere. This module never
writes anything, never opens the network and never mutates a corpus database.

Design rules:

* the candidate and the publisher catalogue must live *outside* the formal
  database directory; caller paths are private configuration, never data;
* both the corpus database and the catalogue are bound by SHA-256 and verified
  to be stable across the whole read (``_stable_source`` / ``fingerprint``);
* the database is opened read-only (``mode=ro`` URI plus ``PRAGMA query_only``)
  with parameterised SQL only;
* every observation is validated against the real source paragraphs and the
  publisher catalogue, so a row is preserved only after verified catalogue
  membership;
* the source text is read into memory *only* for private span validation and is
  never returned, logged or echoed;
* rejection is always a *static* sanitised code (never a raw parser message,
  never a machine path, never input prose, caveats or URLs).

The returned projection intentionally drops every free-text field (the
descriptive ``normalization`` string, the producer ``caveats`` and the
``isbn_rule_sources`` URLs) and replaces them with a single static caution. No
ISBN occurrence is ever mapped to a local edition.
"""

from __future__ import annotations

from ltr.core import contained as _contained
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ltr.library import bibliography
from ltr.quotes.index import _stable_source
from ltr.library.review_queue import fingerprint

SCHEMA = "corpus-publisher-id-candidates-v1"

MAX_BYTES = 2 * 1024 * 1024
MAX_OBSERVATIONS = 2000
MAX_LOCATORS = 16
MAX_RULE_SOURCES = 20
MAX_CAVEATS = 20
MAX_ID_CHARS = 200
MAX_NORMALIZATION_CHARS = 200
MAX_URL_CHARS = 400
MAX_CAVEAT_CHARS = 300

ROLLING_OCR_LINE_WINDOW = 16

CAUTION = (
    "An ISBN occurrence can be an advertisement or a reference and does not "
    "establish local edition identity; every row is an unverified candidate "
    "that still requires human review."
)

TOP_KEYS = frozenset(
    {
        "schema",
        "created_at_utc",
        "source_sha256",
        "publisher_catalog_sha256",
        "isbn_rule_sources",
        "searched_editions",
        "searched_paragraphs",
        "rolling_ocr_line_window",
        "editions_with_candidates",
        "distinct_publisher_records",
        "candidate_rows",
        "observations",
        "caveats",
        "corpus_updated",
    }
)

OBSERVATION_KEYS = frozenset(
    {
        "edition_id",
        "publisher_work_id",
        "isbn13",
        "observed_form",
        "identifier_digits",
        "normalization",
        "paragraph_locators",
        "isbn_label_nearby",
        "requires_review",
        "local_edition_mapping_verified",
        "accepted",
        "human_confirmed",
        "semantic_verified",
        "reference_eligible",
    }
)

LOCATOR_KEYS = frozenset(
    {
        "paragraph_id",
        "ordinal",
        "paragraph_sha256",
        "normalized_start",
        "normalized_end",
    }
)

OBSERVED_FORMS = frozenset({"isbn13", "isbn10_equivalent"})

# Exact flag contract: a candidate always requires review, never carries an
# acceptance, confirmation, mapping or reference decision.
OBSERVATION_FLAGS = {
    "requires_review": 1,
    "local_edition_mapping_verified": 0,
    "accepted": 0,
    "human_confirmed": 0,
    "semantic_verified": 0,
    "reference_eligible": 0,
}

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ISBN13_RE = re.compile(r"^[0-9]{13}$")
_ISBN10_RE = re.compile(r"^[0-9]{9}[0-9X]$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# Approved separator characters: Unicode whitespace plus dash variants.
_DASHES = frozenset("-‐‑‒–—―−－")


class PublisherIdentifierError(ValueError):
    """Static, sanitised publisher-identifier rejection code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# -- primitives --------------------------------------------------------
def _bounded_str(value: Any, limit: int, code: str) -> str:
    if not isinstance(value, str):
        raise PublisherIdentifierError(code)
    if not value.strip() or len(value) > limit or _CONTROL_RE.search(value):
        raise PublisherIdentifierError(code)
    return value


def _nonneg_int(value: Any, code: str) -> int:
    # ``bool`` is deliberately excluded: ``True`` is not a valid count.
    if type(value) is not int or value < 0:
        raise PublisherIdentifierError(code)
    return value


def _sha(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise PublisherIdentifierError(code)
    return value


def _timestamp(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) > 80:
        raise PublisherIdentifierError(code)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        raise PublisherIdentifierError(code) from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise PublisherIdentifierError(code)
    return value


def _official_url(value: Any) -> str:
    code = "publisher_identifiers_url_invalid"
    if not isinstance(value, str):
        raise PublisherIdentifierError(code)
    text = value.strip()
    if not text or len(text) > MAX_URL_CHARS or _CONTROL_RE.search(text):
        raise PublisherIdentifierError(code)
    try:
        parts = urlsplit(text)
    except ValueError:
        raise PublisherIdentifierError(code) from None
    if parts.scheme != "https":
        raise PublisherIdentifierError(code)
    if parts.username is not None or parts.password is not None:
        raise PublisherIdentifierError(code)
    if "@" in (parts.netloc or ""):
        raise PublisherIdentifierError(code)
    try:
        port = parts.port
    except ValueError:
        raise PublisherIdentifierError(code) from None
    if port is not None:
        raise PublisherIdentifierError(code)
    host = (parts.hostname or "").lower()
    if not host or "." not in host:
        raise PublisherIdentifierError(code)
    return text


def _isbn13_ok(value: Any) -> bool:
    if not isinstance(value, str) or not _ISBN13_RE.fullmatch(value):
        return False
    total = sum(
        int(char) * (1 if index % 2 == 0 else 3) for index, char in enumerate(value)
    )
    return total % 10 == 0


def _isbn10_ok(value: Any) -> bool:
    if not isinstance(value, str) or not _ISBN10_RE.fullmatch(value):
        return False
    total = 0
    for index, char in enumerate(value):
        total += (10 - index) * (10 if char == "X" else int(char))
    return total % 11 == 0


def _isbn10_to_13(value: str) -> str:
    core = "978" + value[:9]
    total = sum(
        int(char) * (1 if index % 2 == 0 else 3) for index, char in enumerate(core)
    )
    return core + str((10 - total % 10) % 10)


def _normalize(text: str) -> str:
    """Preserve the entire NFKC paragraph so offsets retain their meaning."""
    return unicodedata.normalize("NFKC", text)


def _strip_separators(text: str) -> str:
    return "".join(char for char in text if not (char.isspace() or char in _DASHES))


def _read_bytes(path: Any, missing: str, unreadable: str, too_large: str) -> bytes:
    if not isinstance(path, (str, Path)):
        raise PublisherIdentifierError("publisher_identifiers_invalid_path")
    target = Path(path)
    try:
        size = target.stat().st_size
    except FileNotFoundError:
        raise PublisherIdentifierError(missing) from None
    except OSError:
        raise PublisherIdentifierError(unreadable) from None
    if size > MAX_BYTES:
        raise PublisherIdentifierError(too_large)
    try:
        with target.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        raise PublisherIdentifierError(missing) from None
    except OSError:
        raise PublisherIdentifierError(unreadable) from None
    if len(raw) > MAX_BYTES:
        raise PublisherIdentifierError(too_large)
    return raw


def _locator(value: Any) -> dict[str, Any]:
    code = "publisher_identifiers_locator_invalid"
    if not isinstance(value, Mapping) or set(value) != LOCATOR_KEYS:
        raise PublisherIdentifierError(code)
    return {
        "paragraph_id": _bounded_str(value["paragraph_id"], MAX_ID_CHARS, code),
        "ordinal": _nonneg_int(value["ordinal"], code),
        "paragraph_sha256": _sha(value["paragraph_sha256"], code),
        "normalized_start": _nonneg_int(value["normalized_start"], code),
        "normalized_end": _nonneg_int(value["normalized_end"], code),
    }


def _observation(
    raw: Any,
    resolve: Any,
    catalog_by_isbn: Mapping[str, str],
) -> dict[str, Any]:
    code = "publisher_identifiers_observation_invalid"
    if not isinstance(raw, Mapping) or set(raw) != OBSERVATION_KEYS:
        raise PublisherIdentifierError(code)
    edition_id = _bounded_str(raw["edition_id"], MAX_ID_CHARS, code)
    work_id = _bounded_str(raw["publisher_work_id"], MAX_ID_CHARS, code)

    isbn13 = raw["isbn13"]
    if not _isbn13_ok(isbn13):
        raise PublisherIdentifierError("publisher_identifiers_isbn_invalid")
    form = raw["observed_form"]
    if not isinstance(form, str) or form not in OBSERVED_FORMS:
        raise PublisherIdentifierError("publisher_identifiers_isbn_invalid")
    digits = raw["identifier_digits"]
    if form == "isbn13":
        if digits != isbn13:
            raise PublisherIdentifierError("publisher_identifiers_isbn_invalid")
    else:
        # A 10-digit equivalent is only meaningful for a 978 ISBN-13.
        if not isbn13.startswith("978") or not _isbn10_ok(digits):
            raise PublisherIdentifierError("publisher_identifiers_isbn_invalid")
        if _isbn10_to_13(digits) != isbn13:
            raise PublisherIdentifierError("publisher_identifiers_isbn_invalid")
    if catalog_by_isbn.get(isbn13) != work_id:
        raise PublisherIdentifierError("publisher_identifiers_catalog_mismatch")

    _bounded_str(
        raw["normalization"], MAX_NORMALIZATION_CHARS,
        "publisher_identifiers_normalization_invalid",
    )
    if type(raw["isbn_label_nearby"]) is not bool:
        raise PublisherIdentifierError("publisher_identifiers_flags_invalid")
    for field, expected in OBSERVATION_FLAGS.items():
        value = raw[field]
        if type(value) is not int or value != expected:
            raise PublisherIdentifierError("publisher_identifiers_flags_invalid")

    locators_raw = raw["paragraph_locators"]
    if not isinstance(locators_raw, list) or not 1 <= len(locators_raw) <= MAX_LOCATORS:
        raise PublisherIdentifierError("publisher_identifiers_locator_invalid")

    safe_locators: list[dict[str, Any]] = []
    seen_locators: set[tuple] = set()
    seen_paragraphs: set[str] = set()
    pieces: list[str] = []
    last_ordinal: int | None = None
    for position, item in enumerate(locators_raw):
        locator = _locator(item)
        paragraph_id = locator["paragraph_id"]
        ordinal, sha, normalized = resolve(edition_id, paragraph_id)
        if locator["ordinal"] != ordinal or locator["paragraph_sha256"] != sha:
            raise PublisherIdentifierError("publisher_identifiers_paragraph_mismatch")
        start = locator["normalized_start"]
        end = locator["normalized_end"]
        if not 0 <= start < end <= len(normalized):
            raise PublisherIdentifierError("publisher_identifiers_span_mismatch")
        if ((position > 0 and start != 0)
                or (position < len(locators_raw) - 1 and end != len(normalized))):
            raise PublisherIdentifierError("publisher_identifiers_span_mismatch")
        if ((position == 0 and start > 0 and normalized[start - 1].upper() in '0123456789X')
                or (position == len(locators_raw) - 1 and end < len(normalized)
                    and normalized[end].upper() in '0123456789X')):
            raise PublisherIdentifierError("publisher_identifiers_span_mismatch")
        key = (paragraph_id, ordinal, start, end)
        if key in seen_locators or paragraph_id in seen_paragraphs:
            raise PublisherIdentifierError("publisher_identifiers_duplicate_locator")
        seen_locators.add(key)
        seen_paragraphs.add(paragraph_id)
        if last_ordinal is not None and ordinal <= last_ordinal:
            raise PublisherIdentifierError("publisher_identifiers_locator_invalid")
        last_ordinal = ordinal
        piece = _strip_separators(normalized[start:end]).upper()
        pieces.append(piece)
        safe_locators.append(
            {
                "paragraph_id": paragraph_id,
                "ordinal": ordinal,
                "paragraph_sha256": sha,
                "normalized_start": start,
                "normalized_end": end,
            }
        )
    if _strip_separators("\n".join(pieces)) != digits:
        raise PublisherIdentifierError("publisher_identifiers_span_mismatch")

    return {
        "edition_id": edition_id,
        "publisher_work_id": work_id,
        "isbn13": isbn13,
        "observed_form": form,
        "identifier_digits": digits,
        "isbn_label_nearby": raw["isbn_label_nearby"],
        "paragraph_locators": safe_locators,
        "requires_review": 1,
        "local_edition_mapping_verified": 0,
        "accepted": 0,
        "human_confirmed": 0,
        "semantic_verified": 0,
        "reference_eligible": 0,
    }


def _load_catalog(catalog_path: Path) -> tuple[dict[str, Any], str]:
    code = "publisher_identifiers_catalog_invalid"
    try:
        sha_before = hashlib.sha256(_read_bytes(catalog_path, code, code, code)).hexdigest()
    except OSError:
        raise PublisherIdentifierError(code) from None
    try:
        catalog = bibliography.load(catalog_path)
    except bibliography.BibliographyError:
        raise PublisherIdentifierError(code) from None
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise PublisherIdentifierError(code) from None
    try:
        sha_after = hashlib.sha256(_read_bytes(catalog_path, code, code, code)).hexdigest()
    except OSError:
        raise PublisherIdentifierError(code) from None
    if sha_before != sha_after:
        raise PublisherIdentifierError("publisher_identifiers_catalog_changed")
    return catalog, sha_after


def _catalog_members(catalog: Mapping[str, Any]) -> dict[str, str]:
    members: dict[str, str] = {}
    for entry in catalog["entries"]:
        isbn13 = entry.get("isbn13")
        if isbn13 is not None:
            members[isbn13] = entry["work_id"]
    return members


def read_candidates(
    candidate_path: Any,
    database_path: Any,
    publisher_catalog_path: Any,
) -> dict[str, Any]:
    """Read one external candidate document; raise ``PublisherIdentifierError``.

    Returns a sanitised projection for a local UI: source metadata, verified
    aggregate counts and bounded safe observation locators. All acceptance and
    confirmation flags are always zero. Nothing is written and no source prose,
    producer caveat, URL or machine path is ever returned.
    """
    try:
        candidate = Path(candidate_path).resolve()
        database = Path(database_path).resolve()
        catalog_path = Path(publisher_catalog_path).resolve()
    except (TypeError, ValueError, OSError):
        raise PublisherIdentifierError("publisher_identifiers_invalid_path") from None

    # Caller paths are private configuration; both files must sit outside the
    # formal database directory.
    if candidate == database or _contained(candidate, database.parent):
        raise PublisherIdentifierError("publisher_identifiers_candidate_not_external")
    if catalog_path == database or _contained(catalog_path, database.parent):
        raise PublisherIdentifierError("publisher_identifiers_catalog_not_external")

    if not database.is_file():
        raise PublisherIdentifierError("publisher_identifiers_source_missing")
    try:
        _stable_source(database)
    except ValueError:
        raise PublisherIdentifierError("publisher_identifiers_unstable_source") from None
    try:
        source_sha = fingerprint(database)
    except OSError:
        raise PublisherIdentifierError("publisher_identifiers_source_unreadable") from None

    raw = _read_bytes(
        candidate,
        "publisher_identifiers_missing",
        "publisher_identifiers_unreadable",
        "publisher_identifiers_too_large",
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise PublisherIdentifierError("publisher_identifiers_invalid_json") from None
    if not isinstance(payload, Mapping) or set(payload) != TOP_KEYS:
        raise PublisherIdentifierError("publisher_identifiers_schema_mismatch")
    if payload.get("schema") != SCHEMA:
        raise PublisherIdentifierError("publisher_identifiers_schema_mismatch")

    created_at_utc = _timestamp(
        payload["created_at_utc"], "publisher_identifiers_timestamp_invalid"
    )
    _sha(payload["source_sha256"], "publisher_identifiers_hash_invalid")
    declared_catalog_sha = _sha(
        payload["publisher_catalog_sha256"], "publisher_identifiers_hash_invalid"
    )
    window = payload["rolling_ocr_line_window"]
    if type(window) is not int or window != ROLLING_OCR_LINE_WINDOW:
        raise PublisherIdentifierError("publisher_identifiers_counts_invalid")
    searched_editions = _nonneg_int(
        payload["searched_editions"], "publisher_identifiers_counts_invalid"
    )
    searched_paragraphs = _nonneg_int(
        payload["searched_paragraphs"], "publisher_identifiers_counts_invalid"
    )
    declared_editions = _nonneg_int(
        payload["editions_with_candidates"], "publisher_identifiers_counts_invalid"
    )
    declared_records = _nonneg_int(
        payload["distinct_publisher_records"], "publisher_identifiers_counts_invalid"
    )
    declared_rows = _nonneg_int(
        payload["candidate_rows"], "publisher_identifiers_counts_invalid"
    )
    if payload["corpus_updated"] is not False:
        raise PublisherIdentifierError("publisher_identifiers_contract_invalid")

    rule_sources = payload["isbn_rule_sources"]
    if not isinstance(rule_sources, list) or len(rule_sources) > MAX_RULE_SOURCES:
        raise PublisherIdentifierError("publisher_identifiers_url_invalid")
    for url in rule_sources:
        _official_url(url)

    caveats = payload["caveats"]
    if not isinstance(caveats, list) or len(caveats) > MAX_CAVEATS:
        raise PublisherIdentifierError("publisher_identifiers_caveats_invalid")
    for caveat in caveats:
        _bounded_str(caveat, MAX_CAVEAT_CHARS, "publisher_identifiers_caveats_invalid")

    observations_raw = payload["observations"]
    if not isinstance(observations_raw, list):
        raise PublisherIdentifierError("publisher_identifiers_observations_invalid")
    if len(observations_raw) > MAX_OBSERVATIONS:
        raise PublisherIdentifierError("publisher_identifiers_too_many_observations")
    if declared_rows != len(observations_raw):
        raise PublisherIdentifierError("publisher_identifiers_counts_invalid")

    if payload["source_sha256"] != source_sha:
        raise PublisherIdentifierError("publisher_identifiers_source_mismatch")

    catalog, catalog_sha = _load_catalog(catalog_path)
    if declared_catalog_sha != catalog_sha:
        raise PublisherIdentifierError("publisher_identifiers_catalog_sha_mismatch")
    catalog_by_isbn = _catalog_members(catalog)

    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        raise PublisherIdentifierError("publisher_identifiers_source_unreadable") from None
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")

        def _count(table: str) -> int:
            try:
                return connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            except sqlite3.Error:
                raise PublisherIdentifierError(
                    "publisher_identifiers_source_unreadable"
                ) from None

        edition_count = _count("editions")
        paragraph_count = _count("paragraphs")
        if (
            searched_editions != edition_count
            or searched_paragraphs != paragraph_count
        ):
            raise PublisherIdentifierError(
                "publisher_identifiers_search_counts_mismatch"
            )

        cache: dict[tuple[str, str], tuple[int, str, str]] = {}

        def resolve(edition_id: str, paragraph_id: str) -> tuple[int, str, str]:
            key = (edition_id, paragraph_id)
            resolved = cache.get(key)
            if resolved is None:
                try:
                    rows = connection.execute(
                        "SELECT ordinal, text FROM paragraphs "
                        "WHERE edition_id=? AND id=?",
                        key,
                    ).fetchall()
                except sqlite3.Error:
                    raise PublisherIdentifierError(
                        "publisher_identifiers_source_unreadable"
                    ) from None
                if len(rows) != 1:
                    raise PublisherIdentifierError(
                        "publisher_identifiers_paragraph_mismatch"
                    )
                text = rows[0]["text"]
                if not isinstance(text, str):
                    raise PublisherIdentifierError(
                        "publisher_identifiers_paragraph_mismatch"
                    )
                resolved = (
                    rows[0]["ordinal"],
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    _normalize(text),
                )
                cache[key] = resolved
            return resolved

        observations: list[dict[str, Any]] = []
        seen_observations: set[tuple] = set()
        for item in observations_raw:
            observation = _observation(item, resolve, catalog_by_isbn)
            locators = observation['paragraph_locators']
            if len(locators) > 1:
                try:
                    actual = connection.execute(
                        'SELECT id FROM paragraphs WHERE edition_id=? AND ordinal>=? AND ordinal<=? '
                        'ORDER BY ordinal,id LIMIT ?',
                        [observation['edition_id'], locators[0]['ordinal'], locators[-1]['ordinal'], MAX_LOCATORS + 1]
                    ).fetchall()
                except sqlite3.Error:
                    raise PublisherIdentifierError('publisher_identifiers_source_unreadable') from None
                if [row['id'] for row in actual] != [loc['paragraph_id'] for loc in locators]:
                    raise PublisherIdentifierError('publisher_identifiers_noncontiguous_span')
            identity = (
                observation["edition_id"],
                observation["isbn13"],
                observation["observed_form"],
                observation["identifier_digits"],
                tuple(
                    (
                        locator["paragraph_id"],
                        locator["ordinal"],
                        locator["normalized_start"],
                        locator["normalized_end"],
                    )
                    for locator in observation["paragraph_locators"]
                ),
            )
            if identity in seen_observations:
                raise PublisherIdentifierError(
                    "publisher_identifiers_duplicate_observation"
                )
            seen_observations.add(identity)
            observations.append(observation)
    finally:
        connection.close()

    editions_with_candidates = len({row["edition_id"] for row in observations})
    distinct_publisher_records = len(
        {row["publisher_work_id"] for row in observations}
    )
    if (
        editions_with_candidates != declared_editions
        or distinct_publisher_records != declared_records
    ):
        raise PublisherIdentifierError("publisher_identifiers_counts_invalid")

    # Re-verify both bound sources are stable across the entire read.
    try:
        _stable_source(database)
    except ValueError:
        raise PublisherIdentifierError("publisher_identifiers_unstable_source") from None
    try:
        if fingerprint(database) != source_sha:
            raise PublisherIdentifierError("publisher_identifiers_source_changed")
        if hashlib.sha256(_read_bytes(catalog_path, "publisher_identifiers_catalog_changed",
                                     "publisher_identifiers_catalog_changed",
                                     "publisher_identifiers_catalog_changed")).hexdigest() != catalog_sha:
            raise PublisherIdentifierError("publisher_identifiers_catalog_changed")
    except OSError:
        raise PublisherIdentifierError("publisher_identifiers_source_changed") from None

    return {
        "schema": SCHEMA,
        "available": True,
        "created_at_utc": created_at_utc,
        "source_sha256": source_sha,
        "publisher_catalog_sha256": catalog_sha,
        "rolling_ocr_line_window": ROLLING_OCR_LINE_WINDOW,
        "searched_editions": searched_editions,
        "searched_paragraphs": searched_paragraphs,
        "editions_with_candidates": editions_with_candidates,
        "distinct_publisher_records": distinct_publisher_records,
        "candidate_rows": len(observations),
        "observations": observations,
        "caution": CAUTION,
        "corpus_updated": False,
        "local_edition_mapping_verified": False,
        "source_unchanged": True,
    }


def _unavailable(code: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "available": False,
        "error": code,
        "observations": [],
        "candidate_rows": 0,
        "caution": CAUTION,
        "corpus_updated": False,
        "local_edition_mapping_verified": False,
    }


def overview(
    candidate_path: Any,
    database_path: Any,
    publisher_catalog_path: Any,
) -> dict[str, Any]:
    """Sanitised, never-raising wrapper around :func:`read_candidates`.

    A missing or malformed candidate document is reported as ``available:
    False`` with a static error code; no exception escapes and no local corpus
    is mutated.
    """
    try:
        return read_candidates(candidate_path, database_path, publisher_catalog_path)
    except PublisherIdentifierError as exc:
        return _unavailable(exc.code)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, RecursionError):
        return _unavailable("publisher_identifiers_invalid")


__all__ = [
    "SCHEMA",
    "CAUTION",
    "OBSERVATION_FLAGS",
    "PublisherIdentifierError",
    "read_candidates",
    "overview",
]
