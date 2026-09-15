"""Check declared review coverage; never establishes semantic accuracy."""
import argparse
import json
from pathlib import Path


def validate(data):
    errors = []
    if not isinstance(data, dict) or data.get('schema') != 'literary-review-manifest-v2':
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

    required_dimensions = {
        'relationships_and_history', 'character_arcs', 'plot_knowledge',
        'fixed_wording', 'addressee_and_register', 'narrative_style',
        'wordplay_imagery_foreshadowing', 'notes_versions_sources'
    }
    understanding = data.get('whole_book_understanding')
    if not isinstance(understanding, dict) or understanding.get('complete_book_read') is not True:
        errors.append('whole_book_understanding_incomplete')
    else:
        dimensions = understanding.get('dimensions')
        if not isinstance(dimensions, list) or any(not isinstance(x, str) for x in dimensions) \
                or set(dimensions) != required_dimensions \
                or len(dimensions) != len(required_dimensions):
            errors.append('understanding_dimensions_incomplete')
        decisions = understanding.get('decisions')
        if not isinstance(decisions, list) or not decisions:
            errors.append('translation_decisions_missing')
        else:
            mapped = set()
            seen_decision_ids = set()
            for decision in decisions:
                if not isinstance(decision, dict) or any(
                    not isinstance(decision.get(key), str) or not decision[key]
                    for key in ['id', 'dimension', 'source_locator', 'translation_decision']
                ):
                    errors.append('translation_decision_invalid')
                    continue
                if decision['id'] in seen_decision_ids:
                    errors.append('translation_decision_duplicates')
                seen_decision_ids.add(decision['id'])
                mapped.add(decision['dimension'])
                if decision['source_locator'] not in expected:
                    errors.append('translation_decision_source_unknown')
            if mapped != required_dimensions:
                errors.append('understanding_not_mapped_to_decisions')

    glossary = data.get('confirmed_glossary')
    if not isinstance(glossary, dict) or not isinstance(glossary.get('version'), str) \
            or not glossary.get('version') or glossary.get('locked') is not True \
            or glossary.get('complete_for_scope') is not True:
        errors.append('confirmed_glossary_not_version_locked')

    source_first = data.get('source_first')
    if not isinstance(source_first, dict) or type(source_first.get('required')) is not bool:
        errors.append('source_first_status_missing')
    elif source_first['required']:
        candidates = source_first.get('candidate_ids')
        frozen = source_first.get('frozen_before_comparison_ids')
        if not isinstance(candidates, list) or any(not isinstance(x, str) or not x for x in candidates):
            errors.append('source_first_candidates_invalid')
        elif candidates != expected:
            errors.append('source_first_candidate_coverage_or_order')
        elif candidates != frozen:
            errors.append('source_first_candidates_not_frozen_before_comparison')
        required_exclusions = {
            'current-target', 'rewrite-advice', 'old-translation-excerpts',
            'draft-derived-target-language-summary'
        }
        allowed_sources = {
            'source', 'whole-book-understanding', 'verified-prior-work',
            'confirmed-glossary'
        }
        context_sources = source_first.get('context_sources')
        artifact_ids = source_first.get('context_artifact_ids')
        if not isinstance(context_sources, list) \
                or any(not isinstance(x, str) for x in context_sources) \
                or 'source' not in context_sources \
                or not set(context_sources) <= allowed_sources \
                or not isinstance(artifact_ids, list) or not artifact_ids \
                or any(not isinstance(x, str) or not x for x in artifact_ids):
            errors.append('source_first_included_inputs_unverified')
        exclusions = source_first.get('excluded_inputs')
        if not isinstance(exclusions, list) or any(not isinstance(x, str) for x in exclusions) \
                or set(exclusions) != required_exclusions:
            errors.append('source_first_context_boundary_unverified')
        if source_first.get('operator_exposure_status') not in [
            'not-claimed', 'exposed', 'verified-unexposed'
        ]:
            errors.append('operator_exposure_status_invalid')
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
    execution = data.get('execution')
    if not isinstance(execution, dict):
        errors.append('actual_execution_metadata_missing')
    else:
        limit = execution.get('user_budget_hard_limit')
        used = execution.get('budget_used')
        budgets_valid = all(
            isinstance(item, dict) and isinstance(item.get('unit'), str) and item['unit']
            and isinstance(item.get('value'), (int, float)) and not isinstance(item.get('value'), bool)
            and item['value'] >= 0
            for item in [limit, used]
        )
        if not budgets_valid or limit['unit'] != used['unit']:
            errors.append('budget_metadata_invalid')
        elif used['value'] > limit['value']:
            errors.append('user_budget_hard_limit_exceeded')
        checkpoints = execution.get('checkpoints')
        if not isinstance(checkpoints, list) or not checkpoints or any(
            not isinstance(c, dict) or not isinstance(c.get('name'), str) or not c['name']
            or c.get('status') not in ['pending', 'running', 'succeeded', 'failed']
            or not isinstance(c.get('coverage_ids'), list) for c in checkpoints
        ):
            errors.append('checkpoint_metadata_missing')
        stages = execution.get('stages')
        if not isinstance(stages, list) or not stages or any(
            not isinstance(s, dict) or any(not isinstance(s.get(k), str) or not s[k]
                                           for k in ['name', 'model', 'reasoning'])
            or not isinstance(s.get('coverage_ids'), list) for s in stages
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
                      'semantic_accuracy_verified': False,
                      'full_book_literary_review_verified': False}, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
