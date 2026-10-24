"""Navigation-only chapter candidates. Never reads ordinary chapter XHTML.

Navigation ranges are evidence for scope selection, not proof of chapter content.
Fragment boundaries always require explicit span selection before body import.
"""
from pathlib import Path
import posixpath
import re
import zipfile
import zlib
from urllib.parse import unquote, urlsplit

from .epub import EpubError, _Book, _local_href, _name, _tag, _xml


class CatalogueError(ValueError):
    """Fixed diagnostic codes only."""


def _navigation_xml(data: bytes, kind: str):
    """Strip one inert, exact standard declaration; never load a DTD.

    This compatibility rule applies only to explicitly designated navigation.
    Internal subsets, entity declarations, extra/unknown doctypes, and all
    ordinary chapter XHTML retain the original strict XML rejection behavior.
    """
    declarations = list(re.finditer(br"<!\s*DOCTYPE\b", data, re.I))
    if declarations:
        if len(declarations) != 1 or re.search(br"<!\s*ENTITY\b", data, re.I):
            raise CatalogueError("CATALOGUE_UNSAFE_XML")
        pattern = (br'''<!DOCTYPE[ \t\r\n]+ncx[ \t\r\n]+PUBLIC[ \t\r\n]+(["'])-//NISO//DTD ncx 2005-1//EN\1[ \t\r\n]+(["'])http://www\.daisy\.org/z3986/2005/ncx-2005-1\.dtd\2[ \t\r\n]*>'''
                   if kind == "ncx" else br"<!DOCTYPE[ \t\r\n]+html[ \t\r\n]*>")
        match = re.search(pattern, data)
        if match is None or match.start() != declarations[0].start():
            raise CatalogueError("CATALOGUE_UNSAFE_XML")
        data = data[:match.start()] + data[match.end():]
    return _xml(data)


def _target(raw: str, base: str) -> str:
    parts = urlsplit(raw)
    path = unquote(parts.path)
    fragment = unquote(parts.fragment)
    if (parts.scheme or parts.netloc or parts.query or not path
            or path.startswith("/") or any(c in path for c in ("\\", "\x00", ":"))
            or any(c in fragment for c in ("\x00", "#"))):
        raise CatalogueError("CATALOGUE_UNSAFE_HREF")
    path = _name(posixpath.normpath(posixpath.join(base, path)))
    return path + ("#" + fragment if "#" in raw else "")


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _matches_label(label: str, candidates: set[str]) -> bool:
    # A conventional ordinal prefix is the only permitted decoration. Match
    # the entire label, never arbitrary substring content or trailing additions.
    return any(re.fullmatch(r"(?:第[一二三四五六七八九十零0-9０-９]+[話话章]\s*)?"
                            + re.escape(candidate), label) is not None
               for candidate in candidates)


def _ncx_records(root, base):
    maps = [e for e in root if _tag(e) == "navMap"]
    if len(maps) != 1:
        raise CatalogueError("CATALOGUE_INVALID_NAVIGATION")
    records = []

    def walk(parent, inherited_end=None):
        points = [e for e in parent if _tag(e) == "navPoint"]
        parsed = []
        for point in points:
            labels = [e for e in point if _tag(e) == "navLabel"]
            contents = [e for e in point if _tag(e) == "content"]
            if len(labels) != 1 or len(contents) != 1:
                raise CatalogueError("CATALOGUE_INVALID_NAVIGATION")
            label = _normalize("".join(labels[0].itertext()))
            href = _target(contents[0].get("src", ""), base)
            parsed.append((point, label, href))
        for index, (point, label, href) in enumerate(parsed):
            end = parsed[index + 1][2] if index + 1 < len(parsed) else inherited_end
            records.append((label, href, end))
            walk(point, end)

    walk(maps[0])
    return records


