"""Export verified private learning proposals without copying source paragraphs."""
from __future__ import annotations

from .storage import read_private_json as _read
import argparse
import json
from pathlib import Path
import re

from .storage import atomic_write, digest, private_root, write_json


CATEGORIES = {
    'characters': '人物与关系', 'storylines': '故事脉络', 'worldview': '世界观',
    'language_style': '语言风格', 'description_techniques': '描写手法',
    'translation_choices': '译名与翻译选择',
}
KINDS = {'source': '日文原文中的作者表现', 'translation': '现有中文译本的选择'}


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', value):
        raise ValueError('invalid_learning_identifier')
    return value


def _completed(root, folder, identifier):
    """Validate completed results, enforcing new envelope bindings when present.

    Legacy claims/results remain compatible but do not prove envelope integrity;
    callers must not interpret payload-only validation as an audited source map.
    """
    base = root / folder / _id(identifier)
    paths = [Path(str(base) + suffix) for suffix in ('.input.json', '.claim.json', '.result.json')]
    if not all(p.exists() for p in paths):
        return None
    job, claim, result = map(_read, paths)
    if (claim.get('status') != 'completed' or claim.get('result_digest') != digest(result)
            or claim.get('payload_digest') != digest(job['payload'])
            or result.get('input_digest') != digest(job['payload'])):
        return None
    if any(key in value for value in (claim, result) for key in ('envelope_digest', 'input_binding')):
        if any(value.get('envelope_digest') != digest(job)
               or value.get('input_binding') != 'job_envelope_v1' for value in (claim, result)):
            return None
    return job, result


def _leaf_records(root, manifest):
    valid, records = {}, {}
    for job_id in manifest['jobs']:
        completed = _completed(root, 'jobs', job_id)
        if completed is None:
            continue
        job, result = completed
        file_id = job['file_id']
        if file_id not in manifest['files'] or job_id not in manifest['files'][file_id]['jobs']:
            continue
        meta = manifest['files'][file_id]
        if job.get('source_hash') != meta['source_hash'] or job.get('kind') != meta['kind']:
            continue
        rows = job['payload']['source']
        original = job.get('original_source_ids', job.get('source_ids', []))
        if len(original) != len(rows) or len({r['id'] for r in rows}) != len(rows):
            continue
        mapping = {r['id']: p for r, p in zip(rows, original)}
        proposed = []
        for record in result.get('records', []):
            evidence = record.get('evidence_ids', [])
            if (record.get('category') not in CATEGORIES or not evidence
                    or not isinstance(record.get('name'), str)
                    or not isinstance(record.get('description'), str)
                    or record.get('certainty') not in {'explicit', 'inferred', 'uncertain'}
                    or not isinstance(record.get('aliases', []), list)
                    or any(not isinstance(alias, str) for alias in record.get('aliases', []))
                    or record.get('id') in records
                    or any(pid not in mapping for pid in evidence)):
                break
            item = {**record, 'source_kind': [job['kind']], 'evidence_trace': [
                {'leaf_record_id': record['id'], 'job_id': job_id, 'paragraph_id': pid,
                 'original_source_ids': mapping[pid] if isinstance(mapping[pid], list) else [mapping[pid]],
                 'file_id': file_id, 'source_hash': meta['source_hash'], 'source_kind': job['kind']}
                for pid in evidence]}
            proposed.append(item)
        else:
            valid[job_id] = proposed
            records.update({r['id']: r for r in proposed})
    return valid, records


def _merged_records(root, book_id, leaves):
    completed = _completed(root, 'merges', book_id)
    if completed is None:
        return None
    job, result = completed
    origins = job.get('origin_map', {})
    supplied = {r['id']: r for r in job['payload']['records']}
    if set(origins) != set(supplied) or set(origins.values()) != set(leaves):
        return None
    # Every merge input must still refer to the validated leaf content.
    for key, origin in origins.items():
        if origin not in leaves or key not in supplied:
            return None
        leaf = leaves[origin]
        expected = {k: v for k, v in leaf.items() if k not in {'source_kind', 'evidence_trace'}}
        expected.update(id=key, origin_kind=leaf['source_kind'][0])
        if supplied[key] != expected:
            return None
    out = []
    for record in result.get('records', []):
        evidence = record.get('evidence_ids', [])
        if (record.get('category') not in CATEGORIES or not evidence
                or any(key not in origins for key in evidence)):
            return None
        trace = [{**trace, 'merge_record_id': record['id'], 'merge_evidence_id': key}
                 for key in evidence for trace in leaves[origins[key]]['evidence_trace']]
        out.append({**record, 'source_kind': sorted({r['source_kind'] for r in trace}),
                    'evidence_trace': trace})
    return {'records': out, 'unresolved': result.get('unresolved', []),
            'coverage_note': result.get('coverage_note', '')}


