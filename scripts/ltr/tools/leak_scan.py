# -*- coding: utf-8 -*-
"""Decisive scan for exact CJK/kana corpus overlaps in engineering artifacts.

Scans the corpus's private/ run directories and the workspace docs/. Private
review inputs that legitimately contain prose are exempt only when listed by
exact root-relative path in a --declared JSON file; directories such as an
installed OCR runtime can be excluded there too:

    {"declared_inputs": ["corpus/private/review/sample.json"],
     "excluded_prefixes": {"corpus/private/ocr/dependencies": "OCR runtime files"}}

The scanner gathers candidate windows from outputs, then scans the corpus once;
it never builds a corpus-wide n-gram index.

Usage: python -m ltr.tools.leak_scan --root DIR --corpus NAME [--declared FILE] [--quiet]
Exit 0 = clean; exit 1 = overlap or source-free read failure.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
from pathlib import Path

try:
    from .corpus_overlap import (candidate_windows, corpus_blob,
                                 match_corpus_windows_to_files)
except ImportError:  # pragma: no cover - direct script execution
    from corpus_overlap import (candidate_windows, corpus_blob,
                                match_corpus_windows_to_files)


SKIP_EXT = {'.jpg', '.png', '.jpeg', '.webp', '.pyc', '.sqlite', '.zip'}
MIN_LEAK_RUN = 8
# Direct corpus-owned data directories are not engineering artifacts.
PREEXISTING_DATA = {'sources', 'editions', 'scans', 'ocr', 'ocr-volumes',
                    'ocr-failure-history', 'text-layout', 'synthetic',
                    'worker-logs', 'dependencies', 'backups'}


def _root_relative(path: str, root: str) -> str:
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    return os.path.normcase(rel).replace('\\', '/')


def _scan_files(root: str, private: str, declared_inputs: set[str],
                excluded_prefixes: dict[str, str]) -> tuple[list[str], list[str], int]:
    targets = [os.path.join(private, directory)
               for directory in sorted(os.listdir(private))
               if os.path.isdir(os.path.join(private, directory))
               and directory not in PREEXISTING_DATA]
    targets.append(os.path.join(root, 'docs'))
    for name in ('STATUS.md', 'CORPUS-MANIFEST.json'):
        path = os.path.join(private, '..', name)
        if os.path.isfile(path):
            targets.append(path)

    files: list[str] = []
    declared: list[str] = []
    seen: set[str] = set()
    allow = {os.path.normcase(path).replace('\\', '/') for path in declared_inputs}
    prefixes = [os.path.normcase(prefix).replace('\\', '/') for prefix in excluded_prefixes]
    excluded_count = 0
    for target in targets:
        paths = [target] if os.path.isfile(target) else glob.glob(
            os.path.join(target, '**', '*'), recursive=True)
        for path in paths:
            if not os.path.isfile(path):
                continue
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            relative = _root_relative(path, root)
            if any(relative == prefix or relative.startswith(prefix + '/') for prefix in prefixes):
                excluded_count += 1
            elif os.path.splitext(path)[1].lower() in SKIP_EXT:
                continue
            elif relative in allow:
                declared.append(path)
            else:
                files.append(path)
    return files, declared, excluded_count


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True, help='Data workspace root')
    ap.add_argument('--corpus', required=True, help='Corpus directory under root (holds dialogue.sqlite and private/)')
    ap.add_argument('--declared', help='JSON with declared_inputs and excluded_prefixes')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    corpus_dir = os.path.join(root, args.corpus)
    declared_inputs: set[str] = set()
    excluded_prefixes: dict[str, str] = {}
    if args.declared:
        try:
            with open(args.declared, encoding='utf-8') as stream:
                spec = json.load(stream)
            declared_inputs = set(spec.get('declared_inputs', []))
            excluded_prefixes = dict(spec.get('excluded_prefixes', {}))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            print('扫描失败：豁免清单读取失败（%s）' % type(exc).__name__)
            return 1
    private = os.path.join(corpus_dir, 'private')
    database = os.path.join(corpus_dir, 'dialogue.sqlite')

    errors: list[tuple[str, str]] = []
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(f"{Path(database).resolve().as_uri()}?mode=ro", uri=True)
        corpus = corpus_blob(row[0] for row in con.execute(
            'select text from paragraphs'))
    except (OSError, sqlite3.Error) as exc:
        print('扫描失败：语料读取失败（%s）' % type(exc).__name__)
        return 1
    finally:
        if con is not None:
            con.close()

    try:
        files, declared, excluded_count = _scan_files(root, private, declared_inputs, excluded_prefixes)
    except OSError as exc:
        print('扫描失败：文件清单读取失败（%s）' % type(exc).__name__)
        return 1

    candidate_files: dict[str, list[str]] = {}
    for path in sorted(files):
        rel = _root_relative(path, root)
        try:
            with open(path, encoding='utf-8') as stream:
                text = stream.read()
        except (UnicodeDecodeError, OSError) as exc:
            errors.append((rel, type(exc).__name__))
            continue
        for window in set(candidate_windows(text, MIN_LEAK_RUN)):
            candidate_files.setdefault(window, []).append(rel)

    matched_counts = match_corpus_windows_to_files(
        candidate_files, corpus, MIN_LEAK_RUN)
    issues = sorted(matched_counts.items())

    if not args.quiet:
        print('扫描文件数 = %d' % len(files))
        print('精确路径排除文件数 = %d；%s' % (excluded_count, '；'.join(
            '%s（%s）' % item for item in sorted(excluded_prefixes.items())) or '无排除目录'))
        print('精确路径豁免复核输入 = %d 个' % len(declared))
        for path in sorted(declared):
            print('   - %s' % _root_relative(path, root))
        print('阈值 = >=%d 连续 CJK/kana 字符；重合按不同窗口计数' % MIN_LEAK_RUN)
        if issues:
            print('语料重合文件数 = %d；重合窗口数 = %d' %
                  (len(issues), sum(count for _, count in issues)))
            for rel, count in issues:
                print('   %s (%d 个窗口)' % (rel, count))
        else:
            print('语料重合 = 0')

    if args.quiet:
        print('精确路径排除文件数 = %d；%s' % (excluded_count, '；'.join(
            '%s（%s）' % item for item in sorted(excluded_prefixes.items())) or '无排除目录'))
    if errors:
        print('读取失败文件数 = %d' % len(errors))
        for rel, error_type in errors:
            print('   %s (%s)' % (rel, error_type))
    if issues and args.quiet:
        print('语料重合文件数 = %d；重合窗口数 = %d' %
              (len(issues), sum(count for _, count in issues)))
    return 1 if issues or errors else 0


if __name__ == '__main__':
    sys.exit(main())
