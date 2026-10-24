"""Read-only access to pending formal completion-alignment rows."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from http import HTTPStatus
from typing import Any, Mapping

from ltr.workbench import MAX_DIALOGUE_TEXT_CHARS, WorkbenchError, _clamp

STATUS_DECISIONS = {
    'unresolved_insufficient_evidence': 'insufficient_evidence',
    'unresolved_low_support_match': 'matched_candidate',
    'unresolved_not_corresponding': 'not_corresponding',
    'unresolved_partial_candidate': 'partial_candidate',
}
STATUS_COUNTS = (*STATUS_DECISIONS, 'other')
ALIGNMENT_COLUMNS = {
    'quote_id', 'run_id', 'task_key', 'target_edition_id', 'source_edition_id',
    'status', 'decision', 'source_ids_json', 'confidence_uncalibrated', 'job_id',
    'binding_sha256', 'input_sha256', 'contract_sha256', 'evidence_ids_json',
    'requires_review', 'accepted', 'human_confirmed', 'semantic_verified',
    'reference_eligible',
}
DIALOGUE_COLUMNS = {'id', 'edition_id', 'ordinal', 'text'}
EDITION_COLUMNS = {'id', 'title', 'language', 'book_key', 'origin_kind'}
_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_DECISION_FLAGS = ('accepted', 'human_confirmed', 'semantic_verified', 'reference_eligible')


def _mismatch() -> WorkbenchError:
    return WorkbenchError(HTTPStatus.CONFLICT, 'completion_alignment_source_mismatch')


def _columns_available(db) -> bool:
    return (db.has('corpus_completion_alignment')
            and ALIGNMENT_COLUMNS <= db.columns('corpus_completion_alignment')
            and db.has('dialogues') and DIALOGUE_COLUMNS <= db.columns('dialogues')
            and db.has('editions') and EDITION_COLUMNS <= db.columns('editions'))


def _source_ids(value: Any) -> list[str] | None:
    try:
        parsed = json.loads(value) if isinstance(value, (str, bytes, bytearray)) else None
    except (ValueError, UnicodeDecodeError):
        return None
    if (not isinstance(parsed, list) or len(parsed) > 20
            or any(not isinstance(item, str) or not 1 <= len(item) <= 200 for item in parsed)
            or len(set(parsed)) != len(parsed)):
        return None
    return parsed


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _confidence(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None


def _count_dialogues(db, edition_id: str, ids: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    unique_ids = sorted(set(ids))
    for offset in range(0, len(unique_ids), 500):
        chunk = unique_ids[offset:offset + 500]
        if not chunk:
            continue
        placeholders = ','.join('?' for _ in chunk)
        rows = db.query(
            f'SELECT id,COUNT(*) AS n FROM dialogues WHERE edition_id=? '
            f'AND id IN ({placeholders}) GROUP BY id', (edition_id, *chunk))
        counts.update({str(row['id']): int(row['n']) for row in rows})
    return counts


def _edition_records(db, ids: list[str]) -> dict[str, list[Any]]:
    found: dict[str, list[Any]] = defaultdict(list)
    unique_ids = sorted(set(ids))
    for offset in range(0, len(unique_ids), 500):
        chunk = unique_ids[offset:offset + 500]
        if not chunk:
            continue
        placeholders = ','.join('?' for _ in chunk)
        rows = db.query(
            f'SELECT id,title,language,book_key,origin_kind FROM editions '
            f'WHERE id IN ({placeholders})', chunk)
        for row in rows:
            found[str(row['id'])].append(row)
    return found


def _row_is_mapped(row, edition_records, dialogue_counts) -> bool:
    target_id, source_id = row['target_edition_id'], row['source_edition_id']
    quote_id = row['quote_id']
    ids = _source_ids(row['source_ids_json'])
    if (not all(isinstance(value, str) and value for value in
                (target_id, source_id, quote_id)) or ids is None):
        return False
    target_editions = edition_records.get(target_id, [])
    source_editions = edition_records.get(source_id, [])
    if (len(target_editions) != 1 or target_editions[0]['language'] != 'zh'
            or len(source_editions) != 1 or source_editions[0]['language'] != 'ja'):
        return False
    if dialogue_counts.get((target_id, quote_id), 0) != 1:
        return False
    return all(dialogue_counts.get((source_id, source_quote_id), 0) == 1
               for source_quote_id in ids)


def validate_pending_filter(params: Mapping[str, Any]) -> bool:
    """Parse the optional query-string flag without accepting bool/int inputs."""
    if not isinstance(params, Mapping):
        raise WorkbenchError(HTTPStatus.BAD_REQUEST, 'invalid_pending_alignment_filter')
    value = params.get('pending_alignment')
    if value is None or value == '':
        return False
    if type(value) is not str or value not in ('0', '1'):
        raise WorkbenchError(HTTPStatus.BAD_REQUEST, 'invalid_pending_alignment_filter')
    return value == '1'


class CompletionAlignmentView:
    """A source-bound, read-only view of pending Japanese completion matches."""

    def __init__(self, store, corpus_id: str) -> None:
        self.store = store
        self.corpus_id = corpus_id

    @staticmethod
    def validate_pending_filter(params: Mapping[str, Any]) -> bool:
        return validate_pending_filter(params)

    def available(self) -> bool:
        try:
            _, db = self.store.database(self.corpus_id)
            if not _columns_available(db):
                return False
            baseline = db.source_sha256(refresh=True)
            return db.source_sha256(refresh=True) == baseline
        except (WorkbenchError, sqlite3.Error, OSError, RuntimeError, ValueError, TypeError):
            return False

    @staticmethod
    def _unavailable() -> dict[str, Any]:
        return {'available': False, 'pending_rows': 0, 'mapped_pending_rows': 0,
                'unmapped_pending_rows': 0, 'statuses': dict.fromkeys(STATUS_COUNTS, 0)}

    def overview(self) -> dict[str, Any]:
        try:
            _, db = self.store.database(self.corpus_id)
            if not _columns_available(db):
                return self._unavailable()
            baseline = db.source_sha256(refresh=True)
            rows = db.query(
                'SELECT quote_id,target_edition_id,source_edition_id,status,source_ids_json '
                'FROM corpus_completion_alignment WHERE requires_review=1')
            statuses = Counter()
            edition_ids: list[str] = []
            dialogue_pairs: list[tuple[str, str]] = []
            for row in rows:
                status = row['status']
                statuses[status if status in STATUS_DECISIONS else 'other'] += 1
                target_id, source_id, quote_id = (row['target_edition_id'],
                                                  row['source_edition_id'], row['quote_id'])
                if isinstance(target_id, str):
                    edition_ids.append(target_id)
                if isinstance(source_id, str):
                    edition_ids.append(source_id)
                if isinstance(target_id, str) and isinstance(quote_id, str):
                    dialogue_pairs.append((target_id, quote_id))
                ids = _source_ids(row['source_ids_json'])
                if isinstance(source_id, str) and ids is not None:
                    dialogue_pairs.extend((source_id, item) for item in ids)
            edition_records = _edition_records(db, edition_ids)
            id_set = sorted({item for _, item in dialogue_pairs})
            dialogue_counts: dict[tuple[str, str], int] = {}
            # Group in bounded chunks by ID; only identifiers and row counts are read.
            for offset in range(0, len(id_set), 500):
                chunk = id_set[offset:offset + 500]
                if not chunk:
                    continue
                placeholders = ','.join('?' for _ in chunk)
                matches = db.query(
                    f'SELECT edition_id,id,COUNT(*) AS n FROM dialogues '
                    f'WHERE id IN ({placeholders}) GROUP BY edition_id,id', chunk)
                dialogue_counts.update({(str(item['edition_id']), str(item['id'])): int(item['n'])
                                        for item in matches})
            mapped = sum(_row_is_mapped(row, edition_records, dialogue_counts) for row in rows)
            if db.source_sha256(refresh=True) != baseline:
                return self._unavailable()
            return {'available': True, 'pending_rows': len(rows),
                    'mapped_pending_rows': mapped, 'unmapped_pending_rows': len(rows) - mapped,
                    'statuses': {status: statuses.get(status, 0) for status in STATUS_COUNTS}}
        except WorkbenchError:
            return self._unavailable()
        except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError):
            return self._unavailable()

    def detail(self, dialogue_id: str, edition_id: str) -> dict[str, Any] | None:
        if (not isinstance(dialogue_id, str) or not dialogue_id
                or not isinstance(edition_id, str) or not edition_id):
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, 'invalid_parameter')
        try:
            _, db = self.store.database(self.corpus_id)
            baseline = db.source_sha256(refresh=True)
            if not _columns_available(db):
                raise _mismatch()
            rows = db.query(
                'SELECT quote_id,target_edition_id,source_edition_id,status,decision,source_ids_json,'
                'confidence_uncalibrated,binding_sha256,input_sha256,contract_sha256,requires_review,'
                'accepted,human_confirmed,semantic_verified,reference_eligible '
                'FROM corpus_completion_alignment WHERE quote_id=? AND requires_review=1',
                (dialogue_id,))
            if not rows:
                result = None
            else:
                if len(rows) != 1:
                    raise _mismatch()
                row = rows[0]
                if row['quote_id'] != dialogue_id or row['target_edition_id'] != edition_id:
                    raise _mismatch()
                if (type(row['requires_review']) is not int or row['requires_review'] != 1
                        or any(type(row[name]) is not int or row[name] != 0 for name in _DECISION_FLAGS)
                        or not all(_valid_digest(row[name]) for name in
                                   ('binding_sha256', 'input_sha256', 'contract_sha256'))):
                    raise _mismatch()
                source_ids = _source_ids(row['source_ids_json'])
                if source_ids is None:
                    raise _mismatch()
                target_editions = _edition_records(db, [edition_id]).get(edition_id, [])
                source_edition_id = row['source_edition_id']
                if not isinstance(source_edition_id, str) or not source_edition_id:
                    raise _mismatch()
                source_editions = _edition_records(db, [source_edition_id]).get(source_edition_id, [])
                if (len(target_editions) != 1 or target_editions[0]['language'] != 'zh'
                        or len(source_editions) != 1 or source_editions[0]['language'] != 'ja'):
                    raise _mismatch()
                target_rows = db.query(
                    'SELECT id,edition_id,ordinal FROM dialogues WHERE id=? AND edition_id=?',
                    (dialogue_id, edition_id))
                if len(target_rows) != 1:
                    raise _mismatch()
                source_quotes = []
                if source_ids:
                    placeholders = ','.join('?' for _ in source_ids)
                    quote_rows = db.query(
                        f'SELECT id,edition_id,ordinal,text FROM dialogues '
                        f'WHERE edition_id=? AND id IN ({placeholders})',
                        (source_edition_id, *source_ids))
                    by_id: dict[str, list[Any]] = defaultdict(list)
                    for quote in quote_rows:
                        by_id[str(quote['id'])].append(quote)
                    if any(len(by_id.get(source_id, [])) != 1 for source_id in source_ids):
                        raise _mismatch()
                    for source_id in source_ids:
                        quote = by_id[source_id][0]
                        text_raw = quote['text'] or ''
                        text = _clamp(text_raw, MAX_DIALOGUE_TEXT_CHARS)
                        if isinstance(text_raw, bytes):
                            text_bytes = bytes(text_raw)
                            text_total_chars = len(text_raw.decode('utf-8'))
                        else:
                            text_value = str(text_raw)
                            text_bytes = text_value.encode('utf-8')
                            text_total_chars = len(text_value)
                        source_quotes.append({
                            'id': _clamp(quote['id'], 200),
                            'edition_id': _clamp(quote['edition_id'], 200),
                            'ordinal': quote['ordinal'] if type(quote['ordinal']) is int else None,
                            'text': text,
                            'text_truncated': text_total_chars > MAX_DIALOGUE_TEXT_CHARS,
                            'text_total_chars': text_total_chars,
                            'text_sha256': hashlib.sha256(text_bytes).hexdigest(),
                        })
                status_value = row['status']
                if status_value in STATUS_DECISIONS:
                    decision = STATUS_DECISIONS[status_value]
                    if row['decision'] != decision:
                        raise _mismatch()
                    status = status_value
                else:
                    status, decision = 'other', 'unknown'
                target_edition, source_edition = target_editions[0], source_editions[0]
                target_book, source_book = target_edition['book_key'], source_edition['book_key']
                result = {
                    'status': status, 'decision': decision,
                    'source_edition': {
                        'id': _clamp(source_edition['id'], 200),
                        'title': _clamp(source_edition['title'], 200),
                        'language': _clamp(source_edition['language'], 40),
                        'origin_kind': _clamp(source_edition['origin_kind'], 80),
                    },
                    'same_book_key': bool(target_book) and target_book == source_book,
                    'source_quotes': source_quotes,
                    'confidence_uncalibrated': _confidence(row['confidence_uncalibrated']),
                    'binding_sha256': row['binding_sha256'],
                    'input_sha256': row['input_sha256'],
                    'contract_sha256': row['contract_sha256'],
                    'requires_review': True, 'accepted': False,
                    'human_confirmed': False, 'semantic_verified': False,
                    'reference_eligible': False,
                    'scope': 'stored_completion_candidate',
                    'semantic_identity_verified': False,
                    'historical_contract_revalidated': False,
                }
        except WorkbenchError:
            raise
        except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError, KeyError):
            raise _mismatch() from None
        try:
            if db.source_sha256(refresh=True) != baseline:
                raise _mismatch()
        except (WorkbenchError, sqlite3.Error, OSError, RuntimeError, ValueError, TypeError):
            raise _mismatch() from None
        return result