def _text(value):
    # Keep model text inert in the generated Markdown.
    return str(value).replace('<', '&lt;').replace('>', '&gt;').replace('\r', '')


def _registry(registry_root, books, manifest):
    from .series_registry import initialize, register_book, register_background
    root = private_root(Path(registry_root))
    if not (root / 'series.json').exists():
        initialize(root, 'series-learning')
    registered = changed = skipped = 0
    for book in books:
        state = _read(root / 'series.json')
        old = state['books'].get(book['book_id'], {})
        meta = manifest['files'][book['source_file']]
        if book.get('publication_order') is not None:
            order, basis = book['publication_order'], 'publication'
        elif book.get('corpus_order') is not None:
            order, basis = book['corpus_order'], 'corpus_traversal'
        else:
            order, basis = old.get('publication_order'), old.get('order_basis', 'publication')
        if not order:
            skipped += 1
            continue
        register_book(root, book['book_id'], meta['source_hash'], order, meta['source_paragraphs'],
                      order_basis=basis)
        registered += 1
        if book['selection'] != 'merged':
            continue
        background_id = _id('learning_' + book['book_id'])
        content = {'format': 'series-learning-background-v1', 'title': book['title'],
                   'records': book['records'], 'coverage': book['coverage'],
                   'unresolved': book['unresolved'], 'coverage_note': book['coverage_note'],
                   'source_kind': 'evidence_trace_per_record', 'status': 'model_proposed',
                   'pair_alignment': 'edition_candidates_not_paragraph_alignment'}
        head = state.get('backgrounds', {}).get(background_id)
        if head:
            previous = _read(root / 'backgrounds' / background_id / f"revision-{head['revision']}.json")
            if (digest(previous) == head['digest'] and previous['content'] == content
                    and previous['source_digest'] == meta['source_hash']):
                continue
        register_background(root, background_id, book['book_id'], content)
        changed += 1
    return {'books_registered': registered, 'background_revisions_created': changed,
            'books_missing_order': skipped}


