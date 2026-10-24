"""Private corpus reading, evidence-linked knowledge proposals, and coverage ledger.

This learns reusable records, not model weights. Never print content or paths.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import threading
from pathlib import Path
from .storage import private_root, write_json, digest, atomic_write, project_lock, StorageError
from .dimension_cycle import call_model, CycleError

CATEGORIES = ('characters','storylines','worldview','language_style','description_techniques','translation_choices')
PROMPT = '''你在为全系列翻译建立知识档案，当前任务是实际通读给定文本块并抽取知识，不是翻译正文。
优先人物、人物关系/变化、事件因果及故事脉络、世界观及其限制；其次语言风格、描写手法、译名和译法选择。
原文与中译将分别完整读取，再进行跨块、跨卷合并。不要假装本块已覆盖全书，不凭作品常识补未给出的设定。
每个事实/判断都用给定段落ID定位。明确人物认知、旁人传闻、叙述者判断与作品确认的事实；不要把推断升格为事实。
人物记录包括出现过的别名、口吻、关系和变化；事件区分叙述先后和故事发生顺序，无法确定日期就明确未知。
世界观包括怪异/机制/规则的作用条件、例外和不可靠解释。风格分别归因于原文作者表现或中文译者选择，不能混为一谈。
其他段落例句可保存为出处，但不要输出长篇原文或译文，不在此生成新译文。
返回JSON：{"records":[{"category":"characters|storylines|worldview|language_style|description_techniques|translation_choices",
"name":"人物/事件/规则/手法的短标签","description":"具体分析及对翻译的影响，不写空泛套话",
"aliases":["明确出现的别名，可空"],"certainty":"explicit|inferred|uncertain","scope":"scene|book|series_candidate",
"evidence_ids":["实际P编号"]}],"coverage_note":"本块无法判断什么"}。
按重要性列出最多45项，优先保证人物/故事/世界观记录，不强行虚构不存在的类别。每项至少1个最多6个证据ID。
描述用中文，整体控制在4500中文字符以内。'''

MERGE_PROMPT = '''将已通读原文和现有中译得到的候选档案合并为当前卷的翻译知识档案。
优先人物关系及变化、故事因果和时间顺序、世界观；其次语言风格、描写手法、已有译名的稳定或分歧。
只能依据输入记录，不调用作品常识。中日版分卷可能不同，不同记录相互冲突时保留分歧，不虚构解决。
区分作者日文习惯和中文译本选择；不同表述不自动认定误译。原文/译文分别阅读不代表逐段双语对齐。
返回 {"records":[{"category":"characters|storylines|worldview|language_style|description_techniques|translation_choices",
"name":"短标签","description":"合并分析，含适用范围与对翻译的影响","aliases":[],
"certainty":"explicit|inferred|uncertain","scope":"book|series_candidate","evidence_ids":["输入K编号"]}],
"unresolved":["需要返回原文核查的问题"],"coverage_note":"真实覆盖限制"}。
最多65项、7000中文字符。不能把候选标成人工确认，优先保存具体信息，不能只列目录。'''


def read(path):
    private_root(Path(path))
    return json.loads(Path(path).read_text(encoding='utf-8'))


def immutable(path, value):
    if path.exists():
        if read(path) != value: raise CycleError('learning_input_changed')
    else: write_json(path,value)


def validate(result, ids):
    records=result.get('records')
    if not isinstance(records,list) or len(records)>100:
        raise CycleError('invalid_learning_records')
    for item in records:
        if (not isinstance(item,dict) or item.get('category') not in CATEGORIES
                or item.get('certainty') not in {'explicit','inferred','uncertain'}
                or not isinstance(item.get('name'),str) or not item['name'].strip()
                or not isinstance(item.get('description'),str) or not item['description'].strip()
                or not isinstance(item.get('evidence_ids'),list) or not item['evidence_ids']
                or any(pid not in ids for pid in item['evidence_ids'])):
            raise CycleError('invalid_learning_evidence')
    return len(records)


def prepare(root, pairing, target_chars=30000):
    from .corpus_import import import_book, import_text, partition_learning
    root=private_root(Path(root)); files={}; books=[]; problems=[]
    candidates=read(pairing)['books']
    for corpus_order,book in enumerate(candidates,1):
        if not book.get('reference_paths'):
            problems.append({'book_id':book['book_id'],'code':'no_reference_candidate'})
            continue
        refs=[]; src=None
        paths=[('source',book['source_path'])]+[('translation',p) for p in book['reference_paths']]
        for kind,rawpath in paths:
            path=Path(rawpath)
            with path.open('rb') as stream: checksum=hashlib.file_digest(stream,'sha256').hexdigest()
            file_id='F'+checksum[:20]
            if file_id not in files:
                try:
                    encoding=book.get('reference_encodings',{}).get(str(path),'utf-8-sig')
                    imported=import_book(path) if path.suffix.lower()=='.epub' else import_text(path,encoding=encoding)
                    parts=partition_learning(imported,target_chars=target_chars)
                    immutable(root/'imports'/f'{file_id}.json',imported)
                    file_jobs=[]
                    for index,chunk in enumerate(parts['chunks']):
                        job_id=f'{file_id}_{index:04d}'
                        rows=[{'id':f'P{i:05d}','text':row['text']} for i,row in enumerate(chunk['paragraphs'])]
                        payload={'edition_kind':kind,'book_title':book['title'],'source':rows}
                        job={'job_id':job_id,'file_id':file_id,'kind':kind,'source_hash':checksum,
                             'source_ids':[r['id'] for r in chunk['paragraphs']],
                             'characters':sum(len(r['text']) for r in rows),'payload':payload}
                        immutable(root/'jobs'/f'{job_id}.input.json',job)
                        file_jobs.append(job_id)
                    files[file_id]={'file_id':file_id,'source_hash':checksum,'kind':kind,'jobs':file_jobs,
                                    'source_paragraphs':len(imported['paragraphs']),
                                    'characters':sum(len(p['text']) for p in imported['paragraphs']),
                                    'coverage':parts['coverage'],'filename':path.name}
                except Exception as error:
                    from .epub import EpubError
                    problems.append({'book_id':book['book_id'],'file_id':file_id,
                                     'code':str(error) if isinstance(error,EpubError) else type(error).__name__})
                    continue
            if kind=='source': src=file_id
            else: refs.append(file_id)
        if src and refs:
            books.append({'book_id':book['book_id'],'title':book['title'],'source_file':src,
                          'reference_files':refs,'pairing_status':book.get('pairing_status','candidate'),
                          'corpus_order':corpus_order,'order_basis':'corpus_traversal',
                          'expected_reference_count':len(book['reference_paths']),
                          'reference_import_complete':len(refs)==len(book['reference_paths'])})
    jobs=[j for f in files.values() for j in f['jobs']]
    manifest={'format':'series-learning-plan-v1','files':files,'books':books,'jobs':jobs,'problems':problems,
              'planned_calls':len(jobs)+len(books),'automatic_retries':0,
              'learning_mode':'evidence_linked_records_not_weight_training',
              'pair_alignment':'edition_candidates_not_paragraph_alignment'}
    immutable(root/'learning-plan.json',manifest)
    return {'files':len(files),'books':len(books),'jobs':len(jobs),'problems':len(problems),
            'characters':sum(f['characters'] for f in files.values()),'planned_calls':manifest['planned_calls']}


def execute(root,job_id,model, *, merge=False):
    """Reserve one job across processes before checking or creating its claim."""
    root=private_root(Path(root)); kind='merges' if merge else 'jobs'
    lock_id=hashlib.sha256(job_id.encode('utf-8')).hexdigest()
    try:
        with project_lock(root/kind/'.dispatch-locks'/lock_id):
            return _execute(root,job_id,model,merge=merge)
    except StorageError as error:
        if str(error)!='project_locked': raise
        return {'job':job_id,'status':'in_progress','records':0}


def _execute(root,job_id,model, *, merge=False):
    """Caller owns the per-job lock. Legacy claims stay payload-only evidence."""
    kind='merges' if merge else 'jobs'; folder=root/kind
    job=read(folder/f'{job_id}.input.json'); payload=job['payload']
    claim_path=folder/f'{job_id}.claim.json'; output=folder/f'{job_id}.result.json'
    if claim_path.exists():
        from .learning_export import _completed
        claim=read(claim_path)
        if _completed(root,kind,job_id) is not None:
            return {'job':job_id,'status':'reused','records':claim['records'],
                    'input_binding':claim.get('input_binding','legacy_payload_only')}
        return {'job':job_id,'status':'requires_resolution'}
    claim={'status':'reserved','model':model,'payload_digest':digest(payload),
           'envelope_digest':digest(job),'input_binding':'job_envelope_v1',
           'attempts':1,'automatic_retries':0}
    immutable(claim_path,claim)
    try:
        result,seconds=call_model(model,MERGE_PROMPT if merge else PROMPT,payload,timeout=420)
        write_json(folder/f'{job_id}.candidate.json',result)
        ids={r['id'] for r in payload['records' if merge else 'source']}
        count=validate(result,ids)
        for index,record in enumerate(result['records']): record['id']=f'{job_id}:R{index:04d}'
        result.update(status='model_proposed',input_digest=digest(payload),
                      envelope_digest=digest(job),input_binding='job_envelope_v1',
                      knowledge_basis='provided_records' if merge else job['kind'])
        write_json(output,result)
        claim.update(status='completed',seconds=seconds,result_digest=digest(result),records=count)
    except Exception as error:
        claim.update(status='failed',code=str(error) if isinstance(error,CycleError) else type(error).__name__)
    write_json(claim_path,claim)
    return {'job':job_id,'status':claim['status'],'records':claim.get('records',0)}


def prepare_merges(root,manifest):
    from .learning_export import _completed
    ready=[]
    for book in manifest['books']:
        records=[]; missing=False
        for file_id in [book['source_file']]+book['reference_files']:
            for job_id in manifest['files'][file_id]['jobs']:
                completed=_completed(root,'jobs',job_id)
                if completed is None: missing=True; continue
                for record in completed[1]['records']:
                    records.append({**record,'origin_kind':manifest['files'][file_id]['kind']})
        if missing: continue
        # K IDs allow short exact references; full origin mapping stays private.
        origin={f'K{i:05d}':r['id'] for i,r in enumerate(records)}
        payload={'title':book['title'],'pairing_status':book['pairing_status'],
                 'records':[{**r,'id':key} for key,r in zip(origin,records)]}
        immutable(root/'merges'/f"{book['book_id']}.input.json",{'payload':payload,'origin_map':origin})
        ready.append(book['book_id'])
    return ready


def status(root):
    manifest=read(root/'learning-plan.json'); counts={}; total_records=0
    for kind in ('jobs','merges'):
        values=[read(p) for p in (root/kind).glob('*.claim.json')]
        counts[kind]={s:sum(v['status']==s for v in values) for s in ('reserved','completed','failed')}
        total_records+=sum(v.get('records',0) for v in values)
    out={'planned_jobs':len(manifest['jobs']),'planned_books':len(manifest['books']),
         'counts':counts,'records_including_merged':total_records,'import_problems':len(manifest['problems'])}
    write_json(root/'progress.json',out)
    return out


def run(root,model,workers=3):
    root=private_root(Path(root))
    if not 1<=workers<=8: raise CycleError('invalid_learning_workers')
    with project_lock(root):
        manifest=read(root/'learning-plan.json')
        halt=threading.Event(); guard=threading.Lock(); failures=0
        def dispatch(job_id,merge):
            nonlocal failures
            if halt.is_set() or (root/'STOP').exists():
                return {'job':job_id,'status':'not_dispatched'}
            outcome=execute(root,job_id,model,merge=merge)
            with guard:
                failures=failures+1 if outcome['status']=='failed' else 0
                if failures>=3: halt.set()
            return outcome
        for phase,ids in [('jobs',manifest['jobs']),('merges',None)]:
            if halt.is_set() or (root/'STOP').exists(): break
            if ids is None: ids=prepare_merges(root,manifest)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures=[pool.submit(dispatch,j,phase=='merges') for j in ids]
                for future in as_completed(futures):
                    result=future.result(); status(root)
                    if result['status'] not in {'not_dispatched','reused'}:
                        print(json.dumps({'phase':phase,**result}),flush=True)
        from .learning_export import export_learning
        export_learning(root,root/'registry')
        return status(root)


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare','run','status'])
    p.add_argument('--project',type=Path,required=True);p.add_argument('--pairing',type=Path)
    p.add_argument('--model');p.add_argument('--workers',type=int,default=3);p.add_argument('--chunk-chars',type=int,default=30000)
    a=p.parse_args(argv)
    try:
        if a.command=='prepare': result=prepare(a.project,a.pairing,a.chunk_chars)
        elif a.command=='run': result=run(a.project,a.model,a.workers)
        else: result=status(a.project)
        print(json.dumps(result),flush=True)
    except Exception as error:
        print(json.dumps({'status':'stopped','code':str(error) if isinstance(error,CycleError) else type(error).__name__}),flush=True)
        raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
