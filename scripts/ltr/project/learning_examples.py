"""Non-target bilingual examples backed by canonical source snapshot IDs."""
from .storage import digest


class ExampleError(ValueError):
    pass


def select_examples(snapshots, target, examples):
    """Examples must resolve to supplied immutable source snapshots, not arbitrary paths.

    Cross-edition aliases must be aligned to the same canonical snapshot first.
    This checks exact span identity, not semantic bilingual alignment quality.
    """
    target_ids = {p['id'] for p in target['paragraphs']}
    target_text = ''.join(p['text'] for p in target['paragraphs'])
    out=[]
    for example in examples:
        source = snapshots.get(example.get('book_hash'))
        if source is None or example.get('snapshot_digest') != digest(source['paragraphs']):
            raise ExampleError('example_snapshot_unverified')
        ids=example.get('source_ids')
        if not isinstance(ids,list) or not ids or len(ids)!=len(set(ids)):
            raise ExampleError('invalid_example_span')
        by_id={p['id']:p for p in source['paragraphs']}
        if any(pid not in by_id for pid in ids):
            raise ExampleError('unknown_example_span')
        if source['book_hash']==target['book_hash'] and target_ids.intersection(ids):
            raise ExampleError('target_translation_example_forbidden')
        source_text=''.join(by_id[pid]['text'] for pid in ids)
        if source_text.strip() and source_text.strip() in target_text:
            raise ExampleError('duplicated_target_example_forbidden')
        if not isinstance(example.get('target'),str) or not example['target'].strip():
            raise ExampleError('missing_example_translation')
        out.append({'source':[{'id':pid,'text':by_id[pid]['text']} for pid in ids],
                    'target':example['target'],'alignment_status':example.get('alignment_status','unverified'),
                    'purpose':example.get('purpose','translation_choice_example')})
    return out
