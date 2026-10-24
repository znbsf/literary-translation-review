"""Create private corpora and check existing ones without exposing source content."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import stat

from . import source_lineage

SCHEMA = "ltr-corpus-v1"
REPORT_SCHEMA = "ltr-corpus-check-v1"
REQUIRED_COLUMNS = {
    "editions": {"id", "book_key", "title", "language", "origin_kind",
                 "source_sha256", "metadata_json"},
    "paragraphs": {"edition_id", "ordinal", "id", "text", "locator_json"},
    "dialogues": {"id", "edition_id", "ordinal", "text", "start_para", "end_para",
                  "opening", "closed", "context_hash", "speaker", "interlocutor",
                  "attribution_status", "mode", "evidence_json"},
    "alignments": {"chinese_id", "japanese_ids_json", "status", "confidence",
                   "method", "evidence_json"},
    "metadata": {"key", "value_json"},
}
DDL = """
BEGIN IMMEDIATE;
CREATE TABLE editions(
    id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(id)) > 0),
    book_key TEXT, title TEXT, language TEXT, origin_kind TEXT,
    source_sha256 TEXT,
    metadata_json TEXT CHECK(metadata_json IS NULL OR json_valid(metadata_json))
);
CREATE TABLE paragraphs(
    edition_id TEXT NOT NULL REFERENCES editions(id),
    ordinal INTEGER NOT NULL CHECK(typeof(ordinal) = 'integer' AND ordinal >= 0),
    id TEXT NOT NULL CHECK(length(trim(id)) > 0), text TEXT NOT NULL,
    locator_json TEXT CHECK(locator_json IS NULL OR json_valid(locator_json)),
    PRIMARY KEY(edition_id, id), UNIQUE(edition_id, ordinal)
);
CREATE TABLE dialogues(
    id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(id)) > 0),
    edition_id TEXT NOT NULL REFERENCES editions(id),
    ordinal INTEGER NOT NULL CHECK(typeof(ordinal) = 'integer' AND ordinal >= 0),
    text TEXT NOT NULL,
    start_para INTEGER NOT NULL CHECK(typeof(start_para) = 'integer' AND start_para >= 0),
    end_para INTEGER NOT NULL CHECK(typeof(end_para) = 'integer' AND end_para >= start_para),
    opening TEXT, closed INTEGER CHECK(closed IN (0, 1)), context_hash TEXT,
    speaker TEXT, interlocutor TEXT, attribution_status TEXT, mode TEXT,
    evidence_json TEXT CHECK(evidence_json IS NULL OR json_valid(evidence_json)),
    UNIQUE(edition_id, ordinal),
    FOREIGN KEY(edition_id, start_para) REFERENCES paragraphs(edition_id, ordinal),
    FOREIGN KEY(edition_id, end_para) REFERENCES paragraphs(edition_id, ordinal)
);
CREATE TABLE alignments(
    chinese_id TEXT PRIMARY KEY NOT NULL REFERENCES dialogues(id),
    japanese_ids_json TEXT NOT NULL CHECK(json_valid(japanese_ids_json)
                                          AND json_type(japanese_ids_json) = 'array'),
    status TEXT, confidence TEXT, method TEXT,
    evidence_json TEXT CHECK(evidence_json IS NULL OR json_valid(evidence_json))
);
CREATE TABLE metadata(
    key TEXT PRIMARY KEY NOT NULL CHECK(length(trim(key)) > 0),
    value_json TEXT NOT NULL CHECK(json_valid(value_json))
);
CREATE INDEX dialogues_edition_range ON dialogues(edition_id, start_para, end_para);
INSERT INTO metadata VALUES ('schema', '"ltr-corpus-v1"');
COMMIT;
"""


class CorpusError(ValueError):
    """Only fixed, content-free codes may be raised or printed."""


def _stable_path(path: Path) -> Path:
    path = Path(path).absolute()
    try:
        source_lineage._no_links(path)
        if not path.is_file():
            raise CorpusError("database_missing")
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise CorpusError("stable_snapshot_required")
        return path
    except (OSError, source_lineage.LineageError):
        raise CorpusError("database_unavailable") from None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check_corpus(path: Path) -> dict:
    """Check rows and required columns, not literary accuracy or arbitrary DDL equivalence.

    Unversioned legacy databases are checked by the same rules and get a warning.
    Existing alignments retain their original statuses; no record is promoted.
    """
    path = _stable_path(path)
    identity = source_lineage._identity(path.stat())
    before = _sha256(path)
    issues, warnings, counts = [], [], {}
    version = None
    with source_lineage.readonly(path) as con:
        con.execute("BEGIN")
        if con.execute("PRAGMA quick_check(1)").fetchall() != [("ok",)]:
            issues.append({"code": "sqlite_integrity_failed"})
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, required in REQUIRED_COLUMNS.items():
            if table not in tables:
                issues.append({"code": "missing_table", "table": table})
                continue
            columns = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
            for column in sorted(required - columns):
                issues.append({"code": "missing_column", "table": table, "column": column})
            counts[table] = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        if not issues:
            _check_rows(con, issues)
            row = con.execute("SELECT value_json FROM metadata WHERE key='schema'").fetchone()
            if row is None:
                warnings.append({"code": "schema_version_missing"})
            else:
                try:
                    version = json.loads(row[0])
                except (ValueError, TypeError):
                    version = None
                if version != SCHEMA:
                    issues.append({"code": "schema_version_unsupported"})
        con.rollback()
    _stable_path(path)
    after = _sha256(path)
    _stable_path(path)
    unchanged = before == after and identity == source_lineage._identity(path.stat())
    if not unchanged:
        issues.append({"code": "database_changed_during_check"})
    return {"schema": REPORT_SCHEMA, "status": "fail" if issues else "pass",
            "schema_version": SCHEMA if version == SCHEMA else None,
            "counts": counts, "issues": issues, "warnings": warnings,
            "source_sha256": before, "source_unchanged": unchanged, "model_calls": 0}


def _check_rows(con: sqlite3.Connection, issues: list) -> None:
    def count(code: str, query: str) -> int:
        value = con.execute(query).fetchone()[0]
        if value:
            issues.append({"code": code, "count": value})
        return value

    for table, keys in (("editions", ("id",)), ("paragraphs", ("edition_id", "id")),
                        ("dialogues", ("id",)), ("alignments", ("chinese_id",)),
                        ("metadata", ("key",))):
        condition = " OR ".join(f'typeof("{k}") != \'text\' OR length(trim("{k}"))=0'
                                for k in keys)
        count(f"{table}_invalid_id", f'SELECT COUNT(*) FROM "{table}" WHERE {condition}')
        key_sql = ",".join(f'"{k}"' for k in keys)
        count(f"{table}_duplicate_id",
              f'SELECT COUNT(*) FROM (SELECT 1 FROM "{table}" GROUP BY {key_sql} HAVING COUNT(*)>1)')
    for table in ("paragraphs", "dialogues"):
        count(f"{table}_invalid_ordinal",
              f'SELECT COUNT(*) FROM "{table}" WHERE typeof(ordinal)!=\'integer\' OR ordinal<0')
        count(f"{table}_duplicate_ordinal",
              f'SELECT COUNT(*) FROM (SELECT 1 FROM "{table}" GROUP BY edition_id,ordinal HAVING COUNT(*)>1)')
        count(f"{table}_invalid_text",
              f'SELECT COUNT(*) FROM "{table}" WHERE typeof(text)!=\'text\'')
        count(f"{table}_missing_edition",
              f'SELECT COUNT(*) FROM "{table}" t LEFT JOIN editions e ON e.id=t.edition_id WHERE e.id IS NULL')
    count("dialogues_invalid_range", """SELECT COUNT(*) FROM dialogues
        WHERE typeof(start_para)!='integer' OR typeof(end_para)!='integer'
           OR start_para<0 OR end_para<start_para""")
    count("dialogues_missing_paragraph", """SELECT COUNT(*) FROM dialogues d
        WHERE NOT EXISTS(SELECT 1 FROM paragraphs p WHERE p.edition_id=d.edition_id AND p.ordinal=d.start_para)
           OR NOT EXISTS(SELECT 1 FROM paragraphs p WHERE p.edition_id=d.edition_id AND p.ordinal=d.end_para)""")
    for table, column, nullable in (("editions", "metadata_json", True),
                                    ("paragraphs", "locator_json", True),
                                    ("dialogues", "evidence_json", True),
                                    ("alignments", "evidence_json", True),
                                    ("metadata", "value_json", False)):
        condition = f'NOT json_valid("{column}")'
        if nullable:
            condition = f'"{column}" IS NOT NULL AND ({condition})'
        count(f"{table}_invalid_json", f'SELECT COUNT(*) FROM "{table}" WHERE {condition}')
    count("alignments_missing_target", """SELECT COUNT(*) FROM alignments a
        LEFT JOIN dialogues d ON d.id=a.chinese_id WHERE d.id IS NULL""")
    invalid = count("alignments_invalid_members", """SELECT COUNT(*) FROM alignments
        WHERE CASE WHEN json_valid(japanese_ids_json)
                   THEN json_type(japanese_ids_json)!='array' ELSE 1 END""")
    if not invalid:
        count("alignments_invalid_member_id", """SELECT COUNT(*) FROM alignments a,
            json_each(a.japanese_ids_json) j WHERE j.type!='text' OR length(trim(j.value))=0""")
        count("alignments_missing_source", """SELECT COUNT(*) FROM alignments a,
            json_each(a.japanese_ids_json) j LEFT JOIN dialogues d ON d.id=j.value WHERE d.id IS NULL""")
        count("alignments_duplicate_member", """SELECT COUNT(*) FROM (
            SELECT a.chinese_id,j.value FROM alignments a,json_each(a.japanese_ids_json) j
            GROUP BY a.chinese_id,j.value HAVING COUNT(*)>1)""")


def init_corpus(root: Path) -> dict:
    """Create a new private directory; never overwrite, append to, or migrate a corpus."""
    root = Path(root).absolute()
    for part in (root, *root.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise CorpusError("linked_private_root")
        if (part / ".git").exists():
            raise CorpusError("private_root_inside_git")
    try:
        root.mkdir()  # Parent must exist. Even an empty existing directory is refused.
    except FileExistsError:
        raise CorpusError("corpus_already_exists") from None
    path = root / "dialogue.sqlite"
    try:
        with closing(sqlite3.connect(path)) as con:
            con.execute("PRAGMA foreign_keys=ON")
            con.executescript(DDL)
        result = check_corpus(path)
        if result["status"] != "pass":
            raise CorpusError("initialization_validation_failed")
    except Exception:
        # Only files created by this initializer in its exclusively reserved directory.
        for suffix in ("-journal", "-wal", "-shm", ""):
            Path(str(path) + suffix).unlink(missing_ok=True)
        root.rmdir()
        raise
    return {**result, "status": "initialized"}


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        print('{"status":"rejected","code":"invalid_arguments"}')
        raise SystemExit(2)


def main() -> int:
    parser = QuietParser(description="Private corpus initialization and read-only structural checks; zero model calls")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create a new private directory containing dialogue.sqlite")
    init.add_argument("directory", type=Path)
    check = commands.add_parser("check", help="Read-only checks; report counts and hashes, never source text or paths")
    check.add_argument("database", type=Path)
    args = parser.parse_args()
    try:
        result = init_corpus(args.directory) if args.command == "init" else check_corpus(args.database)
        print(json.dumps(result, sort_keys=True))
        return 1 if result["status"] == "fail" else 0
    except CorpusError as exc:
        print(json.dumps({"status": "rejected", "code": str(exc)}))
    except Exception:
        print('{"status":"rejected","code":"corpus_operation_failed"}')
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
