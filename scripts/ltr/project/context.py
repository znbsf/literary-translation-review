"""Explicit source-first inputs and immutable local receipts; no model calls."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

from . import ledger
from .contracts import compile_context, ContractError
from .storage import atomic_write, digest, project_lock, StorageError
from ltr.library import glossary
from ltr.library.evidence import SOURCE_KINDS

LEAK_RUN = 20
EXCLUDED = ['current-target', 'rewrite-advice', 'old-translation-excerpts',
            'draft-derived-target-language-summary']
EXPOSURES = ('not-claimed', 'exposed', 'verified-unexposed')
BOUNDARY = ('只保证本工具显式输入、摘要绑定与冻结先后；不证明操作者或主会话从未看过旧译，'
            '不保证语义污染检测完备。operator_exposure_status 默认 not-claimed。零模型、零网络调用。')


class ContextError(ValueError):
    """Static codes only."""


def need(ok, code):
    if not ok:
        raise ContextError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')


def file_path(path):
    return ledger._path(glossary.external_path(path))


def workspace(path):
    # Check lexical ancestors before resolving away reparse points.
    glossary.external_path(Path(path) / '.context-path-check')
    root = ledger._path(path)
    # external_path also checks Windows reparse points, including on Python 3.11.
    glossary.external_path(root / '.context-path-check')
    need(not (root / 'dialogue.sqlite').exists(), 'context_inside_corpus')
    need(not root.exists() or root.is_dir(), 'context_workspace_invalid')
    return root


def read_json(path):
    return json.loads(file_path(path).read_bytes().decode('utf-8'), object_pairs_hook=ledger._pairs)


def create(path, value):
    path = file_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())


def identity(value, prefix):
    need(isinstance(value, str) and re.fullmatch(prefix + '-[0-9a-f]{16}', value),
         'context_id_invalid')
    return value


def event_chain(root):
    path = file_path(root / 'events.jsonl')
    events, previous = [], None
    if not path.exists():
        need(not any((root / name).exists() for name in ('contexts', 'frozen')),
             'context_event_chain_broken')
        return events
    try:
        for raw in path.read_bytes().splitlines(keepends=True):
            event = json.loads(raw, object_pairs_hook=ledger._pairs)
            claimed = event.pop('sha256')
            need(raw.endswith(b'\n') and claimed == digest(event)
                 and event['prev_sha256'] == previous
                 and event['kind'] in ('build', 'freeze', 'compare'), 'context_event_chain_broken')
            event['sha256'] = claimed
            need(raw == encoded(event), 'context_event_chain_broken')
            events.append(event)
            previous = sha(raw)
        # Artifact receipts also detect orphan files after interrupted publication or tail removal.
        expected = set()
        for event in events:
            if event['kind'] == 'build':
                cid = identity(event['context_id'], 'ctx')
                files = [(f'contexts/{cid}/manifest.json', event['manifest_sha256']),
                         (f'contexts/{cid}/context.json', event['context_sha256'])]
            elif event['kind'] == 'freeze':
                fid = identity(event['freeze_id'], 'frz')
                files = [(f'frozen/{fid}.json', event['freeze_sha256'])]
            else:
                files = []
            for name, checksum in files:
                need(name not in expected, 'context_event_chain_broken')
                code = 'context_not_frozen' if event['kind'] == 'freeze' else 'context_event_chain_broken'
                try:
                    raw_artifact = file_path(root / name).read_bytes()
                except OSError:
                    raise ContextError(code) from None
                need(sha(raw_artifact) == checksum, code)
                expected.add(name)
        actual = {p.relative_to(root).as_posix() for folder in ('contexts', 'frozen')
                  for p in (root / folder).rglob('*') if p.is_file()}
        need(actual == expected, 'context_event_chain_broken')
    except ContextError:
        raise
    except (ValueError, KeyError, TypeError, OSError):
        raise ContextError('context_event_chain_broken') from None
    return events


def append_event(root, events, kind, **fields):
    path = file_path(root / 'events.jsonl')
    previous = sha(path.read_bytes().splitlines(keepends=True)[-1]) if events else None
    event = dict(kind=kind, at=ledger._now(), prev_sha256=previous, **fields)
    event['sha256'] = digest(event)
    with path.open('ab') as stream:
        stream.write(encoded(event))
        stream.flush()
        os.fsync(stream.fileno())


def strings(value):
    if isinstance(value, str):
        yield value
        # Embedded JSON metadata must not hide an origin marker or escaped text.
        if value.startswith(('{', '[')):
            try:
                yield from strings(json.loads(value))
            except ValueError:
                pass
    elif isinstance(value, dict):
        if 'origin_kind' in value:
            need(value['origin_kind'] in SOURCE_KINDS, 'context_disallowed_origin')
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def leaks(texts, probes):
    fingerprints = {p[i:i + LEAK_RUN] for p in probes for i in range(len(p) - LEAK_RUN + 1)}
    return any(t[i:i + LEAK_RUN] in fingerprints for t in texts for i in range(len(t) - LEAK_RUN + 1))


def build(workspace_path, project, span, *, ledger_path=None, evidence=(), glossary_path=None,
          glossary_version=None, cards=None, horizon=None, probe_old=()):
    root = workspace(workspace_path)
    value, project_sha = ledger._project(project)
    binding = value.get('corpus_binding')
    if binding:
        need(not root.is_relative_to((Path(project) / binding['database']).resolve().parent),
             'context_inside_corpus')
    order = [p['id'] for p in value['source']]
    need(len(span) == 2 and all(p in order for p in span), 'context_range_invalid')
    start, end = map(order.index, span)
    need(start <= end, 'context_range_reversed')
    selected = value['source'][start:end + 1]
    ids = order[start:end + 1]
    targets = [u['target'] for u in value['units'] if u['id'] in ids]
    context, inputs = {}, []
    omitted = {'candidate': 0, 'clue': 0}

    def add(category, artifact, checksum, rows):
        if rows:
            context.setdefault(category, []).extend(rows)
            inputs.append(dict(category=category, artifact_id=artifact, sha256=checksum, count=len(rows)))

    add('source', 'source:' + span[0] + '-' + span[1], digest(selected),
        [{'id': p['id'], 'text': p['text']} for p in selected])
    if ledger_path:
        data = ledger._load(file_path(ledger_path))
        ledger._validate(data)
        list(strings(data))
        need(data['project_sha256'] == project_sha, 'context_ledger_project_mismatch')
        checksum = digest(data)
        add('whole-book-understanding', 'ledger:' + checksum[:12], checksum,
            [{k: e[k] for k in ('dimension', 'kind', 'source_locator', 'observation', 'translation_decision')}
             for e in data['entries'] if e['status'] == 'decided'])
    target_hashes = {sha(p['text'].encode('utf-8')) for p in selected}
    for path in evidence:
        data = read_json(path)
        need(isinstance(data, dict) and data.get('schema') == 'ltr-evidence-v1', 'context_evidence_invalid')
        list(strings(data))
        rows = []
        def collect(items):
            need(isinstance(items, list), 'context_evidence_invalid')
            for item in items:
                need(isinstance(item, dict) and item.get('level') in ('verified', 'candidate', 'clue'),
                     'context_evidence_invalid')
                # A target-bound relation is forbidden even if its level is still candidate.
                def overlaps(obj):
                    if isinstance(obj, dict):
                        return obj.get('paragraph_sha256') in target_hashes or any(overlaps(v) for v in obj.values())
                    return isinstance(obj, list) and any(overlaps(v) for v in obj)
                need(not overlaps(item), 'prior_work_overlaps_target_range')
                if item['level'] == 'verified':
                    rows.append({k: v for k, v in item.items() if k not in ('correspondences', 'claims', 'review_observations')})
                else:
                    omitted[item['level']] += 1
                collect(item.get('correspondences', []))
        collect(data.get('items'))
        checksum = digest(data)
        add('verified-prior-work', 'prior:' + checksum[:12], checksum, rows)
    need(bool(glossary_path) == bool(glossary_version), 'context_glossary_version_required')
    if glossary_path:
        data = glossary.export(glossary_path, glossary_version)
        list(strings(data))
        rows = data['entries']
        for entry in rows:
            source = ''.join(entry['source_form'].split())
            target = entry['target_form']
            sentence = len(target) >= LEAK_RUN and any(target in t for t in targets)
            for p in selected:
                paragraph = ''.join(p['text'].split())
                threshold = max(LEAK_RUN, (4 * len(paragraph) + 4) // 5)
                sentence |= source == paragraph or any(
                    paragraph[i:i + threshold] in source for i in range(len(paragraph) - threshold + 1))
            need(not sentence, 'glossary_contains_target_sentence')
        add('confirmed-glossary', 'glossary:' + glossary_version, data['sha256'], rows)
    need(bool(cards) == bool(horizon), 'context_cards_horizon_required')
    if cards:
        data = read_json(cards)
        rows = compile_context(data, {p['id']: p['text'] for p in value['source']}, order, horizon, value['book_hash'])
        add('source', 'cards:' + digest(data)[:12], digest(data), rows)
    texts = list(strings(context))
    need(not leaks(texts, targets), 'current_target_leak')
    probes = []
    for path in probe_old:
        raw = file_path(path).read_bytes()
        need(not leaks(texts, [raw.decode('utf-8')]), 'old_translation_leak')
        probes.append({'category': 'old-translation-excerpts', 'sha256': sha(raw)})
    manifest = dict(schema='ltr-context-v1', project_sha256=project_sha, book_hash=value['book_hash'],
                    source_digest=value['source_digest'], range=ids, inputs=inputs, omitted_counts=omitted, excluded_inputs=EXCLUDED,
                    probes=probes, created_at=ledger._now(), context_sha256=sha(encoded(context)))
    cid = 'ctx-' + digest(manifest)[:16]
    with project_lock(root):
        events = event_chain(root)
        folder = root / 'contexts' / cid
        need(not folder.exists(), 'context_already_exists')
        create(folder / 'context.json', context)
        create(folder / 'manifest.json', manifest)
        append_event(root, events, 'build', context_id=cid, manifest_sha256=sha(encoded(manifest)),
                     context_sha256=manifest['context_sha256'])
    return dict(status='built', context_id=cid, inputs=inputs)


def load_context(root, cid, events):
    identity(cid, 'ctx')
    need(any(e['kind'] == 'build' and e['context_id'] == cid for e in events), 'context_not_found')
    data = read_json(root / 'contexts' / cid / 'manifest.json')
    need(cid == 'ctx-' + digest(data)[:16], 'context_digest_mismatch')
    need(sha(file_path(root / 'contexts' / cid / 'context.json').read_bytes()) == data['context_sha256'],
         'context_digest_mismatch')
    return data


def load_frozen(root, fid, events):
    identity(fid, 'frz')
    need(any(e['kind'] == 'freeze' and e['freeze_id'] == fid for e in events), 'context_not_frozen')
    data = read_json(root / 'frozen' / (fid + '.json'))
    need(fid == 'frz-' + digest(data)[:16] and digest(data['candidates']) == data['candidates_sha256'],
         'context_not_frozen')
    load_context(root, data['context_id'], events)
    return data


def freeze(workspace_path, context_id, candidates, supersedes=None, reason=None):
    root = workspace(workspace_path)
    with project_lock(root):
        events = event_chain(root)
        manifest = load_context(root, context_id, events)
        rows = read_json(candidates)
        need(isinstance(rows, list) and all(isinstance(r, dict) and set(r) == {'id', 'target'}
             and ledger._text(r['target']) for r in rows)
             and [r['id'] for r in rows] == manifest['range'], 'context_candidates_invalid')
        prior = [e for e in events if e['kind'] == 'freeze' and e['context_id'] == context_id]
        need(bool(supersedes) == bool(prior) and (not supersedes or ledger._text(reason)),
             'context_supersedes_required')
        changed = []
        if supersedes:
            old = load_frozen(root, supersedes, events)
            need(old['context_id'] == context_id, 'context_supersedes_mismatch')
            changed = [a['id'] for a, b in zip(rows, old['candidates']) if a['target'] != b['target']]
        data = dict(context_id=context_id, candidates=rows, candidates_sha256=digest(rows),
                    frozen_at=ledger._now(), before_first_comparison=not any(
                        e['kind'] == 'compare' and e['context_id'] == context_id for e in events),
                    supersedes=supersedes, reason=reason, changed_ids=changed)
        fid = 'frz-' + digest(data)[:16]
        create(root / 'frozen' / (fid + '.json'), data)
        append_event(root, events, 'freeze', context_id=context_id, freeze_id=fid, freeze_sha256=sha(encoded(data)))
    return dict(status='frozen', freeze_id=fid, count=len(rows), before_first_comparison=data['before_first_comparison'])


def compare(workspace_path, frozen, project, old=(), output=None):
    root = workspace(workspace_path)
    with project_lock(root):
        events = event_chain(root)
        try:
            data = load_frozen(root, frozen, events)
        except (ValueError, OSError, KeyError):
            raise ContextError('context_not_frozen') from None
        manifest = load_context(root, data['context_id'], events)
        if output:
            output = file_path(output)
            need(not output.exists() and not output.is_relative_to(root), 'context_output_exists_or_reserved')
        # Deliberately durable before reading any comparison target, even if subsequent reads fail.
        append_event(root, events, 'compare', context_id=data['context_id'], freeze_id=frozen)
        value, _ = ledger._project(project)
        need(value['book_hash'] == manifest['book_hash'] and value['source_digest'] == manifest['source_digest'],
             'context_project_changed')
        targets = {u['id']: u['target'] for u in value['units']}
        rows = [dict(id=r['id'], candidate=r['target'], current_target=targets[r['id']]) for r in data['candidates']]
        result = rows if not old else dict(paragraphs=rows, old_translations=[
            file_path(p).read_text(encoding='utf-8') for p in old])
        if output:
            create(output, result)
            return dict(status='compared', count=len(rows), old_count=len(old))
        return result


def export(workspace_path, frozen, manifest, operator_exposure='not-claimed', write=False):
    root = workspace(workspace_path)
    need(operator_exposure in EXPOSURES, 'context_exposure_invalid')
    with project_lock(root):
        events = event_chain(root)
        data = load_frozen(root, frozen, events)
        context = load_context(root, data['context_id'], events)
        earliest, cursor = None, data
        while True:
            if cursor['before_first_comparison']:
                earliest = cursor
            if not cursor['supersedes']:
                break
            cursor = load_frozen(root, cursor['supersedes'], events)
        result = dict(required=True, candidate_ids=context['range'], frozen_before_comparison_ids=(
            [r['id'] for r in earliest['candidates']] if earliest else []),
            context_sources=list(dict.fromkeys(i['category'] for i in context['inputs'])),
            context_artifact_ids=[i['artifact_id'] for i in context['inputs']], excluded_inputs=EXCLUDED,
            operator_exposure_status=operator_exposure)
        if write:
            path = file_path(manifest)
            need(not path.is_relative_to(root), 'context_output_reserved')
            with project_lock(path.parent):
                atomic_write(path, ledger._replace_understanding(path.read_bytes(), result, 'source_first'))
    return result


def summaries(workspace_path, selected=None):
    root = workspace(workspace_path)
    with project_lock(root):
        events = event_chain(root)
        result = []
        for e in events:
            key = 'context_id' if e['kind'] == 'build' else 'freeze_id'
            if e['kind'] == 'compare' or (selected is not None and selected != e[key]):
                continue
            if e['kind'] == 'build':
                data = load_context(root, e[key], events)
                result.append(dict(id=e[key], sha256=e['manifest_sha256'], count=len(data['range']), at=data['created_at']))
            else:
                data = load_frozen(root, e[key], events)
                result.append(dict(id=e[key], sha256=e['freeze_sha256'], count=len(data['candidates']), at=data['frozen_at']))
        need(selected is None or bool(result), 'context_not_found')
        return dict(status='ok', event_chain='valid', event_count=len(events), records=result)


def main(argv=None):
    parser = ledger.QuietParser(prog='ltr context', description=BOUNDARY)
    commands = parser.add_subparsers(dest='mode', required=True)
    for mode in ('build', 'freeze', 'compare', 'export', 'list', 'show'):
        p = commands.add_parser(mode, description=BOUNDARY)
        p.add_argument('--workspace', type=Path, required=True)
        if mode in ('build', 'compare'):
            p.add_argument('--project', type=Path, required=True)
        if mode == 'build':
            p.add_argument('--range', nargs=2, required=True)
            for name in ('ledger', 'glossary', 'cards'):
                p.add_argument('--' + name, type=Path)
            for name in ('glossary-version', 'horizon'):
                p.add_argument('--' + name)
            for name in ('evidence', 'probe-old'):
                p.add_argument('--' + name, nargs='+', type=Path, default=[])
        if mode == 'freeze':
            p.add_argument('--context', required=True)
            p.add_argument('--candidates', type=Path, required=True)
            p.add_argument('--supersedes')
            p.add_argument('--reason')
        if mode in ('compare', 'export'):
            p.add_argument('--frozen', required=True)
        if mode == 'compare':
            p.add_argument('--old', nargs='+', type=Path, default=[])
            p.add_argument('--output', type=Path)
        if mode == 'export':
            p.add_argument('--manifest', type=Path, required=True)
            p.add_argument('--operator-exposure', choices=EXPOSURES, default='not-claimed')
            p.add_argument('--write', action='store_true')
        if mode == 'show':
            p.add_argument('--id', required=True)
    a = parser.parse_args(argv)
    try:
        if a.mode == 'build':
            result = build(a.workspace, a.project, a.range, ledger_path=a.ledger, evidence=a.evidence,
                           glossary_path=a.glossary, glossary_version=a.glossary_version, cards=a.cards,
                           horizon=a.horizon, probe_old=a.probe_old)
        elif a.mode == 'freeze':
            result = freeze(a.workspace, a.context, a.candidates, a.supersedes, a.reason)
        elif a.mode == 'compare':
            result = compare(a.workspace, a.frozen, a.project, a.old, a.output)
        elif a.mode == 'export':
            result = export(a.workspace, a.frozen, a.manifest, a.operator_exposure, a.write)
        else:
            result = summaries(a.workspace, getattr(a, 'id', None))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ContextError, ledger.LedgerError, glossary.GlossaryError, StorageError, ContractError) as exc:
        print(json.dumps(dict(status='rejected', code=str(exc))))
    except Exception:
        print('{"status":"rejected","code":"context_operation_failed"}')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
