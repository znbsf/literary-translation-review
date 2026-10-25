"""ltr: one entry point for every toolkit command.

    python scripts/ltr <command> [args]      (from the skill directory)
    python -m ltr <command> [args]           (with scripts/ on PYTHONPATH)

Each command runs the corresponding module exactly as `python -m <module>` would.
"""
import runpy
import sys
from pathlib import Path

if __package__ in (None, ""):  # started as `python scripts/ltr`: make the package importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

COMMANDS = {
    # step 1 — sources, projects and glossary
    "project": ("ltr.project", "EPUB preparation, corpus binding, review and explicitly authorized private workflows"),
    "assemble": ("ltr.project.assemble", "assemble confirmed project translations into TXT/EPUB"),
    "corpus": ("ltr.library.corpus", "initialize a private corpus or check an existing database read-only"),
    "glossary": ("ltr.library.glossary", "private wording proposals, human decisions, locked snapshots and read-only checks"),
    # step 2 — whole-book understanding
    "ledger": ("ltr.project.ledger", "private whole-book understanding and translation decisions"),
    "evidence": ("ltr.library.evidence", "read-only line, speaker and term evidence with provenance levels"),
    "tools": ("ltr.tools", "maintenance tools: manifest and leak scan"),
    # step 3 — evidence candidates
    "quotes": ("ltr.quotes.batch", "quote-span candidates for editions without quote markup"),
    "quotes-review": ("ltr.quotes.review", "independent model review of quote candidates"),
    "quotes-index": ("ltr.quotes.index", "build the read-only quote-candidate index"),
    "align": ("ltr.alignment.candidates", "dialogue alignment proposals"),
    "align-review": ("ltr.alignment.review", "independent review of alignment proposals"),
    "align-plan": ("ltr.alignment.plan", "plan new alignment targets from frozen evidence"),
    "align-campaign": ("ltr.alignment.campaign", "bounded multi-task alignment session"),
    "align-index": ("ltr.alignment.index", "build the read-only alignment index"),
    "attribute": ("ltr.attribution.review", "speaker/addressee suggestions bound to evidence"),
    "attribute-index": ("ltr.attribution.index", "build the read-only identity index"),
    "attribute-projection": ("ltr.attribution.projection", "strict source-bound projection of identity suggestions"),
    "ocr-vision": ("ltr.ocr.vision_run", "whole-page OCR candidates (plan / run)"),
    "ocr-order": ("ltr.ocr.reading_order_run", "reading-order geometry for existing OCR"),
    "ocr-regions": ("ltr.ocr.regions_run", "region transcription candidates into a region store"),
    "review-queue": ("ltr.library.review_queue", "fingerprinted queue of records still needing review"),
    # step 4 — generation and review
    "context": ("ltr.project.context", "source-first context, immutable candidates and comparison receipts"),
    # step 5 — viewer
    "workbench": ("ltr.workbench", "local read-only viewer: overview, dialogues, scan pages, review labels"),
}


def usage() -> str:
    width = max(map(len, COMMANDS))
    rows = "\n".join(f"  {name.ljust(width)}  {text}" for name, (_, text) in COMMANDS.items())
    return f"usage: ltr <command> [args]\n\ncommands:\n{rows}\n\nRun `ltr <command> --help` for options."


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(usage())
        return 0
    name = sys.argv[1]
    if name not in COMMANDS:
        print(f"unknown command: {name}\n\n{usage()}", file=sys.stderr)
        return 2
    module = COMMANDS[name][0]
    sys.argv = [f"ltr {name}", *sys.argv[2:]]
    runpy.run_module(module, run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
