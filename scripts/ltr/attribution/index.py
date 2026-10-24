"""Private, source-bound index of corpus attribution suggestions.

The source corpus is only ever opened read-only. The index keeps dialogue text
and bounded evidence locators (never paragraph or context prose) plus each
distinct model observation; human labels stay separate and unaccepted. Error
diagnostics are source-free codes only.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from ltr.attribution.review import KINDS, load_targets, load_vocabulary, validate_suggestions
from ltr.quotes.batch import digest
from ltr.quotes.index import _stable_source
from ltr.library.review_queue import fingerprint

SCHEMA = 'corpus-attribution-index-v1'
SUGGESTIONS_SCHEMA = 'corpus-attribution-suggestions-v1'
FLAGS = {'requires_review': 1, 'accepted': 0, 'human_confirmed': 0,
         'semantic_verified': 0, 'reference_eligible': 0}
STATUSES = ('suggested', 'unresolved', 'conflict', 'not_dialogue')
EVIDENCE_KEYS = {'edition_id', 'paragraph_id', 'ordinal', 'start', 'end', 'paragraph_sha256'}
ROW_KEYS = {'suggestion_id', 'dialogue_id', 'edition_id', 'ordinal', 'context_sha256',
            'context_window', 'dialogue_text_sha256', 'source_candidate_sha256', 'speaker',
            'addressee', 'kind', 'basis', 'evidence', *FLAGS}
OBSERVATION_KEYS = ('model', 'served_model', 'speaker', 'addressee', 'kind', 'basis',
                    'evidence', 'artifact_sha256')

_TABLES = """
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE records(id TEXT PRIMARY KEY,edition_id TEXT NOT NULL,dialogue_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,context_sha256 TEXT NOT NULL,context_window TEXT NOT NULL,
    dialogue_text_sha256 TEXT NOT NULL,source_candidate_sha256 TEXT NOT NULL,
    selection_kind TEXT NOT NULL,kind TEXT NOT NULL,text TEXT NOT NULL,speaker TEXT,
    addressee TEXT,status TEXT NOT NULL,observation_count INTEGER NOT NULL,with_speaker INTEGER NOT NULL,
    observations_json TEXT NOT NULL,payload_sha256 TEXT NOT NULL,
    requires_review INTEGER NOT NULL CHECK(requires_review=1),
    accepted INTEGER NOT NULL CHECK(accepted=0),
    human_confirmed INTEGER NOT NULL CHECK(human_confirmed=0),
    semantic_verified INTEGER NOT NULL CHECK(semantic_verified=0),
    reference_eligible INTEGER NOT NULL CHECK(reference_eligible=0));
