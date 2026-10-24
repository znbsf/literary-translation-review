"""Bounded private attribution suggestions. Never modifies a corpus database.

Source text lives only in memory, authorized model payloads and private candidate
outputs. Console, progress and summary report hashes, counts and error classes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import urllib.error
import uuid

from ltr.quotes.batch import (is_unsupported_response_content, is_redirect_rejection, digest,
                              failure_metadata, request_model, request_byte_count,
                              resolve_output_token_budget)
from ltr.response_history import resolve_history_mode, validate_history, safe_counts
from ltr.library.review_queue import fingerprint


SYSTEM = """你是私有文学语料的说话人归属整理员。输入是待归属的对白目标 targets，context 是同版本上下文，仅供判断。
为每条目标给出说话人 speaker 与受话人 addressee 建议，无法判断时用 null，不要编造。
身份名必须逐字照录 vocabulary 中的允许字符串，否则必须是当前 target.context 某一段落窗口中的原样连续子串；
非 null 身份必须为非空白字符串且最多 80 字符；忽略首尾空白和大小写后等于 'unknown' 的值不合法。
不得扩展为姓氏或全名、不得转换繁简体、不得自造规范别名。
只输出 JSON {"suggestions":[{"dialogue_id":"给定ID","speaker":null,"addressee":null,
"kind":"spoken|thought|written|not_dialogue|uncertain",
"basis":"explicit_name|direct_address|self_identification|context_inference|insufficient_context|source_not_dialogue",
"evidence":[{"paragraph_id":"给定段落ID","text":"该段落原文连续子串"}]}]}。
必须完整且只覆盖 targets 每条 ID 一次，不添加字段、说明或代码围栏。
任何情况下 evidence 均为 0-3 条；任一身份非 null 时必须给出 1-3 条。每条 evidence.paragraph_id 必须来自该 dialogue_id 对应 target.context；
段落出现在其他目标、context_reference 或历史消息中，不代表可作为当前目标的证据。
evidence.text 必须为 1-200 字符的原样连续子串，且在所指段落唯一出现；若称呼或代词重复，须在此长度范围内复制足够相邻原文使子串唯一，不能只取重复称呼或代词。
同一建议内不得重复引用同一 paragraph_id 的同一原文区间；不同区间可以重叠。
context_window=endpoint_windows 时只提供长引文两端的邻近段落，不代表中间段落已完整提供。
not_dialogue 时 speaker/addressee 必须为 null 且 basis=source_not_dialogue；source_not_dialogue 仅能与 not_dialogue 配对。
若两个身份都是 null 且 kind 不是 not_dialogue，basis 必须为 insufficient_context；此时不要用 explicit_name 或 context_inference。
所有输出只是待人工复核建议，不是确认结果。"""


REFERENCE_RULES = """本次为参考复用模式（reuse-context）。以上全部约束继续有效，另加以下规则：
targets 每条 context 仅给出定位符（context_id、paragraph_id、ordinal），不重复段落原文。
本批首次提供的段落原文只出现在 context_reference 数组内，每条含 context_id、edition_id、paragraph_id、ordinal、text。
同一 context_id 在整个会话中只会提供一次；某段落未出现在本批 context_reference 不代表原文被删除或改写。
复用原文时，仅按当前 target.context 列出的 context_id 查取历史原文；其他目标或历史参考中的段落不因此成为当前目标可引用的证据。
context_id 由 (edition_id, paragraph_id) 构成，不同版本的同名 paragraph_id 属于不同 context_id，不得混用。
输出 evidence 仍须给出原文 paragraph_id 与该段落原文的连续子串，text 必须逐字照录、为 1-200 字符且在所指段落唯一出现。"""


SYSTEM_REFERENCE = SYSTEM + '\n\n' + REFERENCE_RULES


EVIDENCE_HINT_RULES = """本次显式启用 evidence_hints。提示仅保证片段在当前目标 context 的所指段落内可唯一定位，
不证明任何身份、说话人、受话人或语义；提示可能为空，必须自行阅读当前目标上下文判断。
不得仅因某个名字出现在提示片段中就选择该名字为 speaker 或 addressee。
无法判断时仍用 null；原合同允许时可用空 evidence，非空身份仍须满足原有证据要求。
输出 evidence 格式不变，仍只给 paragraph_id 和原样 text，不输出提示中的偏移、哈希或其他字段。
可使用当前 target.context 中其他满足原合同的唯一原文片段，不必选择提示。
参考复用模式仍只按当前 target.context 的 context_id 查取原文；不得修补或重发旧失败响应。"""


KINDS = ('haruhi_unknown', 'completion_quote')
SUGGESTION_KINDS = {'spoken', 'thought', 'written', 'not_dialogue', 'uncertain'}
BASES = {'explicit_name', 'direct_address', 'self_identification', 'context_inference',
         'insufficient_context', 'source_not_dialogue'}
STOP_STATUS = {400, 401, 403, 404, 413, 429}
MAX_REQUEST_BYTES = 16 * 1024 * 1024
CODES = {
    'invalid_kind', 'invalid_budget', 'output_already_exists', 'output_inside_source',
    'unstable_database_snapshot', 'source_database_changed', 'missing_runtime_authentication',
    'missing_corpus_schema', 'missing_context', 'context_too_large', 'ambiguous_context',
    'unmapped_source_id', 'ambiguous_source_id', 'duplicate_source_id', 'vocabulary_too_large',
    'invalid_suggestion_contract', 'inexact_target_coverage', 'duplicate_target_id',
    'invalid_enum', 'invalid_identity', 'unsupported_identity', 'invalid_evidence',
    'unbound_evidence', 'non_unique_evidence_span', 'duplicate_evidence_span',
    'missing_identity_evidence', 'invalid_not_dialogue', 'invalid_null_identity',
    'invalid_session', 'invalid_edition', 'invalid_history_mode',
    'invalid_request_byte_budget', 'invalid_output_token_budget',
    'invalid_context_reference', 'conflicting_context_reference',
    'invalid_evidence_hint_mode', 'invalid_evidence_hint_items',
    'duplicate_evidence_hint_target', 'invalid_evidence_hint_context',
    'invalid_evidence_hint_vocabulary',
}
_SOURCES = {
    'haruhi_unknown': {
        'table': 'attribution_candidates',
        'id': 'dialogue_id',
        'columns': ('dialogue_id', 'edition_id', 'speaker_surface', 'speaker_canonical',
                    'addressee_surface', 'addressee_canonical'),
        'where': "edition_id=? AND (speaker_canonical IS NULL OR TRIM(speaker_canonical) IN ('','unknown'))",
    },
    'completion_quote': {
        'table': 'corpus_completion_quote',
        'id': 'quote_id',
        'columns': ('quote_id', 'edition_id', 'kind', 'speaker_id', 'addressee_id',
                    'speaker_status', 'addressee_status', 'status'),
        'where': 'edition_id=? AND requires_review=1',
    },
}


def _tables(con):
    return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _context(con, edition_id, start_para, end_para):
    """Edition-scoped paragraph window; endpoints must be present and paragraph ids unique."""
    if start_para is None or end_para is None:
        raise ValueError('missing_context')
    low, high = min(start_para, end_para) - 2, max(start_para, end_para) + 2
    if high - low + 1 > 30:
        rows = [dict(r) for r in con.execute(
            'SELECT id,edition_id,ordinal,text FROM paragraphs WHERE edition_id=? AND '
            '(ordinal BETWEEN ? AND ? OR ordinal BETWEEN ? AND ?) ORDER BY ordinal',
            (edition_id,min(start_para,end_para)-2,min(start_para,end_para)+2,
             max(start_para,end_para)-2,max(start_para,end_para)+2))]
    else:
        rows = [dict(r) for r in con.execute(
            "SELECT id,edition_id,ordinal,text FROM paragraphs "
            "WHERE edition_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal",
            (edition_id, low, high))]
    ordinals = {r['ordinal'] for r in rows}
    if start_para not in ordinals or end_para not in ordinals:
        raise ValueError('missing_context')
    if len({(r['edition_id'], r['id']) for r in rows}) != len(rows):
        raise ValueError('ambiguous_context')
    return rows


def load_targets(con, kind, edition_id, dialogue_id=None):
    """Resolve selected source rows to unique same-edition dialogues with bound context."""
    spec = _SOURCES.get(kind)
    if spec is None:
        raise ValueError('invalid_kind')
    if not {'editions', 'paragraphs', 'dialogues'} <= _tables(con) or spec['table'] not in _tables(con):
        raise ValueError('missing_corpus_schema')
    if len(con.execute('SELECT id FROM editions WHERE id=?',(edition_id,)).fetchall()) != 1:
        raise ValueError('invalid_edition')
    columns = ','.join(spec['columns'])
    where = spec['where']
    bindings = [edition_id]
    if dialogue_id is not None:
        where += ' AND ' + spec['id'] + '=?'
        bindings.append(dialogue_id)
    sources = [dict(r) for r in con.execute(
        f"SELECT {columns} FROM {spec['table']} WHERE {where} ORDER BY {spec['id']}", bindings)]
    ids = [row[spec['id']] for row in sources]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate_source_id')
    targets = []
    for row in sources:
        matches = con.execute(
            "SELECT id,edition_id,ordinal,text,start_para,end_para FROM dialogues WHERE id=?",
            (row[spec['id']],)).fetchall()
        if not matches:
            raise ValueError('unmapped_source_id')
        if len(matches) != 1 or matches[0]['edition_id'] != edition_id:
            raise ValueError('ambiguous_source_id')
        dialogue = matches[0]
        context = _context(con, edition_id, dialogue['start_para'], dialogue['end_para'])
        text = dialogue['text'] or ''
        targets.append({
            'dialogue_id': row[spec['id']],
            'edition_id': edition_id,
            'ordinal': dialogue['ordinal'],
            'context_window': 'endpoint_windows' if abs(dialogue['end_para']-dialogue['start_para'])+5 > 30 else 'continuous_window',
            'text': text,
            'context': context,
            'context_sha256': digest([{'id': p['id'], 'edition_id': p['edition_id'],
                                       'ordinal': p['ordinal'], 'text': p['text']} for p in context]),
            'dialogue_text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'source_candidate_sha256': digest({key: row[key] for key in spec['columns']}),
        })
    return sorted(targets,key=lambda target:(target['ordinal'],target['dialogue_id']))


def load_vocabulary(con, edition_id):
    """Unique nonempty, non-unknown speaker/interlocutor and candidate canonical names."""
    names = set()

    def add(value):
        value = (value or '').strip()
        if value and value.lower() != 'unknown':
            names.add(value)

    for row in con.execute("SELECT speaker, interlocutor FROM dialogues WHERE edition_id=?", (edition_id,)):
        add(row[0])
        add(row[1])
    if 'attribution_candidates' in _tables(con):
        for row in con.execute(
                "SELECT speaker_canonical, addressee_canonical FROM attribution_candidates WHERE edition_id=?",
                (edition_id,)):
            add(row[0])
            add(row[1])
    vocabulary = sorted(names)
    if len(vocabulary) > 500:
        raise ValueError('vocabulary_too_large')
    return vocabulary


def _identity(value, vocabulary, context):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 80 or value.strip().lower() == 'unknown':
        raise ValueError('invalid_identity')
    if value not in vocabulary and not any(value in p['text'] for p in context):
        raise ValueError('unsupported_identity')
    return value


def validate_suggestions(payload, items, vocabulary):
    """Exact-coverage, evidence-bound validation; returns locators, never source fragments."""
    if not isinstance(payload, dict) or set(payload) != {'suggestions'}:
        raise ValueError('invalid_suggestion_contract')
    rows = payload['suggestions']
    if not isinstance(rows, list) or len(rows) != len(items):
        raise ValueError('invalid_suggestion_contract')
    by_id = {target['dialogue_id']: target for target in items}
    if len(by_id) != len(items):
        raise ValueError('duplicate_target_id')
    vocab = set(vocabulary)
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'dialogue_id', 'speaker', 'addressee', 'kind', 'basis', 'evidence'}:
            raise ValueError('invalid_suggestion_contract')
        try:
            target = by_id.get(row['dialogue_id'])
        except TypeError:
            raise ValueError('invalid_suggestion_contract') from None
        if target is None or row['dialogue_id'] in seen:
            raise ValueError('inexact_target_coverage')
        seen.add(row['dialogue_id'])
        if row['kind'] not in SUGGESTION_KINDS or row['basis'] not in BASES:
            raise ValueError('invalid_enum')
        speaker = _identity(row['speaker'], vocab, target['context'])
        addressee = _identity(row['addressee'], vocab, target['context'])
        evidence = row['evidence']
        if not isinstance(evidence, list) or len(evidence) > 3:
            raise ValueError('invalid_evidence')
        locators, spans = [], set()
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {'paragraph_id', 'text'}:
                raise ValueError('invalid_evidence')
            text = item['text']
            if not isinstance(text, str) or not 1 <= len(text) <= 200:
                raise ValueError('invalid_evidence')
            paragraph = next((p for p in target['context']
                              if p['id'] == item['paragraph_id'] and p['edition_id'] == target['edition_id']), None)
            if paragraph is None:
                raise ValueError('unbound_evidence')
            start = paragraph['text'].find(text)
            if start < 0 or paragraph['text'].find(text, start + 1) != -1:
                raise ValueError('non_unique_evidence_span')
            key = (paragraph['id'], start, start + len(text))
            if key in spans:
                raise ValueError('duplicate_evidence_span')
            spans.add(key)
            locators.append({'edition_id': paragraph['edition_id'], 'paragraph_id': paragraph['id'],
                             'ordinal': paragraph['ordinal'], 'start': start, 'end': start + len(text),
                             'paragraph_sha256': hashlib.sha256(paragraph['text'].encode('utf-8')).hexdigest()})
        if (speaker is not None or addressee is not None) and not evidence:
            raise ValueError('missing_identity_evidence')
        if row['kind'] == 'not_dialogue':
            if speaker is not None or addressee is not None or row['basis'] != 'source_not_dialogue':
                raise ValueError('invalid_not_dialogue')
        elif row['basis'] == 'source_not_dialogue':
            raise ValueError('invalid_not_dialogue')
        if speaker is None and addressee is None and row['kind'] != 'not_dialogue' \
                and row['basis'] != 'insufficient_context':
            raise ValueError('invalid_null_identity')
        result.append({'dialogue_id': row['dialogue_id'], 'edition_id': target['edition_id'],
                       'ordinal': target['ordinal'], 'context_sha256': target['context_sha256'],
                       'context_window': target['context_window'],
                       'dialogue_text_sha256': target['dialogue_text_sha256'],
                       'source_candidate_sha256': target['source_candidate_sha256'],
                       'speaker': speaker, 'addressee': addressee,
                       'kind': row['kind'], 'basis': row['basis'], 'evidence': locators})
    return result


def validate_partial_suggestions(payload, items, vocabulary):
    """Salvage source-bound rows under an exact global coverage contract.

    The global response object, exact target coverage and id uniqueness are
    checked first; any violation refuses the whole call exactly like the strict
    validator. Otherwise every row is re-checked alone by ``validate_suggestions``
    so only source-bound rows survive. Row-level failures are reported as
    source-free diagnostics; invalid rows are never repaired, normalised or
    guessed, and never become accepted unknown outcomes.
    """
    if not isinstance(payload, dict) or set(payload) != {'suggestions'}:
        raise ValueError('invalid_suggestion_contract')
    rows = payload['suggestions']
    if not isinstance(rows, list) or len(rows) != len(items):
        raise ValueError('invalid_suggestion_contract')
    by_id = {target['dialogue_id']: target for target in items}
    if len(by_id) != len(items):
        raise ValueError('duplicate_target_id')
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('invalid_suggestion_contract')
        dialogue_id = row.get('dialogue_id')
        if not isinstance(dialogue_id, str) or dialogue_id not in by_id or dialogue_id in seen:
            raise ValueError('inexact_target_coverage')
        seen.add(dialogue_id)
    valid, refused = [], []
    for row in rows:
        target = by_id[row['dialogue_id']]
        try:
            valid.extend(validate_suggestions({'suggestions': [row]}, [target], vocabulary))
        except (ValueError, KeyError, TypeError) as exc:
            refused.append({'dialogue_id': row['dialogue_id'],
                            'error_code': str(exc) if isinstance(exc, ValueError) and str(exc) in CODES
                            else type(exc).__name__})
    return valid, refused


def _write_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def _ensure_stable(database, baseline):
    if any(Path(str(database) + suffix).exists() for suffix in ('-wal', '-journal')):
        raise ValueError('unstable_database_snapshot')
    if fingerprint(database) != baseline:
        raise ValueError('source_database_changed')


def _numeric_usage(value):
    keys = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if key in keys and type(item) is int and item >= 0}


def _model_item(target):
    return {'dialogue_id': target['dialogue_id'], 'ordinal': target['ordinal'], 'text': target['text'],
            'context_window': target['context_window'],
            'context': [{'paragraph_id': p['id'], 'ordinal': p['ordinal'], 'text': p['text']}
                        for p in target['context']]}


def _context_id(edition_id, paragraph_id):
    """Edition-scoped locator; edition-local paragraph ids never collide across editions."""
    return digest({'edition_id':edition_id,'paragraph_id':paragraph_id})


def _reference_item(target):
    """Reference-mode target: full dialogue text plus context locators, never repeated paragraph text."""
    return {'dialogue_id': target['dialogue_id'], 'ordinal': target['ordinal'], 'text': target['text'],
            'context_window': target['context_window'],
            'context': [{'context_id': _context_id(p['edition_id'], p['id']),
                         'paragraph_id': p['id'], 'ordinal': p['ordinal']}
                        for p in target['context']]}


def _reference_identity(entry):
    """Validate a supplied paragraph and bind its identity, ordinal and text hash."""
    allowed = {'context_id', 'edition_id', 'paragraph_id', 'ordinal', 'text', 'text_sha256'}
    if not isinstance(entry, dict) or set(entry) - allowed:
        raise ValueError('invalid_context_reference')
    context_id, edition_id = entry.get('context_id'), entry.get('edition_id')
    paragraph_id, ordinal, text = entry.get('paragraph_id'), entry.get('ordinal'), entry.get('text')
    if not all(isinstance(v, str) and v for v in (context_id, edition_id, paragraph_id)) \
            or type(ordinal) is not int or not isinstance(text, str):
        raise ValueError('invalid_context_reference')
    if context_id != _context_id(edition_id, paragraph_id):
        raise ValueError('invalid_context_reference')
    text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
    declared = entry.get('text_sha256')
    if declared is not None and declared != text_hash:
        raise ValueError('conflicting_context_reference')
    return context_id, digest({'edition_id':edition_id,'paragraph_id':paragraph_id,
                               'ordinal':ordinal,'text_sha256':text_hash})


def _supplied_references(history, history_mode="text"):
    """Derive supplied context_id -> text hash from appended user turns only.

    A saved id is trusted only because its text was actually carried by an
    appended user message; malformed shapes and ids reused with different source
    content are refused, including conflicting duplicates hidden by later turns.
    """
    try:
        validate_history(history, history_mode)
    except ValueError:
        raise ValueError('invalid_context_reference') from None
    supplied = {}
    for message in history:
        if (not isinstance(message,dict) or set(message)!={'role','content'}
                or message.get('role') not in {'user','assistant'}
                or (message.get('role') == 'user' and not isinstance(message.get('content'),str))):
            raise ValueError('invalid_context_reference')
        if message.get('role') != 'user':
            continue
        content = message.get('content')
        if not isinstance(content, str):
            raise ValueError('invalid_context_reference')
        try:
            payload = json.loads(content)
        except (ValueError, TypeError):
            raise ValueError('invalid_context_reference') from None
        if not isinstance(payload, dict):
            raise ValueError('invalid_context_reference')
        if 'context_reference' not in payload:
            continue
        references = payload['context_reference']
        if not isinstance(references, list):
            raise ValueError('invalid_context_reference')
        for entry in references:
            context_id, text_hash = _reference_identity(entry)
            if context_id in supplied and supplied[context_id] != text_hash:
                raise ValueError('conflicting_context_reference')
            supplied[context_id] = text_hash
    return supplied


def _new_references(items, supplied):
    """Newly supplied context paragraphs only; dedup within batch and against history."""
    references = []
    for target in items:
        for paragraph in target['context']:
            context_id = _context_id(paragraph['edition_id'], paragraph['id'])
            text = paragraph['text'] or ''
            text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
            reference={'context_id':context_id,'edition_id':paragraph['edition_id'],
                       'paragraph_id':paragraph['id'],'ordinal':paragraph['ordinal'],
                       'text':text,'text_sha256':text_hash}
            _,reference_hash=_reference_identity(reference)
            known = supplied.get(context_id)
            if known is not None:
                if known != reference_hash:
                    raise ValueError('conflicting_context_reference')
                continue
            supplied[context_id] = reference_hash
            references.append(reference)
    return references


def run(args, session=None) -> dict:
    evidence_hint_mode = getattr(args, 'evidence_hints', 'none')
    if type(evidence_hint_mode) is not str or evidence_hint_mode not in ('none', 'unique-spans'):
        raise ValueError('invalid_evidence_hint_mode')
    history_mode = resolve_history_mode(getattr(args, "history_mode", "text"))
    history_options = {} if history_mode == "text" else {"history_mode": history_mode}
    max_output_tokens = getattr(args, 'max_output_tokens', None)
    output_token_budget = resolve_output_token_budget(max_output_tokens)
    if max_output_tokens is not None:
        history_options['max_output_tokens'] = max_output_tokens
    max_request_bytes = getattr(args, 'max_request_bytes', None)
    if max_request_bytes is not None and (type(max_request_bytes) is not int
            or not 1 <= max_request_bytes <= MAX_REQUEST_BYTES):
        raise ValueError('invalid_request_byte_budget')
    if args.kind not in KINDS:
        raise ValueError('invalid_kind')
    if not (getattr(args,'execute',False) is True and type(args.max_calls) is int and 1 <= args.max_calls <= 100
            and type(args.batch_size) is int and 1 <= args.batch_size <= 12
            and type(args.start) is int and args.start >= 0):
        raise ValueError('invalid_budget')
    reference_mode = getattr(args, 'reuse_context', False) is True
    effective_system = SYSTEM_REFERENCE if reference_mode else SYSTEM
    if evidence_hint_mode != 'none':
        effective_system += '\n\n' + EVIDENCE_HINT_RULES
    database = args.db.resolve(strict=True)
    output = args.out.resolve()
    if output.exists():
        raise ValueError('output_already_exists')
    if output == database.parent or database.parent in output.parents:
        raise ValueError('output_inside_source')
    if any(Path(str(database) + suffix).exists() for suffix in ('-wal', '-journal')):
        raise ValueError('unstable_database_snapshot')
    baseline = fingerprint(database)
    con = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute('PRAGMA query_only=ON')
        targets = load_targets(con, args.kind, args.edition)
        vocabulary = load_vocabulary(con, args.edition)
        completed=set()
        if getattr(args,'exclude_completed',None):
            from ltr.attribution.index import load_suggestions
            records,_=load_suggestions(con,args.exclude_completed,baseline)
            completed={r['dialogue_id'] for r in records.values()
                       if r['selection_kind']==args.kind and r['edition_id']==args.edition}
    finally:
        con.close()
    _ensure_stable(database, baseline)
    endpoint = os.environ.get('ANTHROPIC_BASE_URL', '')
    token = os.environ.get('ANTHROPIC_AUTH_TOKEN', '')
    if not endpoint or not token:
        raise ValueError('missing_runtime_authentication')
    if session is None: session = {}
    if not isinstance(session,dict): raise ValueError('invalid_session')
    if not session:
        session.update(model=args.model,database_sha256=baseline,system_sha256=digest(effective_system),
                       session_id=str(uuid.uuid4()),history_mode=history_mode,
                       request_byte_budget=max_request_bytes,output_token_budget=output_token_budget,history=[])
        if evidence_hint_mode != 'none':
            session['evidence_hints_mode'] = evidence_hint_mode
    if (session.get('model') != args.model or session.get('database_sha256') != baseline
            or session.get('system_sha256') != digest(effective_system) or not isinstance(session.get('history'),list)
            or not isinstance(session.get('session_id'),str) or session.get('history_mode','text') != history_mode
            or (session.get('request_byte_budget') is not None
                and type(session.get('request_byte_budget')) is not int)
            or session.get('request_byte_budget') != max_request_bytes
            or type(session.get('output_token_budget',8000)) is not int
            or session.get('output_token_budget',8000) != output_token_budget):
        raise ValueError('invalid_session')
    if session.get('evidence_hints_mode', 'none') != evidence_hint_mode:
        raise ValueError('invalid_session')
    validate_history(session['history'], history_mode)
    session['history_mode'] = history_mode
    session['request_byte_budget'] = max_request_bytes
    session['output_token_budget'] = output_token_budget
    if reference_mode:
        supplied=_supplied_references(session['history'], history_mode)
        selected_targets=[t for t in targets[min(args.start,len(targets)):] if t['dialogue_id'] not in completed]
        _new_references(selected_targets[:args.max_calls*args.batch_size],supplied)
    output.mkdir(parents=True)
    session_id = session['session_id']
    history = session['history']
    ledger = {'schema': 'corpus-attribution-progress-v1', 'kind': args.kind, 'edition_id': args.edition,
              'database_sha256': baseline, 'model': args.model, 'session_id': session_id, 'history_mode': history_mode,
              'request_byte_budget':max_request_bytes,'output_token_budget':output_token_budget,
              'next_request_bytes':None,'stop_reason':'running','calls': []}
    if evidence_hint_mode != 'none':
        ledger['evidence_hints_mode'] = evidence_hint_mode

    def save_progress():
        _write_json(output / 'progress.json', ledger)

    selected=len(targets)
    prefix=min(args.start,selected)
    eligible=[target for target in targets[prefix:] if target['dialogue_id'] not in completed]
    excluded=len(targets[prefix:])-len(eligible)
    partial_mode = getattr(args,'partial_batches',False) is True
    previous_refusals = []
    starts = list(range(0,len(eligible),args.batch_size))
    valid = failed = attempted = resolved_speaker = cache_reads = cache_reports = 0
    consecutive = 0
    stop = False
    stop_reason = 'range_or_budget_complete'
    for number, start in enumerate(starts):
        if number >= args.max_calls:
            break
        items = eligible[start:start + args.batch_size]
        if reference_mode:
            references = _new_references(items, _supplied_references(history, history_mode))
            payload = {'edition_id':args.edition,'selection_kind':args.kind,
                       'targets': [_reference_item(item) for item in items]}
            if references:
                payload['context_reference'] = references
        else:
            payload = {'edition_id':args.edition,'selection_kind':args.kind,'targets': [_model_item(item) for item in items]}
        if previous_refusals:
            payload['previous_batch_validation'] = {
                'refused_rows': previous_refusals,
                'reminder': 'Only emit current targets. Previous refused IDs remain pending. '
                            'Copy allowed identity strings literally. When speaker and addressee '
                            'are both null, use insufficient_context unless kind is not_dialogue; '
                            'not_dialogue requires source_not_dialogue. Do not repair old rows.'}
        previous_refusals = []
        if number == 0:
            payload['vocabulary'] = vocabulary
        if evidence_hint_mode != 'none':
            from ltr.attribution.evidence_hints import build_evidence_hints
            payload['evidence_hints'] = build_evidence_hints(items, vocabulary)
        request_bytes = request_byte_count(args.model, payload, system=effective_system,
                                          history=history, session_id=session_id, **history_options)
        if max_request_bytes is not None and request_bytes > max_request_bytes:
            ledger['next_request_bytes'] = request_bytes
            stop_reason = 'request_byte_budget_exhausted'
            break
        input_sha = digest(payload)
        record = {'batch': number, 'start': start, 'count': len(items), 'input_sha256': input_sha,
                  'target_ids':[item['dialogue_id'] for item in items],
                  'session_id': session_id, 'history_sha256': digest(history), 'request_bytes':request_bytes,
                  'status': 'dispatched_outcome_unknown'}
        ledger['calls'].append(record)
        save_progress()
        usage = {}
        try:
            response = request_model(endpoint, token, args.model, payload, system=effective_system,
                                     history=history, session_id=session_id, **history_options)
            if history_mode != "text" and safe_counts(response.get("content_shape")):
                record["content_shape"] = safe_counts(response["content_shape"])
            usage = _numeric_usage(response.get('usage'))
            if partial_mode:
                rows, refused = validate_partial_suggestions(response['payload'], items, vocabulary)
                _ensure_stable(database, baseline)
                record['refused_rows'] = refused
                previous_refusals = refused
                if rows:
                    for row in rows:
                        row.update(suggestion_id=digest({'source': baseline, 'kind': args.kind,
                                                         'edition': args.edition, 'dialogue_id': row['dialogue_id'],
                                                         'context_sha256': row['context_sha256']})[:16],
                                   requires_review=1, accepted=0, human_confirmed=0,
                                   semantic_verified=0, reference_eligible=0)
                    result = {'schema': 'corpus-attribution-suggestions-v1', 'database_sha256': baseline,
                              'kind': args.kind, 'edition_id': args.edition, 'model': args.model,
                              'served_model': response.get('served_model', args.model),
                              'input_sha256': input_sha, 'session_id': session_id,
                              'partial_batch': True, 'refused_rows': refused, 'rows': rows}
                    if evidence_hint_mode != 'none':
                        result['evidence_hints_mode'] = evidence_hint_mode
                    _write_json(output / f'suggestion-{number:03d}.json', result)
                    record.update(status='valid' if not refused else 'valid_partial',
                                  rows=len(rows), refused=len(refused), usage=usage)
                    valid += len(rows)
                    failed += len(refused)
                    resolved_speaker += sum(row['speaker'] is not None for row in rows)
                    consecutive = 0
                else:
                    record.update(status='failed', rows=0, refused=len(refused), usage=usage)
                    failed += len(items)
                    consecutive += 1
                    save_progress()
                    if consecutive >= 3:
                        stop = True
                        stop_reason = 'consecutive_invalid_responses'
            else:
                rows = validate_suggestions(response['payload'], items, vocabulary)
                _ensure_stable(database, baseline)
                for row in rows:
                    row.update(suggestion_id=digest({'source': baseline, 'kind': args.kind,
                                                     'edition': args.edition, 'dialogue_id': row['dialogue_id'],
                                                     'context_sha256': row['context_sha256']})[:16],
                               requires_review=1, accepted=0, human_confirmed=0,
                               semantic_verified=0, reference_eligible=0)
                result = {'schema': 'corpus-attribution-suggestions-v1', 'database_sha256': baseline,
                          'kind': args.kind, 'edition_id': args.edition, 'model': args.model,
                          'served_model': response.get('served_model', args.model),
                          'input_sha256': input_sha, 'session_id': session_id, 'rows': rows}
                if evidence_hint_mode != 'none':
                    result['evidence_hints_mode'] = evidence_hint_mode
                _write_json(output / f'suggestion-{number:03d}.json', result)
                record.update(status='valid', rows=len(rows), usage=usage)
                valid += len(rows)
                resolved_speaker += sum(row['speaker'] is not None for row in rows)
                consecutive = 0
        except (ValueError, KeyError, TypeError, OSError) as exc:
            usage = getattr(exc, 'usage', None) or usage
            record.update(status='failed', **failure_metadata(exc))
            if isinstance(exc,ValueError) and str(exc) in CODES:
                record['error_code'] = str(exc)
            if isinstance(exc, urllib.error.HTTPError):
                record['http_status'] = exc.code
                if exc.code in STOP_STATUS or is_redirect_rejection(exc):
                    stop = True
                    stop_reason = 'request_rejected'
            if is_unsupported_response_content(exc):
                stop = True
                stop_reason = 'unsupported_response_content'
            if usage:
                record['usage'] = usage
            failed += len(items)
            consecutive += 1
            save_progress()
            if str(exc) in {'source_database_changed', 'unstable_database_snapshot'}:
                raise
            if consecutive >= 3 and not is_redirect_rejection(exc) and not is_unsupported_response_content(exc):
                stop = True
                stop_reason = 'consecutive_invalid_responses'
        attempted += len(items)
        if 'cache_read_input_tokens' in usage:
            cache_reports += 1
            cache_reads += usage['cache_read_input_tokens']
        save_progress()
        if stop:
            break
    _ensure_stable(database, baseline)
    ledger['stop_reason'] = stop_reason
    save_progress()
    valid_calls = sum(r['status'] == 'valid' for r in ledger['calls'])
    partial_calls = sum(r['status'] == 'valid_partial' for r in ledger['calls'])
    failed_calls = sum(r['status'] == 'failed' for r in ledger['calls'])
    summary = {'schema': 'corpus-attribution-summary-v1', 'kind': args.kind, 'edition_id': args.edition,
               'model': args.model, 'database_sha256': baseline, 'session_id': session_id, 'history_mode': history_mode,
               'request_byte_budget':max_request_bytes,'output_token_budget':output_token_budget,
               'next_request_bytes':ledger['next_request_bytes'],
               'selected': selected, 'prefix': prefix, 'vocabulary_size': len(vocabulary),
               'calls': len(ledger['calls']), 'valid': valid, 'failed': failed,
               'unattempted':len(eligible)-attempted, 'remaining':selected-valid-excluded,
               'excluded_completed_rows':excluded,'eligible_rows':len(eligible),
               'attempted_calls':valid_calls + partial_calls + failed_calls,
               'valid_calls':valid_calls,'partial_calls':partial_calls,'failed_calls':failed_calls,
               'attempted_rows':attempted,'valid_rows':valid,'failed_rows':failed,
               'skipped_prefix_rows':prefix,'unattempted_rows':len(eligible)-attempted,
               'resolved_speaker': resolved_speaker,
               'stop_reason':stop_reason,
               'cache_read_input_tokens': cache_reads, 'cache_usage_reports': cache_reports,
               'source_unchanged': fingerprint(database) == baseline and not any(
                   Path(str(database) + suffix).exists() for suffix in ('-wal', '-journal')),
               'corpus_updated': False}
    if evidence_hint_mode != 'none':
        summary['evidence_hints_mode'] = evidence_hint_mode
    _write_json(output / 'summary.json', summary)
    return summary


def _evidence_hint_cli(value):
    if value not in ('none', 'unique-spans'):
        raise argparse.ArgumentTypeError('invalid_evidence_hint_mode')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--kind', required=True, choices=list(KINDS))
    parser.add_argument('--edition', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--max-calls', type=int, default=1)
    parser.add_argument('--batch-size', type=int, default=6)
    parser.add_argument('--start', type=int, default=0)
    parser.add_argument('--exclude-completed',type=Path,action='append')
    parser.add_argument('--partial-batches', action='store_true')
    parser.add_argument('--reuse-context', action='store_true')
    parser.add_argument('--evidence-hints', type=_evidence_hint_cli,
                        choices=('none', 'unique-spans'), default='none')
    parser.add_argument('--history-mode', choices=('text', 'preserve-thinking'), default='text')
    parser.add_argument('--max-request-bytes', type=int)
    parser.add_argument('--max-output-tokens', type=int)
    parser.add_argument('--execute', action='store_true', required=True)
    args = parser.parse_args(argv)
    try:
        summary = run(args)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        code = str(exc) if isinstance(exc, ValueError) and str(exc) in CODES else type(exc).__name__
        parser.exit(1, 'attribution review refused: ' + code + '\n')
    print(json.dumps(summary, ensure_ascii=True))
    return 0 if summary['source_unchanged'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
