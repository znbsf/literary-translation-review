"""Build bounded, source-exact evidence-span hints for attribution requests.

Hints are locators plus exact source substrings. They do not infer speakers or
change the strict suggestion validator's requirements.
"""
from __future__ import annotations

import hashlib


MAX_HINTS_PER_TARGET = 24
MAX_HINTS_PER_PARAGRAPH = 3
MAX_ITEMS = 128
MAX_CONTEXT_PARAGRAPHS = 64
MAX_VOCABULARY_ITEMS = 10000
MAX_PARAGRAPH_CODEPOINTS = 10000
MAX_CANDIDATE_ANCHORS_PER_PARAGRAPH = 16
MAX_CHECKS_PER_TARGET = 128
MAX_SPAN_CODEPOINTS = 200

_PUNCTUATION = frozenset('。！？!?、，,；;：:．.「」『』（）()［］[]【】…—')


def _invalid(code: str) -> ValueError:
    return ValueError(code)


def _candidate_anchors(text: str, vocabulary: tuple[str, ...]) -> list[tuple[int, int]]:
    """Return bounded (start, minimum_end) anchors in stable priority order."""
    size = len(text)
    if not size:
        return []
    anchors: list[tuple[int, int]] = []

    # Exact vocabulary matches are anchors only. Include nearby source context
    # and never interpret a matched name as a speaker judgment.
    vocab_anchors: list[tuple[int, int]] = []
    for token in vocabulary[:128]:
        cursor = 0
        found = 0
        while found < 2:
            position = text.find(token, cursor)
            if position < 0:
                break
            start = max(0, position - 12)
            end = min(size, position + len(token) + 12)
            if end - start <= MAX_SPAN_CODEPOINTS:
                vocab_anchors.append((start, end))
            cursor = position + 1
            found += 1
            if len(vocab_anchors) >= 8:
                break
        if len(vocab_anchors) >= 8:
            break
    anchors.extend(vocab_anchors)

    punctuation_positions = [index for index, char in enumerate(text) if char in _PUNCTUATION]
    if punctuation_positions:
        # Cover both ends and evenly spaced punctuation sites without scanning
        # an unbounded number of candidate windows.
        if len(punctuation_positions) > 4:
            chosen = [punctuation_positions[round(i * (len(punctuation_positions) - 1) / 3)]
                      for i in range(4)]
        else:
            chosen = punctuation_positions
        for index in chosen:
            start = max(0, index - 12)
            end = min(size, index + 13)
            anchors.append((start, end))

    # Prefer a meaningful beginning-of-paragraph context over a single unique
    # character. A short paragraph is represented in full.
    anchors.extend(((0, min(size, 24)), (max(0, size - 40), size)))

    unique: list[tuple[int, int]] = []
    seen = set()
    for start, end in anchors:
        pair = (start, end)
        if pair not in seen and 0 <= start < end <= size and end - start <= MAX_SPAN_CODEPOINTS:
            seen.add(pair)
            unique.append(pair)
        if len(unique) >= MAX_CANDIDATE_ANCHORS_PER_PARAGRAPH:
            break
    return unique


def _is_unique(text: str, start: int, end: int) -> bool:
    span = text[start:end]
    first = text.find(span)
    return first == start and text.find(span, first + 1) == -1


def _unique_span(text: str, start: int, minimum_end: int, checks_left: int):
    maximum_end = min(len(text), start + MAX_SPAN_CODEPOINTS)
    if checks_left <= 0 or minimum_end > maximum_end:
        return None, 0
    # Uniqueness is monotonic as a fixed-start window grows. If the maximum
    # window is still repeated, no prefix at this start can be unique.
    checks_used = 1
    if not _is_unique(text, start, maximum_end):
        return None, checks_used
    low, high = minimum_end, maximum_end
    while low < high and checks_used < checks_left:
        middle = (low + high) // 2
        checks_used += 1
        if _is_unique(text, start, middle):
            high = middle
        else:
            low = middle + 1
    if low != high:
        return None, checks_used
    return (start, low, text[start:low]), checks_used


def build_evidence_hints(items, vocabulary):
    """Return deterministic, bounded exact-span hints for each target.

    The output shape is ``[{dialogue_id, edition_id, spans:[...]}]``. Each
    span is drawn only from that target's same-edition context. Oversized
    paragraphs are skipped; invalid structures fail with static error codes.
    """
    if not isinstance(items, (list, tuple)) or len(items) > MAX_ITEMS:
        raise _invalid('invalid_evidence_hint_items')
    if not isinstance(vocabulary, (list, tuple)) or len(vocabulary) > MAX_VOCABULARY_ITEMS:
        raise _invalid('invalid_evidence_hint_vocabulary')
    vocab = []
    for value in vocabulary:
        if not isinstance(value, str) or not value or len(value) > MAX_SPAN_CODEPOINTS:
            raise _invalid('invalid_evidence_hint_vocabulary')
        vocab.append(value)
    vocab = tuple(sorted(set(vocab)))

    output = []
    seen_targets = set()
    for target in items:
        if not isinstance(target, dict):
            raise _invalid('invalid_evidence_hint_items')
        dialogue_id, edition_id, context = (target.get('dialogue_id'), target.get('edition_id'),
                                            target.get('context'))
        if not isinstance(dialogue_id, str) or not dialogue_id or not isinstance(edition_id, str) or not edition_id:
            raise _invalid('invalid_evidence_hint_items')
        if dialogue_id in seen_targets:
            raise _invalid('duplicate_evidence_hint_target')
        seen_targets.add(dialogue_id)
        if not isinstance(context, (list, tuple)) or len(context) > MAX_CONTEXT_PARAGRAPHS:
            raise _invalid('invalid_evidence_hint_context')

        spans = []
        seen_paragraphs = set()
        seen_spans = set()
        checks_left = MAX_CHECKS_PER_TARGET
        for paragraph in context:
            if not isinstance(paragraph, dict):
                raise _invalid('invalid_evidence_hint_context')
            paragraph_edition = paragraph.get('edition_id')
            # Foreign-edition context is never a source for this target.
            if paragraph_edition != edition_id:
                continue
            paragraph_id, text = paragraph.get('id'), paragraph.get('text')
            if not isinstance(paragraph_id, str) or not paragraph_id or not isinstance(text, str):
                raise _invalid('invalid_evidence_hint_context')
            if paragraph_id in seen_paragraphs:
                raise _invalid('invalid_evidence_hint_context')
            seen_paragraphs.add(paragraph_id)
            if not text or len(text) > MAX_PARAGRAPH_CODEPOINTS:
                continue

            paragraph_added = 0
            for start, minimum_end in _candidate_anchors(text, vocab):
                if checks_left <= 0 or len(spans) >= MAX_HINTS_PER_TARGET:
                    break
                candidate, checks_used = _unique_span(text, start, minimum_end, checks_left)
                checks_left -= checks_used
                if candidate is None:
                    continue
                span_start, span_end, span_text = candidate
                key = (paragraph_id, span_start, span_end)
                if key in seen_spans:
                    continue
                seen_spans.add(key)
                spans.append({'paragraph_id': paragraph_id, 'start': span_start, 'end': span_end,
                              'text': span_text,
                              'paragraph_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()})
                paragraph_added += 1
                if paragraph_added >= MAX_HINTS_PER_PARAGRAPH:
                    break
            if len(spans) >= MAX_HINTS_PER_TARGET:
                break
        output.append({'dialogue_id': dialogue_id, 'edition_id': edition_id, 'spans': spans})
    return output
