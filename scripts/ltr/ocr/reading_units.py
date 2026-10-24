"""Selected-page, source-exact OCR word projections for private local review.

No OCR, character repair, paragraph inference or persistence occurs here.
``text_status=complete`` describes nonempty source word strings only; it does
not establish transcription completeness, reading order or OCR accuracy.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from ltr.ocr import reading_order_index as order


SCHEMA = 'corpus-reading-units-v1'
MAX_SOURCE_LINES = 20_000
MAX_WORDS = MAX_UNITS = 100_000
MAX_WORD_CODEPOINTS = 4096
MAX_TOTAL_TEXT_CODEPOINTS = 1_000_000
MAX_MODULE_BYTES = 1024 * 1024
ERROR_CODES = frozenset({
    'ocr_reading_units_not_configured', 'ocr_reading_units_invalid',
    'ocr_reading_units_unavailable', 'ocr_reading_units_source_changed',
    'ocr_reading_units_candidate_changed', 'ocr_reading_units_receipt_changed',
    'ocr_reading_units_geometry_unavailable', 'ocr_reading_units_text_invalid',
    'ocr_reading_units_limits_exceeded', 'ocr_reading_units_projection_changed',
})


class OCRReadingUnitsError(ValueError):
    def __init__(self, code='ocr_reading_units_invalid'):
        self.code = code if type(code) is str and code in ERROR_CODES else 'ocr_reading_units_invalid'
        super().__init__(self.code)


def _need(ok, code='ocr_reading_units_invalid'):
    if not ok:
        raise OCRReadingUnitsError(code)


def _projection_sha():
    return hashlib.sha256(order.vision._bytes(Path(__file__), MAX_MODULE_BYTES)).hexdigest()


def _source_words(receipt, geometry_lines, count):
    lines = receipt['lines']
    _need(type(lines) is list and len(lines) == len(geometry_lines) <= MAX_SOURCE_LINES)
    source_lines, words = [], {}
    total_chars = 0
    partial = False
    for li, line in enumerate(lines):
        _need(type(line) is dict and type(line.get('words')) is list
              and len(line['words']) == len(geometry_lines[li]['words']))
        output = []
        for wi, raw in enumerate(line['words']):
            _need(len(words) < MAX_WORDS, 'ocr_reading_units_limits_exceeded')
            _need(type(raw) is dict and type(raw.get('text')) is str,
                  'ocr_reading_units_text_invalid')
            text = raw['text']
            _need(len(text) <= MAX_WORD_CODEPOINTS, 'ocr_reading_units_limits_exceeded')
            try:
                text.encode('utf-8')
            except UnicodeError:
                raise OCRReadingUnitsError('ocr_reading_units_text_invalid') from None
            total_chars += len(text)
            _need(total_chars <= MAX_TOTAL_TEXT_CODEPOINTS, 'ocr_reading_units_limits_exceeded')
            partial = partial or text == ''
            box = geometry_lines[li]['words'][wi]
            word = {'ref':[li, wi], 'text':text,
                    'box':[box['x'], box['y'], box['x']+box['w'], box['y']+box['h']]}
            words[(li, wi)] = word
            output.append(word)
        source_lines.append({'index':li, 'words':output})
    _need(len(words) == count)
    return source_lines, words, 'empty' if not words else 'partial' if partial else 'complete'


def _units(candidate, source_lines, words):
    units = []
    if candidate['status'] == 'geometry_candidate_unverified':
        for segment in candidate['segments']:
            units.append({'index':len(units), 'role':segment['role'],
                'band':segment['band'], 'column':segment['column'],
                'source_line':segment['source_line'], 'box':list(segment['box']),
                'word_refs':copy.deepcopy(segment['word_refs']), 'boundary_before':'unknown'})
    else:
        for line in source_lines:
            if not line['words']:
                continue
            boxes = [word['box'] for word in line['words']]
            units.append({'index':len(units), 'role':'unknown', 'band':None, 'column':None,
                'source_line':line['index'],
                'box':[min(b[0] for b in boxes), min(b[1] for b in boxes),
                       max(b[2] for b in boxes), max(b[3] for b in boxes)],
                'word_refs':[list(word['ref']) for word in line['words']],
                'boundary_before':'unknown'})
    _need(len(units) <= MAX_UNITS, 'ocr_reading_units_limits_exceeded')
    refs = []
    for unit in units:
        unit['words'] = []
        for ref in unit['word_refs']:
            _need(type(ref) is list and len(ref) == 2 and all(type(v) is int and v >= 0 for v in ref)
                  and tuple(ref) in words and ref[0] == unit['source_line'])
            refs.append(tuple(ref))
            unit['words'].append(copy.deepcopy(words[tuple(ref)]))
    _need(len(refs) == len(set(refs)) == len(words) and set(refs) == set(words)
          and [list(ref) for ref in refs] == candidate['word_order'])
    return units


def read_reading_units(corpus, database, identity, expected_sha):
    """Lazily project one already-bound geometry page, failing without any text.

    Both word projections independently cover every source word once. A word
    intentionally appears in source_lines and units; neither projection dedupes
    repeated text. Images remain subject to the workbench's image endpoint.
    """
    try:
        _need(getattr(corpus, 'ocr_reading_order_candidates', None) is not None,
              'ocr_reading_units_not_configured')
        _need(type(identity) is str and order.PAGE_ID.fullmatch(identity)
              and type(expected_sha) is str and order._hash(expected_sha))
        projection_sha = _projection_sha()
        before = copy.deepcopy(order.read_candidate(corpus, database, identity, expected_sha))
        _need(type(before) is dict and before.get('available') is not False,
              'ocr_reading_units_geometry_unavailable')
        candidate = before['candidate']
        _need(candidate['status'] in order.STATUSES, 'ocr_reading_units_geometry_unavailable')
        _need(all(type(candidate.get(flag)) is int and candidate[flag] == value
                  for flag, value in order.FLAGS.items()))
        source = candidate['source_sha256']
        fingerprint = database.fingerprint()
        _need(database.source_sha256() == source, 'ocr_reading_units_source_changed')
        page = order._db_page(database, candidate['page_id'])
        raw, lines, count = order._receipt_geometry(corpus, page, candidate)
        order._validate_geometry(candidate, lines, page['width'], page['height'], count)
        source_lines, words, text_status = _source_words(order._safe_json(raw), lines, count)
        units = _units(candidate, source_lines, words)
        after = order.read_candidate(corpus, database, identity, expected_sha)
        _need(before == after, 'ocr_reading_units_candidate_changed')
        raw_after, _, _ = order._receipt_geometry(corpus, page, candidate)
        _need(raw_after == raw, 'ocr_reading_units_receipt_changed')
        _need(order._db_page(database, candidate['page_id']) == page
              and database.source_sha256(refresh=True) == source
              and database.fingerprint() == fingerprint, 'ocr_reading_units_source_changed')
        _need(_projection_sha() == projection_sha, 'ocr_reading_units_projection_changed')
        return {'available':True, 'schema':SCHEMA, 'id':identity,
            'candidate_sha256':expected_sha, 'page_id':candidate['page_id'],
            'source_sha256':source, 'receipt_sha256':candidate['receipt_sha256'],
            'image_sha256':candidate['image_sha256'],
            'geometry_algorithm_sha256':candidate['algorithm_sha256'],
            'projection_algorithm_sha256':projection_sha, 'width':page['width'], 'height':page['height'],
            'status':candidate['status'], 'units':units, 'source_lines':source_lines,
            'flags':dict(order.FLAGS), 'text_status':text_status, 'boundary_status':'unknown'}
    except OCRReadingUnitsError as exc:
        return {'available':False, 'error':exc.code}
    except Exception:
        return {'available':False, 'error':'ocr_reading_units_unavailable'}
