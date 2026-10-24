"""Create a source-bound strict projection of private attribution suggestions.

The corpus and input artifacts are read-only. Only rows rejected by the
existing validator with ``non_unique_evidence_span`` may be quarantined, and
only when explicitly requested. Diagnostics never include source content.
"""
from __future__ import annotations

from ltr.core import sha256 as _sha
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile

from ltr.attribution.index import SUGGESTIONS_SCHEMA, load_suggestions
from ltr.quotes.index import _stable_source
from ltr.library.review_queue import fingerprint

PROJECTION_SCHEMA = 'corpus-attribution-projection-v1'
QUARANTINE_CODE = 'non_unique_evidence_span'


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'attribution projection refused: invalid arguments\n')


def _reparse(path: Path) -> bool:
    """Detect symlinks and Windows reparse points without following them."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) &
                                     getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _assert_no_link_ancestors(path: Path, *, include_leaf=True) -> None:
    path = Path(os.path.abspath(path))
    parts = list(reversed((path, *path.parents)))
    if not include_leaf:
        parts = parts[:-1]
    for part in parts:
        if _reparse(part):
            raise ValueError('projection_link_path_rejected')


def _canonical(path: Path) -> Path:
    return Path(os.path.normcase(str(path.resolve(strict=False))))


def _overlap(left: Path, right: Path) -> bool:
    left, right = _canonical(left), _canonical(right)
    return left == right or left in right.parents or right in left.parents


def _stage_is_owned(stage: Path, parent: Path) -> bool:
    try:
        _assert_no_link_ancestors(stage)
        return stage.is_dir() and stage.resolve(strict=True).parent == parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return False


def _source(database: Path) -> tuple[Path, str]:
    _assert_no_link_ancestors(database)
    database = database.resolve(strict=True)
    if not database.is_file():
        raise ValueError('stable_source_required')
    if any(Path(str(database) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('projection_source_sidecar_present')
    _stable_source(database)
    return database, fingerprint(database)


def _load_one(con, header, row, source, scratch: Path, number: int) -> None:
    wrapper = dict(header)
    wrapper['rows'] = [row]
    path = scratch / f'suggestion-{number:08d}.json'
    try:
        path.write_text(json.dumps(wrapper, ensure_ascii=False), encoding='utf-8')
        load_suggestions(con, scratch, source)
    finally:
        path.unlink(missing_ok=True)


def _validate_artifact(con, data, source, scratch: Path, file_number: int,
                       quarantine: bool) -> list[int]:
    # The normal path delegates the complete artifact to the production loader.
    name = scratch / f'suggestion-{file_number:08d}.json'
    name.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    try:
        load_suggestions(con, scratch, source)
        return []
    except ValueError as exc:
        if str(exc) != QUARANTINE_CODE or not quarantine:
            raise
    finally:
        name.unlink(missing_ok=True)

    rows = data.get('rows')
    if not isinstance(rows, list):
        raise ValueError('attribution_source_malformed')
    header = {key: value for key, value in data.items() if key != 'rows'}
    removed = []
    for index, row in enumerate(rows):
        try:
            _load_one(con, header, row, source, scratch, file_number * 1000000 + index)
        except ValueError as exc:
            if str(exc) == QUARANTINE_CODE:
                removed.append(index)
            else:
                raise
    if not removed:
        # The original failure must be reproduced at row granularity; otherwise
        # it is not safe to classify as the narrowly allowed quarantine case.
        raise ValueError('projection_quarantine_classification_failed')
    return removed


def _collect(inputs: list[Path]) -> list[tuple[int, Path, Path]]:
    files = []
    seen = set()
    for input_number, root in enumerate(inputs):
        _assert_no_link_ancestors(root)
        if not root.is_dir():
            raise ValueError('projection_input_directory_required')
        root = root.resolve(strict=True)
        for path in sorted(root.rglob('suggestion-*.json')):
            _assert_no_link_ancestors(path)
            resolved = path.resolve(strict=True)
            if resolved in seen:
                continue
            if not resolved.is_file():
                raise ValueError('projection_input_file_required')
            seen.add(resolved)
            files.append((input_number, root, resolved))
    if not files:
        raise ValueError('projection_no_suggestions')
    return files


def project(database, inputs, out, *, quarantine_non_unique_evidence=False) -> dict:
    """Write a fresh source-bound directory; never alter the inputs or corpus."""
    database, source = _source(Path(database))
    inputs = [Path(item) for item in inputs]
    if not inputs:
        raise ValueError('projection_input_required')
    out = Path(os.path.abspath(out))
    _assert_no_link_ancestors(out, include_leaf=False)
    if os.path.lexists(out):
        raise ValueError('projection_output_exists')
    repo = Path(__file__).resolve().parents[1]
    if _overlap(out, repo) or _overlap(out, database.parent):
        raise ValueError('projection_output_scope_rejected')
    roots = [root.resolve(strict=True) for root in inputs]
    if any(_overlap(out, root) for root in roots):
        raise ValueError('projection_output_input_overlap')
    if not out.parent.is_dir():
        raise ValueError('projection_output_parent_missing')
    files = _collect(inputs)
    scratch_context = tempfile.TemporaryDirectory(prefix='attribution-projection-')
    scratch_root = Path(scratch_context.name)
    stage = None
    try:
        if (_overlap(scratch_root, repo) or _overlap(scratch_root, database.parent)
                or any(_overlap(scratch_root, root) for root in roots)):
            raise ValueError('projection_scratch_scope_rejected')
        # Keep validation scratch outside source, inputs, repository and output.
        scratch = scratch_root / 'validate'
        scratch.mkdir()
        stage = Path(tempfile.mkdtemp(prefix='.attribution-projection-', dir=out.parent))
        con = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        try:
            con.execute('PRAGMA query_only=ON')
            manifest_files = []
            total_rows = kept_rows = excluded_rows = 0
            for file_number, (input_number, root, path) in enumerate(files):
                raw = path.read_bytes()
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    raise ValueError('attribution_source_malformed') from None
                if not isinstance(data, dict) or data.get('schema') != SUGGESTIONS_SCHEMA:
                    raise ValueError('attribution_source_malformed')
                rows = data.get('rows')
                if not isinstance(rows, list):
                    raise ValueError('attribution_source_malformed')
                removed = _validate_artifact(con, data, source, scratch, file_number,
                                             quarantine_non_unique_evidence)
                output_data = data
                if removed:
                    remove_set = set(removed)
                    output_data = dict(data)
                    output_data['rows'] = [row for index, row in enumerate(rows)
                                           if index not in remove_set]
                    output_data['_attribution_projection'] = {
                        'schema': PROJECTION_SCHEMA,
                        'source_sha256': source,
                        'original_file_sha256': _sha(raw),
                        'excluded_rows': [{'row_number': index + 1,
                                           'row_sha256': _sha(json.dumps(
                                               rows[index], ensure_ascii=False,
                                               separators=(',', ':')).encode('utf-8')),
                                           'code': QUARANTINE_CODE}
                                          for index in removed],
                    }
                    output_raw = json.dumps(output_data, ensure_ascii=False,
                                            separators=(',', ':')).encode('utf-8') + b'\n'
                else:
                    output_raw = raw
                relative = Path(f'input-{input_number:03d}') / path.relative_to(root)
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(output_raw)
                row_count = len(rows)
                kept_count = row_count - len(removed)
                total_rows += row_count
                kept_rows += kept_count
                excluded_rows += len(removed)
                manifest_files.append({
                    'input_number': input_number,
                    'relative_path': path.relative_to(root).as_posix(),
                    'output_relative_path': relative.as_posix(),
                    'original_sha256': _sha(raw),
                    'output_sha256': _sha(output_raw),
                    'original_row_count': row_count,
                    'output_row_count': kept_count,
                    'excluded_rows': [{'row_number': index + 1,
                                       'row_sha256': _sha(json.dumps(
                                           rows[index], ensure_ascii=False,
                                           separators=(',', ':')).encode('utf-8')),
                                       'code': QUARANTINE_CODE}
                                      for index in removed],
                })

            # Inputs and source must still be exactly the bytes validated.
            try:
                current_files = _collect(inputs)
                frozen_files = [(input_number, root, path)
                                for input_number, root, path in files]
                if current_files != frozen_files:
                    raise ValueError('projection_input_changed')
                for record, (_, root, path) in zip(manifest_files, files):
                    _assert_no_link_ancestors(root)
                    _assert_no_link_ancestors(path)
                    if _sha(path.read_bytes()) != record['original_sha256']:
                        raise ValueError('projection_input_changed')
            except (OSError, ValueError, RuntimeError):
                raise ValueError('projection_input_changed') from None
            _stable_source(database)
            if fingerprint(database) != source:
                raise ValueError('projection_source_changed')
            if any(Path(str(database) + suffix).exists()
                   for suffix in ('-wal', '-shm', '-journal')):
                raise ValueError('projection_source_sidecar_present')
        finally:
            con.close()

        manifest = {
            'schema': PROJECTION_SCHEMA,
            'source_sha256': source,
            'files': manifest_files,
            'counts': {'files': len(manifest_files), 'original_rows': total_rows,
                       'output_rows': kept_rows, 'quarantined_rows': excluded_rows},
        }
        (stage / 'projection-manifest.json').write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n',
            encoding='utf-8')
        # Recheck that the destination was not created or redirected meanwhile.
        _assert_no_link_ancestors(out, include_leaf=False)
        if os.path.lexists(out):
            raise ValueError('projection_output_exists')
        if not _stage_is_owned(stage, out.parent):
            raise ValueError('projection_stage_scope_rejected')
        os.replace(stage, out)
        stage = None
        return {'source_sha256': source, **manifest['counts']}
    finally:
        if stage is not None and os.path.lexists(stage):
            if not _stage_is_owned(stage, out.parent):
                raise ValueError('projection_stage_cleanup_scope_rejected')
            shutil.rmtree(stage)
        scratch_context.cleanup()


def main(argv=None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--input', type=Path, action='append', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--quarantine-non-unique-evidence', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = project(args.db, args.input, args.out,
                         quarantine_non_unique_evidence=args.quarantine_non_unique_evidence)
    except (ValueError, OSError, sqlite3.Error, TypeError, KeyError):
        parser.exit(1, 'attribution projection refused: source, evidence or path validation failed\n')
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
