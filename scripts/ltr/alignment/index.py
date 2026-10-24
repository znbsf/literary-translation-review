"""Private source-bound index of proposed OCR alignments and model observations."""
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

from ltr.quotes.batch import digest
from ltr.quotes.index import _stable_source
from ltr.library.review_queue import fingerprint

SCHEMA = 'corpus-alignment-index-v1'
FLAGS = {'requires_review': 1, 'accepted': 0, 'human_confirmed': 0,
         'semantic_verified': 0, 'reference_eligible': 0, 'ocr_verified': 0}


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def dialogue(con, identity, edition):
    rows = con.execute('SELECT * FROM dialogues WHERE id=?', [identity]).fetchall()
    if len(rows) != 1 or rows[0]['edition_id'] != edition:
        raise ValueError('alignment_dialogue_binding')
    return dict(rows[0])


def identity(source, zh, ja, row):
    return digest({'source_sha256': source, 'zh_edition_id': zh, 'ja_edition_id': ja,
                   'chinese_id': row['chinese_id'], 'japanese_ids': row['japanese_ids'],
                   'chinese_text_sha256': row['chinese_text_sha256'],
                   'japanese_hashes': [j['text_sha256'] for j in row['japanese']]})[:16]


def canonical_japanese(references):
    """Canonical ordered ``id``+``text_sha256`` JSON for one Japanese reference tuple.

    Sorting keys and rebuilding each entry keeps the stored string stable, so
    grouping by exact ``(edition_id, ja_edition_id, japanese_json)`` binds both
    the ordered dialogue IDs and their source text hashes regardless of the key
    order in the incoming proposal file.
    """
    return json.dumps([{'id': j['id'], 'text_sha256': j['text_sha256']} for j in references],
                      ensure_ascii=False, sort_keys=True)


def load_proposals(con, directory, source):
    result, seen = [], set()
    for file in sorted(directory.rglob('align-*.json')):
        raw = file.read_bytes(); data = json.loads(raw)
        if data.get('schema') != 'haruhi-alignment-candidates-v1' or data.get('database_sha256') != source:
            raise ValueError('alignment_source_mismatch')
        zh, ja = data['zh_edition_id'], data['ja_edition_id']
        editions = []
        for eid, language in ((zh, 'zh'), (ja, 'ja')):
            rows = con.execute('SELECT language,book_key FROM editions WHERE id=?', [eid]).fetchall()
            if len(rows) != 1 or rows[0]['language'] != language:
                raise ValueError('alignment_edition_mismatch')
            editions.append(rows[0]['book_key'])
        if not editions[0] or editions[0] != editions[1] or editions[0] != data['book_key']:
            raise ValueError('alignment_book_mismatch')
        for row in data['rows']:
            if any(type(row.get(k)) is not int or row[k] != v for k, v in FLAGS.items()):
                raise ValueError('alignment_flags_invalid')
            key = (zh, ja, row['chinese_id'])
            if key in seen:
                raise ValueError('duplicate_alignment_proposal')
            seen.add(key)
            chinese = dialogue(con, row['chinese_id'], zh)
            if text_hash(chinese['text']) != row['chinese_text_sha256']:
                raise ValueError('alignment_text_mismatch')
            jids = row['japanese_ids']
            if (not isinstance(jids, list) or any(not isinstance(j, str) for j in jids)
                    or len(jids) > 5 or len(set(jids)) != len(jids)
                    or [j['id'] for j in row['japanese']] != jids
                    or row['status'] not in {'candidate', 'unmatched'}
                    or (row['status'] == 'candidate') != bool(jids)
                    or row['confidence'] not in {'low', 'medium', 'high'}):
                raise ValueError('alignment_contract_invalid')
            for japanese in row['japanese']:
                if text_hash(dialogue(con, japanese['id'], ja)['text']) != japanese['text_sha256']:
                    raise ValueError('alignment_text_mismatch')
            item = {'id': identity(source, zh, ja, row), 'edition_id': zh, 'ja_edition_id': ja,
                    'chinese_id': row['chinese_id'], 'ordinal': chinese['ordinal'],
                    'text': chinese['text'], 'speaker': chinese.get('speaker') or '',
                    'addressee': chinese.get('addressee') or '', 'status': row['status'],
                    'confidence': row['confidence'], 'model': data['requested_model'],
                    'served_model': data.get('served_model'), 'chinese_text_sha256': row['chinese_text_sha256'],
                    'japanese_json': canonical_japanese(row['japanese']), **FLAGS}
            item['payload_sha256'] = digest(item)
            result.append(item)
    if not result:
        raise ValueError('alignment_index_empty')
    return result


