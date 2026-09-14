"""Check declared review coverage; never establishes semantic accuracy."""
import argparse
import json
from pathlib import Path


def validate(data):
    errors = []
    if not isinstance(data, dict) or data.get('schema') != 'literary-review-manifest-v1':
        return ['schema_invalid']

    def ids(key):
        value = data.get(key)
        if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
            errors.append(key + '_invalid')
            return []
        if len(value) != len(set(value)):
            errors.append(key + '_duplicates')
        return value

    expected = ids('expected_paragraph_ids')
    if not expected:
        errors.append('empty_source_scope')
    for key in ['source_reviewed_ids', 'style_reviewed_ids']:
        if ids(key) != expected:
            errors.append(key + '_coverage_or_order')
    changed = set(ids('changed_ids'))
    checked = set(ids('revision_rechecked_ids'))
    if not changed <= set(expected) or not checked <= set(expected):
        errors.append('revision_unknown_paragraph')
    if not changed <= checked:
        errors.append('changed_paragraphs_not_rechecked')
    note_ids = ids('expected_note_ids')
    notes = data.get('notes')
    if not isinstance(notes, list) or any(not isinstance(n, dict) for n in notes):
        errors.append('notes_invalid')
    else:
        if [n.get('id') for n in notes] != note_ids:
            errors.append('note_coverage_or_order')
        for note in notes:
            if not isinstance(note.get('anchor_id'), str) or note['anchor_id'] not in expected:
                errors.append('note_anchor_invalid')
            if note.get('reviewed') is not True:
                errors.append('note_not_reviewed')
    if not isinstance(data.get('unresolved'), list):
        errors.append('unresolved_list_missing')
    stages = data.get('stages')
    if not isinstance(stages, list) or not stages or any(
        not isinstance(s, dict) or any(not isinstance(s.get(k), str) or not s[k]
                                       for k in ['name', 'model', 'reasoning']) for s in stages
    ):
        errors.append('actual_execution_metadata_missing')
    if type(data.get('human_literary_acceptance')) is not bool:
        errors.append('human_review_status_missing')
    return sorted(set(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    args = parser.parse_args()
    try:
        data = json.loads(args.manifest.read_text(encoding='utf-8'))
        errors = validate(data)
    except (OSError, ValueError):
        errors = ['manifest_unreadable']
        data = {}
    print(json.dumps({'declared_coverage_valid': not errors, 'errors': errors,
                      'unresolved_count': len(data.get('unresolved', [])) if isinstance(data, dict)
                      and isinstance(data.get('unresolved'), list) else None,
                      'semantic_accuracy_verified': False}, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