def export_learning(root, registry_root=None):
    root = private_root(Path(root))
    manifest = _read(root / 'learning-plan.json')
    valid, leaves = _leaf_records(root, manifest)
    books = []
    for original in manifest['books']:
        book = dict(original)
        ids = [job for file_id in [book['source_file']] + book['reference_files']
               for job in manifest['files'][file_id]['jobs']]
        selected_leaves = {r['id']: r for jid in ids for r in valid.get(jid, [])}
        merged = _merged_records(root, book['book_id'], selected_leaves) if all(j in valid for j in ids) else None
        book['selection'] = 'merged' if merged is not None else 'leaf'
        book['records'] = merged['records'] if merged is not None else list(selected_leaves.values())
        book['unresolved'] = merged['unresolved'] if merged is not None else []
        book['coverage_note'] = merged['coverage_note'] if merged is not None else '分块候选尚未合并；跨块分歧未消解。'
        book['coverage'] = {'completed_jobs': sum(j in valid for j in ids), 'planned_jobs': len(ids),
                            'status': 'complete_text_jobs' if all(j in valid for j in ids) else 'partial',
                            'not_paragraph_aligned': True}
        books.append(book)
    entries = [{**record, 'book_id': book['book_id'], 'book_title': book['title']}
               for book in books for record in book['records']]
    groups, aliases = {}, {}
    for record in entries:
        key = (record['category'], record['name'])
        group = groups.setdefault(key, {'category': key[0], 'canonical_name': key[1],
                                       'aliases': [], 'record_ids': [], 'book_ids': []})
        if record['id'] not in group['record_ids']:
            group['record_ids'].append(record['id'])
        group['book_ids'] = sorted(set(group['book_ids'] + [record['book_id']]))
        for name in record.get('aliases', []):
            if name not in group['aliases']:
                group['aliases'].append(name)
        for name in [record['name']] + record.get('aliases', []):
            aliases.setdefault((key[0], name), set()).add(key[1])
    collisions = [{'category': key[0], 'alias': key[1], 'canonical_candidates': sorted(names),
                   'status': 'unresolved_not_merged'} for key, names in aliases.items() if len(names) > 1]
    index = {'format': 'series-knowledge-index-v1', 'status': 'model_proposed',
             'identity_policy': 'exact_name_search_groups_not_confirmed_character_identity',
             'groups': list(groups.values()), 'alias_conflicts': collisions,
             'records': entries, 'books': [{k: v for k, v in b.items() if k != 'records'} for b in books]}
    output = root / '知识档案'
    write_json(output / 'knowledge-index.json', index)
    for category, title in CATEGORIES.items():
        lines = [f'# {title}', '', '模型抽取的候选档案；保留推断和分歧，尚未人工确认。', '']
        for book in books:
            lines.extend([f"## {_text(book['title'])}", '',
                          f"完成 {book['coverage']['completed_jobs']}/{book['coverage']['planned_jobs']} 个文本块；"
                          + ('采用卷级合并记录。' if book['selection'] == 'merged' else '当前为分块记录，可能重复或冲突，尚未卷级合并。'), ''])
            for record in book['records']:
                if record['category'] != category:
                    continue
                lines.extend([f"### {_text(record['name'])}", '', _text(record['description']), '',
                    f"别名：{_text('、'.join(record.get('aliases', [])) or '未记录')}；确定性：{_text(record.get('certainty', 'uncertain'))}；范围：{_text(record.get('scope', 'unknown'))}。", '',
                    '来源：' + '；'.join(KINDS.get(k, k) for k in record['source_kind']) + '。', '',
                    f"记录 ID：`{record['id']}`；证据 ID：" + ', '.join(f'`{eid}`' for eid in record['evidence_ids']) + '。', '',
                    '证据链：' + '；'.join(' → '.join(str(t[k]) for k in
                        ('merge_evidence_id', 'leaf_record_id', 'paragraph_id') if k in t)
                        + ' → ' + ', '.join(map(str, t['original_source_ids'])) for t in record['evidence_trace']) + '。', ''])
        atomic_write(output / f'{title}.md', '\n'.join(lines))
    summary = ['# 系列知识档案', '', '这是基于文本证据的知识学习，不是模型权重训练；所有记录仍为候选。', '',
        f"已验证完成 {len(valid)}/{len(manifest['jobs'])} 个文本块，涉及 {len(books)} 个候选配对卷。",
        '日文原文和现有中译分别阅读；候选配对及完成阅读均不等于逐段对齐，也不证明全系列已收齐。', '',
        '没有核验出版顺序的卷使用资料遍历序号（corpus_traversal）；不能据此推断真实出版顺序或故事发生顺序。完整背景不会因此截断。', '',
        '合并记录优先；没有有效合并的卷展示分块记录，并提示重复风险。证据链详见各页及 knowledge-index.json。', '',
        f'别名冲突 {len(collisions)} 项，保留不同记录，不自动合并人物身份。', '']
    for book in books:
        summary.append(f"- {_text(book['title'])}：{book['coverage']['completed_jobs']}/{book['coverage']['planned_jobs']} 块；{len(book['records'])} 条候选；{book['selection']}。")
    for book in books:
        summary.extend(['', f"## {_text(book['title'])}：覆盖限制与待核查问题", '', _text(book['coverage_note']), ''])
        summary.extend('- ' + _text(issue) for issue in book['unresolved'])
    atomic_write(output / '总览.md', '\n'.join(summary) + '\n')
    result = {'status': 'exported', 'books': len(books), 'completed_jobs': len(valid),
              'planned_jobs': len(manifest['jobs']), 'records': len(entries),
              'unique_record_ids':len({r['id'] for r in entries}),
              'merged_books': sum(b['selection'] == 'merged' for b in books), 'alias_conflicts': len(collisions)}
    if registry_root is not None:
        result['registry'] = _registry(registry_root, books, manifest)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--registry', type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(export_learning(args.project, args.registry)), flush=True)
    except Exception:
        print(json.dumps({'status': 'stopped', 'code': 'learning_export_failed'}), flush=True)
        raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
