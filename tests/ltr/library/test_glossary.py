"""Synthetic glossary lifecycle, CLI, source isolation and read-only HTTP tests."""
from contextlib import closing
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

from ltr.library import glossary as g
from ltr.library.corpus import init_corpus
from ltr.workbench import build_server
from ltr.workbench.common import WorkbenchError
from ltr.workbench.config import WorkbenchConfig
from ltr.workbench.store import WorkbenchStore

REPO = Path(__file__).resolve().parents[3]


def fixture(root):
    corpus = root / "corpus"
    init_corpus(corpus)
    database = corpus / "dialogue.sqlite"
    with closing(sqlite3.connect(database)) as con, con:
        con.executemany("INSERT INTO editions(id,book_key,language,origin_kind) VALUES(?,?,?,?)", [
            ("src", "sample", "ja", "japanese_source"),
            ("other", "other", "ja", "source_epub"),
            ("draft", "sample", "zh", "generated_draft"),
            ("translated", "sample", "zh", "existing_translation")])
        con.executemany("INSERT INTO paragraphs VALUES(?,?,?,?,?)", [
            ("src", 0, "p0", "『星輪』を掲げる。ルミネは青灯を持つ。", "{}"),
            ("src", 1, "p1", "【風舟】が来る。ルミネは青灯を置く。", "{}"),
            ("src", 2, "p2", "〈空糸〉と《遠鐘》を記す。", "{}"),
            ("src", 3, "p3", "『　』の間をおく。", "{}"),
            ("other", 0, "p0", "『別語』を読む。", "{}"),
            ("draft", 0, "p0", "『草稿排除』", "{}"),
            ("translated", 0, "p0", "『译本排除』", "{}")])
    path = root / "glossary.sqlite"
    g.init_store(path)
    config_path = root / "config.json"
    config_path.write_text(json.dumps({"corpora": [{"id": "sample", "path": str(corpus),
                                                    "glossary": str(path)}]}), encoding="utf-8")
    return path, database, config_path


class GlossaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path, self.database, self.config_path = fixture(self.root)
        self.config = WorkbenchConfig.load(self.config_path)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-X", "utf8", "-B", "scripts/ltr", "glossary", *map(str, args)],
                              cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=20)

    def confirmed(self, source="星輪", target="星环", kind="招式", conditions=""):
        row = g.add(self.path, source, kind=kind, conditions=conditions)
        return g.confirm(self.path, row["id"], target)

    def test_init_refuses_existing_and_marks_schema(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(g.GlossaryError, "glossary_already_exists"):
            g.init_store(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        with g.connect(self.path) as con:
            self.assertEqual(con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0], g.SCHEMA)

    def test_add_and_import_never_confirm(self):
        self.assertEqual(g.add(self.path, "手写", "手写译法", "术语")["status"], "proposed")
        imported = self.root / "import.json"
        imported.write_text(json.dumps([{"source_form": "既有", "target_form": "已有", "status": "confirmed",
                                         "sources_json": '[{"edition_id":"synthetic"}]'}]), encoding="utf-8")
        g.propose(self.path, self.config, "sample", from_json=imported)
        rows = g.list_entries(self.path)["items"]
        self.assertTrue(all(r["status"] == "proposed" for r in rows))
        row = next(r for r in rows if r["source_form"] == "既有")
        self.assertEqual(json.loads(row["sources_json"])[0], {"edition_id": "synthetic"})
        self.assertEqual(json.loads(row["sources_json"])[1]["occurrence_count"], 0)

    def test_blank_bracket_content_is_skipped_not_fatal(self):
        result = g.propose(self.path, self.config, "sample", book="sample")
        self.assertGreater(result["added"], 0)
        forms = [r["source_form"] for r in g.list_entries(self.path)["items"]]
        self.assertTrue(all(form.strip() for form in forms))

    def test_limit_keeps_bracketed_terms_first(self):
        g.propose(self.path, self.config, "sample", book="sample", limit=4)
        forms = {r["source_form"] for r in g.list_entries(self.path)["items"]}
        self.assertEqual(forms, {"星輪", "風舟", "空糸", "遠鐘"})

    def test_confirm_requires_target_and_terminal_decisions_stay_terminal(self):
        row = g.add(self.path, "星輪")
        for target in ("", "  ", None):
            with self.subTest(target=target), self.assertRaises(g.GlossaryError):
                g.confirm(self.path, row["id"], target)
        self.assertEqual(g.list_entries(self.path)["items"][0]["status"], "proposed")
        g.decide(self.path, row["id"], note="synthetic rejection")
        with self.assertRaisesRegex(g.GlossaryError, "glossary_decision_terminal"):
            g.confirm(self.path, row["id"], "星环")

    def test_confirmed_spelling_change_requires_supersede(self):
        row = self.confirmed()
        with self.assertRaisesRegex(g.GlossaryError, "glossary_supersede_required"):
            g.confirm(self.path, row["id"], "不同译法")

    def test_empty_and_duplicate_locks_refused(self):
        with self.assertRaisesRegex(g.GlossaryError, "glossary_empty_lock"):
            g.lock(self.path, "v1")
        self.confirmed()
        g.lock(self.path, "v1")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(g.GlossaryError, "glossary_version_exists"):
            g.lock(self.path, "v1")
        self.assertEqual(before, self.path.read_bytes())

    def test_full_snapshot_is_immutable_after_every_working_change(self):
        row = self.confirmed()
        first = g.lock(self.path, "v1", "first")
        frozen = g.markdown(first)
        g.confirm(self.path, row["id"], "星环", "updated note")
        new = self.confirmed(target="星之环")
        g.decide(self.path, row["id"], by=new["id"], note="replaced")
        self.confirmed("風舟", "风舟")
        second = g.lock(self.path, "v2")
        self.assertEqual(first, g.export(self.path, "v1"))
        self.assertEqual(frozen, g.markdown(g.export(self.path, "v1")))
        self.assertEqual(first["entry_count"], 1)
        self.assertEqual(second["entry_count"], 2)
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(set(first["entries"][0]), set(row))

    def test_snapshot_update_and_delete_are_rejected_by_sqlite(self):
        self.confirmed(); g.lock(self.path, "v1")
        with closing(sqlite3.connect(self.path)) as con:
            for sql in ("UPDATE version_entries SET snapshot_json='{}'", "DELETE FROM version_entries",
                        "UPDATE versions SET note='changed'", "DELETE FROM versions",
                        "INSERT INTO version_entries VALUES('v1','extra','{}')"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    con.execute(sql)

    def test_supersede_chain_preserves_rows_and_reasons(self):
        a = self.confirmed(target="旧环")
        b = self.confirmed(target="中环")
        g.decide(self.path, a["id"], by=b["id"], note="first")
        c = self.confirmed(target="新环")
        g.decide(self.path, b["id"], by=c["id"], note="second")
        rows = {r["id"]: r for r in g.list_entries(self.path)["items"]}
        self.assertEqual(rows[a["id"]]["superseded_by"], b["id"])
        self.assertEqual(rows[b["id"]]["superseded_by"], c["id"])
        self.assertEqual(rows[a["id"]]["decision_note"], "first")
        self.assertEqual(len(g.lock(self.path, "v1")["history"]), 2)
        with self.assertRaises(g.GlossaryError):
            g.decide(self.path, c["id"], by=a["id"], note="cycle")

    def test_supersede_requires_reason_and_matching_confirmed_replacement(self):
        old = self.confirmed()
        proposed = g.add(self.path, "星輪")
        other = self.confirmed("風舟", "风舟")
        for by, note in ((old["id"], "self"), (proposed["id"], "pending"),
                         (other["id"], "different"), (other["id"], "")):
            with self.subTest(by=by, note=note), self.assertRaises(g.GlossaryError):
                g.decide(self.path, old["id"], by=by, note=note)

    def test_manifest_metadata_is_stable_but_requires_human_scope_confirmation(self):
        from scripts.check_manifest import validate
        self.confirmed(); data = g.lock(self.path, "v1")
        manifest = json.loads((REPO / "assets/review-manifest.example.json").read_text(encoding="utf-8"))
        manifest["confirmed_glossary"] = copy.deepcopy(data["confirmed_glossary"])
        self.assertIn("confirmed_glossary_not_version_locked", validate(manifest))
        manifest["confirmed_glossary"]["complete_for_scope"] = True
        self.assertEqual(validate(manifest), [])
        self.assertEqual(data, g.export(self.path, "v1"))

    def test_read_commands_do_not_change_store(self):
        self.confirmed(); g.lock(self.path, "v1")
        before = self.path.read_bytes()
        data = g.export(self.path, "v1")
        g.list_entries(self.path, status="confirmed", version="v1")
        g.check_pairs(data, [{"source": "星輪", "target": "星环"}])
        store = WorkbenchStore(self.config)
        try:
            store.glossary({"corpus": "sample"})
        finally:
            store.close()
        self.assertEqual(before, self.path.read_bytes())

    def test_extraction_brackets_repetition_scope_offsets_and_no_corpus_writes(self):
        before = _sha(self.database)
        result = g.propose(self.path, self.config, "sample", book="sample")
        rows = {r["source_form"]: r for r in g.list_entries(self.path)["items"]}
        self.assertEqual(set(rows), {"星輪", "風舟", "空糸", "遠鐘", "ルミネ", "青灯"})
        self.assertEqual(result["added"], 6)
        sources = json.loads(rows["ルミネ"]["sources_json"])
        self.assertEqual(len(sources), 2)
        self.assertTrue(all(s["occurrence_count"] == 2 for s in sources))
        with closing(sqlite3.connect(self.database)) as con:
            for row in rows.values():
                for loc in json.loads(row["sources_json"]):
                    text = con.execute("SELECT text FROM paragraphs WHERE edition_id=? AND id=?",
                                       (loc["edition_id"], loc["paragraph_id"])).fetchone()[0]
                    self.assertEqual(text[loc["start"]:loc["end"]], row["source_form"])
                    self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), loc["paragraph_sha256"])
        self.assertEqual(_sha(self.database), before)
        self.assertEqual(g.propose(self.path, self.config, "sample", book="sample")["added"], 0)

    def test_proposals_use_validated_source_snapshot(self):
        snapshot = self.database.parent / "source.sqlite"
        shutil.copyfile(self.database, snapshot)
        sha = _sha(snapshot)
        with closing(sqlite3.connect(self.database)) as con, con:
            con.execute("INSERT INTO metadata VALUES('synthetic_append','{}')")
        payload = json.loads(self.config_path.read_text())
        payload["corpora"][0].update(source_snapshot_db=str(snapshot), source_snapshot_sha256=sha)
        self.config_path.write_text(json.dumps(payload))
        config = WorkbenchConfig.load(self.config_path)
        before = (_sha(snapshot), _sha(self.database))
        result = g.propose(self.path, config, "sample")
        self.assertEqual(result["source_sha256"], sha)
        self.assertNotEqual(result["formal_sha256"], sha)
        self.assertEqual(before, (_sha(snapshot), _sha(self.database)))

    def test_source_change_refuses_all_proposals(self):
        before = self.path.read_bytes()
        with patch.object(g, "_sha256", side_effect=[_sha(self.database), "0" * 64]):
            with self.assertRaisesRegex(g.GlossaryError, "glossary_source_changed"):
                g.propose(self.path, self.config, "sample")
        self.assertEqual(before, self.path.read_bytes())

    def test_snapshot_corruption_fails_readback(self):
        self.confirmed(); g.lock(self.path, "v1")
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("DROP TRIGGER snapshots_no_update")
            con.execute("UPDATE version_entries SET snapshot_json=json_set(snapshot_json,'$.target_form','corrupt')")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(g.GlossaryError, "glossary_snapshot_mismatch"):
            g.export(self.path, "v1")
        self.assertEqual(before, self.path.read_bytes())

    def test_proposal_limit_applies_after_deduplication(self):
        g.propose(self.path, self.config, "sample", book="sample", limit=1)
        self.assertEqual(g.propose(self.path, self.config, "sample", book="sample", limit=2)["added"], 2)
        self.assertEqual(len(g.list_entries(self.path)["items"]), 3)
        with self.assertRaises(g.GlossaryError):
            g.propose(self.path, self.config, "sample", limit=0)

    def test_bad_import_rolls_back_all_proposals(self):
        imported = self.root / "bad.json"
        imported.write_text('[{"source_form":"valid"},{"source_form":""}]', encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(g.GlossaryError):
            g.propose(self.path, self.config, "sample", from_json=imported)
        self.assertEqual(before, self.path.read_bytes())

    def test_import_preserves_metadata_when_extraction_finds_same_term(self):
        imported = self.root / "import.json"
        imported.write_text(json.dumps({"entries": [{"source_form": "星輪", "target_form": "星环",
            "kind": "招式", "variants": [{"target_form": "星之环", "allowed": True}],
            "status": "confirmed"}]}), encoding="utf-8")
        g.propose(self.path, self.config, "sample", book="sample", from_json=imported)
        rows = [r for r in g.list_entries(self.path)["items"] if r["source_form"] == "星輪"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["target_form"], rows[0]["kind"], rows[0]["status"]), ("星环", "招式", "proposed"))
        self.assertTrue(json.loads(rows[0]["variants_json"])[0]["allowed"])
        self.assertEqual({s["method"] for s in json.loads(rows[0]["sources_json"])}, {"bracket", "import"})

    def test_store_inside_corpus_and_git_refused_before_creation(self):
        git = self.root / "worktree"; git.mkdir(); (git / ".git").write_text("synthetic marker")
        for target in (self.database.parent / "forbidden.sqlite", git / "forbidden.sqlite", REPO / "forbidden.sqlite"):
            with self.subTest(target=target), self.assertRaises(g.GlossaryError):
                g.init_store(target)
            self.assertFalse(target.exists())

    def test_all_configured_corpus_roots_protected_even_without_database(self):
        empty = self.root / "empty"; empty.mkdir()
        target = empty / "store.sqlite"; g.init_store(target)
        payload = json.loads(self.config_path.read_text())
        payload["corpora"].append({"id": "empty", "path": str(empty)})
        self.config_path.write_text(json.dumps(payload))
        config = WorkbenchConfig.load(self.config_path)
        before = target.read_bytes()
        with self.assertRaisesRegex(g.GlossaryError, "glossary_inside_corpus"):
            g.propose(target, config, "sample")
        self.assertEqual(before, target.read_bytes())

    def test_hardlink_store_rejected(self):
        linked = self.root / "alias.sqlite"
        try:
            os.link(self.path, linked)
        except OSError:
            self.skipTest("hardlinks unavailable")
        with self.assertRaisesRegex(g.GlossaryError, "glossary_path_invalid"):
            g.add(linked, "test")

    def test_missing_store_never_created_and_wrong_schema_never_changed(self):
        missing = self.root / "missing.sqlite"
        result = self.cli("add", "--store", missing, "--source", "PRIVATE_SENTINEL", "--kind", "term")
        self.assertEqual(result.returncode, 2); self.assertFalse(missing.exists())
        arbitrary = self.root / "other.sqlite"
        with closing(sqlite3.connect(arbitrary)) as con:
            con.execute("CREATE TABLE unrelated(value TEXT)")
        before = arbitrary.read_bytes()
        result = self.cli("add", "--store", arbitrary, "--source", "PRIVATE_SENTINEL", "--kind", "term")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(before, arbitrary.read_bytes())
        self.assertNotIn("PRIVATE_SENTINEL", result.stdout + result.stderr)
        self.assertNotIn(str(self.root), result.stdout + result.stderr)

    def regression_version(self):
        old = self.confirmed(target="旧环")
        new = self.confirmed(target="星环")
        g.decide(self.path, old["id"], by=new["id"], note="synthetic correction")
        self.confirmed("風舟", "风舟")
        self.confirmed("青灯", "青灯号", kind="普通同形词")
        return g.lock(self.path, "v1")

    def test_check_finds_regression_missing_multiple_and_homonym_clues(self):
        data = self.regression_version()
        report = g.check_pairs(data, [
            {"source": "星輪", "target": "旧环", "locator": "p0"},
            {"source": "星輪", "target": "星环", "locator": "p1"},
            {"source": "風舟", "target": "船来了", "locator": "p2"},
            {"source": "青灯", "target": "灯亮了", "locator": "p3"}])
        codes = {r["code"] for r in report["issues"]}
        self.assertEqual(codes, {"confirmed_target_missing", "superseded_target_present", "multiple_target_forms"})
        self.assertTrue(report["source_coverage_checked"])
        self.assertTrue(any(r["code"] == "superseded_target_present" and r["level"] == "finding" for r in report["issues"]))
        self.assertTrue(all(r["level"] == "clue" for r in report["issues"] if r.get("locator") == "p3"))

    def test_unbound_and_mixed_pairs_never_claim_complete_source_coverage(self):
        data = self.regression_version()
        for pairs in ([{"target": "旧环"}], [{"source": "星輪", "target": "星环"}, {"target": "旧环"}]):
            report = g.check_pairs(data, pairs)
            self.assertFalse(report["source_coverage_checked"])
            self.assertIn("未完成源词覆盖检查", report["message"])
            self.assertFalse(any(r["code"] == "confirmed_target_missing" for r in report["issues"]))
            self.assertTrue(all(r["level"] == "clue" for r in report["issues"]))

    def test_bound_source_absence_does_not_flag_other_target_homonym(self):
        report = g.check_pairs(self.regression_version(), [{"source": "別語", "target": "旧环"}])
        self.assertEqual(report["issues"], [])

    def test_retired_chain_still_checked_when_replacement_is_rejected(self):
        old = self.confirmed(target="旧环")
        new = self.confirmed(target="星环")
        g.decide(self.path, old["id"], by=new["id"], note="replaced")
        g.decide(self.path, new["id"], note="replacement withdrawn")
        self.confirmed("風舟", "风舟")
        report = g.check_pairs(g.lock(self.path, "v1"), [{"source": "星輪", "target": "旧环"}])
        self.assertEqual([i["code"] for i in report["issues"]], ["superseded_target_present"])

    def test_conditions_remain_clues_and_allowed_variants_satisfy_literal_check(self):
        with g.connect(self.path, write=True) as con:
            row = g._add(con, "星輪", kind="招式", variants=[{"target_form": "星之环", "allowed": True}])
        g.confirm(self.path, row["id"], "星环")
        self.confirmed("青灯", "青灯号", conditions="仅作船名时")
        report = g.check_pairs(g.lock(self.path, "v1"), [{"source": "星輪 青灯", "target": "星之环 灯"}])
        self.assertEqual(len(report["issues"]), 1)
        self.assertEqual(report["issues"][0]["level"], "clue")

    def test_project_hydrates_binding_and_checks_all_units_without_writes(self):
        from ltr.project.demo import demo
        from ltr.project.binding import migrate_project
        from ltr.project.storage import apply_edits
        project_root = self.root / "project"
        project = demo(project_root)
        project = apply_edits(project_root, {"book_hash": project["book_hash"],
            "source_digest": project["source_digest"], "base_revision": project["revision"],
            "units": [{"id": u["id"], "target": "synthetic target", "status": "draft"} for u in project["units"]]})
        migrate_project(project_root, project_root / "corpus")
        before = {p.relative_to(project_root): p.read_bytes() for p in project_root.rglob("*") if p.is_file()}
        pairs = g.project_pairs(project_root)
        self.assertEqual([p["source"] for p in pairs], [p["text"] for p in project["source"]])
        self.assertTrue(all(p["target"] == "synthetic target" for p in pairs))
        self.confirmed(pairs[0]["source"], "missing synthetic wording")
        g.lock(self.path, "v1")
        result = self.cli("check", "--store", self.path, "--version", "v1", "--project", project_root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["issues"])
        self.assertEqual(before, {p.relative_to(project_root): p.read_bytes() for p in project_root.rglob("*") if p.is_file()})
        database = project_root / "corpus/dialogue.sqlite"
        with closing(sqlite3.connect(database)) as con, con:
            con.execute("UPDATE paragraphs SET text='tampered' WHERE ordinal=0")
        result = self.cli("check", "--store", self.path, "--version", "v1", "--project", project_root)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(str(project_root), result.stdout + result.stderr)

    def test_all_help_and_errors_are_content_free(self):
        for name in (None, "init", "propose", "add", "confirm", "supersede", "reject", "lock", "export", "check", "list"):
            result = self.cli(*([name] if name else []), "--help")
            self.assertEqual(result.returncode, 0, result.stderr)
        for args in (("confirm", "--store", self.path, "--id", "PRIVATE_SENTINEL"),
                     ("export", "--store", self.path, "--version", "PRIVATE_SENTINEL"),
                     ("list", "--store", self.path, "--status", "PRIVATE_SENTINEL"),
                     ("propose", "--store", self.path, "--config", self.config_path, "--corpus", "PRIVATE_SENTINEL")):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("PRIVATE_SENTINEL", result.stdout + result.stderr)
            self.assertNotIn(str(self.root), result.stdout + result.stderr)

    def test_cli_end_to_end_versions_export_and_preseeded_check(self):
        path = self.root / "e2e.sqlite"
        def command(name, *args):
            result = self.cli(name, "--store", path, *args)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)
        command("init")
        self.assertEqual(command("propose", "--config", self.config_path, "--corpus", "sample", "--book", "sample")["added"], 6)
        entries = {r["source_form"]: r for r in command("list")["items"]}
        command("confirm", "--id", entries["星輪"]["id"], "--target", "旧环")
        first = command("lock", "--version", "v1")
        command("confirm", "--id", entries["風舟"]["id"], "--target", "风舟")
        command("lock", "--version", "v2")
        self.assertEqual(first, command("export", "--version", "v1", "--format", "json"))
        replacement = command("add", "--source", "星輪", "--kind", "招式")
        command("confirm", "--id", replacement["id"], "--target", "星环")
        command("supersede", "--id", entries["星輪"]["id"], "--by", replacement["id"], "--note", "synthetic correction")
        command("lock", "--version", "v3")
        pairs = self.root / "pairs.json"
        pairs.write_text(json.dumps([{"source": "星輪 風舟", "target": "旧环", "locator": "synthetic-p0"}]), encoding="utf-8")
        report = command("check", "--version", "v3", "--pairs", pairs)
        self.assertEqual({r["code"] for r in report["issues"]}, {"confirmed_target_missing", "superseded_target_present"})
        result = self.cli("export", "--store", path, "--version", "v1", "--format", "md")
        self.assertEqual(result.returncode, 0)
        self.assertIn(first["sha256"], result.stdout)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GlossaryHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path, self.database, self.config_path = fixture(self.root)
        self.config = WorkbenchConfig.load(self.config_path)
        self.server = build_server(self.config, 0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True); thread.start()
        self.addCleanup(self.server.server_close); self.addCleanup(thread.join, 2); self.addCleanup(self.server.shutdown)

    def request(self, path, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            conn.request(method, path)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_unconfigured_view_and_empty_configured_store(self):
        status, raw = self.request("/api/glossary?corpus=sample")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["versions"], [])
        self.config.corpora[0].glossary = None
        status, raw = self.request("/api/glossary?corpus=sample")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)["available"])
        self.assertIn("未配置词表", json.loads(raw)["message"])

    def test_api_snapshot_proposal_queue_and_conflict_chains_are_readonly(self):
        old = g.add(self.path, "星輪", kind="招式"); g.confirm(self.path, old["id"], "旧环")
        frozen = g.lock(self.path, "v1")
        new = g.add(self.path, "星輪", kind="招式"); g.confirm(self.path, new["id"], "星环")
        payload = json.loads(self.request("/api/glossary?corpus=sample")[1])
        self.assertEqual(payload["conflicts"][0]["code"], "multiple_confirmed")
        g.decide(self.path, old["id"], by=new["id"], note="replace")
        g.add(self.path, "風舟")
        g.lock(self.path, "v2")
        before = self.path.read_bytes()
        params = {"corpus": "sample", "version": "v1"}
        status, raw = self.request("/api/glossary?" + urlencode(params))
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload, self.server.store.glossary(params))
        self.assertEqual(payload["selected"], frozen)
        self.assertEqual([r["version"] for r in payload["versions"]], ["v2", "v1"])
        self.assertEqual(len(payload["proposals"]), 1)
        self.assertEqual(payload["conflicts"][0]["code"], "superseded_link")
        self.assertEqual(before, self.path.read_bytes())
        self.assertNotIn(str(self.root), raw.decode())
        self.assertEqual(self.request("/api/glossary?corpus=sample", "POST")[0], 404)

    def test_config_rejects_glossary_in_any_corpus(self):
        payload = json.loads(self.config_path.read_text())
        payload["corpora"][0]["glossary"] = str(self.database.parent / "hidden.sqlite")
        self.config_path.write_text(json.dumps(payload))
        with self.assertRaises(WorkbenchError) as ctx:
            WorkbenchConfig.load(self.config_path)
        self.assertEqual(ctx.exception.code, "glossary_must_be_external")

    def test_http_error_is_static(self):
        status, raw = self.request("/api/glossary?corpus=sample&version=PRIVATE_SENTINEL")
        self.assertEqual(status, 409)
        self.assertNotIn(b"PRIVATE_SENTINEL", raw)
        self.assertNotIn(str(self.root).encode(), raw)


if __name__ == "__main__":
    unittest.main()
