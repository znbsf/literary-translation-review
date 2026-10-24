"""Make the toolkit importable for tests: scripts/ holds the `ltr` package and check_manifest."""
import sys
from pathlib import Path

SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
