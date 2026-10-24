"""Deterministic, read-only publisher ISBN occurrence scan.

Only an external, source-bound candidate document is published. Source prose is
used privately for span matching and is never returned by this module's API.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from ltr.library import publisher_identifiers as pi

MAX_BYTES = pi.MAX_BYTES
MAX_OBSERVATIONS = pi.MAX_OBSERVATIONS
WINDOW = pi.ROLLING_OCR_LINE_WINDOW


class PublisherScanError(ValueError):
    """A static rejection code, safe to expose without source context."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _guard(database, catalog, source_sha, catalog_sha):
    pi._stable_source(database)
    if pi.fingerprint(database) != source_sha:
        raise PublisherScanError("publisher_scan_source_changed")
    raw = pi._read_bytes(catalog, "publisher_scan_catalog_changed",
                         "publisher_scan_catalog_changed", "publisher_scan_catalog_changed")
    if hashlib.sha256(raw).hexdigest() != catalog_sha:
        raise PublisherScanError("publisher_scan_catalog_changed")
    pi._stable_source(database)


def _patterns(members):
    separator = r"[\s" + re.escape("".join(sorted(pi._DASHES))) + r"]*"
    for isbn13, work in sorted(members.items()):
        forms = [("isbn13", isbn13)]
        if isbn13.startswith("978"):
            core = isbn13[3:12]
            check = (-sum((10 - i) * int(c) for i, c in enumerate(core))) % 11
            forms.append(("isbn10_equivalent", core + ("X" if check == 10 else str(check))))
        for form, digits in forms:
            # Lookahead permits distinct, overlapping occurrences without losing
            # a second match to the first match's consumption.
            pattern = re.compile(r"(?<![0-9X])(?=(" + separator.join(digits)
                                 + r")(?![0-9X]))", re.IGNORECASE)
            yield pattern, work, isbn13, form, digits


def _collect(connection, members):
    patterns = list(_patterns(members))
    observations, seen, skipped = [], set(), set()

    def inspect(rows):
        text = "\n".join(row["normalized"] for row in rows)
        positions = []
        offset = 0
        for row in rows:
            positions.append((offset, offset + len(row["normalized"]), row))
            offset += len(row["normalized"]) + 1
        for pattern, work, isbn13, form, digits in patterns:
            for match in pattern.finditer(text):
                start, end = match.span(1)
                # First/last matched characters are digits, so they belong to
                # nonempty paragraphs. Keep every physical intermediate row.
                first = next(i for i, (a, b, _) in enumerate(positions) if a <= start < b)
                last = next(i for i, (a, b, _) in enumerate(positions) if a < end <= b)
                spans = [(row, max(start, a) - a, min(end, b) - a)
                         for a, b, row in positions[first:last + 1]]
                identity = (rows[0]["edition_id"], work, form,
                            tuple((r["id"], r["ordinal"], a, b) for r, a, b in spans))
                if any(a >= b for _, a, b in spans):
                    skipped.add(identity)
                    continue
                if identity in seen:
                    continue
                seen.add(identity)
                # One physical ISBN-13 may also contain its ISBN-10 equivalent.
                # Keep a bounded intermediate buffer until containment is known.
                if len(observations) >= MAX_OBSERVATIONS * 2:
                    raise PublisherScanError("publisher_scan_too_many_observations")
                locators = [{"paragraph_id": row["id"], "ordinal": row["ordinal"],
                             "paragraph_sha256": row["sha"],
                             "normalized_start": a, "normalized_end": b}
                            for row, a, b in spans]
                observations.append({
                    "edition_id": rows[0]["edition_id"], "publisher_work_id": work,
                    "isbn13": isbn13, "observed_form": form, "identifier_digits": digits,
                    "normalization": "Whole-paragraph NFKC; literal digits/X with whitespace and dash separators only",
                    "paragraph_locators": locators,
                    "isbn_label_nearby": bool(re.search(r"ISBN", text[max(0, start - 24):start], re.I)),
                    **pi.OBSERVATION_FLAGS,
                })

    rolling = deque(maxlen=WINDOW)
    previous_edition = None
    count = 0
    for raw in connection.execute(
        "SELECT p.edition_id,p.id,p.ordinal,p.text,e.language FROM paragraphs p "
        "JOIN editions e ON e.id=p.edition_id ORDER BY p.edition_id,p.ordinal,p.id"
    ):
        row = dict(raw)
        count += 1
        if not isinstance(row["text"], str):
            raise PublisherScanError("publisher_scan_source_invalid")
        row["sha"] = hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
        row["normalized"] = pi._normalize(row.pop("text"))
        inspect([row])
        if row["edition_id"] != previous_edition or row["language"] != "ja":
            rolling.clear()
        previous_edition = row["edition_id"]
        if row["language"] == "ja":
            rolling.append(row)
            if len(rolling) > 1:
                inspect(list(rolling))
    outer = {}
    for row in observations:
        if row['observed_form'] == 'isbn13':
            locators = row['paragraph_locators']
            bounds = ((locators[0]['ordinal'], locators[0]['paragraph_id'], locators[0]['normalized_start']),
                      (locators[-1]['ordinal'], locators[-1]['paragraph_id'], locators[-1]['normalized_end']))
            outer.setdefault((row['edition_id'], row['publisher_work_id']), []).append(bounds)
    physical, nested = [], 0
    for row in observations:
        locators = row['paragraph_locators']
        start = (locators[0]['ordinal'], locators[0]['paragraph_id'], locators[0]['normalized_start'])
        end = (locators[-1]['ordinal'], locators[-1]['paragraph_id'], locators[-1]['normalized_end'])
        if (row['observed_form'] == 'isbn10_equivalent' and
                any(low <= start and end <= high for low, high in
                    outer.get((row['edition_id'], row['publisher_work_id']), []))):
            nested += 1
            continue
        physical.append(row)
    if len(physical) > MAX_OBSERVATIONS:
        raise PublisherScanError('publisher_scan_too_many_observations')
    observations = physical
    observations.sort(key=lambda r: (r["edition_id"],
        tuple((p["ordinal"], p["paragraph_id"], p["normalized_start"], p["normalized_end"])
              for p in r["paragraph_locators"]), r["publisher_work_id"], r["observed_form"]))
    return observations, count, len(skipped), nested


