"""Local, dependency-free corpus workbench (read-only browsing plus external review labels).

* only existing ``dialogue.sqlite`` files are opened, read-only;
* the HTTP server binds ``127.0.0.1``, checks ``Host``/``Origin`` and accepts
  same-origin JSON POSTs only; there is no CORS;
* every query is parameterised, every list bounded, every error a static code;
* request bodies, query strings and source text are never logged.

Run with ``python -m ltr.workbench --config <file> --port 0``.
"""
from .common import (MAX_DIALOGUE_TEXT_CHARS, WorkbenchError, _bounded_int, _clamp, _clean_text, _like_pattern,
                     _resolved_scan_path, _validate_query_text)
from .config import CorpusConfig, WorkbenchConfig
from .database import CorpusDatabase, SourceSnapshotDatabase
from .reviews import ReviewStore, _sanitise_review
from .server import WorkbenchHandler, WorkbenchServer, build_server, main
from .store import WorkbenchStore, _locator_page_ids

__all__ = ["CorpusConfig", "CorpusDatabase", "ReviewStore", "SourceSnapshotDatabase", "WorkbenchConfig",
           "WorkbenchError", "WorkbenchHandler", "WorkbenchServer", "WorkbenchStore", "build_server", "main"]
