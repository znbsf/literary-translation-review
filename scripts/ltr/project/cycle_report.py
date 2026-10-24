"""Render private cycle results for the reader; never print prose."""
from __future__ import annotations
import argparse
from collections import Counter
import html
import json
from pathlib import Path
from .storage import atomic_write, private_root, write_json


LABELS = {'characters':'人物','relationships':'关系','story_threads':'故事脉络','terms':'术语',
          'ambiguities':'歧义与待确认项','description':'分析','voice':'声音与口吻','evidence_ids':'原文定位',
          'event_order':'事件顺序','narrative_order':'叙述顺序','translation_implication':'对翻译的影响',
          'source':'原词','proposed_target':'建议译法','name':'维度','scope':'适用范围',
          'observation':'原文观察','rule':'执行规则','failure_signal':'偏离判据','limits':'适用限制',
          'evidence':'原文依据','paragraph_id':'段落','quote':'引文','context':'理解背景',
          'story_progress':'故事推进','reader_knowledge':'读者信息','findings':'差异判断',
          'dimension_id':'维度编号','source_evidence':'原文依据','target_id':'译段',
          'category':'判断类型','problem':'问题','reference_choice':'已有中译的选择',
          'action':'处理方式','dimension_changes':'规则修订','old_rule':'旧规则','new_rule':'新规则',
          'reason':'依据','preserve':'应保留的处理','alignment':'对应范围','reference_ids':'中译段落',
          'previous_rule':'前版规则','revision_reason':'修订依据','dimension_results':'逐维度比较',
          'preferred':'较好版本','remaining':'遗留问题','next_change':'下一步修订','overall':'模型结论'}


def readable(value, depth=0):
    if isinstance(value,dict):
        return '\n'.join('  '*depth+'- **'+LABELS.get(k,k)+'**：'+
                         ('\n'+readable(v,depth+1) if isinstance(v,(dict,list)) else str(v))
                         for k,v in value.items() if k not in {'anchor_id','status','promotion','context_scope'})
    if isinstance(value,list):
        return '\n'.join('  '*depth+'- '+('\n'+readable(v,depth+1) if isinstance(v,(dict,list)) else str(v)) for v in value)
    return str(value)


