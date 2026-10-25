"""Explicit command routing for corpus maintenance tools."""
from importlib import import_module
import argparse
import json

COMMANDS = {"manifest": "corpus_manifest", "leak-scan": "leak_scan"}


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in COMMANDS:
        try:
            return import_module("." + COMMANDS[argv[0]], __package__).main(argv[1:])
        except (Exception, KeyboardInterrupt) as error:
            # Do not expose private paths or database contents in diagnostics.
            print(json.dumps({"status": "failed", "error_code": type(error).__name__}))
            return 1
    parser = argparse.ArgumentParser(description="Corpus maintenance tools")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.parse_args(argv)
    return 2
