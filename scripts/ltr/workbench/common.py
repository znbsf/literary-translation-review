"""Constants, errors and input sanitising shared by the workbench."""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

if TYPE_CHECKING:
    from .config import CorpusConfig

SCHEMA_ID = "corpus-workbench-v1"
REVIEW_EXPORT_SCHEMA = "corpus-workbench-review-export-v1"

DB_FILENAME = "dialogue.sqlite"
SCAN_SUBDIR = Path("private") / "scans"

STATIC_ROOT = Path(__file__).resolve().parent / "web"
# Front-end assets: the page itself plus flat style/script files (js/ holds the ES modules).
STATIC_PATH_RE = re.compile(r"^/(?:js/)?[a-z0-9-]+\.(?:html|css|js)$")


def static_file(path: str) -> str | None:
    """Relative asset path for a request path, or None when it is not a front-end asset."""
    if path in ("/", "/index.html"):
        return "index.html"
    return path[1:] if STATIC_PATH_RE.fullmatch(path) else None

DEFAULT_LIMIT = 30
MAX_LIMIT = 100
MAX_OFFSET = 1_000_000
MAX_QUERY_CHARS = 120
MAX_FILTER_CHARS = 80
MAX_ID_CHARS = 200
MAX_BODY_BYTES = 32 * 1024
MAX_NOTE_CHARS = 500
MAX_SPEAKER_CHARS = 60
MAX_CONTEXT_PARAGRAPH_CHARS = 400
MAX_DIALOGUE_TEXT_CHARS = 4000
CONTEXT_WINDOW = 2

ALLOWED_VERDICTS = ("correct", "incorrect", "uncertain")
ALLOWED_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_CORPUS_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class WorkbenchError(Exception):
    """Sanitised request or configuration error."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _clean_text(value: Any) -> str:
    """Coerce to a compact single-line string without control characters."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive
            return ""
    text = str(value)
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL_RE.sub(" ", text)
    return text.strip()


def _clamp(value: Any, limit: int) -> str:
    text = _clean_text(value)
    if len(text) > limit:
        return text[:limit]
    return text


def _json_loads(value: Any) -> Any:
    """Parse optional JSON evidence; every malformed shape becomes ``None``."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            value = bytes(value).decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError, RecursionError):
        return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def _id_list(value: Any) -> list[str]:
    """Extract a bounded list of dialogue IDs from ``japanese_ids_json``."""
    parsed = _json_loads(value)
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    for item in parsed:
        if isinstance(item, (str, int)) and not isinstance(item, bool):
            text = _clean_text(item)
            if text and _ID_RE.match(text) and text not in out:
                out.append(text)
        if len(out) >= 100:
            break
    return out


def _validate_id(value: Any, *, corpus: bool = False) -> str:
    text = _clean_text(value)
    pattern = _CORPUS_ID_RE if corpus else _ID_RE
    if not text or len(text) > MAX_ID_CHARS or not pattern.match(text):
        raise WorkbenchError(HTTPStatus.BAD_REQUEST, "invalid_parameter")
    return text


def _validate_query_text(value: Any, *, limit: int) -> str:
    text = _clean_text(value)
    if len(text) > limit:
        raise WorkbenchError(HTTPStatus.BAD_REQUEST, "invalid_parameter")
    return text


def _bounded_int(value: Any, default: int, maximum: int, *, name: str = "") -> int:
    """Parse an integer parameter, clamping it into ``[0, maximum]``.

    Non-numeric or negative values are sanitised into a ``400`` error; values
    above the maximum are clamped so a caller can never request more than the
    documented cap.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise WorkbenchError(HTTPStatus.BAD_REQUEST, "invalid_parameter") from None
    if number < 0:
        raise WorkbenchError(HTTPStatus.BAD_REQUEST, "invalid_parameter")
    return min(number, maximum)


def _like_pattern(text: str) -> str:
    """Turn user text into a safe ``LIKE`` contains-pattern (``ESCAPE '\\'``)."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


def _contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_relative_image_path(raw: Any) -> Path:
    """Validate a database-recorded image path as a *relative* local path."""
    if not isinstance(raw, str):
        raise WorkbenchError(HTTPStatus.NOT_FOUND, "scan_unavailable")
    text = _clean_text(raw).replace("\\", "/")
    if not text or len(text) > 400:
        raise WorkbenchError(HTTPStatus.NOT_FOUND, "scan_unavailable")
    if text.startswith("//") or text.startswith("/"):
        raise WorkbenchError(HTTPStatus.NOT_FOUND, "scan_unavailable")
    if re.match(r"^[A-Za-z]:", text):
        raise WorkbenchError(HTTPStatus.NOT_FOUND, "scan_unavailable")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise WorkbenchError(HTTPStatus.NOT_FOUND, "scan_unavailable")
    if any(len(part) > 128 for part in parts):
        raise WorkbenchError(HTTPStatus.NOT_FOUND, "scan_unavailable")
    suffix = Path(parts[-1]).suffix.lower()
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        raise WorkbenchError(HTTPStatus.NOT_FOUND, "scan_unavailable")
    return Path(*parts)


def _resolved_scan_path(corpus: CorpusConfig, raw: Any) -> Path:
    if not isinstance(raw, str) or raw.replace("\\", "/").startswith("//"):
        raise WorkbenchError(HTTPStatus.NOT_FOUND, "scan_unavailable")
    root = corpus.scan_root.resolve(strict=True)
    if not _contained(root, corpus.root.resolve(strict=True)):
        raise WorkbenchError(HTTPStatus.NOT_FOUND, "scan_unavailable")
    path = Path(raw)
    if not path.is_absolute():
        relative = _safe_relative_image_path(raw)
        path = corpus.root / relative if relative.parts[:2] == ("private", "scans") else root / relative
    target = path.resolve(strict=True)
    if not _contained(target, root) or target.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
        raise WorkbenchError(HTTPStatus.NOT_FOUND, "scan_unavailable")
    return target


def _image_content_type(path: Path) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(path.suffix.lower(), "application/octet-stream")
