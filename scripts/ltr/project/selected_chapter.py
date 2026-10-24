"""Deterministic chapter clipping; returned private content must never be logged."""
from pathlib import Path
import hashlib
import re
import zipfile
import zlib
from urllib.parse import urlsplit

from .epub import EpubError, _Book, _name, _paragraphs, _tag, _xml


def _boundary(value):
    if not isinstance(value, str) or not value:
        raise EpubError("CHAPTER_INVALID_BOUNDARY")
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.query or ("#" in value and not parts.fragment):
        raise EpubError("CHAPTER_INVALID_BOUNDARY")
    return _name(parts.path), parts.fragment


def _clip(data, start, end):
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise EpubError("EPUB_UNSUPPORTED_ENCODING") from None
    encoding = re.search(br"<\?xml\b[^?]*\bencoding\s*=\s*['\"]([^'\"]+)['\"]", data, re.I)
    if encoding and encoding.group(1).lower() not in {b"utf-8", b"utf8"}:
        raise EpubError("EPUB_UNSUPPORTED_ENCODING")
    # Only the inert HTML5 declaration is compatible; no external DTD/subset.
    data = re.sub(br"<!DOCTYPE[ \t\r\n]+html[ \t\r\n]*>", b"", data, count=1)
    root = _xml(data)
    bodies = [e for e in root if _tag(e) == "body"]
    if _tag(root) != "html" or len(bodies) != 1:
        raise EpubError("EPUB_INVALID_XHTML")
    events, anchors = [], {}

    def walk(node, forbidden=False):
        forbidden = forbidden or _tag(node) in {"ruby", "rt", "rp", "script", "style"}
        index = len(events)
        events.append((node, "enter"))
        for key in {node.get("id"), node.get("{http://www.w3.org/XML/1998/namespace}id"),
                    node.get("name") if _tag(node) == "a" else None} - {None}:
            anchors.setdefault(key, []).append((index, forbidden))
        events.append((node, "text"))
        for child in node:
            walk(child, forbidden)
            events.append((child, "tail"))

    walk(bodies[0])

    def locate(fragment, default):
        if not fragment:
            return default
        matches = anchors.get(fragment, [])
        if len(matches) != 1 or matches[0][1]:
            raise EpubError("CHAPTER_AMBIGUOUS_FRAGMENT")
        return matches[0][0]

    first, last = locate(start, 0), locate(end, len(events))
    if first >= last:
        raise EpubError("CHAPTER_REVERSED_BOUNDARY")
    for index, (node, kind) in enumerate(events):
        if not first <= index < last:
            if kind != "enter":
                setattr(node, kind, None)
            elif _tag(node) == "br":
                # Keep node positions for original DOM paths, but suppress breaks.
                node.tag = "__clipped_break__"
    return root


def extract_selected_chapter(epub_path, catalogue_dict):
    """Return only [start_href, end_href), with original archive/DOM locators.

    Explicit end is mandatory: unresolved last-chapter ranges fail closed.
    Locator start/end index normalized *clipped* text, not raw XML bytes.
    """
    try:
        if catalogue_dict.get("match_count") != 1:
            raise EpubError("CHAPTER_AMBIGUOUS_SELECTION")
        start_path, start_fragment = _boundary(catalogue_dict.get("start_href"))
        end_path, end_fragment = _boundary(catalogue_dict.get("end_href"))
        book = _Book(Path(epub_path))
        with book.archive:
            if catalogue_dict.get("book_hash") != book.book_hash:
                raise EpubError("CHAPTER_STALE_CATALOGUE")
            paths = [item["href"] for item in book.spine]
            if start_path not in paths or end_path not in paths:
                raise EpubError("CHAPTER_UNKNOWN_BOUNDARY")
            first, last = paths.index(start_path), paths.index(end_path)
            if last < first or (last == first and not end_fragment):
                raise EpubError("CHAPTER_REVERSED_BOUNDARY")
            selected = book.spine[first:last + bool(end_fragment)]
            paragraphs = []
            for item in selected:
                source = book.read(item["href"])
                root = _clip(source, start_fragment if item["href"] == start_path else "",
                             end_fragment if item["href"] == end_path else "")
                rows = _paragraphs(root, item["id"])
                for row in rows:
                    row["locator"].update(href=item["href"],
                                          source_sha256=hashlib.sha256(source).hexdigest(),
                                          offset_basis="normalized_clipped_paragraph")
                paragraphs.extend(rows)
            if not paragraphs:
                raise EpubError("CHAPTER_EMPTY_SELECTION")
            return {"book_hash": book.book_hash, "paragraphs": paragraphs,
                    "scope": {"start_href": catalogue_dict["start_href"],
                              "end_href": catalogue_dict["end_href"],
                              "end_exclusive": True,
                              "spine_ids": [item["id"] for item in selected]}}
    except EpubError:
        raise
    except (OSError, ValueError, LookupError, TypeError, AttributeError, RuntimeError,
            zipfile.BadZipFile, zlib.error, EOFError, RecursionError):
        raise EpubError("CHAPTER_INVALID_INPUT") from None
