"""Validate complete book translations and assemble deterministic private files."""
from __future__ import annotations

import argparse
import hashlib
from html import escape
import io
import json
import os
from pathlib import Path
import re
import zipfile
import xml.etree.ElementTree as ET

from .batch_translate import _verified
from .storage import digest, private_root, project_lock


class AssembleError(ValueError):
    """Fixed diagnostic codes only; never include source or translated text."""


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value):
        raise AssembleError("assemble_invalid_identifier")
    return value


def _read(path):
    path = private_root(path)
    if path.stat().st_size > 256 * 1024 * 1024:
        raise AssembleError("assemble_input_limit")
    return json.loads(path.read_text(encoding="utf-8"))


def _filename(book):
    title = book["title"]
    if not isinstance(title, str) or not title.strip():
        raise AssembleError("assemble_invalid_title")
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")[:100]
    if not title:
        raise AssembleError("assemble_invalid_title")
    return f'{book["book_id"]}_{title}_译文'


def _validated(root, book_id):
    plan = _read(root / "plan.json")
    matches = [book for book in plan["books"] if book["book_id"] == book_id]
    if len(matches) != 1:
        raise AssembleError("assemble_unknown_book")
    book = matches[0]
    job_ids = book["jobs"]
    if (not isinstance(job_ids, list) or not job_ids or len(set(job_ids)) != len(job_ids)
            or any(j not in plan["jobs"] for j in job_ids)):
        raise AssembleError("assemble_invalid_plan")
    source = _read(root / "imports" / f'{_id(book["source_file"])}.json')
    if book.get("source_snapshot_digest", digest(source)) != digest(source):
        raise AssembleError("assemble_source_changed")
    if book.get("source_hash", source["book_hash"]) != source["book_hash"]:
        raise AssembleError("assemble_source_changed")
    rows = source["paragraphs"]
    original_ids = [row["id"] for row in rows]
    if not rows or len(set(original_ids)) != len(original_ids):
        raise AssembleError("assemble_invalid_source")
    if any(not isinstance(row["text"], str) or not row["text"].strip() for row in rows):
        raise AssembleError("assemble_invalid_source")
    by_id = {row["id"]: row for row in rows}
    units, completed, bindings, issues = [], 0, [], []
    for job_id in job_ids:
        job = _read(root / "jobs" / f'{_id(job_id)}.input.json')
        if job.get("book_id", book_id) != book_id:
            raise AssembleError("assemble_wrong_book")
        if job.get("source_hash", source["book_hash"]) != source["book_hash"]:
            raise AssembleError("assemble_source_changed")
        result = _verified(root, job_id, job)
        if result is None:
            continue
        mapped = job["source_ids"]
        for original_id, supplied in zip(mapped, job["payload"]["source"]):
            if original_id not in by_id or supplied["text"] != by_id[original_id]["text"]:
                raise AssembleError("assemble_source_text_mismatch")
        if result.get("source_ids", mapped) != mapped:
            raise AssembleError("assemble_result_mapping_mismatch")
        for original_id, translated in zip(mapped, result["units"]):
            units.append({"source_id": original_id, "target": translated["target"],
                          "locator": by_id[original_id]["locator"]})
        completed += 1
        bindings.append({"job_id": job_id, "input_digest": digest(job), "result_digest": digest(result)})
        issues.append({"job_id": job_id, "issues": result.get("issues", [])})
    if completed != len(job_ids):
        return None, {"status": "incomplete", "completed_jobs": completed, "planned_jobs": len(job_ids),
                      "translated_units": len(units), "source_units": len(rows)}
    if [unit["source_id"] for unit in units] != original_ids:
        raise AssembleError("assemble_source_coverage_mismatch")
    items = source["items"]
    item_ids = [item["id"] for item in items]
    scope = source.get("scope", {})
    if (scope.get("kind") != "complete_spine" or scope.get("text_extraction_complete") is not True
            or scope.get("parsed_spine_count") != len(items) or scope.get("spine_ids") != item_ids):
        raise AssembleError("assemble_incomplete_source_snapshot")
    if (len(item_ids) != len(set(item_ids)) or not items
            or [pid for item in items for pid in item["paragraph_ids"]] != original_ids
            or any(row["locator"].get("item_id") not in item_ids for row in rows)):
        raise AssembleError("assemble_spine_coverage_mismatch")
    return (book, source, units, bindings, issues), None


