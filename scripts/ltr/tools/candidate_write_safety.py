"""Transaction, WAL-safe backup and recoverable receipt support for candidate writes.

Only the operation's transaction writes the outbox. Receipt export/recovery opens
the database read-only and never marks or rewrites historical receipts.
"""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid

OUTBOX = "candidate_write_receipt_outbox"
FLAGS = {"requires_review": 1, "human_confirmed": 0,
         "semantic_verified": 0, "reference_eligible": 0}


def identifier(name):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return '"' + name + '"'


def readonly(db):
    con = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        con.execute("pragma query_only=ON")
    except BaseException:
        con.close()
        raise
    return con


def sha_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def digest(con, table, where="", params=()):
    # Order independent, including duplicate multiplicity; no reliance on rowid or
    # a query planner's scan order. Encode BLOBs explicitly for general old tables.
    rows = []
    for row in con.execute(f"select * from {identifier(table)}" +
                           (" where " + where if where else ""), params):
        payload = json.dumps(tuple(row), ensure_ascii=True, separators=(",", ":"),
                             default=lambda b: {"blob_hex": b.hex()}).encode()
        rows.append(hashlib.sha256(payload).digest())
    return {"rows": len(rows), "sha256": hashlib.sha256(b"".join(sorted(rows))).hexdigest()}


def table_names(con):
    return [r[0] for r in con.execute(
        "select name from sqlite_master where type='table' order by name")]


def snapshot(con, exclude=()):
    return {t: digest(con, t) for t in table_names(con) if t not in exclude}


def schema_snapshot(con):
    return [tuple(r) for r in con.execute("select type,name,tbl_name,sql from sqlite_master order by type,name")]


def check_health(con):
    checks = [r[0] for r in con.execute("pragma quick_check")]
    foreign = list(con.execute("pragma foreign_key_check"))
    if checks != ["ok"] or foreign:
        raise ValueError(f"database health guard failed: quick_check={checks!r}, FK rows={len(foreign)}")
    return {"pragma_quick_check": "ok", "foreign_key_violations": 0}


def check_flags(con, table):
    clause = " OR ".join(f"{identifier(k)} IS NOT {v}" for k, v in FLAGS.items())
    if con.execute(f"select count(*) from {identifier(table)} where {clause}").fetchone()[0]:
        raise ValueError("candidate safety flags are invalid")


def preflight_file(path):
    """Check target shape and actual directory creation permission before BEGIN.

    This is a preflight, not a promise that later export cannot fail. The outbox
    covers revocation, races and process failure after this check.
    """
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"refusing to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Actual create/flush catches ACL and read-only directory errors.
    fd, name = tempfile.mkstemp(prefix=".candidate-preflight-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"preflight\n")
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        Path(name).unlink(missing_ok=True)
    return path


def backup_locked(db, target, writer):
    """Copy the committed snapshot while BEGIN IMMEDIATE prevents other writers.

    A separate read-only connection is essential: backup() on a connection with
    an active write transaction can wait forever. SQLite backup includes WAL
    commits; comparing source main-file bytes would not validate that snapshot.
    """
    target = Path(target)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with closing(readonly(db)) as source, closing(sqlite3.connect(target)) as dest:
            source.backup(dest)
            check_health(dest)
            if schema_snapshot(writer) != schema_snapshot(dest) or snapshot(writer) != snapshot(dest):
                raise ValueError("logical backup differs from locked source snapshot")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {"file": target.name, "sha256": sha_file(target),
            "bytes": target.stat().st_size, "verification": "SQLite backup; schema and all table digests match locked source; quick_check and foreign_key_check pass"}


def save_receipt(con, receipt, operation):
    receipt_id = uuid.uuid4().hex
    receipt["receipt_id"] = receipt_id
    receipt["receipt_storage"] = OUTBOX
    receipt["database_state"] = "Validated within the write transaction; this receipt and the guarded changes commit together. Export or recovery time does not describe a new database after-state."
    con.execute(f"""CREATE TABLE IF NOT EXISTS {OUTBOX} (
        receipt_id TEXT PRIMARY KEY, operation TEXT NOT NULL,
        receipt_json TEXT NOT NULL)""")
    payload = json.dumps(receipt, ensure_ascii=False, indent=1)
    con.execute(f"INSERT INTO {OUTBOX} (receipt_id,operation,receipt_json) VALUES (?,?,?)", (receipt_id, operation, payload))
    if con.execute(f"SELECT receipt_json FROM {OUTBOX} WHERE receipt_id=?", (receipt_id,)).fetchone()[0] != payload:
        raise ValueError("outbox receipt readback differs")
    return receipt_id


def check_outbox_preserved(con, receipt_id, previous):
    old = digest(con, OUTBOX, "receipt_id <> ?", (receipt_id,))
    if (previous is not None and old != previous) or (previous is None and old["rows"]):
        raise ValueError("pre-existing outbox rows changed")
    if con.execute(f"SELECT count(*) FROM {OUTBOX} WHERE receipt_id=?", (receipt_id,)).fetchone()[0] != 1:
        raise ValueError("new outbox receipt missing or duplicated")


def export_receipt(db, receipt_id, target):
    """Read a committed outbox entry; atomically publish without clobbering."""
    target = Path(target)
    con = readonly(db)
    try:
        row = con.execute(f"SELECT receipt_json FROM {OUTBOX} WHERE receipt_id=?", (receipt_id,)).fetchone()
        if row is None:
            raise ValueError(f"receipt not found: {receipt_id}")
        payload = row[0]
        if json.loads(payload)["receipt_id"] != receipt_id:
            raise ValueError("outbox receipt identity differs")
    finally:
        con.close()
    if target.exists() or target.is_symlink():
        if target.is_file() and not target.is_symlink():
            try:
                same = json.loads(target.read_text(encoding="utf-8-sig")) == json.loads(payload)
            except (ValueError, UnicodeError):
                same = False
            if same:
                return json.loads(payload)
        raise FileExistsError(f"refusing to overwrite output: {target}")
    preflight_file(target)
    fd, name = tempfile.mkstemp(prefix=".candidate-receipt-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        # Same-directory hard link publishes a complete file atomically and
        # raises if another process has already created the target.
        os.link(name, target)
    finally:
        Path(name).unlink(missing_ok=True)
    return json.loads(payload)


def list_receipts(db):
    """Discover receipt IDs even if a process died before printing its commit ID."""
    with closing(readonly(db)) as con:
        if OUTBOX not in table_names(con):
            return []
        return [{"receipt_id": row[0], "operation": row[1]} for row in con.execute(
            f"SELECT receipt_id,operation FROM {OUTBOX} ORDER BY receipt_id")]


def finish_export(db, receipt_id, target):
    try:
        receipt = export_receipt(db, receipt_id, target)
    except BaseException as exc:
        print(json.dumps({"status": "committed_receipt_pending", "receipt_id": receipt_id,
                          "error_code": type(exc).__name__,
                          "recovery": "rerun with --db DB --recover-receipt RECEIPT_ID --receipt NEW_PATH; recovery is read-only"},
                         ensure_ascii=False))
        return 2
    print(json.dumps({"status": "committed", "receipt_id": receipt_id,
                      "rows": receipt.get("rows_inserted", receipt.get("rows_corrected"))}, ensure_ascii=False))
    return 0


def require_args(parser, args, names):
    for name in names:
        if not getattr(args, name):
            parser.error("--" + name.replace("_", "-") + " is required for writes")
