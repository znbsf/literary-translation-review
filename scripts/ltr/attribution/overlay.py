"""Read-only workbench projection of the private attribution suggestion index."""
from __future__ import annotations

from contextlib import contextmanager
import json
import re
import sqlite3

from ltr.attribution import index as ai


class AttributionOverlay:
    def __init__(self, store, corpus_id):
        self.store = store
        self.corpus, self.database = store.database(corpus_id)
        self._vocabularies = {}

    @contextmanager
    def index(self):
        from ltr.workbench import WorkbenchError
        if getattr(self.corpus, 'attribution_index', None) is None:
            raise WorkbenchError(404, 'attribution_index_unavailable')
        try:
            with ai.read_index(self.corpus.attribution_index,
                               self.database.source_sha256()) as con:
                yield con
        except (ValueError, OSError, sqlite3.Error):
            raise WorkbenchError(409, 'attribution_index_stale_or_invalid') from None

    def overview(self):
        with self.index() as con:
            result=dict(ai.summary(con),available=True)
        coverage={}
        scopes=[('haruhi_unknown','attribution_candidates','dialogue_id',('speaker_canonical',),
                 "speaker_canonical IS NULL OR TRIM(speaker_canonical) IN ('','unknown')"),
                ('completion_quote','corpus_completion_quote','quote_id',('requires_review',),'requires_review=1')]
        for kind,table,key,columns,where in scopes:
            if not self.database.has(table) or not all(self.database.has_column(table,c) for c in (key,*columns)):
                continue
            source_rows=self.database.scalar('SELECT COUNT(DISTINCT '+key+') FROM '+table+' WHERE '+where)
            with_results=result['selection_counts'].get(kind,0)
            if with_results>source_rows:
                from ltr.workbench import WorkbenchError
                raise WorkbenchError(409,'attribution_coverage_mismatch')
            coverage[kind]={'source_rows':source_rows,'with_results':with_results,'without_results':source_rows-with_results}
        result['coverage']=coverage
        return result

    def edition_counts(self):
        with self.index() as con:
            return dict(con.execute('SELECT edition_id,COUNT(*) FROM records GROUP BY edition_id'))

    def _vocabulary(self, edition_id):
        from ltr.workbench import WorkbenchError
        vocabulary = self._vocabularies.get(edition_id)
        if vocabulary is None:
            try:
                from ltr.attribution.review import load_vocabulary
                vocabulary = load_vocabulary(self.database.connect(), edition_id)
            except (ValueError, sqlite3.Error):
                raise WorkbenchError(409, 'attribution_source_mismatch') from None
            self._vocabularies[edition_id] = vocabulary
        return vocabulary

    def _validate(self, row):
        from ltr.workbench import WorkbenchError
        try:
            return ai.validate_record(self.database.connect(), row['selection_kind'],
                                      row['edition_id'], row, self.database.source_sha256(),
                                      self._vocabulary(row['edition_id']))
        except (ValueError, KeyError, TypeError, sqlite3.Error, json.JSONDecodeError):
            raise WorkbenchError(409, 'attribution_source_mismatch') from None

    def _edition(self, edition_id):
        from ltr.workbench import WorkbenchError
        rows = self.database.query('SELECT id,title,language FROM editions WHERE id=?', [edition_id])
        if len(rows) != 1:
            raise WorkbenchError(409, 'attribution_edition_mismatch')
        return rows[0]

    @staticmethod
    def _identity(value):
        from ltr.workbench import WorkbenchError
        if not isinstance(value, str) or not re.fullmatch(r'identity:[0-9a-f]{16}', value):
            raise WorkbenchError(400, 'invalid_identity_candidate_id')
        return value.split(':', 1)[1]

    def _row(self, identity):
        from ltr.workbench import WorkbenchError
        with self.index() as con:
            row = con.execute('SELECT * FROM records WHERE id=?', [self._identity(identity)]).fetchone()
        if row is None:
            raise WorkbenchError(404, 'identity_candidate_not_found')
        self._validate(row)
        return row

    def _item(self, row):
        from ltr.workbench import _clamp,MAX_DIALOGUE_TEXT_CHARS
        self._validate(row)
        edition = self._edition(row['edition_id'])
        return {'id': 'identity:' + row['id'], 'edition_id': row['edition_id'],
                'edition_title': edition['title'], 'language': edition['language'],
                'ordinal': row['ordinal'], 'text': _clamp(row['text'],MAX_DIALOGUE_TEXT_CHARS),
                'text_truncated':len(row['text'])>MAX_DIALOGUE_TEXT_CHARS,'text_total_chars':len(row['text']),
                'speaker': row['speaker'] or '', 'addressee': row['addressee'] or '',
                'attribution_status': 'candidate', 'reference_eligible': False,
                'has_alignment': False, 'source': 'identities',
                'identity_status': row['status']}

    def list(self, params):
        from ltr.workbench import _bounded_int, _validate_query_text, _like_pattern, WorkbenchError
        offset = _bounded_int(params.get('offset'), 0, 1_000_000)
        limit = max(1, _bounded_int(params.get('limit'), 30, 100))
        query = _validate_query_text(params.get('q'), limit=120)
        edition = _validate_query_text(params.get('edition'), limit=200)
        speaker = _validate_query_text(params.get('speaker'), limit=80)
        status = _validate_query_text(params.get('status'), limit=80)
        where, bindings = [], []
        if edition:
            where.append('edition_id=?'); bindings.append(edition)
        if query:
            where.append("text LIKE ? ESCAPE '\\'"); bindings.append(_like_pattern(query))
        if status:
            if status not in ai.STATUSES:
                raise WorkbenchError(400, 'invalid_parameter')
            where.append('status=?'); bindings.append(status)
        if speaker:
            # Match any observation's proposed speaker, even inside a conflict.
            where.append('id IN (SELECT id FROM names WHERE name=?)'); bindings.append(speaker)
        sql = 'SELECT * FROM records' + (' WHERE ' + ' AND '.join(where) if where else '')
        with self.index() as con:
            rows = con.execute(sql + ' ORDER BY edition_id,ordinal,id LIMIT ? OFFSET ?',
                               [*bindings, limit + 1, offset]).fetchall()
        return {'items': [self._item(r) for r in rows[:limit]], 'offset': offset,
                'limit': limit, 'has_more': len(rows) > limit, 'source': 'identities'}

    def _span(self, edition_id, locator):
        from ltr.workbench import WorkbenchError
        rows = self.database.query('SELECT text FROM paragraphs WHERE edition_id=? AND id=?',
                                   [edition_id, locator['paragraph_id']])
        if len(rows) != 1:
            raise WorkbenchError(409, 'attribution_source_mismatch')
        text = rows[0]['text'] or ''
        return text[locator['start']:locator['end']][:200]

    def _suggestions(self, row):
        observations = json.loads(row['observations_json'])
        items = []
        for observation in observations:
            evidence = [{**locator, 'text': self._span(row['edition_id'], locator)}
                        for locator in observation['evidence']]
            items.append({'model': observation.get('model'),
                          'served_model': observation.get('served_model'),
                          'speaker': observation['speaker'], 'addressee': observation['addressee'],
                          'kind': observation['kind'], 'basis': observation['basis'],
                          'evidence': evidence})
        return {'status': row['status'], 'observations': items, 'requires_review':True,
                'reference_eligible':False,'accepted':False,'human_confirmed':False,'semantic_verified':False}

    def detail(self, identity):
        from ltr.workbench import _sanitise_review
        row = self._row(identity)
        original = self.store.dialogue({'corpus': self.corpus.id, 'source': 'formal',
                                        'id': row['dialogue_id']})
        review = self.store.reviews.get(self.corpus.id, identity)
        if review and (review.get('context_hash') != row['payload_sha256']
                       or review.get('corpus_fingerprint') != self.database.fingerprint()):
            review = dict(review, stale=True)
        original['item'] = self._item(row)
        original['review'] = _sanitise_review(review)
        original['entry'] = {'reference_eligible': False, 'attribution_status': 'candidate'}
        original['identity_suggestions'] = self._suggestions(row)
        return original

    def review_target(self, identity):
        row = self._row(identity)
        return {'id': identity, 'edition_id': row['edition_id'],
                'context_hash': row['payload_sha256']}, self._edition(row['edition_id'])['title']
