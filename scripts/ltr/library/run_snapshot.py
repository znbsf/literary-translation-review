"""Bounded read-only worker-record snapshots; neither liveness nor publication proof.

The caller registers an external file and supplies the current source SHA. No
worker directories, models, accounts, history or candidate prose are inspected.
Scope identity is opaque: this reader cannot prove cross-worker disjointness.
"""
from __future__ import annotations

from ltr.core import stat_identity as _identity
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat

SCHEMA = 'corpus-workbench-run-snapshot-v1'
MAX_BYTES = 256 * 1024
MAX_WORKERS = 32
MAX_ROWS = 10_000_000
MAX_CALLS = 1_000_000
MAX_CACHE_TOKENS = 1_000_000_000_000
STOP_REASONS = frozenset({
    'running', 'range_or_budget_complete', 'finite_scope_complete', 'planned_pages_complete',
    'global_budget_exhausted', 'call_budget_exhausted', 'request_byte_budget_exhausted',
    'request_rejected', 'request_timeout', 'unsupported_response_content', 'consecutive_invalid_responses',
    'source_changed', 'inputs_changed', 'image_changed', 'plan_changed', 'page_binding_changed',
    'image_dimensions_mismatch', 'invalid_model_json', 'incomplete_model_response',
    'invalid_candidate_contract', 'request_or_response_failed', 'request_or_publication_failed',
    'interrupted', 'first_failure_stop', 'legacy_unspecified_stop',
    'domain_failure_budget_exhausted', 'failure_row_budget_exhausted', 'global_call_budget_exhausted',
    'unclassified_or_incomplete_failure', 'controller_error', 'source_database_changed',
    'qualification_not_passed',
})
KINDS = frozenset({'identity', 'alignment', 'ocr'})
ROW_FIELDS = ('valid_rows', 'failed_rows', 'outcome_unknown_rows', 'unattempted_rows')
COUNTS = ('planned_rows', *ROW_FIELDS, 'attempted_calls', 'cache_read_input_tokens')
_TOP = {'schema', 'source_sha256', 'scope_sha256', 'generated_at', 'workers'}
_WORKER = {'worker_id', 'kind', 'terminal', 'stop_reason', *COUNTS}
_HASH = re.compile(r'[0-9a-f]{64}')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
_TIME = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)')
_CODES = frozenset({'run_snapshot_invalid', 'run_snapshot_limits_exceeded',
    'run_snapshot_source_mismatch', 'run_snapshot_unavailable', 'run_snapshot_path_unsafe',
    'run_snapshot_changed', 'run_snapshot_expected_source_invalid'})


class RunSnapshotError(ValueError):
    def __init__(self, code):
        self.code = code if code in _CODES else 'run_snapshot_invalid'
        super().__init__(self.code)


def _require(value, code='run_snapshot_invalid'):
    if not value:
        raise RunSnapshotError(code)


def _ancestors(path):
    identities = []
    for current in (path, *path.parents):
        info = current.lstat()
        _require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
                 'run_snapshot_path_unsafe')
        # Directory mtimes change for unrelated siblings; bind their identities only.
        identities.append((info.st_dev, info.st_ino, info.st_mode))
    return identities


def _bytes(path):
    ancestors = _ancestors(path)
    before = path.lstat()
    _require(stat.S_ISREG(before.st_mode), 'run_snapshot_path_unsafe')
    _require(before.st_size <= MAX_BYTES, 'run_snapshot_limits_exceeded')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        _require(_identity(before) == _identity(os.fstat(stream.fileno())), 'run_snapshot_changed')
        raw = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
    _require(len(raw) <= MAX_BYTES, 'run_snapshot_limits_exceeded')
    _require(_ancestors(path) == ancestors and _identity(before) == _identity(after) == _identity(path.lstat()),
             'run_snapshot_changed')
    return raw


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result)
            result[key] = value
        return result
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique)


def _validate(data, source):
    _require(type(data) is dict and set(data) == _TOP and data.get('schema') == SCHEMA)
    for key in ('source_sha256', 'scope_sha256'):
        _require(type(data[key]) is str and _HASH.fullmatch(data[key]))
    _require(data['source_sha256'] == source, 'run_snapshot_source_mismatch')
    stamp = data['generated_at']
    _require(type(stamp) is str and _TIME.fullmatch(stamp))
    parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    _require(parsed.utcoffset() == timedelta(0))
    workers = data['workers']
    _require(type(workers) is list and len(workers) <= MAX_WORKERS)
    seen = set()
    projected = []
    for worker in workers:
        _require(type(worker) is dict and set(worker) == _WORKER)
        identity = worker['worker_id']
        _require(type(identity) is str and _ID.fullmatch(identity) and identity not in seen)
        seen.add(identity)
        _require(type(worker['kind']) is str and worker['kind'] in KINDS
                 and type(worker['terminal']) is bool and type(worker['stop_reason']) is str
                 and worker['stop_reason'] in STOP_REASONS
                 and worker['terminal'] == (worker['stop_reason'] != 'running'))
        for key in COUNTS:
            maximum = MAX_CALLS if key == 'attempted_calls' else MAX_CACHE_TOKENS if key == 'cache_read_input_tokens' else MAX_ROWS
            _require(type(worker[key]) is int and 0 <= worker[key] <= maximum)
        _require(sum(worker[key] for key in ROW_FIELDS) == worker['planned_rows'])
        projected.append({key: worker[key] for key in ('worker_id', 'kind', 'terminal', 'stop_reason', *COUNTS)})
    return {**{key: data[key] for key in ('schema', 'source_sha256', 'scope_sha256', 'generated_at')},
            'workers': projected}


def read_run_snapshot(path, expected_source_sha256):
    """Return a safe projection or raise a static ``RunSnapshotError``.

    ``None`` means unconfigured. A terminal worker can still have failed,
    unknown or unattempted rows. No inferred publication or liveness is returned.
    The caller must keep this registered path outside its formal source tree.
    """
    try:
        _require(type(expected_source_sha256) is str and _HASH.fullmatch(expected_source_sha256),
                 'run_snapshot_expected_source_invalid')
        if path is None:
            return {'available': False}
        lexical = Path(path).absolute()  # Do not resolve away a caller's symlink.
        raw = _bytes(lexical)
        result = _validate(_json(raw), expected_source_sha256)
        # Catch replacement during validation as well as during the read itself.
        _require(_bytes(lexical) == raw, 'run_snapshot_changed')
        return {'available': True, **result, 'snapshot_sha256': hashlib.sha256(raw).hexdigest()}
    except RunSnapshotError:
        raise
    except OSError:
        raise RunSnapshotError('run_snapshot_unavailable') from None
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise RunSnapshotError('run_snapshot_invalid') from None
