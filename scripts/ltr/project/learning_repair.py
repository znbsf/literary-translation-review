"""Explicit, bounded re-grounding of invalid records; retain failed attempts."""
from copy import deepcopy
import argparse
import json
from pathlib import Path
import re
from .dimension_cycle import call_model, validate_quotes, CycleError
from .series_learning import read, validate, immutable
from .storage import private_root, write_json, digest

PROMPT='''重新核查候选记录的证据。给定记录之前引用了不存在的段落ID，不能把编号简单改成相邻编号。
必须返回原文重新核查每个判断；不支持的记录置null，不凭作品常识保留。
只处理指定bad_records，不生成其他记录。返回{"replacements":[{"index":实际索引,"record":修正后的原格式完整记录或null,
"evidence":[{"paragraph_id":"给定P编号","quote":"该段精确连续短引，最多80字符"}],"reason":"处理依据"}]}。
非null记录的evidence_ids必须与evidence中的paragraph_id集合一致。保持category/名称/范围意义，不能借修编号新增事实。
null记录说明不支持之处，不必提供引文。不输出整篇文本。'''


def apply_replacements(candidate,repaired,bad,rows):
    values=repaired.get('replacements',[])
    if sorted(v.get('index',-1) for v in values)!=sorted(bad):
        raise CycleError('repair_index_mismatch')
    by_index={v['index']:v for v in values}; ids={p['id'] for p in rows}
    out=deepcopy(candidate); result=[]
    for index,record in enumerate(out['records']):
        if index in by_index:
            replacement=by_index[index]; record=replacement.get('record')
            if record is None: continue
            validate({'records':[record]},ids)
            evidence=replacement.get('evidence',[])
            validate_quotes(evidence,rows)
            if (set(record['evidence_ids'])!={v['paragraph_id'] for v in evidence}
                    or any(len(v['quote'])>80 for v in evidence)):
                raise CycleError('repair_evidence_mismatch')
        result.append(record)
    out['records']=result
    validate(out,ids)
    return out


def repair(root,job_id,model):
    root=private_root(Path(root))
    if not re.fullmatch(r'F[0-9a-f]{20}_[0-9]{4}',job_id):raise CycleError('invalid_repair_job')
    base=root/'jobs'; job=read(base/f'{job_id}.input.json'); old=read(base/f'{job_id}.claim.json')
    if old.get('status')!='failed' or old.get('code')!='invalid_learning_evidence':
        raise CycleError('not_an_evidence_failure')
    candidate=read(base/f'{job_id}.candidate.json'); rows=job['payload']['source']; ids={p['id'] for p in rows}; bad=[]
    for index,item in enumerate(candidate['records']):
        try:validate({'records':[item]},ids)
        except CycleError:bad.append(index)
    if not 1<=len(bad)<=3:raise CycleError('bulk_repair_required')
    folder=root/'repairs'/job_id
    if (folder/'request.json').exists():raise CycleError('repair_already_attempted')
    payload={'source':rows,'edition_kind':job['kind'],'bad_records':[{'index':i,'record':candidate['records'][i]} for i in bad]}
    immutable(folder/'request.json',{'payload':payload,'payload_digest':digest(payload),'attempts':1,'automatic_retries':0})
    result,seconds=call_model(model,PROMPT,payload,timeout=420)
    write_json(folder/'candidate.json',result)
    fixed=apply_replacements(candidate,result,bad,rows)
    history=root/'attempt-history'/job_id
    write_json(history/'attempt-1.claim.json',old)
    write_json(history/'attempt-1.candidate.json',candidate)
    for index,record in enumerate(fixed['records']):record['id']=f'{job_id}:R{index:04d}'
    fixed.update(status='model_proposed',input_digest=digest(job['payload']),knowledge_basis=job['kind'],
                 evidence_repair={'records_checked':len(bad),'repair_digest':digest(result)})
    write_json(base/f'{job_id}.result.json',fixed)
    updated={**old,'status':'completed','attempts':2,'records':len(fixed['records']),
             'result_digest':digest(fixed),'repair_seconds':seconds,'repair_payload_digest':digest(payload)}
    updated.pop('code',None)
    write_json(base/f'{job_id}.claim.json',updated)
    write_json(folder/'result.json',{'status':'completed','repaired_record_indices':bad,'result_digest':digest(fixed)})
    return {'status':'completed','job':job_id,'records':len(fixed['records']),'repaired_records':len(bad)}


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--project',type=Path,required=True);p.add_argument('--job',required=True);p.add_argument('--model',required=True)
    a=p.parse_args(argv)
    try:print(json.dumps(repair(a.project,a.job,a.model)),flush=True)
    except Exception as e:
        print(json.dumps({'status':'repair_failed','code':str(e) if isinstance(e,CycleError) else type(e).__name__}),flush=True)
        raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