def render(root):
    root = private_root(Path(root))
    def load(name):
        return json.loads((root/name).read_text(encoding="utf-8"))
    source = load('passage.json')
    dimensions = load('dimensions-v1.json')
    first, second = load('translation-v1.json'), load('translation-v2.json')
    comparison, review = load('comparison-v1.json'), load('comparison-v2.json')
    reference = load('reference-window.json')
    a = {u['id']:u['target'] for u in first['units']}
    b = {u['id']:u['target'] for u in second['units']}
    selected = set(comparison['alignment']['reference_ids'])
    references = [p for p in reference['paragraphs'] if p['id'] in selected]
    scores = Counter(x['preferred'] for x in review['dimension_results'])
    summary = {'format':'private-translation-cycle-summary-v1',
               'paragraphs':len(source['paragraphs']), 'source_characters':source['characters'],
               'dimensions':len(dimensions['dimensions']),
               'dimension_changes':len(comparison['dimension_changes']),
               'findings':len(comparison['findings']),
               'reference_alignment':comparison['alignment']['status'],
               'matched_reference_paragraphs':len(references),
               'changed_paragraphs':sum(a[k]!=b[k] for k in a),
               'model_preferences':{'v2':scores['X'],'v1':scores['Y'],'tie':scores['tie'],'uncertain':scores['uncertain']},
               'remaining_issues':len(review['remaining']),
               'status':'single_passage_model_review_not_series_validated'}
    third=None
    if (root/'translation-v3.json').exists() and (root/'comparison-v3.json').exists():
        third={u['id']:u['target'] for u in load('translation-v3.json')['units']}
        third_review=load('comparison-v3.json')
        third_scores=Counter(x['preferred'] for x in third_review['dimension_results'])
        summary['third_pass']={'changed_paragraphs':sum(b[k]!=third[k] for k in b),
            'model_preferences':{'v3':third_scores['X'],'v2':third_scores['Y'],'tie':third_scores['tie'],'uncertain':third_scores['uncertain']},
            'remaining_issues':len(third_review['remaining'])}
    parts = ['# 翻译维度 → 初译 → 实际文本对照 → 维度修订 → 重译\n',
             '本报告包含私人原文及译文，请在本地阅读。结果为单片段实验，未经人工确认；不能证明全系列风格稳定。\n',
             '本轮使用完整所选篇章作为理解背景。全系列上下文接口允许更多信息；实际覆盖范围以背景档案为准。\n',
             ('已有目标中译在初译冻结后才用于比较。\n' if reference['paragraphs'] else
              '本轮没有已有中译，仅进行原文校审；不构成三方比较。\n'),
             '## 本轮数量与状态\n',
             f"{summary['paragraphs']} 个原文段落，{summary['source_characters']} 个日文字符；{summary['dimensions']} 项维度，{summary['findings']} 项差异判断，{summary['dimension_changes']} 项规则修改。\n",
             f"已有中译对应状态：{summary['reference_alignment']}；{len(references)} 个中译段落。模型偏好：v2 {scores['X']} 项，v1 {scores['Y']} 项，持平 {scores['tie']} 项，无法判断 {scores['uncertain']} 项。遗留 {summary['remaining_issues']} 项问题。\n"]
    if (root/'chapter-context.json').exists():
        parts += ['## 人物、关系与故事理解\n', readable(load('chapter-context.json'))+'\n']
    for heading, value in [('翻译维度 v1',dimensions),('首轮对照与修订依据',comparison),
                           ('修订后的维度',load('dimensions-v2.json'))]:
        parts += [f'## {heading}\n', readable(value)+'\n']
    parts += ['## 逐段原文、初译与重译\n']
    for p in source['paragraphs']:
        parts += [f"### {p['id']}\n", f"**日文**\n\n{p['text']}\n",
                  f"**初译 v1**\n\n{a[p['id']]}\n", f"**重译 v2**\n\n{b[p['id']]}\n"]
        if third is not None:
            parts += [f"**问题修订 v3**\n\n{third[p['id']]}\n"]
    parts += ['## 比较模型识别的已有中译范围\n',
              '范围为模型提出的对应关系，尚未经人工校核；不得将版本来源自动视为官方定本。\n']
    for p in references:
        parts += [f"### {p['id']}\n", p['text']+'\n']
    parts += ['## 第二轮比较（X = v2，Y = v1）\n',
              '同一模型的分开调用；隐藏版本编号不代表独立模型，也不排除偏好偏差。\n',
              readable(review)+'\n']
    if third is not None:
        parts += ['## 第三版复核（X = v3，Y = v2）\n',
                  '第三版接入系列档案，处理前轮剩余问题；仍是同模型复核，不能保证没有其他问题。\n',
                  readable(third_review)+'\n']
    markdown='\n'.join(parts)
    atomic_write(root/'阅读报告.md',markdown)
    # Self-contained, inert HTML, no network/fonts/scripts. No automatic browser launch.
    cells=[]
    for p in source['paragraphs']:
        values=[p['id'],p['text'],a[p['id']],b[p['id']]]
        if third is not None: values.append(third[p['id']])
        cells.append('<tr>'+''.join('<td>'+html.escape(v)+'</td>' for v in values)+'</tr>')
    page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
    page+='<title>翻译迭代实录</title><style>body{max-width:1500px;margin:32px auto;padding:16px;font:17px/1.8 system-ui}td,th{vertical-align:top;border:1px solid #bbb;padding:16px;width:30%;white-space:pre-wrap;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere}h1{font-size:28px}</style>'
    page+='<h1>翻译迭代实录</h1><p>单片段实验，模型比较，未经人工确认。</p><table><tr><th>段落</th><th>日文</th><th>初译 v1</th><th>重译 v2</th>'+('<th>修订 v3</th>' if third is not None else '')+'</tr>'+''.join(cells)+'</table>'
    page+='<h2>维度、对照与完整记录</h2><pre>'+html.escape(markdown)+'</pre></html>'
    atomic_write(root/'逐段对照.html',page)
    write_json(root/'summary.json',summary)
    return summary


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--project',type=Path,required=True)
    args=parser.parse_args(argv)
    print(json.dumps(render(args.project),ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
