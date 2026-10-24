"""Run from the skill directory: python scripts/ltr project --help."""
from __future__ import annotations
import argparse
import json
from importlib import import_module
from pathlib import Path
import sys

from .epub import EpubError, inspect_epub, import_items
from .storage import StorageError, private_root, write_json, create_project, read_project, apply_edits
from .preview import render
from .contracts import ContractError, compile_context
from .catalogue import CatalogueError, locate_chapter


WORKFLOW_COMMANDS = {
    "batch-assemble": "batch_assemble",
    "batch-translate": "batch_translate",
    "cycle-report": "cycle_report",
    "dimension-cycle": "dimension_cycle",
    "learning-detail": "learning_detail",
    "learning-export": "learning_export",
    "learning-finalize": "learning_finalize",
    "learning-repair": "learning_repair",
    "series-learning": "series_learning",
    "series-synthesis": "series_synthesis",
    "series-workflow": "series_workflow",
    "series-registry": "series_registry",
    "reference-window": "reference_window",
}


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        print('{"status":"rejected","code":"invalid_arguments"}')
        raise SystemExit(2)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in WORKFLOW_COMMANDS:
        return import_module("." + WORKFLOW_COMMANDS[argv[0]], __package__).main(argv[1:])
    parser = QuietParser(description="Translation preparation and explicitly authorized private workflows")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="Inspect EPUB package metadata only")
    inspect.add_argument("epub", type=Path)
    inspect.add_argument("--output", type=Path, required=True)
    catalogue = sub.add_parser("locate", help="Locate an exact chapter label through EPUB navigation only")
    catalogue.add_argument("epub", type=Path)
    catalogue.add_argument("--labels", nargs="+", required=True)
    catalogue.add_argument("--output", type=Path, required=True)
    imp = sub.add_parser("import", help="Import explicitly selected spine items into a new private project")
    imp.add_argument("epub", type=Path)
    imp.add_argument("--items", nargs="+", required=True)
    imp.add_argument("--project", type=Path, required=True)
    imp.add_argument("--corpus", type=Path, help="New corpus directory directly inside the new project")
    for name in ("migrate", "bind", "recover"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--project", type=Path, required=True)
        if name != "recover":
            cmd.add_argument("--corpus", type=Path, required=True)
        if name == "bind":
            cmd.add_argument("--edition", required=True)
    for name in ("render", "preflight", "demo"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--project", type=Path, required=True)
    apply = sub.add_parser("apply", help="Apply manually edited translations with revision checks")
    apply.add_argument("--project", type=Path, required=True)
    apply.add_argument("--edits", type=Path, required=True)
    context = sub.add_parser("check-context", help="Validate selected context cards at a reading horizon")
    context.add_argument("--project", type=Path, required=True)
    context.add_argument("--cards", type=Path, required=True)
    context.add_argument("--horizon", required=True)
    for name in WORKFLOW_COMMANDS:
        sub.add_parser(name, help="Private workflow; use this subcommand's --help")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            from .demo import demo
            project = demo(args.project)
            print(json.dumps({"status": "synthetic_demo_ready", "paragraphs": len(project["source"]), "model_calls": 0}))
        elif args.command == "inspect":
            result = inspect_epub(args.epub)
            write_json(args.output, result)
            print(json.dumps({"status": "metadata_saved", "spine_count": len(result["spine"]), "model_calls": 0}))
        elif args.command == "locate":
            result = locate_chapter(args.epub, args.labels)
            write_json(args.output, result)
            print(json.dumps({"status": "navigation_checked", "confidence": result["confidence"],
                              "match_count": result["match_count"], "model_calls": 0}))
        elif args.command == "import":
            private_root(args.project)
            imported = import_items(args.epub, args.items)
            if args.corpus is None:
                project = create_project(args.project, imported)
            else:
                from .binding import create_bound_project
                project = create_bound_project(args.project, imported, args.corpus)
            render(args.project, project)
            print(json.dumps({"status": "imported", "paragraphs": len(project["source"]), "model_calls": 0}))
        elif args.command in ("migrate", "bind", "recover"):
            from .binding import migrate_project, bind_project, recover_project
            if args.command == "migrate":
                project = migrate_project(args.project, args.corpus)
            elif args.command == "bind":
                project = bind_project(args.project, args.corpus, args.edition)
            else:
                project = recover_project(args.project)
            render(args.project, project)
            print(json.dumps({"status": args.command + "_complete", "revision": project["revision"], "model_calls": 0}))
        elif args.command == "render":
            render(args.project, read_project(args.project))
            print('{"status":"rendered","model_calls":0}')
        elif args.command == "apply":
            if args.edits.stat().st_size > 20_000_000:
                raise StorageError("edit_file_too_large")
            project = apply_edits(args.project, json.loads(args.edits.read_text(encoding="utf-8")))
            render(args.project, project)
            print(json.dumps({"status": "saved", "revision": project["revision"], "model_calls": 0}))
        elif args.command == "check-context":
            project = read_project(args.project)
            if args.cards.stat().st_size > 1_000_000:
                raise StorageError("context_file_too_large")
            cards = json.loads(args.cards.read_text(encoding="utf-8"))
            checked = compile_context(cards, {p["id"]: p["text"] for p in project["source"]},
                                      [p["id"] for p in project["source"]], args.horizon, project["book_hash"])
            # Validation does not promote cards or grant runtime authorization.
            print(json.dumps({"status": "context_valid", "count": len(checked), "model_calls": 0}))
        else:
            project = read_project(args.project)
            plan = {"execution_authorized": False, "model_calls": 0,
                    "max_calls_proposed": 18, "retry_limit": 0,
                    "book_hash": project["book_hash"], "source_digest": project["source_digest"],
                    "project_revision": project["revision"],
                    "missing": ["verified_three_spans", "verified_context", "model_endpoint",
                                "cost_limit", "transport_privacy_validation", "runtime_authorization"],
                    "transport_implemented": False}
            write_json(args.project / "preflight.json", plan)
            print('{"status":"not_executable","model_calls":0}')
        return 0
    except (EpubError, StorageError, ContractError, CatalogueError) as error:
        # These two exception classes must contain fixed, content-free error codes.
        print(json.dumps({"status": "rejected", "code": str(error)}))
        return 2
    except Exception:
        # Never echo parser errors, provider responses, source strings, or local paths.
        print('{"status":"rejected","code":"local_operation_failed"}')
        return 2


if __name__ == "__main__":
    sys.exit(main())
