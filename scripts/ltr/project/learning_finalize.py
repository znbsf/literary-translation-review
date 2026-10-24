"""Finish an already-running private corpus job without scheduling another task."""
import argparse
import json
from pathlib import Path
import time
from .storage import private_root, write_json
from .series_learning import read, status
from .learning_export import export_learning
from .series_synthesis import run as synthesize


def ready(progress):
    jobs,merges=progress['counts']['jobs'],progress['counts']['merges']
    return (jobs['completed']==progress['planned_jobs'] and merges['completed']==progress['planned_books']
            and not any(x['reserved'] or x['failed'] for x in (jobs,merges)))


def finish(root,model,timeout=14400):
    root=private_root(Path(root)); start=time.monotonic()
    state=root/'finalization-status.json'
    write_json(state,{'status':'waiting_for_full_reading_and_volume_merges'})
    while True:
        if (root/'STOP').exists():
            result={'status':'stopped_by_marker'};write_json(state,result);return result
        progress=status(root)
        if not (root/'.writer.lock').exists():
            if ready(progress):
                break
            active=any(progress['counts'][phase]['reserved'] for phase in ('jobs','merges'))
            if not active:
                result={'status':'incomplete_reading_requires_resolution','progress':progress}
                write_json(state,result);return result
        if time.monotonic()-start>=timeout:
            result={'status':'wait_deadline_reached'};write_json(state,result);return result
        time.sleep(10)
    write_json(state,{'status':'cross_book_synthesis_running'})
    exported=export_learning(root,root/'registry')
    synthesis=synthesize(root,model,workers=3)
    result={'status':'completed_model_proposals' if synthesis['completed_categories']==6 else 'synthesis_requires_resolution',
            'knowledge_export':exported,'synthesis':synthesis}
    write_json(state,result)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--project',type=Path,required=True);p.add_argument('--model',required=True)
    a=p.parse_args()
    try:
        print('{"status":"finalization_attached"}',flush=True)
        print(json.dumps(finish(a.project,a.model)),flush=True)
    except Exception as error:
        write_json(a.project/'finalization-status.json',{'status':'failed','code':type(error).__name__})
        print(json.dumps({'status':'failed','code':type(error).__name__}),flush=True)
        raise SystemExit(2)
