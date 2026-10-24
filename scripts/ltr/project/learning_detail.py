"""Export every validated leaf analysis once, without source paragraph payloads.

All generated content is private. Diagnostics contain only fixed status/counts.
This does not call a model or replace leaf records with merge summaries.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .learning_export import CATEGORIES, KINDS, _completed, _leaf_records, _read
from .storage import atomic_write, digest, private_root, write_json

_FIELDS = ("id", "category", "name", "description", "aliases", "certainty", "scope",
           "evidence_ids", "source_kind", "evidence_trace")


def _text(value):
    # Disable raw HTML and Markdown media/link interpolation from model output.
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            .replace("`", "\\`").replace("\r", ""))


def _trace_text(trace):
    return "；".join(_text(
        f'{item["file_id"]} / {item["job_id"]} / {item["leaf_record_id"]} / '
        f'{item["paragraph_id"]} → {", ".join(item["original_source_ids"])} '
        f'/ SHA-256 {item["source_hash"]}') for item in trace)


def export_details(project):
    root = private_root(Path(project))
    manifest = _read(root / "learning-plan.json")
    valid, leaves = _leaf_records(root, manifest)
    associations = {key: [] for key in manifest["files"]}
    for book in manifest["books"]:
        for file_id in dict.fromkeys([book["source_file"], *book["reference_files"]]):
            associations[file_id].append({"book_id": book["book_id"], "title": book["title"]})

    records, jobs = [], []
    for job_id, proposed in valid.items():
        completed = _completed(root, "jobs", job_id)
        if completed is None:
            raise ValueError("learning_detail_snapshot_changed")
        job, result = completed
        expected = [{key: value for key, value in record.items()
                     if key not in {"source_kind", "evidence_trace"}} for record in proposed]
        if result.get("records", []) != expected:
            raise ValueError("learning_detail_snapshot_changed")
        file_id = job["file_id"]
        books = associations[file_id]
        for record in proposed:
            item = {key: record[key] for key in _FIELDS if key in record}
            item.update(book_associations=books, status="model_proposed_not_human_verified")
            records.append(item)
        jobs.append({"job_id": job_id, "file_id": file_id, "source_hash": job["source_hash"],
                     "source_kind": job["kind"], "book_associations": books,
                     "record_ids": [r["id"] for r in proposed],
                     "coverage_note": result.get("coverage_note", ""),
                     "evidence_trace": [trace for r in proposed for trace in r["evidence_trace"]],
                     "result_digest": digest(result),
                     "input_binding": result.get("input_binding", "legacy_payload_only")})
    ids = [record["id"] for record in records]
    if len(ids) != len(set(ids)) or set(ids) != set(leaves):
        raise ValueError("learning_detail_coverage_mismatch")
    counts = {category: sum(record["category"] == category for record in records)
              for category in CATEGORIES}
    overview = {"valid_jobs": len(jobs), "planned_jobs": len(manifest["jobs"]),
                "unique_records": len(records), "categories": counts}
    document = {"format": "series-learning-leaf-details-v1", "status": "model_proposed_not_human_verified",
                "count_semantics": "analysis_records_not_distinct_people_or_events",
                "coverage_semantics": "all_validated_leaf_records_without_merge_replacement",
                "pair_alignment": "edition_candidates_not_paragraph_alignment",
                "counts": overview, "records": records, "jobs": jobs}
    output = root / "分块全记录"
    write_json(output / "records.json", document)
    for category, title in CATEGORIES.items():
        lines = [f"# {title}：全部分块记录", "",
                 "以下均为模型候选分析，尚未人工确认。保留全部有效分块记录，卷级摘要不会替换或删减这些记录。", "",
                 f"本类 {counts[category]} 条分析记录；记录数不代表不同人物、事件或设定的数量。相同对象可能多次出现，分歧尚需核查。", ""]
        for record in records:
            if record["category"] != category:
                continue
            books = "；".join(f'{b["book_id"]} {_text(b["title"])}' for b in record["book_associations"])
            lines.extend([f'## {_text(record["name"])}', "", _text(record["description"]), "",
                          f'记录 ID：{_text(record["id"])}。', "",
                          f'关联卷：{books or "暂无配对卷关联"}。', "",
                          '来源：' + '；'.join(KINDS.get(kind, _text(kind)) for kind in record["source_kind"]) + '。', "",
                          f'别名：{_text("、".join(record.get("aliases", [])) or "未记录")}；'
                          f'确定性：{_text(record.get("certainty", "uncertain"))}；'
                          f'适用范围：{_text(record.get("scope", "unknown"))}。', "",
                          '证据链：' + _trace_text(record["evidence_trace"]) + '。', ""])
        atomic_write(output / f"{title}.md", "\n".join(lines) + "\n")
    lines = ["# 分块全记录：覆盖限制", "",
             "这是基于文本证据的候选知识学习，不是模型权重训练；模型分析不等于人工确认。", "",
             f'验证有效 {len(jobs)}/{len(manifest["jobs"])} 个文本块，保留 {len(records)} 条唯一分块分析记录。', "",
             "条数不能当作人物数。相同中文参考可能关联多个日文分卷，但每条分块记录只保存一次，关联卷全部保留。", "",
             "日文与中文分别读取，不等于逐段对齐；文本块完成不证明图像文字已经 OCR，也不证明全系列作品已收齐。", "",
             "本文逐块保留模型报告的覆盖限制；空白表示模型未填写，不能解释成没有限制。旧记录仅有 payload 绑定时，不声称完整任务封装已审计。", ""]
    for job in jobs:
        books = "；".join(f'{b["book_id"]} {_text(b["title"])}' for b in job["book_associations"])
        lines.extend([f'## {_text(job["job_id"])}', "", f'关联卷：{books or "暂无配对卷关联"}。', "",
                      f'来源：{KINDS.get(job["source_kind"], _text(job["source_kind"]))}；文件 ID：{_text(job["file_id"])}。', "",
                      f'输入绑定：{_text(job["input_binding"])}；分析记录 {len(job["record_ids"])} 条。', "",
                      '覆盖限制：' + (_text(job["coverage_note"]) if job["coverage_note"] else '模型未填写。'), "",
                      '记录 ID：' + '、'.join(_text(rid) for rid in job["record_ids"]) + '。', "",
                      '证据链：' + _trace_text(job["evidence_trace"]) + '。', ""])
    atomic_write(output / "覆盖限制.md", "\n".join(lines) + "\n")
    return {"status": "exported", **overview}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(export_details(args.project)), flush=True)
    except Exception:
        print(json.dumps({"status": "stopped", "code": "learning_detail_export_failed"}), flush=True)
        raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