CREATE INDEX records_edition ON records(edition_id,ordinal);
CREATE TABLE names(id TEXT NOT NULL,name TEXT NOT NULL);
CREATE INDEX names_name ON names(name);
"""


def identity(source, kind, edition, dialogue_id, context_sha256):
    """Stable suggestion identity for one source target under one selection kind."""
    return digest({'source': source, 'kind': kind, 'edition': edition,
                   'dialogue_id': dialogue_id, 'context_sha256': context_sha256})[:16]


def _single_target(con, kind, edition, dialogue_id):
    targets = load_targets(con, kind, edition, dialogue_id)
    if len(targets) != 1:
        raise ValueError('attribution_target_unresolved')
    return targets[0]


def _revalidate(target, vocabulary, observation):
    """Rebuild evidence spans in RAM and re-run the suggestion validator."""
    evidence = observation.get('evidence')
    if not isinstance(evidence, list):
        raise ValueError('attribution_evidence_invalid')
    context = {(p['id'], p['edition_id']): p for p in target['context']}
    rebuilt = []
    for locator in evidence:
        if not isinstance(locator, dict) or set(locator) != EVIDENCE_KEYS:
            raise ValueError('attribution_evidence_invalid')
        paragraph = context.get((locator['paragraph_id'], locator['edition_id']))
        if paragraph is None or paragraph['ordinal'] != locator['ordinal']:
            raise ValueError('attribution_evidence_unbound')
        text = paragraph['text']
        if hashlib.sha256(text.encode('utf-8')).hexdigest() != locator['paragraph_sha256']:
            raise ValueError('attribution_evidence_mismatch')
        start, end = locator['start'], locator['end']
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            raise ValueError('attribution_evidence_bounds')
        rebuilt.append({'paragraph_id': locator['paragraph_id'], 'text': text[start:end]})
    model_row = {'dialogue_id': target['dialogue_id'], 'speaker': observation.get('speaker'),
                 'addressee': observation.get('addressee'), 'kind': observation.get('kind'),
                 'basis': observation.get('basis'), 'evidence': rebuilt}
    validated = validate_suggestions({'suggestions': [model_row]}, [target], vocabulary)[0]
    if (validated['speaker'] != observation.get('speaker')
            or validated['addressee'] != observation.get('addressee')
            or validated['kind'] != observation.get('kind')
            or validated['basis'] != observation.get('basis')
            or validated['evidence'] != evidence):
        raise ValueError('attribution_binding_mismatch')
    return validated


def load_suggestions(con, directory, source):
    """Validate every suggestion file against the source and group observations."""
    records, observations, seen = {}, defaultdict(list), set()
    vocabularies = {}
    directories=[directory] if isinstance(directory,(str,Path)) else list(directory)
    files={path.resolve() for root in directories for path in Path(root).rglob('suggestion-*.json')}
    for file in sorted(files):
        raw = file.read_bytes()
        try:
            data = json.loads(raw)
        except ValueError:
            raise ValueError('attribution_source_malformed') from None
        if (not isinstance(data, dict) or data.get('schema') != SUGGESTIONS_SCHEMA
                or data.get('database_sha256') != source):
            raise ValueError('attribution_source_mismatch')
        kind, edition = data.get('kind'), data.get('edition_id')
        if kind not in KINDS or not isinstance(edition, str) or not edition:
            raise ValueError('attribution_source_malformed')
        if not isinstance(data.get('model'),str) or not data['model'].strip():
            raise ValueError('attribution_model_missing')
        if edition not in vocabularies:
            vocabularies[edition] = load_vocabulary(con, edition)
        vocabulary = vocabularies[edition]
        artifact = hashlib.sha256(raw).hexdigest()
        rows = data.get('rows')
        if not isinstance(rows, list):
            raise ValueError('attribution_source_malformed')
        for row in rows:
            if not isinstance(row, dict) or set(row) != ROW_KEYS:
                raise ValueError('attribution_source_malformed')
            if any(type(row[key]) is not int or row[key] != value for key, value in FLAGS.items()):
                raise ValueError('attribution_flags_invalid')
            if row['edition_id'] != edition:
                raise ValueError('attribution_edition_mismatch')
            target = _single_target(con, kind, edition, row['dialogue_id'])
            if (row['ordinal'] != target['ordinal'] or row['context_sha256'] != target['context_sha256']
                    or row['context_window'] != target['context_window']
                    or row['dialogue_text_sha256'] != target['dialogue_text_sha256']
                    or row['source_candidate_sha256'] != target['source_candidate_sha256']):
                raise ValueError('attribution_binding_mismatch')
            identifier = identity(source, kind, edition, row['dialogue_id'], target['context_sha256'])
            if row['suggestion_id'] != identifier:
                raise ValueError('attribution_identity_mismatch')
            validated = _revalidate(target, vocabulary, row)
            records[identifier] = {
                'id': identifier, 'edition_id': edition, 'dialogue_id': target['dialogue_id'],
                'ordinal': target['ordinal'], 'context_sha256': target['context_sha256'],
                'context_window': target['context_window'],
                'dialogue_text_sha256': target['dialogue_text_sha256'],
                'source_candidate_sha256': target['source_candidate_sha256'],
                'selection_kind': kind, 'text': target['text']}
            observation = {'model': data.get('model'), 'served_model': data.get('served_model'),
                           'speaker': validated['speaker'], 'addressee': validated['addressee'],
                           'kind': validated['kind'], 'basis': validated['basis'],
                           'evidence': validated['evidence'], 'artifact_sha256': artifact}
            marker = (identifier, digest(observation))
            if marker not in seen:
                seen.add(marker)
                observations[identifier].append(observation)
    return records, observations


def validate_record(con, kind, edition, record, source, vocabulary=None):
    """Re-resolve one stored record against the source and re-check all bindings."""
    if any(type(record[key]) is not int or record[key] != value for key,value in FLAGS.items()):
        raise ValueError('attribution_flags_invalid')
    if record['edition_id'] != edition:
        raise ValueError('attribution_edition_mismatch')
    target = _single_target(con, kind, edition, record['dialogue_id'])
    if (record['ordinal'] != target['ordinal'] or record['context_sha256'] != target['context_sha256']
            or record['context_window'] != target['context_window']
            or record['dialogue_text_sha256'] != target['dialogue_text_sha256']
            or record['source_candidate_sha256'] != target['source_candidate_sha256']
            or record['text'] != target['text']):
        raise ValueError('attribution_binding_mismatch')
    identifier = identity(source, kind, edition, record['dialogue_id'], target['context_sha256'])
    if record['id'] != identifier:
        raise ValueError('attribution_identity_mismatch')
    if vocabulary is None:
        vocabulary = load_vocabulary(con, edition)
    try:
        observations = json.loads(record['observations_json'])
    except (ValueError, TypeError):
        raise ValueError('attribution_source_malformed') from None
    if not isinstance(observations, list) or not observations:
        raise ValueError('attribution_source_malformed')
    for observation in observations:
        _revalidate(target, vocabulary, observation)
    status,speaker,addressee,aggregate_kind=_aggregate(observations)
    if (record['status']!=status or record['speaker']!=speaker or record['addressee']!=addressee
            or record['kind']!=aggregate_kind or record['payload_sha256']!=_payload(record,observations)
            or record['observation_count']!=len(observations)
            or record['with_speaker']!=int(any(o['speaker'] is not None for o in observations))):
        raise ValueError('attribution_consensus_mismatch')
    return {'target': target, 'observations': observations, 'identity': identifier}


def _aggregate(observations):
    kinds = {o['kind'] for o in observations}
    speakers = {o['speaker'] for o in observations}
    addressees = {o['addressee'] for o in observations}
    if len(kinds) > 1 or len(speakers) > 1 or len(addressees) > 1:
        return 'conflict', '', '', ''
    if kinds == {'not_dialogue'}:
        return 'not_dialogue', '', '', 'not_dialogue'
    if not any(speaker is not None for speaker in speakers) \
            and not any(addressee is not None for addressee in addressees):
        return 'unresolved', '', '', next(iter(kinds))
    return ('suggested', next(iter(speakers)) or '', next(iter(addressees)) or '',
            next(iter(kinds)))


def _payload(record, observations):
    base = {key: record[key] for key in ('id', 'edition_id', 'dialogue_id', 'ordinal',
            'context_sha256', 'context_window', 'dialogue_text_sha256',
            'source_candidate_sha256', 'selection_kind')}
    base['observations'] = [{key: o[key] for key in OBSERVATION_KEYS} for o in observations]
    return digest(base)


@contextmanager
def read_index(path, source):
    """Read-only, query-only access to an index bound to ``source``."""
    path = Path(path)
    if not path.is_file():
        raise ValueError('attribution_index_missing')
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute('PRAGMA query_only=ON')
        metadata = dict(con.execute('SELECT key,value FROM metadata'))
        if metadata.get('schema') != SCHEMA or metadata.get('source_sha256') != source:
            raise ValueError('attribution_index_stale')
        yield con
    finally:
        con.close()


def summary(con):
    statuses = dict(con.execute('SELECT status,COUNT(*) FROM records GROUP BY status'))
    selection_counts=dict(con.execute('SELECT selection_kind,COUNT(DISTINCT dialogue_id) FROM records GROUP BY selection_kind'))
    total, with_speaker, observations = con.execute(
        'SELECT COUNT(*),COALESCE(SUM(with_speaker),0),COALESCE(SUM(observation_count),0) '
        'FROM records').fetchone()
    return {'total': total, 'with_speaker': with_speaker, 'statuses': statuses,
            'observations': observations,'selection_counts':selection_counts}


def build(database, input, out, refresh=False):
    """Build (or refresh) the private index; the source is never modified."""
    database = Path(database).resolve(strict=True)
    out = Path(out).resolve()
    if out == database or database.parent in out.parents:
        raise ValueError('attribution_output_must_be_external')
    _stable_source(database)
    source = fingerprint(database)
    if out.exists():
        if not refresh:
            raise ValueError('attribution_index_exists')
        with read_index(out, source):
            pass
    con = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute('PRAGMA query_only=ON')
        records, observations = load_suggestions(con, input, source)
    finally:
        con.close()
    if not records:
        raise ValueError('attribution_index_empty')
    out.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.attribution-index-', suffix='.sqlite',
                                             dir=out.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    destination = sqlite3.connect(temporary)
    try:
        destination.executescript(_TABLES)
        columns = [row[1] for row in destination.execute('PRAGMA table_info(records)')]
        for identifier, record in records.items():
            values = observations[identifier]
            status, speaker, addressee, kind = _aggregate(values)
            record.update(kind=kind, speaker=speaker, addressee=addressee, status=status,
                          observation_count=len(values),
                          with_speaker=int(any(o['speaker'] is not None for o in values)),
                          observations_json=json.dumps(values, ensure_ascii=False),
                          payload_sha256=_payload(record, values), **FLAGS)
            destination.execute('INSERT INTO records VALUES (' + ','.join('?' for _ in columns) + ')',
                                [record[column] for column in columns])
            for name in sorted({o['speaker'] for o in values if o['speaker'] is not None}):
                destination.execute('INSERT INTO names VALUES (?,?)', (identifier, name))
        destination.executemany('INSERT INTO metadata VALUES (?,?)',
                                [('schema', SCHEMA), ('source_sha256', source)])
        destination.commit()
        if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('attribution_index_integrity')
        result = dict(summary(destination), schema=SCHEMA, source_sha256=source,
                      source_unchanged=True, corpus_updated=False)
        destination.close()
        _stable_source(database)
        if fingerprint(database) != source:
            raise ValueError('attribution_source_changed')
        os.replace(temporary, out)
        return result
    finally:
        destination.close()
        if temporary.exists():
            temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('db', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--input',type=Path,required=True,action='append')
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = build(args.db, args.input, args.out, refresh=args.refresh)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
        parser.exit(1, 'attribution index refused: source, evidence or output validation failed\n')
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
