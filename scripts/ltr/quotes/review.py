"""Private second-model review of source-bound unquoted dialogue candidates."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
import urllib.error
from ltr.quotes.batch import is_unsupported_response_content, is_redirect_rejection, digest, request_model, failure_metadata, unique_source_span_start
from ltr.library.review_queue import fingerprint

SYSTEM = """你是第二位独立的文学语料复核员。判断给出的未标引号片段是否真的是人物说话、心语或书面引用，普通叙述应 reject，证据不足 uncertain。
独立判断每条，不因提取模型选择了它而接受。不要推断作者未提供的信息。
只输出 JSON {"reviews":[{"candidate_id":"给定ID","decision":"keep|reject|uncertain","kind":"spoken|thought|written|uncertain","basis":"direct_speech|narration|thought|written_quote|insufficient_context|identity_ambiguous"}]}。
必须完整且只覆盖给定候选ID一次，不输出原文或额外说明。keep 仍只是机器候选，不表示人工通过。"""


def validate_review(payload, ids):
    if not isinstance(payload, dict) or set(payload) != {'reviews'} or not isinstance(payload['reviews'], list):
        raise ValueError('invalid_review_contract')
    rows = payload['reviews']
    if len(rows) != len(ids) or {r.get('candidate_id') for r in rows if isinstance(r, dict)} != set(ids):
        raise ValueError('inexact_review_coverage')
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'candidate_id','decision','kind','basis'}:
            raise ValueError('invalid_review_contract')
        if row['decision'] not in {'keep','reject','uncertain'} or row['kind'] not in {'spoken','thought','written','uncertain'}:
            raise ValueError('invalid_review_contract')
        if row['basis'] not in {'direct_speech','narration','thought','written_quote','insufficient_context','identity_ambiguous'}:
            raise ValueError('invalid_review_contract')
    return rows


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--model',required=True)
    parser.add_argument('--max-calls',type=int,default=4)
    parser.add_argument('--batch-size',type=int,default=12)
    parser.add_argument('--start',type=int,default=0)
    parser.add_argument('--shards',type=int,default=1)
    parser.add_argument('--shard-index',type=int,default=0)
    parser.add_argument('--exclude-reviewed',type=Path)
    parser.add_argument('--execute',action='store_true',required=True)
    args=parser.parse_args(argv)
    try:
        if not 1 <= args.max_calls <= 100 or not 1 <= args.batch_size <= 12 or args.start < 0 or not 1 <= args.shards <= 8 or not 0 <= args.shard_index < args.shards:
            raise ValueError('invalid_budget')
        db=args.db.resolve(strict=True);output=args.out.resolve()
        if output.exists() or output == db.parent or db.parent in output.parents:
            raise ValueError('invalid_output')
        baseline=fingerprint(db)
        con=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)
        con.row_factory=sqlite3.Row
        try:
            con.execute('pragma query_only=on')
            candidates=[]
            for file in sorted(args.input.rglob('batch-*.json')):
                batch=json.loads(file.read_text(encoding='utf-8'))
                if batch.get('database_sha256') != baseline:
                    raise ValueError('stale_candidate_source')
                for row in batch['candidates']:
                    source=con.execute('select edition_id,ordinal,text from paragraphs where edition_id=? and id=?',(batch['edition_id'],row['paragraph_id'])).fetchone()
                    if (source is None or source['text'][row['start']:row['end']] != row['text']
                            or unique_source_span_start(source['text'], row['text']) != row['start']):
                        raise ValueError('changed_source_span')
                    if hashlib.sha256(source['text'].encode()).hexdigest() != row['paragraph_sha256']:
                        raise ValueError('changed_source_paragraph')
                    context=[dict(r) for r in con.execute('select ordinal,text from paragraphs where edition_id=? and ordinal between ? and ? order by ordinal',(source['edition_id'],max(0,source['ordinal']-1),source['ordinal']+1))]
                    candidate_id=digest({'edition_id':batch['edition_id'],'paragraph_id':row['paragraph_id'],'start':row['start'],'end':row['end'],'source':baseline})[:16]
                    candidates.append({'candidate_id':candidate_id,'candidate':row,'context':context})
        finally:con.close()
        if len({r['candidate_id'] for r in candidates}) != len(candidates):
            raise ValueError('duplicate_candidate')
        already=set()
        if args.exclude_reviewed:
            for file in sorted(args.exclude_reviewed.rglob('review-*.json')):
                previous=json.loads(file.read_text(encoding='utf-8'))
                if previous.get('database_sha256') != baseline:
                    raise ValueError('stale_candidate_source')
                already.update(str(row['candidate_id'])[:16] for row in previous['reviews'])
        candidates=[row for row in candidates if row['candidate_id'] not in already]
        candidates=[row for index,row in enumerate(candidates) if index % args.shards == args.shard_index]
        output.mkdir(parents=True)
        results=[]
        history=[]; session_id=str(uuid.uuid4())
        fatal_stop_reason=None;attempted_rows=failed_rows=0
        for number,start in enumerate(range(args.start,len(candidates),args.batch_size)):
            if number>=args.max_calls:break
            items=candidates[start:start+args.batch_size]
            attempted_rows+=len(items)
            record={'batch':number,'model':args.model,'input_sha256':digest(items),'status':'dispatched_outcome_unknown',
                    'session_id':session_id,'history_sha256':digest(history)}
            results.append(record)
            (output/'progress.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
            try:
                response=request_model(os.environ['ANTHROPIC_BASE_URL'],os.environ['ANTHROPIC_AUTH_TOKEN'],args.model,{'items':items},system=SYSTEM,history=history,session_id=session_id)
                reviews=validate_review(response['payload'],[r['candidate_id'] for r in items])
                for row in reviews:row.update(human_confirmed=0,semantic_verified=0,accepted=0,reference_eligible=0,requires_review=1)
                (output/f'review-{number:03d}.json').write_text(json.dumps({'schema':'unquoted-candidate-model-review-v1','database_sha256':baseline,'input_sha256':record['input_sha256'],'model':args.model,'served_model':response.get('served_model',args.model),'reviews':reviews},ensure_ascii=False,indent=2),encoding='utf-8')
                record.update(status='valid_model_review',usage=response['usage'],counts={k:sum(r['decision']==k for r in reviews) for k in ['keep','reject','uncertain']})
            except (ValueError,KeyError,TypeError,OSError) as exc:
                record.update(status='failed',**failure_metadata(exc))
                failed_rows+=len(items)
                fatal_stop_reason=('request_rejected' if is_redirect_rejection(exc) else
                                   'unsupported_response_content' if is_unsupported_response_content(exc) else None)
                if isinstance(exc,urllib.error.HTTPError):record['http_status']=exc.code
            (output/'progress.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
            if fatal_stop_reason is not None or record.get('http_status') in {401,403,404,429}:break
        summary={'candidates_total':len(candidates),'excluded_previously_reviewed':len(already),'session_id':session_id,'shard_index':args.shard_index,'shards':args.shards,'reviewed':sum(sum(r.get('counts',{}).values()) for r in results),'counts':{k:sum(r.get('counts',{}).get(k,0) for r in results) for k in ['keep','reject','uncertain']},'calls':len(results),'source_unchanged':fingerprint(db)==baseline,'corpus_updated':False,'cache_read_tokens_reported':sum(r.get('usage',{}).get('cache_read_input_tokens',0) for r in results),'cache_usage_reports':sum('cache_read_input_tokens' in r.get('usage',{}) for r in results)}
        if fatal_stop_reason is not None:
            summary.update(stop_reason=fatal_stop_reason,attempted_calls=len(results),
                           failed_calls=sum(row['status']=='failed' for row in results),attempted_rows=attempted_rows,failed_rows=failed_rows,
                           unattempted_rows=max(0,len(candidates)-args.start-attempted_rows))
        (output/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
        print(json.dumps(summary));return 0 if summary['source_unchanged'] else 1
    except (ValueError,KeyError,TypeError,OSError,sqlite3.Error) as exc:
        code=str(exc) if isinstance(exc,ValueError) and str(exc) in {'invalid_budget','invalid_output','stale_candidate_source','changed_source_span','changed_source_paragraph','duplicate_candidate'} else type(exc).__name__
        parser.exit(1,'candidate review refused: '+code+'\n')


if __name__=='__main__':raise SystemExit(main())
