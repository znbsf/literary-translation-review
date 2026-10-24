"""Synthetic-only contract tests for the isolated geometry index."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import sqlite3
import tempfile
import unittest

from ltr import workbench as wb
from ltr.ocr import reading_order_index as index

class GeometryIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name); self.root=base/"source"; (self.root/"private"/"ocr").mkdir(parents=True)
        self.phase=base/"phase"; self.phase.mkdir()
        self.dbpath=self.root/"dialogue.sqlite"
        with sqlite3.connect(self.dbpath) as con:
            con.execute("CREATE TABLE scan_pages(id TEXT PRIMARY KEY,collection_id TEXT,ordinal INTEGER,image_path TEXT,image_sha256 TEXT,width INTEGER,height INTEGER,ocr_status TEXT,ocr_sha256 TEXT)")
            self.rows=[]
            for i in range(1,11):
                page=f"FIX:{i:04d}"; collection="FIX"
                receipt={"schema":"haruhi-page-ocr-v1","page_id":page,"image_sha256":"a"*64,"width":100,"height":200,"engine":"Windows.Media.Ocr","language":"ja","status":"completed","human_verified":False,"lines":([{"words":[{"x":10,"y":10,"w":5,"h":5,"text":"synthetic"},{"x":20,"y":15,"w":5,"h":5,"text":"synthetic"}]}] if i in (1,2) else [])}
                raw=json.dumps(receipt,separators=(",",":"),ensure_ascii=False).encode()
                (self.root/"private"/"ocr"/f"FIX_{i:04d}.json").write_bytes(raw)
                rh=hashlib.sha256(raw).hexdigest()
                con.execute("INSERT INTO scan_pages VALUES (?,?,?,?,?,?,?,?,?)",(page,collection,i,"unused.png","a"*64,100,200,"completed_unverified",rh))
                self.rows.append((page,rh))
        self.corpus=SimpleNamespace(root=self.root,db_path=self.dbpath,ocr_reading_order_candidates=self.phase)
        self.db=wb.CorpusDatabase(self.corpus)
        self.addCleanup(self.db.close)
        self._build()

    def _build(self):
        source=self.db.source_sha256(); inv="b"*64; alg="c"*64; helper="d"*64
        entries=[]
        for n,(page,rh) in enumerate(self.rows,1):
            if n==1:
                obj={"schema":index.PAGE_SCHEMA,"page_id":page,"book_key":"synthetic","collection_id":"FIX","ordinal":n,"source_sha256":source,"receipt_sha256":rh,"image_sha256":"a"*64,"algorithm_sha256":alg,"method":"vertical-word-bands-v2","status":"geometry_candidate_unverified","layout":"vertical_single_band","raw_line_count":1,"raw_word_count":2,"word_order":[[0,0],[0,1]],"segments":[{"band":0,"box":[10,10,25,20],"column":0,"role":"main","source_line":0,"word_refs":[[0,0],[0,1]]}],"bands":[[0,20]],"raw_backward_band_steps":0,"new_backward_band_steps":0,"main":[0],"ruby":[],"other":[],"main_ruby_role_status":"geometry_candidate_not_semantically_verified","geometry_candidate":True,"requires_review":1,"accepted":0,"human_confirmed":0,"human_verified":0,"semantic_verified":0,"reference_eligible":0,"reading_order_verified":0,"ocr_verified":0}
            elif n==2:
                obj={"schema":index.PAGE_SCHEMA,"page_id":page,"book_key":"synthetic","collection_id":"FIX","ordinal":n,"source_sha256":source,"receipt_sha256":rh,"image_sha256":"a"*64,"algorithm_sha256":alg,"method":"vertical-word-bands-v2","status":"unclassified_geometry_only","layout":"unclassified","raw_line_count":1,"raw_word_count":2,"word_order":[[0,0],[0,1]],"segments":[],"bands":[],"raw_backward_band_steps":0,"new_backward_band_steps":0,"main":[],"ruby":[],"other":[],"main_ruby_role_status":"geometry_candidate_not_semantically_verified","geometry_candidate":False,"requires_review":1,"accepted":0,"human_confirmed":0,"human_verified":0,"semantic_verified":0,"reference_eligible":0,"reading_order_verified":0,"ocr_verified":0}
            else:
                obj={"schema":index.PAGE_SCHEMA,"page_id":page,"book_key":"synthetic","collection_id":"FIX","ordinal":n,"source_sha256":source,"receipt_sha256":rh,"image_sha256":"a"*64,"algorithm_sha256":alg,"method":"vertical-word-bands-v2","status":"empty_geometry_unclassified","error_code":None,"layout":"unclassified","raw_line_count":0,"raw_word_count":0,"word_order":[],"segments":[],"bands":[],"raw_backward_band_steps":0,"new_backward_band_steps":0,"main":[],"ruby":[],"other":[],"main_ruby_role_status":"geometry_candidate_not_semantically_verified","geometry_candidate":False,"requires_review":1,"accepted":0,"human_confirmed":0,"human_verified":0,"semantic_verified":0,"reference_eligible":0,"reading_order_verified":0,"ocr_verified":0}
            raw=json.dumps(obj,separators=(",",":"),ensure_ascii=False).encode(); name=f"geometry-{n:02d}.json"; (self.phase/name).write_bytes(raw)
            entries.append({"page_id":page,"file":name,"sha256":hashlib.sha256(raw).hexdigest(),"status":obj["status"],"geometry_candidate":obj["geometry_candidate"]})
        manifest={"schema":index.SCHEMA,"source_sha256":source,"inventory_sha256":inv,"algorithm_sha256":alg,"helper_sha256":helper,"candidates":entries}
        mraw=json.dumps(manifest,separators=(",",":"),ensure_ascii=False).encode(); (self.phase/"manifest.json").write_bytes(mraw)
        verify={"schema":index.VERIFY_SCHEMA,"verified_at_utc":"2026-10-20T00:00:00Z","source_sha256":source,"inventory_sha256":inv,"algorithm_sha256":alg,"helper_sha256":helper,"manifest_sha256":hashlib.sha256(mraw).hexdigest(),"scope_pages":10,"candidate_pages":1,"unclassified_pages":9,"rejected_pages":0,"requires_review":1,"accepted":0,"human_confirmed":0,"human_verified":0,"semantic_verified":0,"reference_eligible":0,"reading_order_verified":0,"ocr_verified":0,"source_unchanged":True,"inputs_unchanged":True,"algorithm_unchanged":True,"qualification_flags_unchanged":True,"corpus_updated":False,"model_calls":0}
        (self.phase/"verification.json").write_text(json.dumps(verify,separators=(",",":")),encoding="utf-8")

    def _resign_first(self, mutate):
        path=self.phase/"geometry-01.json"
        obj=json.loads(path.read_text(encoding="utf-8")); mutate(obj)
        raw=json.dumps(obj,separators=(",",":"),ensure_ascii=False,allow_nan=True).encode()
        path.write_bytes(raw)
        manifest_path=self.phase/"manifest.json"; manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["candidates"][0]["sha256"]=hashlib.sha256(raw).hexdigest()
        mraw=json.dumps(manifest,separators=(",",":"),ensure_ascii=False).encode(); manifest_path.write_bytes(mraw)
        verify_path=self.phase/"verification.json"; verify=json.loads(verify_path.read_text(encoding="utf-8"))
        verify["manifest_sha256"]=hashlib.sha256(mraw).hexdigest(); verify_path.write_text(json.dumps(verify,separators=(",",":")),encoding="utf-8")
        return manifest["candidates"][0]["sha256"]

    def test_classified_candidate_detail_is_geometry_only(self):
        listed=index.list_candidates(self.corpus,self.db)
        self.assertTrue(listed["available"]); self.assertEqual((listed["total"],listed["items"][0]["status"]),(10,"geometry_candidate_unverified"))
        self.assertNotIn("synthetic",json.dumps(listed))
        item=listed["items"][0]
        detail=index.read_candidate(self.corpus,self.db,item["id"],item["candidate_sha256"])
        self.assertTrue(detail["candidate"]["geometry_candidate"])
        self.assertEqual(detail["candidate"]["segments"][0]["box"],[10,10,25,20])
        self.assertNotIn("text",json.dumps(detail)); self.assertNotIn("lines",detail)

    def test_resigned_structural_and_qualification_tampering_fails(self):
        cases=[
            ("duplicate_refs",lambda x:(x.update(word_order=[[0,0],[0,0]]),x["segments"][0].update(word_refs=[[0,0],[0,0]]))),
            ("out_of_range",lambda x:(x.update(word_order=[[0,0],[0,2]]),x["segments"][0].update(word_refs=[[0,0],[0,2]]))),
            ("box",lambda x:x["segments"][0].update(box=[10,10,24,20])),
            ("role_partition",lambda x:x.update(main=[],other=[0])),
            ("qualification",lambda x:x.update(accepted=True)),
            ("extra_text",lambda x:x.update(ocr_text="synthetic")),
            ("nan",lambda x:x["segments"][0].update(box=[float("nan"),10,25,20])),
            ("source",lambda x:x.update(source_sha256="0"*64)),
            ("image",lambda x:x.update(image_sha256="0"*64)),
            ("receipt",lambda x:x.update(receipt_sha256="0"*64)),
        ]
        for label,mutation in cases:
            with self.subTest(label=label):
                self._build()
                sha=self._resign_first(mutation)
                result=index.read_candidate(self.corpus,self.db,"FIX:0001",sha)
                self.assertFalse(result.get("available",True),label)

    def test_receipt_page_and_hash_are_bound(self):
        item=index.list_candidates(self.corpus,self.db)["items"][0]
        path=self.root/"private"/"ocr"/"FIX_0001.json"
        path.write_text("{}",encoding="utf-8")
        result=index.read_candidate(self.corpus,self.db,item["id"],item["candidate_sha256"])
        self.assertFalse(result.get("available",True))

    def test_words_outside_occupied_band_keep_nearest_midpoint_assignment(self):
        digest = self._resign_first(lambda c: c.update(bands=[[30, 80]]))
        result = index.read_candidate(self.corpus, self.db, "FIX:0001", digest)
        self.assertIn("candidate", result)

    def test_wrong_nearest_band_is_rejected_after_resigning(self):
        digest = self._resign_first(lambda c: c.update(
            bands=[[0, 5], [10, 30]], layout="vertical_multi_band"))
        result = index.read_candidate(self.corpus, self.db, "FIX:0001", digest)
        self.assertFalse(result.get("available", True))

    def test_unclassified_word_order_must_remain_raw_source_order(self):
        items=index.list_candidates(self.corpus,self.db)["items"]
        row=next(x for x in items if x["id"]=="FIX:0002")
        good=index.read_candidate(self.corpus,self.db,row["id"],row["candidate_sha256"])
        self.assertTrue(good["candidate"]["status"]=="unclassified_geometry_only")
        self.assertEqual(good["candidate"]["word_order"],[[0,0],[0,1]])
        sha=self._resign_first(lambda obj: None)  # re-signing first page keeps manifest machinery exercised
        obj=json.loads((self.phase/"geometry-02.json").read_text(encoding="utf-8"))
        obj["word_order"]=[[0,1],[0,0]]
        raw=json.dumps(obj,separators=(",",":"),ensure_ascii=False).encode()
        (self.phase/"geometry-02.json").write_bytes(raw)
        manifest_path=self.phase/"manifest.json"; manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["candidates"][1]["sha256"]=hashlib.sha256(raw).hexdigest()
        mraw=json.dumps(manifest,separators=(",",":"),ensure_ascii=False).encode(); manifest_path.write_bytes(mraw)
        verify_path=self.phase/"verification.json"; verify=json.loads(verify_path.read_text(encoding="utf-8"))
        verify["manifest_sha256"]=hashlib.sha256(mraw).hexdigest(); verify_path.write_text(json.dumps(verify,separators=(",",":")),encoding="utf-8")
        bad=index.read_candidate(self.corpus,self.db,"FIX:0002",manifest["candidates"][1]["sha256"])
        self.assertFalse(bad.get("available",True))

    def test_phase_change_during_index_read_is_rejected(self):
        original=index._read_file
        changed={"done":False}
        def racing(directory,name,maximum=index.MAX_FILE_BYTES):
            result=original(directory,name,maximum)
            if name=="geometry-01.json" and not changed["done"]:
                changed["done"]=True
                (directory/name).write_text("{}",encoding="utf-8")
            return result
        with mock.patch.object(index,"_read_file",side_effect=racing):
            self.assertFalse(index.overview(self.corpus,self.db)["available"])

    def _catalog(self, count=10, reject_last=False):
        templates = [json.loads((self.phase/f"geometry-{i:02d}.json").read_bytes()) for i in range(1, 11)]
        with sqlite3.connect(self.dbpath) as con:
            con.execute("CREATE TABLE scan_collections(id TEXT,book_key TEXT)")
            con.executemany("INSERT INTO scan_collections VALUES (?,?)", [("FIX", "fixture-a"), ("SECOND", "fixture-b")])
            for i in range(11, count+1):
                con.execute("INSERT INTO scan_pages VALUES (?,?,?,?,?,?,?,?,?)",(f"FIX:{i:04d}","SECOND",i,"unused.png","a"*64,100,200,"completed_unverified",self.rows[-1][1]))
        self.db.close()
        source = self.db.source_sha256(refresh=True)
        for path in self.phase.iterdir(): path.unlink()
        self.catalog_pages = []
        for i in range(1, count+1):
            candidate = json.loads(json.dumps(templates[min(i,10)-1]))
            collection = "FIX" if i <= 10 else "SECOND"
            candidate.update(schema=index.CATALOG_PAGE_SCHEMA, source_sha256=source, page_id=f"FIX:{i:04d}",
                collection_id=collection, ordinal=i, book_key="fixture-a" if i<=10 else "fixture-b",error_code=None)
            if reject_last and i == count:
                candidate.update(status=index.REJECT_STATUS, geometry_candidate=False, error_code="receipt_invalid")
                candidate.update({key:None for key in ("layout","raw_line_count","raw_word_count","word_order","segments","bands",
                    "raw_backward_band_steps","new_backward_band_steps","main","ruby","other")})
            name=f"geometry-{i:06d}.json"
            raw=json.dumps(candidate,separators=(",",":"),allow_nan=False).encode()
            (self.phase/name).write_bytes(raw)
            self.catalog_pages.append({"page_id":candidate["page_id"],"file":name,"candidate_sha256":hashlib.sha256(raw).hexdigest(),
                "collection_id":collection,"ordinal":i,"book_key":candidate["book_key"],"image_sha256":"a"*64,
                "ocr_sha256":candidate["receipt_sha256"],"width":100,"height":200,"status":candidate["status"],
                "geometry_candidate":candidate["geometry_candidate"],"raw_word_count":candidate["raw_word_count"],
                "raw_line_count":candidate["raw_line_count"],"segment_count":None if candidate["segments"] is None else len(candidate["segments"]),
                "error_code":candidate["error_code"]})
        for name in ("plan.json","progress.json"):
            (self.phase/name).write_text(json.dumps({"schema":"synthetic-metadata-v1","pages":count}),encoding="utf-8")
        self.catalog_manifest={"schema":index.CATALOG_SCHEMA,"source_sha256":source,"plan_sha256":"b"*64,
            "algorithm_sha256":"c"*64,"helper_module_sha256":"d"*64,"flags":dict(index.FLAGS),"pages":self.catalog_pages}
        self._sign_catalog()

    def _sign_catalog(self):
        manifest=self.catalog_manifest;rows=manifest["pages"]
        counts={"page_count":len(rows),"classified_count":sum(row["geometry_candidate"] for row in rows),
            "rejected_count":sum(row["status"]==index.REJECT_STATUS for row in rows),"unattempted_count":0}
        counts["unclassified_count"]=len(rows)-counts["classified_count"]-counts["rejected_count"]
        manifest.update(counts)
        raw=json.dumps(manifest,separators=(",",":"),allow_nan=False).encode();(self.phase/"manifest.json").write_bytes(raw)
        verify={"schema":index.CATALOG_VERIFY_SCHEMA,"verified_at_utc":"2026-10-20T00:00:00Z",
            **{key:manifest[key] for key in ("source_sha256","plan_sha256","algorithm_sha256","helper_module_sha256","flags")},
            **counts,"manifest_sha256":hashlib.sha256(raw).hexdigest(),
            "plan_file_sha256":hashlib.sha256((self.phase/"plan.json").read_bytes()).hexdigest(),
            "progress_file_sha256":hashlib.sha256((self.phase/"progress.json").read_bytes()).hexdigest(),
            "source_unchanged":True,"inputs_unchanged":True,"outputs_unchanged":True,"corpus_updated":False,"model_calls":0,"complete":True}
        (self.phase/"verification.json").write_text(json.dumps(verify,separators=(",",":")),encoding="utf-8")

    def _resign_catalog_candidate(self, number, mutate, *, update_row=True):
        row=self.catalog_pages[number-1];path=self.phase/row["file"];candidate=json.loads(path.read_bytes());mutate(candidate)
        raw=json.dumps(candidate,separators=(",",":"),allow_nan=False).encode();path.write_bytes(raw)
        row["candidate_sha256"]=hashlib.sha256(raw).hexdigest()
        if update_row:
            for key in ("status","geometry_candidate","raw_word_count","raw_line_count","error_code"):
                row[key]=candidate[key]
            row["segment_count"]=None if candidate["segments"] is None else len(candidate["segments"])
        self._sign_catalog();return row["candidate_sha256"]

    def test_catalog_4096_metadata_pagination_and_collection_do_not_read_geometry(self):
        self._catalog(4096)
        # An unselected file deliberately is not JSON: summary counts describe
        # metadata availability, never 4096 successfully validated details.
        (self.phase/"geometry-004096.json").write_bytes(b"not-json")
        opened=[];original=index._catalog_bytes
        def observed(directory,name,maximum):
            opened.append(name)
            self.assertFalse(name.startswith("geometry-"))
            return original(directory,name,maximum)
        with mock.patch.object(index,"_catalog_bytes",side_effect=observed), mock.patch.object(index,"_receipt_geometry",side_effect=AssertionError("detail only")), mock.patch.object(self.db,"query",wraps=self.db.query) as query, mock.patch.object(self.db,"source_sha256",wraps=self.db.source_sha256) as source_hash:
            overview=index.overview(self.corpus,self.db)
            self.assertTrue(overview["available"]);self.assertEqual(overview["total"],4096)
            self.assertTrue(overview["metadata_only"]);self.assertIsNone(overview["detail_validation_count"])
            self.assertEqual([c["page_count"] for c in overview["collections"]],[10,4086])
            self.assertEqual(sum("FROM scan_pages p JOIN" in c.args[0] for c in query.call_args_list),1)
            self.assertEqual(source_hash.call_count,2)
            listed=index.list_candidates(self.corpus,self.db,offset=100,limit=3,collection_id="SECOND")
        self.assertEqual((listed["total"],listed["offset"],listed["limit"],len(listed["items"])),(4086,100,3,3))
        self.assertEqual(listed["items"][0]["id"],"FIX:0111")
        self.assertNotIn("unused.png",json.dumps(listed));self.assertNotIn("text",json.dumps(listed))

    def test_catalog_detail_reads_selected_geometry_and_source_receipt(self):
        self._catalog();row=self.catalog_pages[0];opened=[];original=index._catalog_bytes
        def observed(directory,name,maximum):
            opened.append(name);return original(directory,name,maximum)
        with mock.patch.object(index,"_catalog_bytes",side_effect=observed):
            detail=index.read_candidate(self.corpus,self.db,row["page_id"],row["candidate_sha256"])
        self.assertIn("candidate",detail);self.assertTrue(detail["receipt_geometry_validated"])
        self.assertEqual({name for name in opened if name.startswith("geometry-")},{"geometry-000001.json"})
        self.assertEqual(detail["candidate"]["segments"][0]["box"],[10,10,25,20])
        self.assertNotIn("text",json.dumps(detail))

    def test_catalog_rejected_null_counts_and_image_metadata_are_available(self):
        self._catalog(reject_last=True)
        overview=index.overview(self.corpus,self.db)
        self.assertEqual((overview["classified_count"],overview["unclassified_count"],overview["rejected_count"]),(1,8,1))
        row=index.list_candidates(self.corpus,self.db)["items"][-1]
        self.assertEqual(row["scan_ordinal"],10);self.assertIsNone(row["raw_word_count"]);self.assertIsNone(row["segment_count"])
        detail=index.read_candidate(self.corpus,self.db,row["id"],row["candidate_sha256"])
        self.assertIn("candidate",detail);self.assertTrue(detail["image_bound"]);self.assertFalse(detail["receipt_geometry_validated"])
        self.assertIsNone(detail["candidate"]["word_order"])

    def test_catalog_resigned_structure_flags_and_dimensions_are_rejected(self):
        self._catalog()
        cases=[lambda c:c.update(accepted=True),lambda c:c["segments"][0].update(box=[10,10,24,20]),
            lambda c:c.update(word_order=[[0,0],[0,0]]),lambda c:c.update(extra_text="synthetic"),
            lambda c:c.update(receipt_sha256="0"*64),lambda c:c.update(image_sha256="0"*64)]
        original=json.loads((self.phase/"geometry-000001.json").read_bytes())
        for mutate in cases:
            with self.subTest(mutation=cases.index(mutate)):
                (self.phase/"geometry-000001.json").write_text(json.dumps(original),encoding="utf-8")
                digest=self._resign_catalog_candidate(1,mutate)
                self.assertFalse(index.read_candidate(self.corpus,self.db,"FIX:0001",digest).get("available",True))
        self.catalog_pages[0]["width"]=101;self._sign_catalog()
        self.assertFalse(index.overview(self.corpus,self.db)["available"])

    def test_catalog_manifest_and_selected_hash_receipt_tampering(self):
        self._catalog();row=self.catalog_pages[0];original=(self.phase/row["file"]).read_bytes()
        (self.phase/row["file"]).write_bytes(b"{}")
        self.assertTrue(index.overview(self.corpus,self.db)["available"])
        self.assertEqual(index.read_candidate(self.corpus,self.db,row["page_id"],row["candidate_sha256"])["error"],"ocr_reading_order_hash_mismatch")
        # Restore a well-bound selected geometry, then change its source receipt.
        (self.phase/row["file"]).write_bytes(original)
        (self.root/"private/ocr/FIX_0001.json").write_bytes(b"{}")
        self.assertEqual(index.read_candidate(self.corpus,self.db,row["page_id"],row["candidate_sha256"])["error"],"ocr_reading_order_receipt_mismatch")

    def test_catalog_pagination_and_unknown_collection(self):
        self._catalog()
        for kwargs in ({"offset":True},{"offset":-1},{"limit":0},{"limit":101},{"collection_id":[]},{"collection_id":"../bad"}):
            self.assertFalse(index.list_candidates(self.corpus,self.db,**kwargs)["available"])
        self.assertEqual(index.list_candidates(self.corpus,self.db,collection_id="UNKNOWN")["items"],[])
        self.assertEqual(index.list_candidates(self.corpus,self.db,offset=100)["items"],[])

    def test_catalog_duplicate_page_and_nonexact_directory_are_rejected(self):
        self._catalog();self.catalog_pages[1]["page_id"]=self.catalog_pages[0]["page_id"];self._sign_catalog()
        self.assertFalse(index.overview(self.corpus,self.db)["available"])
        self.catalog_pages[1]["page_id"]="FIX:0002";self._sign_catalog();(self.phase/"extra.json").write_bytes(b"{}")
        self.assertFalse(index.overview(self.corpus,self.db)["available"])

    def test_catalog_final_manifest_progress_and_source_guards(self):
        self._catalog();original=index._catalog_db_pages
        def racing(database,rows):
            result=original(database,rows);(self.phase/"progress.json").write_bytes(b"{}");return result
        with mock.patch.object(index,"_catalog_db_pages",side_effect=racing):
            self.assertFalse(index.overview(self.corpus,self.db)["available"])


    def test_catalog_limits_and_signed_manifest_flags(self):
        self._catalog()
        self.catalog_manifest["flags"]["accepted"]=True;self._sign_catalog()
        self.assertFalse(index.overview(self.corpus,self.db)["available"])
        self.catalog_manifest["flags"]["accepted"]=0;self._sign_catalog()
        with (self.phase/"geometry-000010.json").open("r+b") as stream:
            stream.truncate(index.MAX_FILE_BYTES+1)
        self.assertEqual(index.overview(self.corpus,self.db)["error"],"ocr_reading_order_limits_exceeded")
        (self.phase/"geometry-000010.json").write_bytes(b"{}")
        self.catalog_manifest["pages"] = [dict(self.catalog_pages[0]) for _ in range(index.MAX_PAGES+1)]
        self._sign_catalog()
        self.assertEqual(index.overview(self.corpus,self.db)["error"],"ocr_reading_order_limits_exceeded")

    def test_catalog_source_changes_and_duplicate_collection_are_rejected(self):
        self._catalog();source=self.catalog_manifest["source_sha256"]
        with mock.patch.object(self.db,"source_sha256",side_effect=[source,"0"*64]):
            self.assertEqual(index.overview(self.corpus,self.db)["error"],"ocr_reading_order_source_mismatch")
        self.db.close()
        with sqlite3.connect(self.dbpath) as con:
            con.execute("INSERT INTO scan_collections VALUES ('FIX','fixture-a')")
        source=self.db.source_sha256(refresh=True)
        self.catalog_manifest["source_sha256"]=source;self._sign_catalog()
        self.assertEqual(index.overview(self.corpus,self.db)["error"],"ocr_reading_order_page_mismatch")

    def test_catalog_reparse_entry_is_rejected_without_following_it(self):
        self._catalog()
        entry=SimpleNamespace(name="manifest.json",stat=lambda follow_symlinks:SimpleNamespace(
            st_mode=0o100600,st_file_attributes=0x400,st_size=1))
        with mock.patch.object(index.os,"scandir") as scandir:
            scandir.return_value.__enter__.return_value=iter([entry])
            self.assertEqual(index.overview(self.corpus,self.db)["error"],"ocr_reading_order_invalid")

    def test_catalog_selected_and_manifest_change_during_detail_is_rejected(self):
        self._catalog();row=self.catalog_pages[0];original=index._validate_geometry
        def racing(*args):
            original(*args)
            (self.phase/"manifest.json").write_bytes(b"{}")
        with mock.patch.object(index,"_validate_geometry",side_effect=racing):
            self.assertFalse(index.read_candidate(self.corpus,self.db,row["page_id"],row["candidate_sha256"]).get("available",True))


if __name__=="__main__": unittest.main()
