"""Read-only workbench projection of the independent OCR alignment index."""
from __future__ import annotations

from contextlib import contextmanager
import json
import re
import sqlite3

from ltr.alignment import index as ai


REUSE_PREVIEW_LIMIT = 10


class AlignmentOverlay:
    def __init__(self, store, corpus_id):
        self.store = store
        self.corpus, self.database = store.database(corpus_id)

    @contextmanager
    def index(self):
        from ltr.workbench import WorkbenchError
        if self.corpus.alignment_index is None:
            raise WorkbenchError(404, 'alignment_index_unavailable')
        try:
            with ai.read_index(self.corpus.alignment_index, self.database.source_sha256()) as con:
                yield con
        except (ValueError, OSError, sqlite3.Error):
            raise WorkbenchError(409, 'alignment_index_stale_or_invalid') from None

    def overview(self):
        with self.index() as con:
            return dict(ai.summary(con), available=True, target_reuse_filter_available=True,
                        source_coverage=self._source_coverage(con))

    def _source_coverage(self, con):
        """Whole-edition proposal coverage, independent of model opinions.

        Index metadata alone binds a source SHA, not each stored proposal row.
        Validate row identities and source hashes before counting either matched
        or unmatched proposals. Source text stays private in memory.
        """
        from ltr.workbench import WorkbenchError, _clamp, _clean_text
        code = 'alignment_source_coverage_invalid'
        try:
            baseline = self.database.source_sha256()
            binding = con.execute("SELECT value FROM metadata WHERE key='source_sha256'").fetchall()
            if len(binding) != 1 or binding[0][0] != baseline:
                raise ValueError(code)
            pick = self.database._pick
            editions = self.database.query(
                f'SELECT id,{pick("editions", "title")} AS title,language,'
                f'{pick("editions", "book_key")} AS book_key FROM editions ORDER BY title,id')
            by_edition = {row['id']: row for row in editions}
            if len(by_edition) != len(editions):
                raise ValueError(code)
            chinese = {row['id']: row for row in editions if row['language'] == 'zh'}
            counts = {identity: {'source_dialogues': 0, 'proposal_dialogues': 0,
                                 'matched_dialogues': 0, 'unmatched_dialogues': 0,
                                 'unprocessed_dialogues': 0, 'conflicting_candidate_dialogues': 0,
                                 'source_paragraphs': 0}
                      for identity in chinese}
            source_ids = {identity: set() for identity in chinese}
            proposal_statuses = {identity: {'candidate': set(), 'unmatched': set()}
                                 for identity in chinese}
            # A duplicate global source ID must never resolve to an arbitrary
            # edition; only proposed IDs are required to resolve unambiguously.
            dialogues = {}
            for row in self.database.query('SELECT id,edition_id,ordinal,text FROM dialogues'):
                if row['edition_id'] in counts:
                    if not isinstance(row['id'], str) or not row['id']:
                        raise ValueError(code)
                    source_ids[row['edition_id']].add(row['id'])
                identity = row['id']
                if identity in dialogues:
                    dialogues[identity] = None
                else:
                    dialogues[identity] = row
            for edition_id, identities in source_ids.items():
                counts[edition_id]['source_dialogues'] = len(identities)
            if self.database.has('paragraphs'):
                for row in self.database.query('SELECT edition_id,COUNT(*) AS n FROM paragraphs GROUP BY edition_id'):
                    if row['edition_id'] in counts:
                        counts[row['edition_id']]['source_paragraphs'] = row['n']
            seen = set()
            seen_pairs = set()
            for proposal in con.execute('SELECT * FROM candidates'):
                identity = proposal['chinese_id']
                pair_identity = (proposal['edition_id'], proposal['ja_edition_id'], identity)
                original = dialogues.get(identity)
                edition = by_edition.get(proposal['edition_id'])
                japanese_edition = by_edition.get(proposal['ja_edition_id'])
                if (not isinstance(identity, str) or not identity or pair_identity in seen_pairs
                        or original is None or edition is None or edition['language'] != 'zh'
                        or original['edition_id'] != proposal['edition_id']
                        or original['ordinal'] != proposal['ordinal']
                        or not isinstance(original['text'], str) or original['text'] != proposal['text']
                        or ai.text_hash(original['text']) != proposal['chinese_text_sha256']
                        or japanese_edition is None or japanese_edition['language'] != 'ja'
                        or not isinstance(edition['book_key'], str) or not edition['book_key']
                        or edition['book_key'] != japanese_edition['book_key']
                        or any(type(proposal[key]) is not int or proposal[key] != expected
                               for key, expected in ai.FLAGS.items())):
                    raise ValueError(code)
                references = json.loads(proposal['japanese_json'])
                if (not isinstance(references, list) or len(references) > 5
                        or proposal['status'] not in {'candidate', 'unmatched'}
                        or (proposal['status'] == 'candidate') != bool(references)):
                    raise ValueError(code)
                seen_japanese = set()
                for reference in references:
                    if not isinstance(reference, dict) or set(reference) != {'id', 'text_sha256'}:
                        raise ValueError(code)
                    jid = reference['id']
                    if not isinstance(jid, str) or jid in seen_japanese:
                        raise ValueError(code)
                    seen_japanese.add(jid)
                    source = dialogues.get(jid)
                    if (source is None or source['edition_id'] != proposal['ja_edition_id']
                            or not isinstance(source['text'], str)
                            or ai.text_hash(source['text']) != reference['text_sha256']):
                        raise ValueError(code)
                seen_pairs.add(pair_identity)
                proposal_statuses[proposal['edition_id']][proposal['status']].add(identity)
                if identity not in seen:
                    seen.add(identity)
                    counts[proposal['edition_id']]['proposal_dialogues'] += 1
            japanese_by_key = {}
            for row in editions:
                key = row['book_key']
                if row['language'] == 'ja' and isinstance(key, str) and key.strip():
                    japanese_by_key[key] = japanese_by_key.get(key, 0) + 1
            items = []
            for identity, edition in chinese.items():
                measured = counts[identity]
                if measured['proposal_dialogues'] > measured['source_dialogues']:
                    raise ValueError(code)
                matched = proposal_statuses[identity]['candidate']
                unmatched = proposal_statuses[identity]['unmatched']
                if not (matched | unmatched).issubset(source_ids[identity]):
                    raise ValueError(code)
                conflicts = matched & unmatched
                unmatched_only = unmatched - matched
                unprocessed = source_ids[identity] - (matched | unmatched)
                measured.update(matched_dialogues=len(matched),
                                unmatched_dialogues=len(unmatched_only),
                                unprocessed_dialogues=len(unprocessed),
                                conflicting_candidate_dialogues=len(conflicts))
                if (measured['proposal_dialogues'] != len(matched | unmatched)
                        or measured['source_dialogues'] != (len(matched) + len(unmatched_only)
                                                            + len(unprocessed))):
                    raise ValueError(code)
                items.append({'edition_id': _clean_text(identity), 'title': _clamp(edition['title'], 200),
                              **measured,
                              'missing_dialogues': measured['source_dialogues'] - measured['proposal_dialogues'],
                              'same_book_key_japanese_editions': japanese_by_key.get(edition['book_key'], 0)})
            self.database.require()
            if self.database.source_sha256() != baseline:
                raise ValueError(code)
            return {'available': True,
                    'source_dialogues': sum(item['source_dialogues'] for item in items),
                    'covered_dialogues': len(seen),
                    'missing_dialogues': sum(item['missing_dialogues'] for item in items),
                    'matched_dialogues': sum(item['matched_dialogues'] for item in items),
                    'unmatched_dialogues': sum(item['unmatched_dialogues'] for item in items),
                    'unprocessed_dialogues': sum(item['unprocessed_dialogues'] for item in items),
                    'conflicting_candidate_dialogues': sum(item['conflicting_candidate_dialogues'] for item in items),
                    'zero_dialogue_editions': sum(item['source_dialogues'] == 0 for item in items),
                    'editions': items}
        except (ValueError, TypeError, KeyError, IndexError, OSError, sqlite3.Error):
            raise WorkbenchError(409, code) from None

    def edition_counts(self):
        with self.index() as con:
            return dict(con.execute('SELECT edition_id,COUNT(*) FROM candidates GROUP BY edition_id'))

    def _validate(self, row):
        from ltr.workbench import WorkbenchError
        chinese = self.database.query('SELECT * FROM dialogues WHERE id=?', [row['chinese_id']])
        if (len(chinese) != 1 or chinese[0]['edition_id'] != row['edition_id']
                or chinese[0]['ordinal'] != row['ordinal'] or chinese[0]['text'] != row['text']
                or ai.text_hash(chinese[0]['text']) != row['chinese_text_sha256']
                or any(row[k] != v for k, v in ai.FLAGS.items())):
            raise WorkbenchError(409, 'alignment_source_mismatch')
        japanese = []
        for reference in json.loads(row['japanese_json']):
            matches = self.database.query('SELECT id,edition_id,ordinal,text FROM dialogues WHERE id=?', [reference['id']])
            if (len(matches) != 1 or matches[0]['edition_id'] != row['ja_edition_id']
                    or ai.text_hash(matches[0]['text']) != reference['text_sha256']):
                raise WorkbenchError(409, 'alignment_source_mismatch')
            japanese.append(dict(matches[0]))
        return japanese

    @staticmethod
    def _identity(value):
        from ltr.workbench import WorkbenchError
        if not isinstance(value, str) or not re.fullmatch(r'alignment:[0-9a-f]{16}', value):
            raise WorkbenchError(400, 'invalid_alignment_candidate_id')
        return value.split(':', 1)[1]

    def _row(self, identity):
        from ltr.workbench import WorkbenchError
        with self.index() as con:
            row = con.execute('SELECT * FROM candidates WHERE id=?', [self._identity(identity)]).fetchone()
        if row is None: raise WorkbenchError(404, 'alignment_candidate_not_found')
        self._validate(row); return row

    def _target_reuse(self, row):
        """Bounded same-group reuse view for one candidate row.

        The group is the exact ``(edition_id, ja_edition_id, japanese_json)``
        tuple over ``status='candidate'`` rows. Unmatched proposals have no
        applicable group. Only bounded locators ``{id, ordinal}`` are exposed:
        no source prose, paths or raw SQL errors leave this method, and every
        linked candidate still passes the normal detail validation.
        """
        if (row['status'] != 'candidate' or not row['japanese_json']
                or row['japanese_json'] in ('', '[]')):
            return None
        key = [row['edition_id'], row['ja_edition_id'], row['japanese_json']]
        with self.index() as con:
            stats = con.execute(
                'SELECT COUNT(DISTINCT chinese_id), MIN(ordinal), MAX(ordinal) FROM candidates '
                "WHERE edition_id=? AND ja_edition_id=? AND japanese_json=? AND status='candidate'",
                key).fetchone()
            count = int(stats[0] or 0)
            low, high = stats[1], stats[2]
            span = int(high) - int(low) if low is not None and high is not None else 0
            others = con.execute(
                'SELECT id, ordinal FROM candidates '
                "WHERE edition_id=? AND ja_edition_id=? AND japanese_json=? AND status='candidate' "
                'AND id<>? ORDER BY ordinal ASC, id ASC LIMIT ?',
                [*key, row['id'], REUSE_PREVIEW_LIMIT]).fetchall()
        examples = [{'id': 'alignment:' + str(item[0]), 'ordinal': item[1]} for item in others]
        return {'chinese_count': count, 'ordinal_min': low, 'ordinal_max': high, 'ordinal_span': span,
                'preview_limit': REUSE_PREVIEW_LIMIT, 'total': count, 'examples': examples,
                'has_more': max(0, count - 1) > len(examples)}

    def _item(self, row):
        from ltr.workbench import _clamp,MAX_DIALOGUE_TEXT_CHARS
        self._validate(row)
        edition = self.database.query('SELECT title,language FROM editions WHERE id=?', [row['edition_id']])[0]
        return {'id': 'alignment:' + row['id'], 'edition_id': row['edition_id'], 'edition_title': edition['title'],
                'language': edition['language'], 'ordinal': row['ordinal'], 'text': _clamp(row['text'],MAX_DIALOGUE_TEXT_CHARS),
                'text_truncated':len(row['text'])>MAX_DIALOGUE_TEXT_CHARS,'text_total_chars':len(row['text']),
                'speaker': row['speaker'], 'addressee': row['addressee'], 'attribution_status': 'candidate',
                'reference_eligible': False, 'has_alignment': False, 'source': 'alignments',
                'alignment_status': row['status'], 'model_review_status': row['model_review_status']}

    def list(self, params):
        from ltr.workbench import WorkbenchError, _bounded_int, _validate_query_text, _like_pattern
        limit = max(1, _bounded_int(params.get('limit'), 30, 100))
        offset = _bounded_int(params.get('offset'), 0, 1_000_000)
        where, bindings = [], []
        reuse = params.get('target_reuse', '')
        if type(reuse) is not str or reuse not in {'', '0', '1'}:
            raise WorkbenchError(400, 'invalid_alignment_target_reuse')
        if reuse == '1':
            where.append("status='candidate' AND (edition_id,ja_edition_id,japanese_json) IN "
                         "(SELECT edition_id,ja_edition_id,japanese_json FROM candidates "
                         "WHERE status='candidate' GROUP BY edition_id,ja_edition_id,japanese_json "
                         "HAVING COUNT(DISTINCT chinese_id)>1)")
        for column, key, maximum in [('edition_id', 'edition', 200), ('speaker', 'speaker', 80), ('model_review_status', 'status', 80)]:
            value = _validate_query_text(params.get(key), limit=maximum)
            if value:
                where.append(column + '=?'); bindings.append(value)
        query = _validate_query_text(params.get('q'), limit=120)
        if query:
            where.append("text LIKE ? ESCAPE '\\'"); bindings.append(_like_pattern(query))
        sql = 'SELECT * FROM candidates' + (' WHERE ' + ' AND '.join(where) if where else '')
        with self.index() as con:
            rows = con.execute(sql + ' ORDER BY edition_id,ordinal,id LIMIT ? OFFSET ?', [*bindings, limit + 1, offset]).fetchall()
        return {'items': [self._item(r) for r in rows[:limit]], 'offset': offset, 'limit': limit,
                'has_more': len(rows) > limit, 'source': 'alignments'}

    def detail(self, identity):
        from ltr.workbench import _sanitise_review, _clamp
        row = self._row(identity)
        original = self.store.dialogue({'corpus': self.corpus.id, 'source': 'formal', 'id': row['chinese_id']})
        review = self.store.reviews.get(self.corpus.id, identity)
        if review and (review.get('context_hash') != row['payload_sha256']
                       or review.get('corpus_fingerprint') != self.database.fingerprint()):
            review = dict(review, stale=True)
        original['item'] = self._item(row)
        original['review'] = _sanitise_review(review)
        original['alignment'] = None
        original['entry'] = {'reference_eligible': False, 'alignment_status': 'candidate', 'attribution_status': 'candidate'}
        original['alignment_candidate'] = {
            'status': row['status'], 'confidence': row['confidence'], 'model': row['model'],
            'review_status': row['model_review_status'], 'review_observations': json.loads(row['observations_json']),
            'ocr_verified': False, 'reference_eligible': False,
            'target_reuse': self._target_reuse(row),
            'japanese': [{'id': r['id'], 'ordinal': r['ordinal'], 'text': _clamp(r['text'], 6000),
                          'truncated': len(r['text']) > 6000, 'total_chars': len(r['text'])}
                         for r in self._validate(row)]}
        return original

    def review_target(self, identity):
        row = self._row(identity)
        title = self.database.query('SELECT title FROM editions WHERE id=?', [row['edition_id']])[0]['title']
        return {'id': identity, 'edition_id': row['edition_id'], 'context_hash': row['payload_sha256']}, title
