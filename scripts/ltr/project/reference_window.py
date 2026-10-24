"""Explicit existing-edition candidate windows; never claims bilingual alignment.

All returned content is private runtime data, never engineering diagnostics.
Call only after the independent draft is frozen and comparison is authorized.
No title/body guessing: EPUB needs a selected spine; TXT needs selected lines.
"""
import hashlib
from pathlib import Path
import zipfile
import zlib

from .epub import EpubError, MAX_ARCHIVE_BYTES, EpubBook, extract_paragraphs
from .selected_chapter import clip_xhtml


class ReferenceWindowError(ValueError):
    """Fixed error codes only, with no content or source paths."""


def extract_reference_window(path, *, spine_id=None, start_paragraph=0,
                             start_line=None, end_line=None, encoding="utf-8-sig",
                             max_characters=10000):
    """Extract at most 10000 Unicode characters from an explicitly selected area.

    EPUB: exactly one spine_id, optional zero-based start_paragraph.
    TXT: explicit one-based inclusive start_line/end_line; default end is EOF.
    max_characters counts all characters, including punctuation, and clips the
    last row if needed. Row IDs are local to this window, not alignment IDs.
    """
    try:
        if type(max_characters) is not int or not 1 <= max_characters <= 10000:
            raise ReferenceWindowError("REFERENCE_INVALID_LIMIT")
        source = Path(path)
        suffix = source.suffix.lower()
        rows = []
        scope = {"alignment_status": "unverified_candidate", "max_characters": max_characters}
        if suffix == ".epub":
            if (not isinstance(spine_id, str) or not spine_id or start_line is not None
                    or end_line is not None or type(start_paragraph) is not int or start_paragraph < 0):
                raise ReferenceWindowError("REFERENCE_EXPLICIT_SPINE_REQUIRED")
            book = EpubBook(source)
            with book.archive:
                items = [item for item in book.spine if item["id"] == spine_id]
                if len(items) != 1:
                    raise ReferenceWindowError("REFERENCE_UNKNOWN_SPINE")
                item = items[0]
                raw = book.read(item["href"])
                rows = extract_paragraphs(clip_xhtml(raw, "", ""), spine_id)
                if start_paragraph >= len(rows):
                    raise ReferenceWindowError("REFERENCE_EMPTY_SELECTION")
                rows = rows[start_paragraph:]
                for row in rows:
                    row["locator"].update(href=item["href"],
                        source_sha256=hashlib.sha256(raw).hexdigest(),
                        offset_basis="normalized_paragraph")
                source_hash = book.book_hash
                scope.update(format="epub", spine_id=spine_id, start_paragraph=start_paragraph)
        elif suffix == ".txt":
            if (spine_id is not None or start_paragraph != 0 or type(start_line) is not int
                    or start_line < 1 or (end_line is not None and
                    (type(end_line) is not int or end_line < start_line))):
                raise ReferenceWindowError("REFERENCE_EXPLICIT_LINES_REQUIRED")
            if encoding not in {"utf-8", "utf-8-sig", "gb18030", "big5"}:
                raise ReferenceWindowError("REFERENCE_UNSUPPORTED_ENCODING")
            absolute = source.absolute()
            if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)()
                   for p in (absolute, *absolute.parents)):
                raise ReferenceWindowError("REFERENCE_SOURCE_LINK")
            with source.open("rb") as handle:
                raw = handle.read(MAX_ARCHIVE_BYTES + 1)
            if len(raw) > MAX_ARCHIVE_BYTES:
                raise ReferenceWindowError("REFERENCE_FILE_LIMIT")
            source_hash = hashlib.sha256(raw).hexdigest()
            text = raw.decode(encoding)
            lines = text.splitlines(keepends=True)
            if start_line > len(lines) or (end_line is not None and end_line > len(lines)):
                raise ReferenceWindowError("REFERENCE_LINE_OUT_OF_RANGE")
            offset = 0
            for index, line in enumerate(lines, 1):
                value = line.rstrip("\r\n")
                if start_line <= index <= (end_line or len(lines)) and value.strip():
                    rows.append({"text": value, "locator": {"line": index,
                        "start": 0, "end": len(value), "decoded_char_start": offset,
                        "offset_basis": "decoded_line", "encoding": encoding}})
                offset += len(line)
            scope.update(format="txt", start_line=start_line, end_line=end_line or len(lines))
        else:
            raise ReferenceWindowError("REFERENCE_UNSUPPORTED_FORMAT")
        selected = []
        remaining = max_characters
        for row in rows:
            if not remaining:
                break
            text = row["text"][:remaining]
            if not text:
                continue
            locator = dict(row["locator"])
            locator["end"] = locator.get("start", 0) + len(text)
            selected.append({"id": f"R{len(selected) + 1:04d}", "text": text,
                             "locator": locator})
            remaining -= len(text)
        if not selected:
            raise ReferenceWindowError("REFERENCE_EMPTY_SELECTION")
        scope["truncated"] = sum(len(row["text"]) for row in rows) > max_characters
        return {"source_hash": source_hash, "paragraphs": selected, "scope": scope}
    except ReferenceWindowError:
        raise
    except EpubError as error:
        raise ReferenceWindowError(str(error)) from None
    except (OSError, ValueError, LookupError, TypeError, RuntimeError,
            zipfile.BadZipFile, zlib.error, EOFError, RecursionError):
        raise ReferenceWindowError("REFERENCE_INVALID_INPUT") from None


def main(argv=None):
    """Save an explicitly selected private window; print only status and counts."""
    import argparse
    import json
    from .storage import private_root, write_json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--spine-id")
    parser.add_argument("--start-paragraph", type=int, default=0)
    parser.add_argument("--start-line", type=int)
    parser.add_argument("--end-line", type=int)
    parser.add_argument("--encoding", default="utf-8-sig")
    parser.add_argument("--max-characters", type=int, default=10000)
    args = parser.parse_args(argv)
    try:
        output = private_root(args.output)
        if output.exists():
            raise ReferenceWindowError("REFERENCE_OUTPUT_EXISTS")
        result = extract_reference_window(
            args.source, spine_id=args.spine_id, start_paragraph=args.start_paragraph,
            start_line=args.start_line, end_line=args.end_line, encoding=args.encoding,
            max_characters=args.max_characters,
        )
        write_json(output, result)
        print(json.dumps({"status": "window_saved", "paragraphs": len(result["paragraphs"]),
                          "model_calls": 0}))
        return 0
    except Exception:
        print('{"status":"rejected","code":"reference_window_failed"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
