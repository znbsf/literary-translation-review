"""Subprocess/HTTP regressions for the documented workbench CLI entry point."""

from __future__ import annotations

import http.client
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlencode


REPO_ROOT = Path(__file__).resolve().parents[3]
INVALID_FILTER = "SYNTHETIC_SENTINEL_INVALID_TARGET_REUSE"


class WorkbenchCliHTTPTests(unittest.TestCase):
    """Exercise the actual ``python -m ltr.workbench`` process on fixtures."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="workbench-cli-test-")
        cls.root = Path(cls.temp.name)
        cls.corpus_dir = cls.root / "corpus"
        cls.corpus_dir.mkdir()
        cls.database_path = cls.corpus_dir / "dialogue.sqlite"
        with closing(sqlite3.connect(cls.database_path)) as conn, conn:
            conn.executescript(
                "CREATE TABLE editions (id TEXT PRIMARY KEY, title TEXT, language TEXT);"
                "CREATE TABLE dialogues (id TEXT PRIMARY KEY, edition_id TEXT, ordinal INTEGER, text TEXT);"
                "INSERT INTO editions VALUES ('ed1','Synthetic','zh');"
                "INSERT INTO dialogues VALUES ('d1','ed1',1,'synthetic fixture text');"
            )

        cls.stale_index = cls.root / "stale-alignment.sqlite"
        with closing(sqlite3.connect(cls.stale_index)) as conn, conn:
            conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.executemany(
                "INSERT INTO metadata VALUES (?, ?)",
                [("schema", "synthetic-obsolete-schema"), ("source_sha256", "0" * 64)],
            )

        cls.config_path = cls.root / "config.json"
        cls.config_path.write_text(
            json.dumps(
                {
                    "corpora": [
                        {"id": "missing-index", "path": str(cls.corpus_dir)},
                        {
                            "id": "stale-index",
                            "path": str(cls.corpus_dir),
                            "alignment_index": str(cls.stale_index),
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        cls.process = subprocess.Popen(
            [sys.executable, "-B", "-m", "ltr.workbench", "--config", str(cls.config_path), "--port", "0"],
            cwd=REPO_ROOT / "scripts",
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        startup: queue.Queue[str] = queue.Queue()

        def read_startup() -> None:
            assert cls.process.stdout is not None
            startup.put(cls.process.stdout.readline())

        reader = threading.Thread(target=read_startup, daemon=True)
        reader.start()
        reader.join(timeout=10)
        if reader.is_alive():
            cls._stop_process()
            cls.temp.cleanup()
            raise RuntimeError("workbench CLI did not announce startup")
        line = startup.get_nowait()
        if not line.startswith("corpus-workbench: listening on http://127.0.0.1:"):
            cls._stop_process()
            cls.temp.cleanup()
            raise RuntimeError("workbench CLI failed to start")
        cls.port = int(line.rsplit(":", 1)[1].strip().rstrip("/"))

    @classmethod
    def _stop_process(cls) -> tuple[str, str]:
        process = getattr(cls, "process", None)
        if process is None:
            return "", ""
        if process.poll() is None:
            process.terminate()
            try:
                return process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        return process.communicate(timeout=5)

    @classmethod
    def tearDownClass(cls) -> None:
        stdout, stderr = cls._stop_process()
        cls.cli_output = stdout + stderr
        cls.temp.cleanup()

    def _get(self, corpus: str, **params: str) -> tuple[int, dict[str, object], str]:
        query = urlencode({"corpus": corpus, "source": "alignments", **params})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("GET", f"/api/dialogues?{query}")
            response = connection.getresponse()
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw), raw
        finally:
            connection.close()

    def test_cli_overlay_errors_keep_statuses_and_do_not_leak_input(self) -> None:
        status, payload, raw = self._get("stale-index", target_reuse=INVALID_FILTER)
        self.assertEqual(status, 400)
        self.assertEqual(payload.get("error"), "invalid_alignment_target_reuse")
        self.assertNotIn(INVALID_FILTER, raw)

        missing_status, missing, _ = self._get("missing-index")
        self.assertEqual(missing_status, 404)
        self.assertEqual(missing.get("error"), "alignment_index_unavailable")

        stale_status, stale, _ = self._get("stale-index")
        self.assertEqual(stale_status, 409)
        self.assertEqual(stale.get("error"), "alignment_index_stale_or_invalid")

        stdout, stderr = self._stop_process()
        self.cli_output = stdout + stderr
        self.assertNotIn(INVALID_FILTER, self.cli_output)

    def test_tools_manifest_preserves_database_and_reports_candidate_flags(self) -> None:
        before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        result = subprocess.run(
            [sys.executable, "-B", "-m", "ltr.tools", "manifest",
             str(self.corpus_dir)], cwd=REPO_ROOT / "scripts", capture_output=True, text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((self.corpus_dir / "CORPUS-MANIFEST.json").read_text("utf-8"))
        self.assertEqual(manifest["database"]["sha256"], before)
        self.assertEqual(hashlib.sha256(self.database_path.read_bytes()).hexdigest(), before)
        self.assertEqual(manifest["table_counts"]["dialogues"], 1)
        self.assertNotIn("synthetic fixture text", json.dumps(manifest))


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()