def _nav_records(root, base):
    navs = [e for e in root.iter() if _tag(e) == "nav" and
            ("toc" in e.get("{http://www.idpf.org/2007/ops}type", "").split()
             or e.get("role") == "doc-toc")]
    if len(navs) != 1:
        raise CatalogueError("CATALOGUE_INVALID_NAVIGATION")
    records = []

    def walk(parent, inherited_end=None):
        lists = [e for e in parent if _tag(e) == "ol"]
        if len(lists) > 1:
            raise CatalogueError("CATALOGUE_INVALID_NAVIGATION")
        if not lists:
            return
        parsed = []
        for item in lists[0]:
            if _tag(item) != "li":
                raise CatalogueError("CATALOGUE_INVALID_NAVIGATION")
            labels = [e for e in item if _tag(e) in ("a", "span")]
            if len(labels) != 1:
                raise CatalogueError("CATALOGUE_INVALID_NAVIGATION")
            node = labels[0]
            label = _normalize("".join(node.itertext()))
            href = _target(node.get("href", ""), base) if _tag(node) == "a" else None
            parsed.append((item, label, href))
        for index, (item, label, href) in enumerate(parsed):
            end = parsed[index + 1][2] if index + 1 < len(parsed) else inherited_end
            # A heading with no anchor cannot establish a reliable boundary.
            if index + 1 < len(parsed) and end is None:
                end = "!unresolved"
            records.append((label, href, end))
            walk(item, end)

    walk(navs[0])
    return records


def locate_chapter(path: Path, label_candidates: list[str]) -> dict:
    """Return opaque scope candidates without returning any navigation labels."""
    try:
        if (not isinstance(label_candidates, list) or not label_candidates
                or any(not isinstance(x, str) or not _normalize(x) for x in label_candidates)):
            raise CatalogueError("CATALOGUE_INVALID_LABELS")
        labels = {_normalize(x) for x in label_candidates}
        book = _Book(path)
        with book.archive:
            result = {"book_hash": book.book_hash, "match_count": 0,
                      "spine_ids": [], "confidence": "unmatched"}
            container = _xml(book.read("META-INF/container.xml"))
            opf = _local_href(next(e.get("full-path") for e in container.iter() if _tag(e) == "rootfile"), "")
            package = _xml(book.read(opf))
            manifest = next(e for e in package if _tag(e) == "manifest")
            spine = next(e for e in package if _tag(e) == "spine")
            navs = [e for e in manifest if "nav" in e.get("properties", "").split()]
            kind = "nav"
            if not navs:
                kind = "ncx"
                toc = spine.get("toc")
                navs = [e for e in manifest if (e.get("id") == toc if toc else
                        e.get("media-type") == "application/x-dtbncx+xml")]
            if not navs:
                return result
            if len(navs) != 1:
                raise CatalogueError("CATALOGUE_AMBIGUOUS_NAVIGATION")
            nav = navs[0]
            expected_media = "application/xhtml+xml" if kind == "nav" else "application/x-dtbncx+xml"
            if nav.get("media-type") != expected_media:
                raise CatalogueError("CATALOGUE_INVALID_NAVIGATION")
            nav_path = _local_href(nav.get("href", ""), posixpath.dirname(opf))
            # Navigation may legally be in the spine, but it is opened solely
            # because the manifest explicitly identifies it as navigation.
            root = _navigation_xml(book.read(nav_path), kind)
            if _tag(root) != ("html" if kind == "nav" else "ncx"):
                raise CatalogueError("CATALOGUE_INVALID_NAVIGATION")
            records = (_nav_records if kind == "nav" else _ncx_records)(root, posixpath.dirname(nav_path))
            matches = [(start, end) for label, start, end in records if _matches_label(label, labels)]
            result["match_count"] = len(matches)
            if not matches:
                return result
            result["confidence"] = "ambiguous"
            if len(matches) != 1:
                return result
            start, end = matches[0]
            if start is None or end == "!unresolved":
                return result
            result.update(start_href=start, end_href=end)
            hrefs = [i["href"] for i in book.spine]
            start_path, _, start_fragment = start.partition("#")
            end_path, _, end_fragment = (end or "").partition("#")
            if start_path not in hrefs or (end is not None and end_path not in hrefs):
                return result
            first = hrefs.index(start_path)
            last = hrefs.index(end_path) if end is not None else len(hrefs)
            if last < first or (last == first and not end_fragment):
                return result
            exclusive = last + bool(end_fragment)
            result["spine_ids"] = [i["id"] for i in book.spine[first:exclusive]]
            result["confidence"] = ("needs-fragment-selection" if "#" in start or (end is not None and "#" in end)
                                    else "navigation-only")
            return result
    except CatalogueError:
        raise
    except EpubError as error:
        raise CatalogueError(str(error)) from None
    except (OSError, ValueError, LookupError, RuntimeError, StopIteration, TypeError, zipfile.BadZipFile, zlib.error):
        raise CatalogueError("CATALOGUE_INVALID_ARCHIVE") from None
