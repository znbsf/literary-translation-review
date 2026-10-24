# -*- coding: utf-8 -*-
"""Small, source-free helpers for exact corpus overlap gates."""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Iterator, MutableMapping


CJK_RUN = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]+")


def candidate_windows(text: str, minimum: int) -> Iterator[str]:
    """Yield every fixed-size window from continuous CJK/kana runs."""
    if minimum < 1:
        raise ValueError("minimum must be positive")
    for match in CJK_RUN.finditer(text):
        run = match.group(0)
        for start in range(len(run) - minimum + 1):
            yield run[start:start + minimum]


def matching_window_count(text: str, corpus: str, minimum: int) -> int:
    """Count distinct candidate windows found verbatim in the corpus blob.

    Looking up windows on demand avoids retaining a corpus-wide n-gram index.
    A newline-delimited corpus blob preserves paragraph boundaries because a
    candidate window contains only CJK/kana characters.
    """
    return sum(1 for window in set(candidate_windows(text, minimum)) if window in corpus)


def has_corpus_overlap(text: str, corpus: str, minimum: int) -> bool:
    """Return as soon as one qualifying window occurs verbatim in corpus."""
    return any(window in corpus for window in candidate_windows(text, minimum))


def match_corpus_windows_to_files(
        window_files: MutableMapping[str, list[str]], corpus: str,
        minimum: int) -> dict[str, int]:
    """Match output windows against one corpus pass, mutating away found keys.

    ``window_files`` is built only from engineering outputs, so memory is
    proportional to candidate output rather than the much larger corpus.
    Each distinct window is counted once per file even if repeated in corpus.
    """
    counts: dict[str, int] = defaultdict(int)
    for window in candidate_windows(corpus, minimum):
        paths = window_files.pop(window, None)
        if paths is not None:
            for path in paths:
                counts[path] += 1
            if not window_files:
                break
    return dict(counts)


def corpus_blob(paragraphs: Iterable[str]) -> str:
    """Join source paragraphs while keeping a hard boundary between each."""
    return "\n".join(paragraphs)
