"""Assemble confirmed project translations into private reader artifacts."""
from __future__ import annotations
import argparse
import io
import json
import os
from pathlib import Path
import tempfile
from html import escape
import zipfile
import xml.etree.ElementTree as ET
from .storage import digest, private_root, read_project, project_lock, StorageError

class AssembleError(ValueError):
    def __init__(self, code, paragraph_ids=()):
        super().__init__(code)
        self.paragraph_ids = list(paragraph_ids)

def _sections(source, units):
    """Keep source selection order and contiguous EPUB item boundaries."""
    sections = []
    for paragraph, unit in zip(source, units):
        item = paragraph.get("locator", {}).get("item_id")
        if not sections or sections[-1]["item_id"] != item:
            sections.append({"item_id": item, "entries": []})
        sections[-1]["entries"].append({"kind": "text", **unit})
    return sections


def _xml_bytes(text):
    data = text.encode("utf-8")
    ET.fromstring(data)
    return data


def _epub(book, sections, units, draft=False):
    text_sections = sections
    title = escape(book["title"])
    identifier = "urn:sha256:" + digest({"book": book["book_id"], "units": units})
    files = {"META-INF/container.xml": _xml_bytes(
        '<?xml version="1.0" encoding="utf-8"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
        '<rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')}
    files["EPUB/title.xhtml"] = _xml_bytes(
        '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN">'
        f'<head><title>{title}</title></head><body><h1>{title}</h1>'
        + ('<p>含未确认草稿</p>' if draft else '') + '</body></html>')
    manifest = ['<item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>']
    spine = ['<itemref idref="title"/>']
    nav, exported_ids = [], []
    for number, section in enumerate(text_sections, 1):
        filename = f"chapter-{number:04d}.xhtml"
        content = []
        for entry in section["entries"]:
            paragraph_id = f"p{len(exported_ids) + 1:07d}"
            exported_ids.append(entry["source_id"])
            normalized = entry["target"].replace("\r\n", "\n").replace("\r", "\n")
            content.append(f'<p id="{paragraph_id}">{escape(normalized).replace(chr(10), "<br/>")}</p>')
        files[f"EPUB/{filename}"] = _xml_bytes(
            '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN">'
            f'<head><title>第{number}部分</title><style>p{{white-space:pre-wrap;text-indent:2em;line-height:1.7}}</style></head>'
            '<body>' + ''.join(content) + '</body></html>')
        manifest.append(f'<item id="c{number}" href="{filename}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{number}"/>')
        nav.append(f'<li><a href="{filename}">第{number}部分</a></li>')
    if exported_ids != [unit["source_id"] for unit in units]:
        raise AssembleError("assemble_epub_coverage_mismatch")
    files["EPUB/nav.xhtml"] = _xml_bytes(
        '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN">'
        f'<head><title>{title}</title></head><body><nav epub:type="toc" id="toc"><h1>目录</h1><ol>{"".join(nav)}</ol></nav></body></html>')
    files["EPUB/package.opf"] = _xml_bytes(
        '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0" unique-identifier="bookid">'
        f'<metadata><dc:identifier id="bookid">{identifier}</dc:identifier><dc:title>{title}</dc:title>'
        '<dc:language>zh-CN</dc:language><dc:description>项目译文；纯文字整理，未收录图片。</dc:description>'
        '<meta property="dcterms:modified">2000-01-01T00:00:00Z</meta></metadata>'
        '<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        + ''.join(manifest) + '</manifest><spine>' + ''.join(spine) + '</spine></package>')
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in [("mimetype", b"application/epub+zip"), *files.items()]:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    data = stream.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if archive.testzip() is not None or archive.infolist()[0].filename != "mimetype":
            raise AssembleError("assemble_invalid_epub")
        targets = []
        for number in range(1, len(text_sections) + 1):
            root = ET.fromstring(archive.read(f"EPUB/chapter-{number:04d}.xhtml"))
            for paragraph in root.iter("{http://www.w3.org/1999/xhtml}p"):
                # br is the only generated inline element; reconstruct its newline.
                targets.append((paragraph.text or "") + ''.join("\n" + (child.tail or "") for child in paragraph))
        if targets != [unit["target"].replace("\r\n", "\n").replace("\r", "\n") for unit in units]:
            raise AssembleError("assemble_epub_text_mismatch")
    return data


def _check_existing(path):
    private_root(path)
    if path.exists():
        raise AssembleError("assemble_output_exists")


def _write_new(path, data):
    """Publish a complete sibling temporary file atomically, without replacement."""
    _check_existing(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # link is atomic and fails if the destination already exists, including
        # races after preflight; unlike replace it cannot overwrite another file.
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise AssembleError("assemble_output_exists") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def assemble_project(root, destination, format="both", allow_draft=False):
    if format not in {"epub", "txt", "both"}:
        raise AssembleError("assemble_invalid_format")
    root, destination = private_root(Path(root)), private_root(Path(destination))
    with project_lock(root):
        project = read_project(root, allow_missing_units=True)
        if project["format"] != "translation-project-v2":
            raise AssembleError("assemble_requires_v2")
        by_id = {unit["id"]: unit for unit in project["units"]}
        pending = [row["id"] for row in project["source"]
                   if by_id.get(row["id"], {}).get("status") != "accepted"
                   or not by_id.get(row["id"], {}).get("target", "").strip()]
        if pending and not allow_draft:
            raise AssembleError("assemble_unconfirmed_paragraphs", pending)
        units = [{"source_id": row["id"],
                  "target": (by_id.get(row["id"], {}).get("target", "")
                             if by_id.get(row["id"], {}).get("target", "").strip() else "〔缺失译文〕")}
                 for row in project["source"]]
        # Do not export internal metadata, identifiers, source locators or paths.
        book = {"title": "译文", "book_id": project["book_hash"]}
        sections = _sections(project["source"], units)
        outputs = {}
        if format in {"txt", "both"}:
            header = "译文\n" + ("含未确认草稿\n" if allow_draft else "")
            text = header + "\n" + "\n\n".join(
                "\n".join(entry["target"] for entry in section["entries"]) for section in sections) + "\n"
            outputs[destination / "translation.txt"] = text.encode("utf-8")
        if format in {"epub", "both"}:
            outputs[destination / "translation.epub"] = _epub(book, sections, units, allow_draft)
        for path in outputs:
            _check_existing(path)
        for path, data in outputs.items():
            _write_new(path, data)
        return {"status": "assembled", "draft": allow_draft, "paragraphs": len(units),
                "unconfirmed_paragraph_ids": pending, "destination_files": len(outputs)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--format", choices=("epub", "txt", "both"), default="both")
    parser.add_argument("--allow-draft", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(assemble_project(args.project, args.out, args.format, args.allow_draft)))
        return 0
    except (AssembleError, StorageError) as error:
        print(json.dumps({"status": "stopped", "code": str(error),
                          "paragraph_ids": getattr(error, "paragraph_ids", [])}))
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "stopped", "code": "assemble_validation_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
