"""Read-only source-snapshot validation and append-only SQLite proofs.

Digests cover every field, type and duplicate row; public reports contain no
source prose. These helpers never create a database, change a pragma that writes
to disk, grant review eligibility, or substitute a historical SHA for a live DB.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct


class LineageError(ValueError):
    """A source-free refusal code, safe to report without SQLite/source details."""


def _need(condition, code):
    if not condition:
        raise LineageError(code)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)


def _no_links(path):
    for part in (path, *path.parents):
        info = part.lstat()
        _need(not stat.S_ISLNK(info.st_mode)
              and not getattr(info, "st_file_attributes", 0) & 0x400,
              "source_snapshot_path_unsafe")


def validate_snapshot_path(formal_db, snapshot_db, expected_sha256):
    """Validate one explicitly configured byte snapshot beside the formal DB.

    Call again when admitting a request, not merely when loading configuration.
    Lexical ancestors are checked before resolving paths so junctions/symlinks
    cannot silently widen the configured capability. Hard links are refused too.
    """
    _need(isinstance(expected_sha256, str)
          and re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is not None,
          "source_snapshot_sha256_invalid")
    try:
        formal = Path(formal_db).absolute()
        snapshot_path = Path(snapshot_db).absolute()
        _no_links(formal)
        _no_links(snapshot_path)
        _need(formal.is_file() and snapshot_path.is_file(), "source_snapshot_not_file")
        _need(snapshot_path.parent.resolve(strict=True) == formal.parent.resolve(strict=True),
              "source_snapshot_parent_mismatch")
        _need(not snapshot_path.samefile(formal), "source_snapshot_is_formal")
        before = snapshot_path.stat()
        _need(before.st_nlink == 1, "source_snapshot_path_unsafe")
        _need(not any(Path(str(snapshot_path) + suffix).exists()
                      for suffix in ("-wal", "-shm", "-journal")), "source_snapshot_unstable")
        digest = hashlib.sha256()
        with snapshot_path.open("rb") as stream:
            _need(_identity(before) == _identity(os.fstat(stream.fileno())),
                  "source_snapshot_changed")
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
            _need(_identity(before) == _identity(os.fstat(stream.fileno())),
                  "source_snapshot_changed")
        _no_links(snapshot_path)
        _need(_identity(before) == _identity(snapshot_path.stat()), "source_snapshot_changed")
        _need(not any(Path(str(snapshot_path) + suffix).exists()
                      for suffix in ("-wal", "-shm", "-journal")), "source_snapshot_unstable")
        _need(digest.hexdigest() == expected_sha256, "source_snapshot_sha256_mismatch")
        return snapshot_path.resolve(strict=True)
    except LineageError:
        raise
    except (OSError, ValueError, RuntimeError, TypeError):
        raise LineageError("source_snapshot_unavailable") from None


@contextmanager
def readonly(path):
    """Open an existing database read-only; transaction ownership stays local."""
    con = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + "?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        yield con
    finally:
        con.close()


def _identifier(value):
    # Schema names are inspected, not interpolated as executable SQL.
    return '"' + value.replace('"', '""') + '"'


def _value(value):
    if value is None:
        return ["null"]
    if type(value) is int:
        return ["integer", str(value)]
    if type(value) is float:
        return ["real", struct.pack(">d", value).hex()]
    if type(value) is str:
        return ["text", value]
    if type(value) is bytes:
        return ["blob", value.hex()]
    raise LineageError("source_lineage_value_invalid")


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True,
                                    separators=(",", ":")).encode("ascii")).hexdigest()


def _schema(con):
    return {(row[0], row[1], row[2]): row[3] for row in con.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name,tbl_name")}


def _row_hashes(con, table):
    # Virtual-table hidden columns (hidden=1), e.g. FTS5's table-name cursor and
    # rank, are connection/query state rather than persisted source fields.
    # Generated columns (2/3) remain included. All actual FTS shadow tables are
    # independently discovered and protected, including their index BLOBs.
    columns = [row[1] for row in con.execute("PRAGMA table_xinfo(" + _identifier(table) + ")")
               if row[6] != 1]
    return Counter(_hash([_value(v) for v in row])
                   for row in con.execute("SELECT " + ",".join(_identifier(c) for c in columns)
                                          + " FROM " + _identifier(table)))


def _table_digest(con, table):
    hashes = _row_hashes(con, table)
    columns = [tuple(row) for row in con.execute("PRAGMA table_xinfo(" + _identifier(table) + ")")]
    return {"rows": sum(hashes.values()), "sha256": _hash(sorted(hashes.items())),
            "columns_sha256": _hash(columns)}


def snapshot(con):
    """Return schema and full-row multiset hashes only; do not change the DB.

    For a concurrent DB the caller must hold a stable read transaction or the
    operation's write lock. A digest alone never proves an append-only subset.
    """
    schema = _schema(con)
    tables = sorted(key[1] for key in schema if key[0] == "table")
    return {"schema_sha256": _hash(sorted((*key, sql) for key, sql in schema.items())),
            "tables": {table: _table_digest(con, table) for table in tables}}


def assert_append_only(before_con, after_con, *, append_tables=(), added_tables=(),
                       added_schema_objects=()):
    """Prove old schema/rows preserved, permitting only explicitly listed growth.

    ``added_schema_objects`` contains ``(type, name, tbl_name)`` tuples. New table
    objects and their automatic unique/PK indexes follow ``added_tables``; all
    other new indexes, triggers and views require an explicit tuple. Call inside
    the import transaction, again after writing its outbox, and after commit.
    """
    before_schema, after_schema = _schema(before_con), _schema(after_con)
    append_tables, added_tables = set(append_tables), set(added_tables)
    extra_objects = {tuple(key) for key in added_schema_objects}
    old_tables = {key[1] for key in before_schema if key[0] == "table"}
    new_tables = {key[1] for key in after_schema if key[0] == "table"} - old_tables
    _need(append_tables <= old_tables and not (added_tables & old_tables),
          "source_lineage_allowlist_invalid")
    _need(new_tables <= added_tables, "source_lineage_unexpected_table")
    for key, sql in before_schema.items():
        _need(key in after_schema and after_schema[key] == sql, "source_lineage_schema_changed")
    for key in after_schema.keys() - before_schema.keys():
        kind, name, table = key
        auto_index = (kind == "index" and table in added_tables
                      and name.startswith("sqlite_autoindex_" + table + "_")
                      and after_schema[key] is None)
        _need((kind == "table" and name in added_tables) or auto_index or key in extra_objects,
              "source_lineage_unexpected_schema_object")
    before, after = snapshot(before_con), snapshot(after_con)
    appended = {}
    for table in sorted(old_tables):
        old, new = before["tables"][table], after["tables"][table]
        _need(old["columns_sha256"] == new["columns_sha256"], "source_lineage_columns_changed")
        if table in append_tables:
            old_rows, new_rows = _row_hashes(before_con, table), _row_hashes(after_con, table)
            _need(not (old_rows - new_rows), "source_lineage_old_rows_changed")
            appended[table] = new["rows"] - old["rows"]
        else:
            _need(old == new, "source_lineage_protected_table_changed")
    return {"schema_before_sha256": before["schema_sha256"],
            "schema_after_sha256": after["schema_sha256"],
            "protected_tables": {table: before["tables"][table]
                                 for table in sorted(old_tables - append_tables)},
            "appended_rows": appended,
            "added_tables": {table: after["tables"][table] for table in sorted(new_tables)},
            "old_rows_preserved": True}
