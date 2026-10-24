"""Bounded, local EPUB import. Never prints content or includes paths in errors.

Offsets index the normalized paragraph text, not raw XML bytes. element_path is
a namespace-independent, one-based element path in the immutable EPUB snapshot.
Metadata inspection never opens XHTML, navigation, or NCX resources.
Ordinary ASCII layout whitespace is collapsed; explicit br newlines, Unicode
spaces, and pre whitespace survive. Source XHTML snapshots are private data,
never executable HTML, preview markup, model payloads, or engineering output.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import posixpath
import re
import stat
from urllib.parse import unquote, urlsplit
import zipfile
import zlib
import xml.etree.ElementTree as ET


MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ENTRY_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_RATIO = 200
MAX_XML_DEPTH = 128
MAX_XML_NODES = 200_000


class EpubError(ValueError):
    """Only fixed codes are safe for engineering diagnostics."""


def safe_archive_name(value: str) -> str:
    if (not value or "\\" in value or "\x00" in value or ":" in value
            or value.startswith("/")
            or any(p in (".", "..", "") for p in value.rstrip("/").split("/"))):
        raise EpubError("EPUB_UNSAFE_PATH")
    return value


def local_href(value: str, base: str) -> str:
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.query or parts.fragment:
        raise EpubError("EPUB_UNSAFE_PATH")
    path = safe_archive_name(unquote(parts.path))
    return safe_archive_name(posixpath.join(base, path))


def xml_tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def parse_xml(data: bytes) -> ET.Element:
    # NUL also excludes UTF-16/32 encodings that could bypass the declaration scan.
    if b"\x00" in data or re.search(br"<!\s*(DOCTYPE|ENTITY)\b", data, re.I):
        raise EpubError("EPUB_UNSAFE_XML")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise EpubError("EPUB_INVALID_XML") from None
    pending = [(root, 1)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if depth > MAX_XML_DEPTH or count > MAX_XML_NODES:
            raise EpubError("EPUB_XML_LIMIT")
        pending.extend((child, depth + 1) for child in node)
    return root


class EpubBook:
    def __init__(self, path: Path):
        source_path = Path(path).absolute()
        for component in (source_path, *source_path.parents):
            if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
                raise EpubError("EPUB_SOURCE_LINK")
        with source_path.open("rb") as handle:
            snapshot = handle.read(MAX_ARCHIVE_BYTES + 1)
        if len(snapshot) > MAX_ARCHIVE_BYTES:
            raise EpubError("EPUB_ARCHIVE_LIMIT")
        self.book_hash = hashlib.sha256(snapshot).hexdigest()
        self.archive = zipfile.ZipFile(io.BytesIO(snapshot))
        entries = self.archive.infolist()
        if len(entries) > MAX_ENTRIES:
            raise EpubError("EPUB_ENTRY_LIMIT")
        names = set()
        total = 0
        for entry in entries:
            safe_archive_name(entry.orig_filename)  # ZipInfo normalizes backslashes on Windows.
            name = safe_archive_name(entry.filename)
            if name.rstrip("/") in names:
                raise EpubError("EPUB_DUPLICATE_ENTRY")
            names.add(name.rstrip("/"))
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise EpubError("EPUB_SYMLINK")
            if entry.flag_bits & 1 or name.lower() == "meta-inf/encryption.xml":
                raise EpubError("EPUB_ENCRYPTED")
            if (entry.file_size > MAX_ENTRY_BYTES or entry.file_size < 0
                    or entry.file_size > MAX_RATIO * max(entry.compress_size, 1)):
                raise EpubError("EPUB_EXPANSION_LIMIT")
            total += entry.file_size
        if total > MAX_TOTAL_BYTES:
            raise EpubError("EPUB_EXPANSION_LIMIT")
        container = parse_xml(self.read("META-INF/container.xml"))
        roots = [e for e in container.iter() if xml_tag(e) == "rootfile"]
        if len(roots) != 1 or not roots[0].get("full-path"):
            raise EpubError("EPUB_INVALID_CONTAINER")
        opf_path = local_href(roots[0].get("full-path"), "")
        package = parse_xml(self.read(opf_path))
        if xml_tag(package) != "package":
            raise EpubError("EPUB_INVALID_PACKAGE")
        children = {xml_tag(e): e for e in package}
        if len(children) != len(package):
            raise EpubError("EPUB_INVALID_PACKAGE")
        if not all(k in children for k in ("metadata", "manifest", "spine")):
            raise EpubError("EPUB_INVALID_PACKAGE")
        metadata = children["metadata"]
        self.title = next(("".join(e.itertext()).strip() for e in metadata if xml_tag(e) == "title"), "")
        self.language = next(("".join(e.itertext()).strip() for e in metadata if xml_tag(e) == "language"), "")
        manifest = {}
        hrefs = {}
        href_counts = {}
        for item in children["manifest"]:
            if xml_tag(item) != "item":
                raise EpubError("EPUB_INVALID_MANIFEST")
            key = item.get("id")
            if not key or key in manifest:
                raise EpubError("EPUB_INVALID_MANIFEST")
            href = local_href(item.get("href", ""), posixpath.dirname(opf_path))
            media_type = item.get("media-type")
            if (href not in names or
                    (href in hrefs and hrefs[href] != media_type)):
                raise EpubError("EPUB_INVALID_MANIFEST")
            # Some editions alias the same cover under two manifest IDs. This
            # is harmless only for matching media types outside the text spine.
            hrefs[href] = media_type
            href_counts[href] = href_counts.get(href, 0) + 1
            manifest[key] = (href, media_type)
        self.spine = []
        for entry in children["spine"]:
            key = entry.get("idref")
            if (xml_tag(entry) != "itemref" or key not in manifest
                    or any(i["id"] == key for i in self.spine)):
                raise EpubError("EPUB_INVALID_SPINE")
            href, media_type = manifest[key]
            if href_counts[href] != 1:
                raise EpubError("EPUB_INVALID_MANIFEST")
            if media_type != "application/xhtml+xml":
                raise EpubError("EPUB_UNSUPPORTED_CONTENT")
            self.spine.append({"id": key, "href": href})
        if not self.spine:
            raise EpubError("EPUB_INVALID_SPINE")

    def read(self, name: str) -> bytes:
        with self.archive.open(name) as handle:
            data = handle.read(MAX_ENTRY_BYTES + 1)
        if len(data) > MAX_ENTRY_BYTES:
            raise EpubError("EPUB_EXPANSION_LIMIT")
        return data

    def metadata(self) -> dict:
        return {"book_hash": self.book_hash, "title": self.title,
                "language": self.language, "spine": self.spine}


def extract_paragraphs(root: ET.Element, item_id: str) -> list[dict]:
    if xml_tag(root) != "html":
        raise EpubError("EPUB_INVALID_XHTML")
    bodies = [e for e in root if xml_tag(e) == "body"]
    if len(bodies) != 1:
        raise EpubError("EPUB_INVALID_XHTML")
    paragraphs = []
    chunks = []
    ruby = []
    owner = "/html[1]/body[1]"
    preserve_space = False
    block_tags = {"p", "div", "section", "article", "blockquote", "li", "dt", "dd",
                  "h1", "h2", "h3", "h4", "h5", "h6", "pre", "td", "th"}
    ignored = {"script", "style", "rt", "rp", "head"}

    def normalize(parts: list[tuple[str, bool]]) -> str:
        output = []
        pending_space = False
        for value, preserve in parts:
            for char in value:
                if not preserve and char in " \t\r\n\f\v":
                    pending_space = True
                    continue
                if pending_space and output and output[-1] != "\n" and char != "\n":
                    output.append(" ")
                pending_space = False
                output.append(char)
        return "".join(output)

    def inline(node: ET.Element) -> str:
        if xml_tag(node) in ignored:
            return ""
        return (node.text or "") + "".join(inline(c) + (c.tail or "") for c in node)

    def flush() -> None:
        text = normalize(chunks)
        if text:
            paragraphs.append({"id": f"{item_id}:p{len(paragraphs) + 1:06d}",
                               "text": text,
                               "locator": {"item_id": item_id, "element_path": owner,
                                           "start": 0, "end": len(text)},
                               "ruby": list(ruby)})
        chunks.clear()
        ruby.clear()

    def visit(node: ET.Element, path: str) -> None:
        nonlocal owner, preserve_space
        tag = xml_tag(node)
        if tag in ignored:
            return
        if tag == "ruby":
            base = normalize([(inline(node), preserve_space)])
            reading = normalize([("".join("".join(e.itertext()) for e in node.iter() if xml_tag(e) == "rt"), preserve_space)])
            chunks.append((base, preserve_space))
            ruby.append({"base": base, "reading": reading})
            return
        if tag == "br":
            chunks.append(("\n", True))
            return
        previous = owner
        previous_preserve = preserve_space
        is_block = tag in block_tags
        if is_block:
            flush()
            owner = path
        preserve_space = preserve_space or tag == "pre"
        chunks.append((node.text or "", preserve_space))
        counters = {}
        for child in node:
            name = xml_tag(child)
            counters[name] = counters.get(name, 0) + 1
            visit(child, f"{path}/{name}[{counters[name]}]")
            chunks.append((child.tail or "", preserve_space))
        if is_block:
            flush()
            owner = previous
        preserve_space = previous_preserve

    visit(bodies[0], owner)
    flush()
    return paragraphs


def _operate(path: Path, item_ids: list[str] | None) -> dict:
    try:
        book = EpubBook(path)
        with book.archive:
            result = book.metadata()
            if item_ids is not None:
                if (not isinstance(item_ids, list) or not item_ids
                        or any(not isinstance(i, str) for i in item_ids)
                        or len(set(item_ids)) != len(item_ids)):
                    raise EpubError("EPUB_INVALID_SELECTION")
                by_id = {i["id"]: i for i in book.spine}
                if any(i not in by_id for i in item_ids):
                    raise EpubError("EPUB_UNKNOWN_ITEM")
                result["items"] = []
                for key in item_ids:
                    source = book.read(by_id[key]["href"])
                    root = parse_xml(source)
                    # EPUB XHTML is required here to be UTF-8. Keeping the BOM
                    # if present makes UTF-8 encoding reconstruct the exact bytes.
                    try:
                        source_xhtml = source.decode("utf-8")
                    except UnicodeDecodeError:
                        raise EpubError("EPUB_UNSUPPORTED_ENCODING") from None
                    result["items"].append({
                        **by_id[key], "paragraphs": extract_paragraphs(root, key),
                        "source_xhtml": source_xhtml,
                        "source_encoding": "utf-8",
                        "source_sha256": hashlib.sha256(source).hexdigest(),
                    })
            return result
    except EpubError:
        raise
    except (OSError, ValueError, LookupError, RuntimeError, NotImplementedError, zipfile.BadZipFile, zlib.error,
            EOFError, OverflowError, RecursionError):
        raise EpubError("EPUB_INVALID_ARCHIVE") from None


def inspect_epub(path: Path) -> dict:
    """Return private metadata; callers must not log the returned title or paths."""
    return _operate(path, None)


def import_items(path: Path, item_ids: list[str]) -> dict:
    """Read exactly the selected spine items from one immutable in-memory snapshot."""
    if item_ids is None:
        raise EpubError("EPUB_INVALID_SELECTION")
    return _operate(path, item_ids)