def load_observations(directory, source, proposals):
    result = defaultdict(list); seen = set()
    if directory is None:
        return result
    for file in sorted(directory.rglob('review-*.json')):
        data = json.loads(file.read_bytes())
        if data.get('schema') != 'haruhi-alignment-model-review-v1':
            continue
        if data.get('database_sha256') != source:
            raise ValueError('alignment_review_source_mismatch')
        for row in data['rows']:
            rid = row['review_id']
            if rid not in proposals or any(type(row.get(k)) is not int or row[k] != v for k, v in FLAGS.items()):
                raise ValueError('alignment_review_binding')
            proposal = proposals[rid]; japanese = json.loads(proposal['japanese_json'])
            if (row['zh_edition_id'] != proposal['edition_id'] or row['ja_edition_id'] != proposal['ja_edition_id']
                    or row['chinese_id'] != proposal['chinese_id'] or row['chinese_text_sha256'] != proposal['chinese_text_sha256']
                    or row['japanese_ids'] != [j['id'] for j in japanese]
                    or row['japanese_hashes'] != [j['text_sha256'] for j in japanese]):
                raise ValueError('alignment_review_binding')
            if (row['decision'] not in {'keep', 'reject', 'uncertain'}
                    or row['basis'] not in {'semantic_match','semantic_mismatch','ocr_unclear','insufficient_context','partial_match'}
                    or data['reviewer_requested_model'] == data['producer_requested_model']):
                raise ValueError('alignment_review_contract')
            observation = {'model': data['reviewer_requested_model'], 'served_model': data.get('reviewer_served_model'),
                           'decision': row['decision'], 'basis': row['basis'],
                           'artifact_sha256': hashlib.sha256(file.read_bytes()).hexdigest()}
            marker = (rid, digest(observation))
            if marker not in seen:
                seen.add(marker); result[rid].append(observation)
    return result


@contextmanager
def read_index(path, source):
    if not path.is_file():
        raise ValueError('alignment_index_missing')
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True); con.row_factory = sqlite3.Row
    try:
        con.execute('PRAGMA query_only=ON')
        metadata = dict(con.execute('SELECT key,value FROM metadata'))
        if metadata.get('schema') != SCHEMA or metadata.get('source_sha256') != source:
            raise ValueError('alignment_index_stale')
        yield con
    finally:
        con.close()


def target_reuse(con):
    """Exact ordered-target reuse aggregate over candidate proposals.

    A reuse group is the exact tuple ``(edition_id, ja_edition_id,
    japanese_json)`` over ``status='candidate'`` rows whose Japanese reference
    tuple is non-empty. ``japanese_json`` is the builder's canonical ordered
    ``id``+``text_sha256`` serialisation, so equality binds the exact ordered
    tuple and source hash. Unmatched rows carry an empty tuple and never form a
    group, so they cannot collapse into one giant "reuse" group.

    Every field is a review-priority clue only: several Chinese fragments may
    legitimately point at one Japanese record, so this is not an error or
    correctness rate. Fields stay ``0`` only when there is genuinely no matched
    group at this grain.
    """
    rows = con.execute(
        "SELECT COUNT(DISTINCT chinese_id), MIN(ordinal), MAX(ordinal) FROM candidates "
        "WHERE status='candidate' AND japanese_json IS NOT NULL "
        "AND japanese_json NOT IN ('', '[]') "
        "GROUP BY edition_id, ja_edition_id, japanese_json").fetchall()
    groups = affected = largest = span = 0
    for count, low, high in rows:
        count = int(count or 0)
        if count > largest:
            largest = count
        if low is not None and high is not None and int(high) - int(low) > span:
            span = int(high) - int(low)
        if count >= 2:
            groups += 1
            affected += count
    return {'groups_with_multiple_chinese': groups, 'affected_chinese_records': affected,
            'largest_group': largest, 'largest_chinese_ordinal_span': span}


