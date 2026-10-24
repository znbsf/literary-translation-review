"""Resumable complete-chapter execution. Private outputs, count-only diagnostics."""
from .storage import read_private_json as _read
import argparse
import json
from pathlib import Path
from .catalogue import locate_chapter
from .selected_chapter import extract_selected_chapter
from .storage import private_root, project_lock, write_json, digest, atomic_write
from .dimension_cycle import run_stage, CycleError
from .cycle_report import render

STAGES = {"context": "chapter-context.json", "dimensions": "dimensions-v1.json",
          "translate": "translation-v1.json", "compare": "comparison-v1.json",
          "retranslate": "translation-v2.json", "review": "comparison-v2.json"}


def _immutable(path, value):
    if path.exists():
        if _read(path) != value:
            raise CycleError("immutable_artifact_mismatch")
    else:
        write_json(path, value)


def partition_source(source, target_chars=1800):
    rows = source["paragraphs"]
    if (type(target_chars) is not int or target_chars < 1 or not rows
            or any(not isinstance(p.get("id"), str) or not p["id"] or
                   not isinstance(p.get("text"), str) for p in rows)
            or not any(p['text'].strip() for p in rows)
            or len({p["id"] for p in rows}) != len(rows)):
        raise CycleError("invalid_partition_source")
    chunks, selected, layout, count = [], [], [], 0
    for index, row in enumerate(rows):
        if not row['text'].strip():
            layout.append({'source_index':index, **row})
            continue
        selected.append(row)
        count += len(row["text"])
        if count >= target_chars or index == len(rows) - 1:
            chunks.append({"book_hash": source["book_hash"], "paragraphs": selected,
                           "source_digest": digest(selected), "horizon": selected[-1]["id"],
                           "selection": "complete_chapter_contiguous_chunk", "characters": count,
                           'layout_units':layout})
            selected, layout, count = [], [], 0
    if selected:
        chunks.append({'book_hash':source['book_hash'],'paragraphs':selected,'source_digest':digest(selected),
                       'horizon':selected[-1]['id'],'selection':'complete_chapter_contiguous_chunk',
                       'characters':count,'layout_units':layout})
    elif layout:
        chunks[-1]['layout_units'].extend(layout)
    return chunks


def prepare_chapter(epub, label, private_project):
    root = private_root(Path(private_project))
    with project_lock(root):
        if (root / "source.json").exists():
            raise CycleError("source_already_prepared")
        source = extract_selected_chapter(epub, locate_chapter(epub, [label]))
        chunks = partition_source(source)
        write_json(root / "source.json", source)
        write_json(root / "passage.json", chunks[0])
    return {"status": "prepared", "chunks": len(chunks), "paragraphs": len(source["paragraphs"])}


def prepare_chunk(root, index, passage):
    root = private_root(Path(root))
    if type(index) is not int or index < 0:
        raise CycleError("invalid_chunk_index")
    chunk = private_root(root / "chunks" / f"{index:04d}")
    with project_lock(chunk):
        for name, value in (("passage.json", passage), ("source.json", _read(root / "source.json")),
                            ("chapter-context.json", _read(root / "chapter-context.json"))):
            _immutable(chunk / name, value)
    return chunk


def assemble_chapter(root, source, chunks):
    translated={}
    for index,chunk in enumerate(chunks):
        units=_read(root/'chunks'/f'{index:04d}'/'translation-v2.json')['units']
        if [u['id'] for u in units] != [p['id'] for p in chunk['paragraphs']]:
            raise CycleError('assembly_coverage_mismatch')
        translated.update({u['id']:u['target'] for u in units})
    units=[{'id':p['id'],'target':translated[p['id']] if p['text'].strip() else p['text'],
            'kind':'translation' if p['text'].strip() else 'preserved_layout'} for p in source['paragraphs']]
    result={'source_digest':digest(source),'status':'model_proposal','units':units}
    write_json(root/'chapter-translation-v2.json',result)
    atomic_write(root/'chapter-translation-v2.txt','\n\n'.join(u['target'] for u in units))
    return digest(result)


def run_chapter(project, model, series_root=None, series_run=None):
    root = private_root(Path(project))
    with project_lock(root):
        source = _read(root / "source.json")
        chunks = partition_source(source)
        series_dependency = None
        if series_root is not None:
            from .series_registry import build_context_pack
            series_dependency = digest(build_context_pack(series_root,series_run))
        _immutable(root / "passage.json", chunks[0])
        plan = {"source_digest": digest(source), "chunks": len(chunks), "model": model,
                "planned_calls": 1 + 5 * len(chunks), "automatic_retries": 0,
                "series_dependency": series_dependency}
        _immutable(root / "workflow-plan.json", plan)
        attempted = 0

        def stage(directory, name, passage):
            nonlocal attempted
            claim_path, artifact = directory / (name + ".claim.json"), directory / STAGES[name]
            if claim_path.exists():
                claim = _read(claim_path)
                if (claim.get("status") != "completed" or not artifact.exists()
                        or claim.get("source_digest") != passage["source_digest"]
                        or claim.get('result_digest') != digest(_read(artifact))):
                    raise CycleError("stage_claim_requires_resolution")
                return
            if artifact.exists():
                raise CycleError("unclaimed_stage_artifact")
            attempted += 1
            write_json(root / "workflow-progress.json", {"status": "running", "new_calls_reserved": attempted})
            run_stage(directory, name, model, series_root=series_root, series_run=series_run)

        try:
            stage(root, "context", chunks[0])
            summaries = []
            for index, passage in enumerate(chunks):
                chunk = prepare_chunk(root, index, passage)
                for name in list(STAGES)[1:]:
                    if name == "compare" and not (chunk / "reference-window.json").exists():
                        write_json(chunk / "reference-window.json", {"paragraphs": [],
                            "scope": {"status": "no_existing_translation"}})
                    stage(chunk, name, passage)
                summaries.append(render(chunk))
                print(json.dumps({"chunk": index, "paragraphs": len(passage["paragraphs"]), "status": "completed"}))
            assembled_digest=assemble_chapter(root,source,chunks)
            result = {"status": "complete_model_proposals_not_human_accepted", "chunks": len(chunks),
                      "paragraphs": len(source["paragraphs"]), "planned_calls": plan["planned_calls"],
                      'translated_paragraphs':sum(len(c['paragraphs']) for c in chunks),
                      'preserved_layout_units':sum(len(c['layout_units']) for c in chunks),
                      'assembled_translation_digest':assembled_digest,
                      "new_calls_reserved": attempted, "chunk_summaries": summaries}
            write_json(root / "aggregate.json", result)
            write_json(root / "workflow-progress.json", {"status": "completed", "new_calls_reserved": attempted})
            return result
        except Exception:
            write_json(root / "workflow-progress.json", {"status": "stopped", "new_calls_reserved": attempted})
            raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "run"])
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--epub", type=Path)
    parser.add_argument("--label")
    parser.add_argument("--model")
    parser.add_argument("--series-root", type=Path)
    parser.add_argument("--series-run")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            if not args.epub or not args.label:
                raise CycleError("missing_selection")
            print(json.dumps(prepare_chapter(args.epub, args.label, args.project)))
        else:
            if not args.model:
                raise CycleError("missing_model")
            result = run_chapter(args.project, args.model, args.series_root, args.series_run)
            print(json.dumps({"status": result["status"], "chunks": result["chunks"]}))
    except Exception:
        print('{"status":"stopped","code":"workflow_failed"}')
        raise SystemExit(2)


if __name__ == "__main__":
    main()
