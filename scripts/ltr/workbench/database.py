"""Read-only corpus database access, snapshot verification and evidence routing."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from functools import wraps
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from ltr.library import source_lineage
from .common import WorkbenchError, _as_int

if TYPE_CHECKING:
    from .config import CorpusConfig


class CorpusDatabase:
    """Read-only connection to one corpus database plus its fingerprint."""

    def __init__(self, corpus: CorpusConfig) -> None:
        self.corpus = corpus
        self.path = corpus.db_path
        self._tables: set[str] | None = None
        self._columns: dict[str, set[str]] = {}
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._fingerprint: str | None = None
        self._fingerprint_key: tuple[int, int] | None = None
        self._source_sha256: str | None = None

    # -- lifecycle -----------------------------------------------------
    def require(self) -> None:
        """Refuse to work with a database that does not already exist."""
        if not self.path.exists() or not self.path.is_file():
            raise WorkbenchError(HTTPStatus.NOT_FOUND, "database_missing")
        if any(Path(str(self.path) + suffix).exists() for suffix in ("-wal", "-journal")):
            raise WorkbenchError(HTTPStatus.CONFLICT, "stable_snapshot_required")

    def connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self.require()
                try:
                    uri = self.path.resolve().as_uri() + "?mode=ro"
                except (OSError, ValueError):  # pragma: no cover - defensive
                    raise WorkbenchError(
                        HTTPStatus.INTERNAL_SERVER_ERROR, "database_unreadable"
                    ) from None
                self._conn = sqlite3.connect(
                    uri,
                    uri=True,
                    check_same_thread=False,
                    timeout=5.0,
                )
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA query_only = ON")
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # -- schema --------------------------------------------------------
    def tables(self) -> set[str]:
        if self._tables is None:
            conn = self.connect()
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
            self._tables = {str(row[0]) for row in rows}
        return self._tables

    def has(self, table: str) -> bool:
        return table in self.tables()

    def columns(self, table: str) -> set[str]:
        cached = self._columns.get(table)
        if cached is not None:
            return cached
        if not self.has(table):
            return set()
        conn = self.connect()
        try:
            rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        except sqlite3.Error:
            return set()
        resolved = {str(row[1]) for row in rows}
        self._columns[table] = resolved
        return resolved

    def _pick(self, table: str, *candidates: str) -> str:
        """Resolve an optional column to a safe literal expression."""
        columns = self.columns(table)
        for candidate in candidates:
            if candidate in columns:
                return f'"{candidate}"'
        return "NULL"

    def has_column(self, table: str, column: str) -> bool:
        return column in self.columns(table)

    def fingerprint(self, *, refresh: bool = False) -> str:
        """Stable digest of the database content and schema.

        The digest is recomputed whenever the file's size/mtime changes so a
        review saved against changed data is never bound to a stale label.
        """
        self.require()
        try:
            stat = self.path.stat()
        except OSError:
            raise WorkbenchError(HTTPStatus.INTERNAL_SERVER_ERROR, "database_unreadable") from None
        key = (int(stat.st_size), int(stat.st_mtime_ns))
        with self._lock:
            if not refresh and self._fingerprint is not None and self._fingerprint_key == key:
                return self._fingerprint

        digest = hashlib.sha256()
        try:
            with open(self.path, "rb") as handle:
                while True:
                    chunk = handle.read(1 << 20)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError:
            raise WorkbenchError(HTTPStatus.INTERNAL_SERVER_ERROR, "database_unreadable") from None
        schema_digest = hashlib.sha256()
        try:
            conn = self.connect()
            for row in conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            ):
                schema_digest.update(str(row[0]).encode("utf-8", "replace"))
                schema_digest.update(b"\x00")
                schema_digest.update(str(row[1]).encode("utf-8", "replace"))
                schema_digest.update(b"\x00")
                schema_digest.update(str(row[2] or "").encode("utf-8", "replace"))
                schema_digest.update(b"\n")
        except sqlite3.Error:
            raise WorkbenchError(
                HTTPStatus.INTERNAL_SERVER_ERROR, "database_unreadable"
            ) from None
        source_sha256 = digest.hexdigest()
        digest.update(schema_digest.digest())
        value = digest.hexdigest()
        with self._lock:
            self._fingerprint = value
            self._fingerprint_key = key
            self._source_sha256 = source_sha256
        return value

    def source_sha256(self, *, refresh: bool = False) -> str:
        self.fingerprint(refresh=refresh)
        assert self._source_sha256 is not None
        return self._source_sha256

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> int:
        try:
            conn = self.connect()
            row = conn.execute(sql, tuple(params)).fetchone()
        except sqlite3.Error:
            raise WorkbenchError(HTTPStatus.INTERNAL_SERVER_ERROR, "database_query_failed") from None
        if row is None:
            return 0
        return _as_int(row[0])

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        try:
            conn = self.connect()
            return list(conn.execute(sql, tuple(params)).fetchall())
        except sqlite3.Error:
            raise WorkbenchError(HTTPStatus.INTERNAL_SERVER_ERROR, "database_query_failed") from None



class SourceSnapshotDatabase(CorpusDatabase):
    """A real historical DB, fully verified at each outer read boundary.

    Inside a boundary, require/query/scalar inspect only identity tokens and
    sidecars. Full before/after SHA checks catch same-token in-place mutation.
    A failed verification permanently invalidates this instance.
    """

    def __init__(self, corpus, formal_path, expected_sha256):
        super().__init__(corpus)
        self.formal_path = formal_path
        self.expected_sha256 = expected_sha256
        self._source_token = None
        self._invalid_code = None
        self._read_context = threading.local()

    def _token(self):
        source_lineage._no_links(self.formal_path.absolute())
        source_lineage._no_links(self.path.absolute())
        if (not self.path.is_file() or not self.formal_path.is_file()
                or self.path.parent.resolve() != self.formal_path.parent.resolve()
                or self.path.samefile(self.formal_path)):
            raise source_lineage.LineageError("source_snapshot_path_unsafe")
        if any(Path(str(self.path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise source_lineage.LineageError("source_snapshot_unstable")
        info = self.path.stat()
        if info.st_nlink != 1:
            raise source_lineage.LineageError("source_snapshot_path_unsafe")
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _fail(self, code):
        self._invalid_code = code
        raise WorkbenchError(HTTPStatus.CONFLICT, code) from None

    def require(self):
        if self._invalid_code is not None:
            self._fail(self._invalid_code)
        try:
            token = self._token()
            if self._source_token is None:
                self.verify_source()
            elif token != self._source_token:
                self._fail("source_snapshot_changed")
        except source_lineage.LineageError as exc:
            self._fail(str(exc))
        except (OSError, ValueError, RuntimeError):
            self._fail("source_snapshot_unavailable")

    def verify_source(self):
        if self._invalid_code is not None:
            self._fail(self._invalid_code)
        try:
            before = self._token()
            if self._source_token is not None and before != self._source_token:
                self._fail("source_snapshot_changed")
            source_lineage.validate_snapshot_path(self.formal_path, self.path, self.expected_sha256)
            after = self._token()
            if after != before:
                self._fail("source_snapshot_changed")
            self._source_token = after
        except source_lineage.LineageError as exc:
            self._fail(str(exc))
        except (OSError, ValueError, RuntimeError):
            self._fail("source_snapshot_unavailable")

    @contextmanager
    def verified_read(self):
        depth = getattr(self._read_context, "depth", 0)
        if depth == 0:
            self.verify_source()
        self._read_context.depth = depth + 1
        try:
            yield self
        finally:
            self._read_context.depth -= 1
            if depth == 0:
                self.verify_source()

    def fingerprint(self, *, refresh=False):
        self.require()
        if refresh and not getattr(self._read_context, "depth", 0):
            self.verify_source()
        if self._fingerprint is None:
            # Compute the original algorithm from actual snapshot bytes/schema,
            # preserving existing labels. Never assign the formal DB an old SHA.
            return super().fingerprint(refresh=True)
        return self._fingerprint

    def connect(self):
        self.require()
        return super().connect()

    def query(self, sql, params=()):
        self.require()
        result = super().query(sql, params)
        self.require()
        return result

    def scalar(self, sql, params=()):
        self.require()
        result = super().scalar(sql, params)
        self.require()
        return result


@contextmanager
def _verified_evidence(database):
    if isinstance(database, SourceSnapshotDatabase):
        with database.verified_read():
            yield database
    else:
        database.require()
        yield database
        database.require()


class _EvidenceStore:
    """Route overlay construction to its true source before any validation."""

    def __init__(self, store):
        self._store = store

    def database(self, corpus_id):
        return self._store.evidence_database(corpus_id)

    def __getattr__(self, name):
        return getattr(self._store, name)


class _EvidenceOverlay:
    """Check snapshot bytes before/after even direct-connection index readers."""

    def __init__(self, overlay):
        self._overlay = overlay

    def __getattr__(self, name):
        value = getattr(self._overlay, name)
        if not callable(value):
            return value
        @wraps(value)
        def checked(*args, **kwargs):
            with _verified_evidence(self._overlay.database):
                return value(*args, **kwargs)
        return checked


def _evidence_read(method):
    @wraps(method)
    def checked(self, params):
        _, database = self.evidence_database(params.get("corpus"))
        with _verified_evidence(database):
            return method(self, params)
    return checked
