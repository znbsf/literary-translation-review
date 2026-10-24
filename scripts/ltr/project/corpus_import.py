"""Private, complete-volume import and deterministic learning partitions.

Return values contain private prose. Callers must log only counts/fixed codes.
EPUB paragraph text uses the established normalizer; original XHTML is retained
byte-reversibly as UTF-8, and blank blocks are recorded separately as layout.
No language inference or content-based chapter deletion occurs here.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import zipfile
import zlib

from .epub import EpubError, _Book, _paragraphs, _tag, _xml
from .selected_chapter import _clip

MAX_TEXT_BYTES = 64 * 1024 * 1024
_BLOCKS = {"p", "div", "section", "article", "blockquote", "li", "dt", "dd",
           "h1", "h2", "h3", "h4", "h5", "h6", "pre", "td", "th"}


def _corpus_root(raw):
    """Narrow compatibility for inert XHTML declarations and one misplaced image.

    Never resolve a DTD or infer a general HTML repair. Original bytes stay in
    the snapshot. A standalone, textless img before html can be moved into body;
    no character data is discarded, and repaired DOM locators are labeled.
    """
    repairs = []
    standard = (br'<!DOCTYPE\s+html\s+PUBLIC\s+([\"\'])-//W3C//DTD XHTML 1\.1//EN\1'
                br'\s+([\"\'])http://www\.w3\.org/TR/xhtml11/DTD/xhtml11\.dtd\2\s*>')
    compatible, count = re.subn(standard, b"", raw, count=1)
    if count:
        repairs.append("inert_xhtml11_declaration")
        # These editions rely on the DTD only for the standard nonbreaking space.
        compatible, entities = re.subn(br'&nbsp;', b'&#160;', compatible)
        if entities:
            repairs.append("standard_nbsp_entity")
    try:
        return _clip(compatible, "", ""), repairs
    except EpubError as error:
        if str(error) != "EPUB_INVALID_XML":
            raise
    # The supported misplaced node has no child/text and cannot hide declarations.
    compatible = re.sub(br'<!DOCTYPE[ \t\r\n]+html[ \t\r\n]*>', b'', compatible, count=1)
    match = re.match(br'\s*(?:<\?xml\b[^?]*\?>\s*)?(<img\b[^<>]*/>)\s*', compatible)
    if not match:
        raise EpubError("EPUB_INVALID_XML")
    image = _xml(match.group(1))
    if _tag(image) != "img" or len(image) or image.text:
        raise EpubError("EPUB_INVALID_XML")
    root = _clip(compatible[match.end():], "", "")
    body = next(node for node in root if _tag(node) == "body")
    body.insert(0, image)
    repairs.append("leading_textless_image_moved_into_body")
    return root, repairs


def _element_order(root):
    result = {}

    def visit(node, path):
        result[path] = len(result)
        counters = {}
        for child in node:
            tag = _tag(child)
            counters[tag] = counters.get(tag, 0) + 1
            visit(child, f"{path}/{tag}[{counters[tag]}]")

    visit(root, "/html[1]")
    return result


def _blank_blocks(root, item, source_hash, existing):
    """Record whitespace-only leaf blocks lost by ordinary ASCII normalization."""
    result = []

    def visit(node, path):
        if _tag(node) in {"script", "style", "head", "ruby", "rt", "rp"}:
            return
        text = "".join(node.itertext())
        if (_tag(node) in _BLOCKS and not text.strip() and path not in existing
                and not any(_tag(child) in _BLOCKS for child in node.iter() if child is not node)
                and all(_tag(child) in {"br", "span", "a"} for child in node.iter() if child is not node)):
            result.append({"id": f'{item["id"]}:layout{len(result) + 1:06d}',
                           "text": text, "kind": "blank_block",
                           "breaks": sum(_tag(child) == "br" for child in node.iter()),
                           "locator": {"item_id": item["id"], "href": item["href"],
                                       "element_path": path, "source_sha256": source_hash}})
        counters = {}
        for child in node:
            tag = _tag(child)
            counters[tag] = counters.get(tag, 0) + 1
            visit(child, f"{path}/{tag}[{counters[tag]}]")

    visit(root, "/html[1]")
    return result


def import_book(path):
    """Read every spine resource, or fail entirely; image-only resources stay recorded."""
    try:
        book = _Book(Path(path))
        paragraphs, layout, items = [], [], []
        with book.archive:
            for spine_index, item in enumerate(book.spine):
                raw = book.read(item["href"])
                root, repairs = _corpus_root(raw)
                element_order = _element_order(root)
                source_hash = hashlib.sha256(raw).hexdigest()
                rows = _paragraphs(root, item["id"])
                for row in rows:
                    row["locator"].update(href=item["href"], spine_index=spine_index,
                                          document_index=element_order[row["locator"]["element_path"]],
                                          source_sha256=source_hash,
                                          offset_basis="normalized_paragraph")
                    if row["text"].strip():
                        paragraphs.append(row)
                    else:
                        layout.append({**row, "kind": "whitespace_paragraph"})
                blanks = _blank_blocks(root, item, source_hash,
                                       {row["locator"]["element_path"] for row in rows})
                for row in blanks:
                    row["locator"]["spine_index"] = spine_index
                    row["locator"]["document_index"] = element_order[row["locator"]["element_path"]]
                layout.extend(blanks)
                images = sum(_tag(node) in {"img", "image", "svg"} for node in root.iter())
                items.append({**item, "spine_index": spine_index,
                              "source_xhtml": raw.decode("utf-8"),
                              "source_encoding": "utf-8", "source_sha256": source_hash,
                              "paragraph_ids": [r["id"] for r in rows if r["text"].strip()],
                              "textless": not any(r["text"].strip() for r in rows),
                              "image_elements": images})
                if repairs:
                    items[-1]["parser_compatibility"] = repairs
                if "leading_textless_image_moved_into_body" in repairs:
                    for row in rows + blanks:
                        row["locator"]["element_path_basis"] = "compatibility_dom"
            if not paragraphs:
                raise EpubError("CORPUS_NO_TEXT")
            layout.sort(key=lambda row: (row["locator"]["spine_index"], row["locator"]["document_index"]))
            return {"format": "epub", "book_hash": book.book_hash,
                    "metadata": {"title": book.title, "declared_language": book.language,
                                 "body_language": "unverified"},
                    "paragraphs": paragraphs, "layout": layout, "items": items,
                    "scope": {"kind": "complete_spine", "spine_ids": [i["id"] for i in book.spine],
                              "parsed_spine_count": len(items), "text_extraction_complete": True,
                              "image_text_verified": False}}
    except EpubError:
        raise
    except (OSError, ValueError, LookupError, TypeError, AttributeError, RuntimeError,
            zipfile.BadZipFile, zlib.error, EOFError, RecursionError, OverflowError):
        raise EpubError("CORPUS_INVALID_BOOK") from None


def import_text(path, *, encoding):
    """Explicitly decoded reference text; every physical line and newline is retained."""
    if not isinstance(encoding, str) or encoding not in {"utf-8", "utf-8-sig", "gb18030", "utf-16"}:
        raise EpubError("CORPUS_UNSUPPORTED_ENCODING")
    try:
        source_path = Path(path).absolute()
        for component in (source_path, *source_path.parents):
            if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
                raise EpubError("CORPUS_SOURCE_LINK")
        with source_path.open("rb") as handle:
            raw = handle.read(MAX_TEXT_BYTES + 1)
        if len(raw) > MAX_TEXT_BYTES:
            raise EpubError("CORPUS_TEXT_LIMIT")
        if encoding == "utf-16" and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise EpubError("CORPUS_UTF16_BOM_REQUIRED")
        text = raw.decode(encoding, errors="strict")
        book_hash = hashlib.sha256(raw).hexdigest()
        paragraphs, layout = [], []
        offset = 0
        for number, line in enumerate(text.splitlines(keepends=True), 1):
            row = {"id": f"text:p{number:06d}", "text": line,
                   "locator": {"line": number, "start": offset, "end": offset + len(line),
                               "offset_basis": "decoded_text", "source_sha256": book_hash}, "ruby": []}
            offset += len(line)
            if line.strip():
                paragraphs.append(row)
            else:
                layout.append({**row, "kind": "whitespace_line"})
        if not paragraphs:
            raise EpubError("CORPUS_NO_TEXT")
        return {"format": "txt", "book_hash": book_hash, "source_encoding": encoding,
                "source_text": text, "metadata": {"body_language": "unverified"},
                "paragraphs": paragraphs, "layout": layout,
                "scope": {"kind": "complete_text", "text_extraction_complete": True,
                          "decoded_characters": len(text)}}
    except EpubError:
        raise
    except UnicodeError:
        raise EpubError("CORPUS_DECODE_FAILED") from None
    except (OSError, ValueError, TypeError, RuntimeError):
        raise EpubError("CORPUS_INVALID_TEXT") from None


def partition_learning(source, target_chars=22000):
    """Partition complete nonblank paragraphs, preserving order and exact-once coverage.

    target_chars is a soft limit: one longer paragraph stays whole in its own
    chunk. Layout stays outside model learning chunks but remains in the result.
    """
    if not isinstance(target_chars, int) or isinstance(target_chars, bool) or target_chars <= 0:
        raise EpubError("CORPUS_INVALID_PARTITION_SIZE")
    try:
        rows = source["paragraphs"]
        book_hash = source["book_hash"]
        if not isinstance(rows, list) or not rows or not isinstance(book_hash, str) or not book_hash:
            raise EpubError("CORPUS_INVALID_SOURCE")
        ids = [r["id"] for r in rows]
        if (any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids)
                or any(not isinstance(r["text"], str) or not r["text"].strip() for r in rows)):
            raise EpubError("CORPUS_INVALID_SOURCE")
        chunks, pending, chars = [], [], 0

        def flush():
            nonlocal pending, chars
            if pending:
                chunks.append({"id": f"chunk{len(chunks) + 1:05d}", "book_hash": book_hash,
                               "paragraphs": pending, "source_ids": [r["id"] for r in pending],
                               "characters": chars, "exceeds_target": chars > target_chars})
                pending, chars = [], 0

        for row in rows:
            size = len(row["text"])
            if pending and chars + size > target_chars:
                flush()
            pending.append(row)
            chars += size
        flush()
        covered = [key for chunk in chunks for key in chunk["source_ids"]]
        if covered != ids:
            raise EpubError("CORPUS_COVERAGE_FAILED")
        return {"book_hash": book_hash, "chunks": chunks, "layout": source.get("layout", []),
                "coverage": {"source_ids": ids, "source_count": len(ids), "covered_count": len(covered),
                             "complete": True, "order_preserved": True}, "target_chars": target_chars}
    except EpubError:
        raise
    except (KeyError, TypeError, AttributeError):
        raise EpubError("CORPUS_INVALID_SOURCE") from None
