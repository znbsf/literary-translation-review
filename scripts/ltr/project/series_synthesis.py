"""Evidence-linked cross-book proposals from verified private book summaries."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path

from .dimension_cycle import call_model, CycleError
from .learning_export import CATEGORIES, KINDS, _completed, _leaf_records, _merged_records, _read, _text
from .series_learning import immutable, validate
from .storage import atomic_write, digest, private_root, project_lock, write_json


PROMPT = '''为后续全系列翻译生成当前类别的跨卷知识档案。输入是经过出处核验的卷级候选分析，不是已确认事实。
仅使用本次 records，所有结论引用实际 K 编号。不能调用作品常识补充未知信息，也不能引用未提供的记录。
优先形成可执行且有适用边界的翻译背景：人物关系与变化、事件因果与主线、世界规则及例外、作者语言习惯、描写手法、译名与译法。
只输出 category 指定的一类；避免把其它类别补成常识百科。跨卷别名同一性仍是待核验提案，不因相似名字自动认定同一人物。
原文作者的表现、中文译者的选择必须区分：source_kind=translation 的记录不能证明日文作者习惯；混合来源明确分开叙述并保留待核验条件。
矛盾、不确定、角色认知和叙述者推断不得自动抹掉或升格为事实。将冲突双方的 K 编号保留在相关项中，未解决问题列入 unresolved。
books.source_coverage_notes 保留逐块阅读时注明的未知与限制，不能因卷级摘要没有复述就认为问题已解决；它们只作为限制，不作为新增事实的证据。
输入覆盖只是给定资料；corpus_traversal 是遍历顺序，不是出版或故事顺序。日期无法确定就写未知。不得声称已经掌握全系列。
不得复制长篇原文或中译，不生成新译文；描述是分析与规则，例句用证据ID指向原记录。
返回JSON：{"records":[{"category":"输入category","name":"短标签","description":"跨卷分析、具体变化及翻译影响、限制与分歧",
"aliases":[],"certainty":"explicit|inferred|uncertain","scope":"series_candidate","evidence_ids":["K00000"]}],
"unresolved":["需进一步核验的问题"],"coverage_note":"本次实际覆盖和不足"}。
每项至少1条证据，最多100条记录，输出控制在8000中文字符以内。没有足够证据就保留空records并解释限制。'''


def _verified_index(root, allow_partial):
    manifest = _read(root / 'learning-plan.json')
    index = _read(root / '知识档案' / 'knowledge-index.json')
    valid, _ = _leaf_records(root, manifest)
    accepted, books = [], []
    for book in manifest['books']:
        ids = [job for file_id in [book['source_file']] + book['reference_files']
               for job in manifest['files'][file_id]['jobs']]
        if not all(job in valid for job in ids) or book.get('reference_import_complete') is False:
            continue
        leaves = {r['id']: r for job in ids for r in valid[job]}
        merged = _merged_records(root, book['book_id'], leaves)
        if merged is None:
            continue
        expected = [{**r, 'book_id': book['book_id'], 'book_title': book['title']}
                    for r in merged['records']]
        exported = [r for r in index['records'] if r.get('book_id') == book['book_id']]
        exported_book = next((b for b in index['books'] if b['book_id'] == book['book_id']), None)
        if exported_book is None or exported_book.get('selection') != 'merged':
            continue
        if digest(exported) != digest(expected):
            raise CycleError('stale_knowledge_index')
        accepted.extend(expected)
        books.append({'book_id': book['book_id'], 'title': book['title'],
                      'unresolved': merged['unresolved'], 'coverage_note': merged['coverage_note'],
                      'source_coverage_notes': [{'job_id': job_id,
                          'note': _completed(root, 'jobs', job_id)[1].get('coverage_note', '')}
                          for job_id in ids],
                      'order_basis': book.get('order_basis', 'unknown')})
    if not books:
        raise CycleError('no_verified_book_merges')
    complete = len(books) == len(manifest['books'])
    if not complete and not allow_partial:
        raise CycleError('incomplete_book_merges')
    coverage = {'merged_books': len(books), 'planned_books': len(manifest['books']),
                'status': 'planned_books_complete' if complete else 'partial',
                'import_problems': len(manifest.get('problems', [])),
                'not_full_series_claim': True, 'not_paragraph_aligned': True,
                'plan_digest': digest(manifest)}
    return accepted, books, coverage


def prepare(root, *, allow_partial=False):
    root = private_root(Path(root))
    records, books, coverage = _verified_index(root, allow_partial)
    folder = root / '跨卷档案'
    inputs = {}
    for category in CATEGORIES:
        selected = [r for r in records if r['category'] == category]
        origins = {f'K{i:05d}': {'book_id': r['book_id'], 'record_id': r['id'],
                    'record_digest': digest(r), 'source_kind': r['source_kind'],
                    'evidence_trace': r['evidence_trace']} for i, r in enumerate(selected)}
        # No source paragraphs, source paths or full evidence payloads enter the model.
        payload = {'category': category, 'coverage': coverage, 'books': books,
                   'records': [{**{k: r[k] for k in ('category', 'name', 'description', 'certainty',
                        'scope', 'aliases', 'source_kind', 'book_id', 'book_title') if k in r}, 'id': key}
                               for key, r in zip(origins, selected)]}
        job = {'payload': payload, 'origin_map': origins, 'format': 'series-synthesis-input-v1'}
        immutable(folder / f'{category}.input.json', job)
        inputs[category] = digest(job)
    immutable(folder / 'synthesis-plan.json', {'categories': list(CATEGORIES),
        'coverage': coverage, 'input_digests': inputs, 'automatic_retries': 0, 'planned_calls': 6})
    return coverage


def _result(folder, category, job):
    claim_path, result_path = folder / f'{category}.claim.json', folder / f'{category}.result.json'
    if not claim_path.exists() or not result_path.exists():
        return None
    claim, result = _read(claim_path), _read(result_path)
    if (claim.get('status') != 'completed' or claim.get('input_digest') != digest(job)
            or claim.get('result_digest') != digest(result) or result.get('input_digest') != digest(job)):
        return None
    return result


def execute(root, category, model):
    if category not in CATEGORIES:
        raise CycleError('invalid_synthesis_category')
    folder = private_root(Path(root)) / '跨卷档案'
    job = _read(folder / f'{category}.input.json')
    plan = _read(folder / 'synthesis-plan.json')
    if digest(job) != plan['input_digests'][category]:
        raise CycleError('synthesis_input_changed')
    claim_path = folder / f'{category}.claim.json'
    if claim_path.exists():
        old = _result(folder, category, job)
        return {'category': category, 'status': 'reused' if old is not None else 'requires_resolution',
                'records': len(old['records']) if old is not None else 0}
    claim = {'status': 'reserved', 'model': model, 'input_digest': digest(job),
             'attempts': 1, 'automatic_retries': 0}
    immutable(claim_path, claim)
    try:
        candidate, seconds = call_model(model, PROMPT, job['payload'], timeout=420)
        write_json(folder / f'{category}.candidate.json', candidate)
        validate(candidate, set(job['origin_map']))
        if (not isinstance(candidate.get('unresolved', []), list)
                or any(not isinstance(v, str) for v in candidate.get('unresolved', []))):
            raise CycleError('invalid_synthesis_unresolved')
        for i, record in enumerate(candidate['records']):
            if (record['category'] != category or record.get('scope') != 'series_candidate'
                    or not isinstance(record.get('aliases', []), list)
                    or any(not isinstance(a, str) for a in record.get('aliases', []))):
                raise CycleError('invalid_synthesis_scope')
            refs = [{**job['origin_map'][key], 'synthesis_evidence_id': key}
                    for key in record['evidence_ids']]
            record.update(id=f'{category}:S{i:04d}', status='model_proposed',
                          source_kind=sorted({kind for ref in refs for kind in ref['source_kind']}),
                          evidence_refs=refs, alias_identity_status='proposed_not_confirmed')
        result = {**candidate, 'input_digest': digest(job), 'coverage': job['payload']['coverage'],
                  'status': 'model_proposed', 'kind': 'cross_book_synthesis_not_weight_training'}
        write_json(folder / f'{category}.result.json', result)
        claim.update(status='completed', result_digest=digest(result), seconds=seconds,
                     records=len(result['records']))
    except Exception as error:
        claim.update(status='failed', code=str(error) if isinstance(error, CycleError) else type(error).__name__)
    write_json(claim_path, claim)
    return {'category': category, 'status': claim['status'], 'records': claim.get('records', 0)}


def export(root):
    folder = private_root(Path(root)) / '跨卷档案'
    plan = _read(folder / 'synthesis-plan.json')
    results, statuses = {}, {}
    for category, title in CATEGORIES.items():
        job = _read(folder / f'{category}.input.json')
        if digest(job) != plan['input_digests'][category]:
            raise CycleError('synthesis_input_changed')
        result = _result(folder, category, job)
        statuses[category] = 'completed' if result is not None else 'incomplete'
        lines = [f'# 跨卷{title}', '', '候选知识档案，尚未人工确认；别名关联不构成人物身份确认。', '',
            f"覆盖 {plan['coverage']['merged_books']}/{plan['coverage']['planned_books']} 个计划卷；不代表全部已出版作品。",
            '资料遍历序号不代表出版顺序或故事时间。原文作者表现与中文译本选择分别归因。', '']
        if result is None:
            lines.append('本类汇总未完成或校验未通过。')
        else:
            results[category] = result
            lines.extend([_text(result.get('coverage_note', '')), ''])
            for record in result['records']:
                lines.extend([f"## {_text(record['name'])}", '', _text(record['description']), '',
                    '来源：' + '；'.join(KINDS.get(k, k) for k in record['source_kind']) + '。', '',
                    f"确定性：{_text(record['certainty'])}；别名候选：{_text('、'.join(record.get('aliases', [])) or '无')}。", '',
                    '证据：' + '；'.join(f"{ref['synthesis_evidence_id']} → {ref['book_id']} / {ref['record_id']}"
                        for ref in record['evidence_refs']) + '。完整叶块和段落出处见 index.json。', ''])
            lines.extend(['## 待核查分歧', ''] + ['- ' + _text(v) for v in result.get('unresolved', [])])
        atomic_write(folder / f'{title}.md', '\n'.join(lines) + '\n')
    write_json(folder / 'index.json', {'format': 'series-synthesis-index-v1', 'status': 'model_proposed',
        'coverage': plan['coverage'], 'categories': statuses, 'results': results,
        'source_kind_policy': 'attributed_per_evidence_not_interchangeable',
        'alias_identity_policy': 'proposed_not_confirmed'})
    return {'status': 'exported', 'completed_categories': len(results), 'planned_categories': 6,
            'records': sum(len(r['records']) for r in results.values()),
            'merged_books': plan['coverage']['merged_books'], 'planned_books': plan['coverage']['planned_books']}


def run(root, model, workers=3, *, allow_partial=False):
    root = private_root(Path(root))
    if not 1 <= workers <= 4:
        raise CycleError('invalid_synthesis_workers')
    with project_lock(root / '跨卷档案'):
        prepare(root, allow_partial=allow_partial)
        def dispatch(category):
            if (root / 'STOP').exists():
                return {'category': category, 'status': 'not_dispatched', 'records': 0}
            return execute(root, category, model)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(dispatch, category) for category in CATEGORIES]
            for future in as_completed(futures):
                print(json.dumps(future.result()), flush=True)
        return export(root)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--allow-partial', action='store_true')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run(args.project, args.model, args.workers, allow_partial=args.allow_partial)), flush=True)
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'code': str(error) if isinstance(error, CycleError)
                         else 'series_synthesis_failed'}), flush=True)
        raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