def summary(con):
    counts = dict(con.execute('SELECT model_review_status,COUNT(*) FROM candidates GROUP BY model_review_status'))
    statuses = dict(con.execute('SELECT status,COUNT(*) FROM candidates GROUP BY status'))
    return {'total': sum(counts.values()), 'reviewed': sum(v for k, v in counts.items() if k not in {'unreviewed', 'not_applicable'}),
            'review_status': counts, 'matched': statuses.get('candidate', 0), 'unmatched': statuses.get('unmatched', 0),
            'target_reuse': target_reuse(con)}


def build(database, directory, output, reviews=None, *, refresh=False):
    database = database.resolve(strict=True); output = output.resolve(); _stable_source(database)
    if output == database or database.parent in output.parents:
        raise ValueError('alignment_output_must_be_external')
    source = fingerprint(database)
    if output.exists():
        if not refresh:
            raise ValueError('alignment_index_exists')
        with read_index(output, source):
            pass
    con = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True); con.row_factory = sqlite3.Row
    try:
        con.execute('PRAGMA query_only=ON'); proposals = load_proposals(con, directory, source)
    finally:
        con.close()
    observations = load_observations(reviews, source, {r['id']: r for r in proposals})
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.alignment-index-', suffix='.sqlite', dir=output.parent)
    os.close(fd); temporary = Path(temporary); destination = sqlite3.connect(temporary)
    try:
        destination.executescript('''CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE candidates(id TEXT PRIMARY KEY,edition_id TEXT,ja_edition_id TEXT,chinese_id TEXT,
                ordinal INTEGER,text TEXT,speaker TEXT,addressee TEXT,status TEXT,confidence TEXT,
                model TEXT,served_model TEXT,chinese_text_sha256 TEXT,japanese_json TEXT,payload_sha256 TEXT,
                model_review_status TEXT,observations_json TEXT,requires_review INTEGER CHECK(requires_review=1),
                accepted INTEGER CHECK(accepted=0),human_confirmed INTEGER CHECK(human_confirmed=0),
                semantic_verified INTEGER CHECK(semantic_verified=0),reference_eligible INTEGER CHECK(reference_eligible=0),
                ocr_verified INTEGER CHECK(ocr_verified=0));
            CREATE INDEX alignment_edition ON candidates(edition_id,ordinal);''')
        columns = [r[1] for r in destination.execute('PRAGMA table_info(candidates)')]
        for row in proposals:
            values = observations.get(row['id'], []); decisions = {r['decision'] for r in values}
            status = ('not_applicable' if row['status'] == 'unmatched' else 'unreviewed') if not decisions else next(iter(decisions)) if len(decisions) == 1 else 'conflict'
            row.update(observations_json=json.dumps(values, ensure_ascii=False), model_review_status=status)
            destination.execute('INSERT INTO candidates VALUES (' + ','.join('?' for _ in columns) + ')', [row[c] for c in columns])
        destination.executemany('INSERT INTO metadata VALUES (?,?)', [('schema', SCHEMA), ('source_sha256', source)])
        destination.commit()
        if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('alignment_index_integrity')
        result = dict(summary(destination), schema=SCHEMA, source_sha256=source,
                      source_unchanged=True, corpus_updated=False)
        destination.close(); _stable_source(database)
        if fingerprint(database) != source:
            raise ValueError('alignment_source_changed')
        os.replace(temporary, output)
        return result
    finally:
        destination.close()
        if temporary.exists(): temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('db', 'input', 'out'): parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--reviews', type=Path); parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args(argv)
    try: result = build(args.db, args.input, args.out, args.reviews, refresh=args.refresh)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
        parser.exit(1, 'alignment index refused: source, review or output validation failed\n')
    print(json.dumps(result)); return 0


if __name__ == '__main__': raise SystemExit(main())
