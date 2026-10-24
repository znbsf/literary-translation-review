"""Shared primitives for every corpus module.

Error codes are static identifiers chosen by our own code, so they can be shown
or logged without leaking source text, paths or parser messages. Hashes bind
*data* (databases, images, candidates); code identity is recorded only as
provenance and is never a read gate.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

HASH_RE = re.compile(r"[0-9a-f]{64}")
ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,200}")
MODEL_RE = re.compile(r"[A-Za-z0-9._:/-]{1,200}")
_CODE_RE = re.compile(r"[a-z0-9_]{1,80}")

# Machine output is always a candidate: these flags are fixed for every record
# a model produced, and only an explicit human review path may change them.
REVIEW_FLAGS = {"requires_review": 1, "accepted": 0, "human_confirmed": 0, "human_verified": 0,
                "semantic_verified": 0, "reference_eligible": 0}
OCR_FLAGS = {**REVIEW_FLAGS, "ocr_verified": 0, "reading_order_verified": 0}


class ContractError(ValueError):
    """A failed check, identified only by a static code."""

    def __init__(self, code: str = "invalid"):
        self.code = code if type(code) is str and _CODE_RE.fullmatch(code) else "invalid"
        super().__init__(self.code)


def need(condition: Any, code: str, error: type[ContractError] = ContractError) -> None:
    if not condition:
        raise error(code)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value: Any) -> bytes:
    """Stable JSON bytes used for every content hash."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return sha256(canonical(value))


def loads_strict(raw: bytes | str, code: str = "json_invalid", error: type[ContractError] = ContractError) -> Any:
    """Parse JSON rejecting duplicate keys, NaN/Infinity and invalid UTF-8."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            need(key not in result, code, error)
            result[key] = value
        return result

    def constant(_value):
        raise error(code)

    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return json.loads(text, object_pairs_hook=unique, parse_constant=constant)
    except ContractError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise error(code) from None


def is_hash(value: Any) -> bool:
    return type(value) is str and HASH_RE.fullmatch(value) is not None


def is_id(value: Any) -> bool:
    return type(value) is str and ID_RE.fullmatch(value) is not None


def is_int(value: Any, low: int = 0, high: int = (1 << 63) - 1) -> bool:
    return type(value) is int and low <= value <= high


def check_flags(record: dict, flags: dict[str, int], code: str = "flags_invalid",
                error: type[ContractError] = ContractError) -> None:
    need(all(type(record.get(key)) is int and record[key] == value for key, value in flags.items()), code, error)


def no_links(path: Path, code: str = "path_unsafe") -> None:
    """Refuse symlinks and Windows reparse points anywhere in the path."""
    for current in (path, *path.parents):
        info = current.lstat()
        need(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400, code)


def outside(path: Path, root: Path) -> bool:
    resolved, base = Path(path).resolve(), Path(root).resolve()
    return resolved != base and base not in resolved.parents


def read_bounded(path: Path | str, maximum: int) -> bytes:
    """Read a regular file without following links; fail if it changes while read."""
    path = Path(path).absolute()
    no_links(path)
    before = path.lstat()
    need(stat.S_ISREG(before.st_mode), "path_unsafe")
    need(before.st_size <= maximum, "limits_exceeded")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    need(len(raw) <= maximum, "limits_exceeded")
    need((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns), "file_changed")
    return raw


def write_atomic(path: Path | str, data: bytes, *, replace: bool = False) -> str:
    """Write via a temporary file; refuse to overwrite unless ``replace``. Returns the SHA-256."""
    path = Path(path).absolute()
    no_links(path.parent)
    need(replace or not os.path.lexists(path), "output_exists")
    descriptor, name = tempfile.mkstemp(prefix=".corpus-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()
    return sha256(data)


def write_json(path: Path | str, value: Any, *, replace: bool = False) -> str:
    return write_atomic(path, canonical(value) + b"\n", replace=replace)
