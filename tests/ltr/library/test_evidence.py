"""Synthetic evidence regression fixtures; never read a private corpus."""
from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlencode

from ltr.alignment import index as ai
from ltr.library.corpus import init_corpus
from ltr.library.evidence import query_evidence
from ltr.library.review_queue import fingerprint
from ltr.workbench import build_server
from ltr.workbench.common import WorkbenchError
from ltr.workbench.config import WorkbenchConfig
from ltr.workbench.store import WorkbenchStore

REPO = Path(__file__).resolve().parents[3]


def fixture(root):
    root.mkdir(exist_ok=True)
    corpus = root / "corpus"
    init_corpus(corpus)
    db = corpus / "dialogue.sqlite"
    texts = {"ja0": "SYNTHETIC source alpha.", "ja1": "SYNTHETIC source beta.",
             "ja2": "prefix " + "x" * 190 + " literal %_ marker suffix",
             "old0": "SYNTHETIC target one.", "old1": "SYNTHETIC target two.",
             "old2": "SYNTHETIC no correspondence.", "draft0": "SYNTHETIC PRIVATE_DRAFT alpha.",
             "other0": "SYNTHETIC other-work alpha."}
    flags = {"qualification": {"human_confirmed": 1, "semantic_verified": 1,
                              "reference_eligible": 1, "requires_review": 0}}
    with closing(sqlite3.connect(db)) as con, con:
        con.executemany("INSERT INTO editions(id,book_key,title,language,origin_kind) VALUES(?,?,?,?,?)", [
            ("ja","work","Synthetic original","ja","japanese_source"),
            ("old-a","work","Synthetic translation A","zh","existing_translation"),
            ("old-b","work","Synthetic translation B","zh","existing_translation"),
            ("draft","work","Synthetic current draft","zh","generated_draft"),
            ("other","other-work","Synthetic other work","ja","japanese_source")])
        for identity, edition, ordinal, speaker, recipient in [
            ("ja0","ja",0,"",""), ("ja1","ja",1,"",""), ("ja2","ja",2,"",""),
            ("old0","old-a",0,"Captain","Scout"), ("old1","old-b",0,"Mira","Other"),
            ("old2","old-a",1,"",""), ("draft0","draft",0,"Captain","Scout"),
            ("other0","other",0,"Captain","Scout")]:
            text = texts[identity]
            con.execute("INSERT INTO paragraphs VALUES(?,?,?,?,?)",(edition,ordinal,"p"+str(ordinal),text,"{}"))
            con.execute("""INSERT INTO dialogues(id,edition_id,ordinal,text,start_para,end_para,
                speaker,interlocutor,attribution_status,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (identity,edition,ordinal,text,ordinal,ordinal,speaker,recipient,"model_attributed","{}"))
        con.executemany("INSERT INTO alignments VALUES(?,?,?,?,?,?)", [
            ("old0",'["ja0","ja1"]',"model_matched","high","model","{}"),
            ("old1",'["ja0"]',"human_confirmed","high","human",json.dumps(flags)),
            ("old2","[]","unmatched","low","model","{}"),
            ("draft0",'["ja0"]',"human_confirmed","high","human",json.dumps(flags))])
        con.executescript("""CREATE TABLE attribution_candidates(
            dialogue_id TEXT,edition_id TEXT,speaker_surface TEXT,speaker_canonical TEXT,
            addressee_surface TEXT,addressee_canonical TEXT,
            requires_review INTEGER,human_confirmed INTEGER,semantic_verified INTEGER,reference_eligible INTEGER);
            INSERT INTO attribution_candidates VALUES('old0','old-a','Captain','Mira','Scout','Scout',1,0,0,0);""")
    sha = fingerprint(db)
    inputs = root/"inputs";inputs.mkdir()
    reviews = root/"opinions";reviews.mkdir()
    row = {"chinese_id":"old0","japanese_ids":["ja0","ja1"],
        "japanese":[{"id":i,"text_sha256":ai.text_hash(texts[i])} for i in ("ja0","ja1")],
        "chinese_text_sha256":ai.text_hash(texts["old0"]),"status":"candidate","confidence":"medium",**ai.FLAGS}
    batch = {"schema":"haruhi-alignment-candidates-v1","database_sha256":sha,"book_key":"work",
        "zh_edition_id":"old-a","ja_edition_id":"ja","requested_model":"synthetic-producer","rows":[row]}
    (inputs/"align-000.json").write_text(json.dumps(batch),encoding="utf-8")
    rid = ai.identity(sha,"old-a","ja",row)
    review = {"schema":"haruhi-alignment-model-review-v1","database_sha256":sha,
        "reviewer_requested_model":"synthetic-reviewer","producer_requested_model":"synthetic-producer",
        "rows":[{"review_id":rid,"decision":"keep","basis":"semantic_match","zh_edition_id":"old-a",
            "ja_edition_id":"ja","chinese_id":"old0","chinese_text_sha256":row["chinese_text_sha256"],
            "japanese_ids":row["japanese_ids"],"japanese_hashes":[j["text_sha256"] for j in row["japanese"]],**ai.FLAGS}]}
    (reviews/"review-000.json").write_text(json.dumps(review),encoding="utf-8")
    index=root/"alignment.sqlite"
    ai.build(db,inputs,index,reviews)
    config=root/"config.json"
    config.write_text(json.dumps({"corpora":[{"id":"sample","name":"Synthetic evidence","path":str(corpus),
        "alignment_index":str(index)}]}),encoding="utf-8")
    return {"root":root,"corpus":corpus,"database":db,"index":index,"config":config,"texts":texts,
            "candidate_id":"alignment:"+rid,"sha":sha}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.data=fixture(Path(self.temp.name))
        self.store=WorkbenchStore(WorkbenchConfig.load(self.data["config"]))
        self.addCleanup(self.store.close)

    def query(self,mode="line",**params):
        return query_evidence(self.store,{"corpus":"sample","mode":mode,**params})

    def test_reverse_one_to_many_levels_and_independent_review(self):
        result=self.query(dialogue="ja0")
        self.assertEqual([r["level"] for r in result["items"]],["verified","candidate","candidate"])
        self.assertEqual(result["items"][0]["record"]["edition_id"],"old-b")
        self.assertEqual([r["id"] for r in result["items"][1]["references"]],["ja0","ja1"])
        indexed=next(r for r in result["items"] if r["origin"]=="alignment_index")
        self.assertEqual(indexed["review_observations"],[{"decision":"keep","basis":"semantic_match"}])
        self.assertEqual(indexed["level"],"candidate")
        self.assertTrue(all(r["record"]["edition_id"]!="draft" for r in result["items"]))

    def test_record_hashes_and_half_open_offsets(self):
        result=self.query(dialogue="old0")
        records=[result["subject"]]
        for item in result["items"]:records += [item["record"],*item["references"]]
        for record in records:
            self.assertEqual(record["offset_status"],"exact")
            for b in record["bindings"]:
                self.assertEqual(len(b["paragraph_sha256"]),64)
                self.assertEqual((b["start"],b["end"]),(0,record["text_total_chars"]))

    def test_unmatched_and_model_matched_never_become_verified(self):
        result=self.query(dialogue="old2")
        self.assertTrue(result["no_correspondence_found"])
        self.assertEqual(result["items"][0]["relation"],"unmatched")
        self.assertTrue(all(r["level"]=="candidate" for r in self.query(dialogue="old0")["items"]))

    def test_current_draft_excluded_everywhere(self):
        with self.assertRaises(WorkbenchError) as ctx:self.query(dialogue="draft0")
        self.assertEqual(ctx.exception.code,"evidence_source_excluded")
        self.assertEqual(self.query("term",text="PRIVATE_DRAFT")["items"],[])
        self.assertTrue(all(r["record"]["edition_id"]!="draft"
                            for r in self.query("speaker",name="Captain")["items"]))

    def test_candidate_id_resolves_to_source(self):
        self.assertEqual(self.query(dialogue=self.data["candidate_id"])["subject"]["id"],"old0")

    def test_literal_term_hit_stays_clue_with_separate_relationships(self):
        hit=self.query("term",text="source alpha",lang="ja")["items"][0]
        self.assertEqual((hit["level"],hit["relation"]),("clue","retrieval"))
        self.assertEqual({r["level"] for r in hit["correspondences"]},{"verified","candidate"})
        self.assertEqual(hit["record"]["bindings"][0]["start"],10)
        self.assertEqual(len(self.query("term",text="%_")["items"]),1)
        self.assertEqual(self.query("term",text="%_",lang="zh")["items"],[])

    def test_short_excerpt_and_explicit_full_text(self):
        record=self.query("term",text="%_",excerpt_chars=24)["items"][0]["record"]
        self.assertIn("%_",record["excerpt"]);self.assertNotIn("text",record)
        self.assertLessEqual(len(record["excerpt"]),24)
        full=self.query("term",text="%_",with_text=True)["items"][0]["record"]
        self.assertEqual(full["text"],self.data["texts"]["ja2"])
        self.assertFalse(full["text_truncated"])

    def test_speaker_aliases_book_and_same_claim_addressee(self):
        result=self.query("speaker",name="Mira",book="work",addressee="Scout")
        self.assertEqual(result["aliases"],["Captain","Mira"])
        self.assertEqual([r["record"]["id"] for r in result["items"]],["old0"])
        self.assertTrue(all(c["level"]=="candidate" for c in result["items"][0]["claims"]))
        self.assertEqual(self.query("speaker",name="Mira",book="work",addressee="Nobody")["items"],[])

    def test_ranked_pagination_keeps_every_version(self):
        expected=self.query(dialogue="ja0")["items"]
        pages=[self.query(dialogue="ja0",limit=1,offset=n) for n in range(3)]
        self.assertEqual([p["items"][0] for p in pages],expected)
        self.assertTrue(pages[0]["has_more"]);self.assertFalse(pages[-1]["has_more"])

    def test_all_queries_are_read_only(self):
        before={k:self.data[k].read_bytes() for k in ("database","index")}
        self.query(dialogue="ja0");self.query("speaker",name="Mira");self.query("term",text="alpha")
        for key,raw in before.items():self.assertEqual(self.data[key].read_bytes(),raw)

    def test_tampered_index_fails_closed(self):
        with closing(sqlite3.connect(self.data["index"])) as con,con:
            con.execute("UPDATE candidates SET chinese_text_sha256=?",["0"*64])
        with self.assertRaises(WorkbenchError) as ctx:self.query(dialogue="ja0")
        self.assertEqual(ctx.exception.code,"alignment_source_mismatch")

    def test_snapshot_hash_is_not_the_formal_hash(self):
        self.store.close()
        snapshot=self.data["corpus"]/"source.sqlite";shutil.copyfile(self.data["database"],snapshot)
        with closing(sqlite3.connect(self.data["database"])) as con,con:
            con.execute("INSERT INTO metadata VALUES('synthetic_append','{}')")
        payload=json.loads(self.data["config"].read_text())
        payload["corpora"][0].update(source_snapshot_db=str(snapshot),source_snapshot_sha256=self.data["sha"])
        config=self.data["root"]/"snapshot-config.json";config.write_text(json.dumps(payload))
        store=WorkbenchStore(WorkbenchConfig.load(config));self.addCleanup(store.close)
        result=query_evidence(store,{"corpus":"sample","mode":"line","dialogue":"ja0"})
        self.assertEqual(result["source_sha256"],self.data["sha"])
        self.assertNotEqual(result["formal_sha256"],result["source_sha256"])

    def test_long_range_keeps_endpoints_without_guessing_offsets(self):
        self.store.close()
        with closing(sqlite3.connect(self.data["database"])) as con,con:
            for ordinal in range(3,106):
                con.execute("INSERT INTO paragraphs VALUES('ja',?,?,?,?)",
                            (ordinal,"p"+str(ordinal),"synthetic long range paragraph","{}"))
            con.execute("UPDATE dialogues SET end_para=105 WHERE id='ja0'")
        config=WorkbenchConfig.load(self.data["config"])
        config.corpora[0].alignment_index=None
        store=WorkbenchStore(config);self.addCleanup(store.close)
        result=query_evidence(store,{"corpus":"sample","mode":"line","dialogue":"ja0"})
        subject=result["subject"]
        self.assertEqual(subject["offset_status"],"endpoint_range")
        self.assertEqual([b["ordinal"] for b in subject["bindings"]],[0,105])
        self.assertTrue(all(b["start"] is None and b["end"] is None for b in subject["bindings"]))
        self.assertEqual(subject["paragraph_range"],{"start":0,"end":105})

    def test_pending_string_cannot_override_explicit_verified_flags(self):
        self.store.close()
        proof={"qualification":{"human_confirmed":1,"semantic_verified":1,"reference_eligible":1,"requires_review":"1"}}
        with closing(sqlite3.connect(self.data["database"])) as con,con:
            con.execute("UPDATE alignments SET evidence_json=? WHERE chinese_id='old1'",[json.dumps(proof)])
        config=WorkbenchConfig.load(self.data["config"]);config.corpora[0].alignment_index=None
        store=WorkbenchStore(config);self.addCleanup(store.close)
        result=query_evidence(store,{"corpus":"sample","mode":"line","dialogue":"old1"})
        self.assertEqual(result["items"][0]["level"],"candidate")
        self.assertFalse(result["has_verified_correspondence"])

    def test_bad_input_uses_fixed_errors(self):
        for params in ({"mode":"missing"},{"mode":"speaker","name":""},{"mode":"term","text":""},
                       {"mode":"line","dialogue":"ja0","limit":-1},
                       {"mode":"line","dialogue":"ja0","excerpt_chars":9999},
                       {"mode":"line","dialogue":"../PRIVATE_SENTINEL"}):
            with self.subTest(params=params),self.assertRaises(WorkbenchError) as ctx:
                query_evidence(self.store,{"corpus":"sample",**params})
            self.assertNotIn("PRIVATE_SENTINEL",ctx.exception.code)
            self.assertNotIn(str(self.data["root"]),ctx.exception.code)

    def cli(self,*args):
        return subprocess.run([sys.executable,"-X","utf8","-B","scripts/ltr","evidence",*map(str,args)],
            cwd=REPO,capture_output=True,text=True,encoding="utf-8",timeout=20)

    def test_cli_payload_help_and_private_output(self):
        args=["--config",self.data["config"],"--corpus","sample"]
        result=self.cli("line",*args,"--dialogue","ja0")
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        self.assertEqual(json.loads(result.stdout),self.query(dialogue="ja0"))
        self.assertNotIn(str(self.data["root"]),result.stdout+result.stderr)
        for mode in ("line","speaker","term"):self.assertEqual(self.cli(mode,"--help").returncode,0)
        out=self.data["root"]/"evidence-result.json"
        result=self.cli("term",*args,"--text","%_","--with-text","--output",out)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        self.assertNotIn("prefix",result.stdout)
        self.assertIn("text",json.loads(out.read_text())["items"][0]["record"])
        self.assertEqual(self.cli("term",*args,"--text","x","--output",out).returncode,2)

    def test_aliases_exclude_draft_and_other_book_names(self):
        self.store.close()
        with closing(sqlite3.connect(self.data["database"])) as con,con:
            con.execute("INSERT INTO attribution_candidates VALUES('draft0','draft','Mira','PRIVATE_DRAFT_ALIAS','Scout','Scout',1,0,0,0)")
            con.execute("INSERT INTO attribution_candidates VALUES('other0','other','Mira','OTHER_BOOK_ALIAS','Scout','Scout',1,0,0,0)")
        store=WorkbenchStore(WorkbenchConfig.load(self.data["config"]));self.addCleanup(store.close)
        result=query_evidence(store,{"corpus":"sample","mode":"speaker","name":"Mira","book":"work"})
        self.assertEqual(result["aliases"],["Captain","Mira"])
        self.assertNotIn("PRIVATE_DRAFT",json.dumps(result))

    def test_formal_change_during_query_fails(self):
        from unittest.mock import patch
        formal=self.store.database("sample")[1]
        original=formal.source_sha256
        checks=[]
        def digest(*,refresh=False):
            if refresh:
                checks.append(True)
                if len(checks)>1:return "0"*64
            return original(refresh=refresh)
        with patch.object(formal,"source_sha256",side_effect=digest):
            with self.assertRaises(WorkbenchError) as ctx:self.query(dialogue="ja0")
        self.assertEqual(ctx.exception.code,"evidence_formal_changed")

    def test_cli_does_not_write_into_corpus_or_git(self):
        args=["line","--config",self.data["config"],"--corpus","sample","--dialogue","ja0"]
        for target in (self.data["corpus"]/"PRIVATE_SENTINEL.json",REPO/"PRIVATE_SENTINEL.json"):
            result=self.cli(*args,"--output",target)
            self.assertEqual(result.returncode,2)
            self.assertFalse(target.exists())
            self.assertNotIn("PRIVATE_SENTINEL",result.stdout+result.stderr)


class EvidenceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.data=fixture(Path(self.temp.name))
        self.config=WorkbenchConfig.load(self.data["config"])
        self.server=build_server(self.config,0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.thread.join,2);self.addCleanup(self.server.shutdown)

    def get(self,path):
        con=http.client.HTTPConnection("127.0.0.1",self.server.server_address[1],timeout=10)
        try:
            con.request("GET",path);r=con.getresponse();return r.status,r.read()
        finally:con.close()

    def test_http_cli_library_share_the_same_response(self):
        for params in ({"mode":"line","dialogue":"ja0"},{"mode":"speaker","name":"Mira"},
                       {"mode":"term","text":"alpha"}):
            params={"corpus":"sample",**params}
            status,raw=self.get("/api/evidence?"+urlencode(params))
            self.assertEqual(status,200,raw)
            self.assertEqual(json.loads(raw),self.server.store.evidence(params))
            self.assertNotIn(str(self.data["root"]).encode(),raw)

    def test_http_error_never_echoes_source_or_path(self):
        status,raw=self.get("/api/evidence?"+urlencode({"corpus":"sample","mode":"line","dialogue":"../PRIVATE_SENTINEL"}))
        self.assertEqual(status,400)
        self.assertNotIn(b"PRIVATE_SENTINEL",raw)

    def test_evidence_asset_and_detail_entry_are_served(self):
        for path in ("/js/evidence.js","/js/detail.js","/"):
            status,raw=self.get(path);self.assertEqual(status,200)
        self.assertIn(b"renderEvidenceTab",self.get("/js/detail.js")[1])
        self.assertIn(b'data-view="evidence"',self.get("/")[1])


class EvidenceSourceTests(unittest.TestCase):
    def test_identity_index_stays_candidate_and_resolves_source(self):
        from tests.ltr.attribution import test_index as fixtures
        from ltr.attribution import index as identities
        from ltr.workbench.config import CorpusConfig
        base=fixtures.AttributionIndexTests();base.setUp();self.addCleanup(base.doCleanups)
        with closing(sqlite3.connect(base.db)) as con,con:
            con.execute("ALTER TABLE editions ADD COLUMN origin_kind TEXT DEFAULT 'existing_translation'")
        base.sha=fingerprint(base.db)
        base.write("suggestion-000.json",[base.suggested()])
        identities.build(base.db,base.inputs,base.index)
        store=WorkbenchStore(WorkbenchConfig([CorpusConfig("sample","Synthetic",base.source,attribution_index=base.index)],None))
        self.addCleanup(store.close)
        result=query_evidence(store,{"corpus":"sample","mode":"speaker","name":"Alice","addressee":"Bob"})
        indexed=[r for r in result["items"] if r["origin"]=="attribution_index"]
        self.assertEqual(len(indexed),1)
        self.assertEqual(indexed[0]["level"],"candidate")
        line=query_evidence(store,{"corpus":"sample","mode":"line","dialogue":indexed[0]["id"]})
        self.assertEqual(line["subject"]["id"],"d1")
        self.assertEqual(line["subject"]["offset_status"],"paragraph_range")

    def test_quote_index_has_exact_binding(self):
        from tests.ltr.quotes import test_index as fixtures
        from ltr.quotes import index as quotes
        from ltr.workbench.config import CorpusConfig
        base=fixtures.CandidateIndexTests();base.setUp();self.addCleanup(base.doCleanups)
        with closing(sqlite3.connect(base.db)) as con,con:
            con.execute("UPDATE editions SET origin_kind='existing_translation'")
        base.sha=fingerprint(base.db);base.batch["database_sha256"]=base.sha;base.write_batch()
        quotes.build(base.db,base.inputs,base.index)
        with closing(sqlite3.connect(base.index)) as con:
            identity="candidate:"+con.execute("SELECT id FROM candidates").fetchone()[0]
        store=WorkbenchStore(WorkbenchConfig([CorpusConfig("sample","Synthetic",base.source,candidate_index=base.index)],None))
        self.addCleanup(store.close)
        result=query_evidence(store,{"corpus":"sample","mode":"line","dialogue":identity})
        self.assertEqual(result["subject"]["bindings"][0]["paragraph_id"],"p1")
        self.assertEqual(result["subject"]["offset_status"],"exact")
        self.assertTrue(result["no_correspondence_found"])


if __name__=="__main__":
    unittest.main()