def _review_notes(source, units, issues):
    originals = {row["id"]: row["text"] for row in source["paragraphs"]}
    hints = []
    for unit in units:
        source_length, target_length = len(originals[unit["source_id"]]), len(unit["target"])
        flags = []
        if source_length >= 80 and target_length < 30 and target_length < source_length * 0.25:
            flags.append("unusually_short_output")
        if re.search(r"[\u3041-\u3096\u30a1-\u30fa]", unit["target"]):
            flags.append("kana_present")
        if flags:
            hints.append({"source_id": unit["source_id"], "flags": flags,
                          "source_characters": source_length, "target_characters": target_length})
    lines = ["# 待核查问题", "", "本书已通过段落对应、顺序及覆盖检查；这些检查仅证明结构完整，不证明语义准确或人工验收通过。", "",
             "以下汇集各文本块翻译时记录的问题。没有记录问题，不等于没有翻译错误。未进行自动重译或二轮润色。", ""]

    def safe(value):
        return escape(str(value), quote=False).replace("[", "\\[").replace("]", "\\]").replace("`", "\\`")

    for job in issues:
        lines.extend([f'## {safe(job["job_id"])}', ""])
        if not job["issues"]:
            lines.extend(["模型未记录待核查问题。", ""])
        for issue in job["issues"]:
            text = issue if isinstance(issue, str) else json.dumps(issue, ensure_ascii=False)
            lines.extend(["- " + safe(text), ""])
    lines.extend(["## 额外定位提示", "", "这些是定位提示，不是错误判定。假名可能属于必要的专名、语音或语言游戏；短译文也可能合理。", ""])
    if not hints:
        lines.append("未触发异常短输出或假名提示；仍需人工检查语义。")
    for item in hints:
        labels = ["输出相对原文异常短" if flag == "unusually_short_output" else "译文仍含假名" for flag in item["flags"]]
        lines.append(f'- {safe(item["source_id"])}：{"；".join(labels)}。')
    return hints, ("\n".join(lines) + "\n").encode("utf-8")


def _sections(source, units):
    targets = {unit["source_id"]: unit for unit in units}
    result = []
    for item in source["items"]:
        entries = []
        for index, source_id in enumerate(item["paragraph_ids"]):
            row = targets[source_id]
            if row["locator"]["item_id"] != item["id"]:
                raise AssembleError("assemble_spine_coverage_mismatch")
            order = row["locator"].get("document_index")
            if not isinstance(order, int):
                raise AssembleError("assemble_missing_layout_order")
            entries.append((order, index, {"kind": "text", **row}))
        for layout in source.get("layout", []):
            if layout["locator"].get("item_id") != item["id"]:
                continue
            if layout.get("text", "").strip():
                raise AssembleError("assemble_nonblank_layout")
            if (type(layout.get("breaks", 0)) is not int or not 0 <= layout.get("breaks", 0) <= 10000
                    or layout.get("text", "").count("\n") > 10000):
                raise AssembleError("assemble_layout_limit")
            order = layout["locator"].get("document_index")
            if not isinstance(order, int):
                raise AssembleError("assemble_missing_layout_order")
            entries.append((order, -1, {"kind": "layout", "text": layout.get("text", ""),
                                        "breaks": layout.get("breaks", 0)}))
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        section = {"item_id": item["id"], "entries": [entry[2] for entry in entries]}
        if [row["source_id"] for row in section["entries"] if row["kind"] == "text"] != item["paragraph_ids"]:
            raise AssembleError("assemble_layout_reorders_source")
        result.append(section)
    return result


def _xml_bytes(text):
    data = text.encode("utf-8")
    ET.fromstring(data)
    return data