def scan(database_path, publisher_catalog_path, output_path):
    """Publish fresh external candidates into an existing output directory.

    Return safe aggregates only. Existing output names (including dangling
    symlinks) are never reused; publication requires filesystem hard-link support.
    """
    temporary = None
    try:
        if os.path.lexists(output_path):
            raise PublisherScanError("publisher_scan_output_exists")
        database, catalog, output = (Path(p).resolve() for p in
                                     (database_path, publisher_catalog_path, output_path))
        if pi._contained(output, database.parent) or output in (database, catalog):
            raise PublisherScanError("publisher_scan_output_not_external")
        if pi._contained(catalog, database.parent):
            raise PublisherScanError("publisher_scan_catalog_not_external")
        if os.path.lexists(output):
            raise PublisherScanError("publisher_scan_output_exists")
        pi._stable_source(database)
        source_sha = pi.fingerprint(database)
        catalog_data, catalog_sha = pi._load_catalog(catalog)
        members = pi._catalog_members(catalog_data)
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            editions = connection.execute("SELECT COUNT(*) FROM editions").fetchone()[0]
            paragraphs = connection.execute("SELECT COUNT(*) FROM paragraphs").fetchone()[0]
            observations, scanned, skipped, nested = _collect(connection, members)
            if scanned != paragraphs:
                raise PublisherScanError("publisher_scan_source_invalid")
        finally:
            connection.close()
        _guard(database, catalog, source_sha, catalog_sha)
        payload = {
            "schema": pi.SCHEMA, "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_sha256": source_sha, "publisher_catalog_sha256": catalog_sha,
            "isbn_rule_sources": ["https://www.isbn-international.org/content/isbn-calculator",
                                  "https://www.isbn.org/faqs_general_questions"],
            "searched_editions": editions, "searched_paragraphs": paragraphs,
            "rolling_ocr_line_window": WINDOW,
            "editions_with_candidates": len({r["edition_id"] for r in observations}),
            "distinct_publisher_records": len({r["publisher_work_id"] for r in observations}),
            "candidate_rows": len(observations), "observations": observations,
            "caveats": [pi.CAUTION], "corpus_updated": False,
        }
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_BYTES:
            raise PublisherScanError("publisher_scan_too_large")
        descriptor, name = tempfile.mkstemp(prefix=".publisher-scan-", suffix=".json", dir=output.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        validated = pi.read_candidates(temporary, database, catalog)
        if (validated["source_sha256"] != source_sha
                or validated["publisher_catalog_sha256"] != catalog_sha):
            raise PublisherScanError("publisher_scan_binding_changed")
        _guard(database, catalog, source_sha, catalog_sha)
        try:
            os.link(temporary, output)
        except FileExistsError:
            raise PublisherScanError("publisher_scan_output_exists") from None
        return {key: payload[key] for key in (
            "source_sha256", "publisher_catalog_sha256", "searched_editions", "searched_paragraphs",
            "editions_with_candidates", "distinct_publisher_records", "candidate_rows", "corpus_updated"
        )} | {"output_sha256": hashlib.sha256(encoded).hexdigest(),
              "skipped_empty_spans": skipped, "nested_equivalents_omitted": nested, "source_unchanged": True,
              "local_edition_mapping_verified": False}
    except PublisherScanError:
        raise
    except pi.PublisherIdentifierError as exc:
        raise PublisherScanError(exc.code) from None
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, RecursionError, OverflowError):
        raise PublisherScanError("publisher_scan_invalid") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def main(argv=None):
    # Suppress argparse's echo of private path-valued/unrecognized arguments.
    class SafeParser(argparse.ArgumentParser):
        def error(self, message):
            raise PublisherScanError("publisher_scan_arguments_invalid")

    parser = SafeParser(description=__doc__)
    for flag in ("db", "catalog", "out"):
        parser.add_argument("--" + flag, required=True)
    try:
        args = parser.parse_args(argv)
        print(json.dumps(scan(args.db, args.catalog, args.out), sort_keys=True))
        return 0
    except PublisherScanError as exc:
        print(json.dumps({"error": exc.code, "corpus_updated": False}))
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