def _epub(book, sections, units):
    # Keep every original spine item in the source audit, but do not publish
    # image-only/blank-only items as empty text chapters or navigation entries.
    text_sections = [section for section in sections
                     if any(entry["kind"] == "text" for entry in section["entries"])]
    title = escape(book["title"])
    identifier = "urn:sha256:" + digest({"book": book["book_id"], "units": units})
    files = {"META-INF/container.xml": _xml_bytes(
        '<?xml version="1.0" encoding="utf-8"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
        '<rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')}
    manifest, spine, nav, exported_ids = [], [], [], []
    for number, section in enumerate(text_sections, 1):
        filename = f"chapter-{number:04d}.xhtml"
        content = []
        for entry in section["entries"]:
            if entry["kind"] == "layout":
                count = max(1, entry["text"].count("\n"), entry.get("breaks", 0))
                if count > 10000:
                    raise AssembleError("assemble_layout_limit")
                content.append('<div class="blank">' + '<br/>' * count + '</div>')
            else:
                paragraph_id = f"p{len(exported_ids) + 1:07d}"
                exported_ids.append(entry["source_id"])
                normalized = entry["target"].replace("\r\n", "\n").replace("\r", "\n")
                content.append(f'<p id="{paragraph_id}">{escape(normalized).replace(chr(10), "<br/>")}</p>')
        files[f"EPUB/{filename}"] = _xml_bytes(
            '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN">'
            f'<head><title>第{number}部分</title><style>p{{white-space:pre-wrap;text-indent:2em;line-height:1.7}}.blank{{height:auto}}</style></head>'
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
        '<dc:language>zh-CN</dc:language><dc:description>中文机器初译；纯文字整理，未收录图片或识别图片内文字，尚未逐段复核。</dc:description>'
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


def _check_existing(path, data):
    private_root(path)
    if path.exists() and (not path.is_file() or path.read_bytes() != data):
        raise AssembleError("assemble_output_conflict")


def _write_new(path, data):
    _check_existing(path, data)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with path.open("xb") as stream:
            created = True
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        _check_existing(path, data)
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        raise
    if path.read_bytes() != data:
        raise AssembleError("assemble_write_verification_failed")


def assemble_book(root, book_id, destination):
    try:
        root, destination = private_root(Path(root)), private_root(Path(destination))
        _id(book_id)
        with project_lock(root / "books" / book_id):
            validated, progress = _validated(root, book_id)
            if validated is None:
                return progress
            book, source, units, bindings, issues = validated
            sections = _sections(source, units)
            text_sections = []
            for section in sections:
                lines = []
                for entry in section["entries"]:
                    if entry["kind"] == "text":
                        lines.append(entry["target"])
                    else:
                        lines.extend([""] * max(1, entry["text"].count("\n"), entry.get("breaks", 0)))
                text_sections.append("\n".join(lines))
            text = ("\n\n".join(text_sections) + "\n").encode("utf-8")
            epub = _epub(book, sections, units)
            hints, review = _review_notes(source, units, issues)
            value = {"format": "complete-book-translation-v1", "book_id": book_id, "title": book["title"],
                     "source_hash": source["book_hash"], "source_snapshot_digest": digest(source),
                     "status": "complete_single_pass_draft_not_human_reviewed", "units": units,
                     "layout": source.get("layout", []), "job_bindings": bindings,
                     "issues_by_job": issues, "review_hints_not_error_verdicts": hints,
                     "coverage": {"source_units": len(source["paragraphs"]), "translated_units": len(units),
                                  "exact_once": True, "order_preserved": True},
                     "images_included": False, "image_text_ocr_performed": False,
                     "edition_layout_reproduced": False,
                     "artifacts": {"txt_sha256": hashlib.sha256(text).hexdigest(), "epub_sha256": hashlib.sha256(epub).hexdigest()}}
            name = _filename(book)
            note = (book["title"] + "\n\n本书为一次完整机器初译，尚未经逐段人工复核。\n"
                    "纯文字整理，未收录插图，未识别图片内文字。按原文件的正文顺序分段，不复刻原版分页和版式。\n").encode("utf-8")
            outputs = {destination / f"{name}.txt": text, destination / f"{name}.epub": epub,
                       destination / f"{name}_说明.txt": note,
                       destination / f"{name}_待核查问题.md": review,
                       root / "books" / book_id / "待核查问题.md": review,
                       root / "books" / book_id / "translation.json":
                       (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")}
            for path, data in outputs.items():
                _check_existing(path, data)
            for path, data in outputs.items():
                _write_new(path, data)
            return {"status": "assembled", "completed_jobs": len(bindings), "planned_jobs": len(bindings),
                    "translated_units": len(units), "source_units": len(source["paragraphs"]),
                    "spine_items": len(sections), "layout_blocks": len(source.get("layout", [])),
                    "destination_files": 4, "model_issue_count": sum(len(job["issues"]) for job in issues),
                    "review_hint_units": len(hints)}
    except AssembleError:
        raise
    except Exception:
        raise AssembleError("assemble_validation_failed") from None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--book", required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(assemble_book(args.project, args.book, args.destination)), flush=True)
    except AssembleError as error:
        print(json.dumps({"status": "stopped", "code": str(error)}), flush=True)
        raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
