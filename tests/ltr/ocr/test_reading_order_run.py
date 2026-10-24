import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from PIL import Image

from ltr.ocr import reading_order_geometry as geometry
from ltr.ocr import reading_order_run as run
from ltr.ocr import reading_order_index as catalog_index
from ltr.workbench import CorpusConfig, CorpusDatabase

class GeometryRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.addCleanup(self.tmp.cleanup)
        base=Path(self.tmp.name); self.root=base/"source"; (self.root/"private"/"scans").mkdir(parents=True); (self.root/"private"/"ocr").mkdir()
        image_stream=io.BytesIO()
        Image.new("RGB",(100,200),(17,31,47)).save(image_stream,format="PNG")
        self.image_raw=image_stream.getvalue()
        (self.root/"private"/"scans"/"p.png").write_bytes(self.image_raw)
        self.db=self.root/"dialogue.sqlite"
        self.words=[]
        for n in range(4):
            self.words.append({"x":20,"y":20+n*12,"w":5,"h":30,"text":"synthetic latin"})
        receipt={"schema":"haruhi-page-ocr-v1","page_id":"FIX:p0001","image_sha256":hashlib.sha256(self.image_raw).hexdigest(),"width":100,"height":200,"engine":"Windows.Media.Ocr","language":"ja","status":"completed","human_verified":False,"lines":[{"words":[word]} for word in self.words]}
        self.receipt_raw=json.dumps(receipt,separators=(",",":"),ensure_ascii=False).encode()
        (self.root/"private"/"ocr"/"COLL_0001.json").write_bytes(self.receipt_raw)
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE scan_collections(id TEXT PRIMARY KEY,book_key TEXT)")
            con.execute("INSERT INTO scan_collections VALUES (?,?)",("COLL","fixture-book"))
            con.execute("CREATE TABLE scan_pages(id TEXT PRIMARY KEY,collection_id TEXT,ordinal INTEGER,image_path TEXT,image_sha256 TEXT,width INTEGER,height INTEGER,ocr_status TEXT,ocr_sha256 TEXT)")
            con.execute("INSERT INTO scan_pages VALUES (?,?,?,?,?,?,?,?,?)",("FIX:p0001","COLL",1,"p.png",receipt["image_sha256"],100,200,"completed_unverified",hashlib.sha256(self.receipt_raw).hexdigest()))
        self.ids=base/"ids.json"; self.ids.write_text(json.dumps(["FIX:p0001"]),encoding="utf-8")
        self.out=base/"isolated-output"
        self.input_budget=2*1024*1024; self.output_budget=2*1024*1024

    def test_plan_execute_and_terminal_catalog_are_source_bound(self):
        planned=run.build_plan(self.db,self.out,self.ids,max_pages=2,max_total_input_bytes=self.input_budget,max_total_output_bytes=self.output_budget)
        self.assertEqual(planned["status"],"planned")
        self.assertEqual({p.name for p in self.out.iterdir()},{"plan.json","progress.json"})
        original=self.db.read_bytes()
        result=run.execute_plan(self.db,self.out,self.ids,max_pages=2,max_total_input_bytes=self.input_budget,max_total_output_bytes=self.output_budget)
        self.assertEqual((result["status"],result["page_count"],result["unattempted_count"]),("complete",1,0))
        self.assertEqual(self.db.read_bytes(),original)
        candidate=json.loads((self.out/"geometry-000001.json").read_text(encoding="utf-8"))
        self.assertEqual(candidate["schema"],"corpus-reading-order-page-v1")
        self.assertNotIn("text",json.dumps(candidate))
        self.assertEqual(candidate["requires_review"],1)
        self.assertEqual(candidate["accepted"],0)
        manifest=json.loads((self.out/"manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"],"corpus-reading-order-manifest-v1")
        self.assertEqual(manifest["pages"][0]["candidate_sha256"],hashlib.sha256((self.out/"geometry-000001.json").read_bytes()).hexdigest())
        verify=json.loads((self.out/"verification.json").read_text(encoding="utf-8"))
        self.assertTrue(verify["complete"]); self.assertEqual(verify["model_calls"],0)
        corpus=CorpusConfig("fixture","Synthetic",self.root,ocr_reading_order_candidates=self.out)
        database=CorpusDatabase(corpus); self.addCleanup(database.close)
        self.assertTrue(catalog_index.overview(corpus,database)["available"])
        listed=catalog_index.list_candidates(corpus,database,limit=1)
        self.assertEqual(listed["total"],1)
        detail=catalog_index.read_candidate(corpus,database,"FIX:p0001",manifest["pages"][0]["candidate_sha256"])
        self.assertEqual(detail["candidate"]["schema"],"corpus-reading-order-page-v1")
        self.assertNotIn("synthetic latin",json.dumps(detail))

    def test_changed_receipt_stops_without_manifest(self):
        run.build_plan(self.db,self.out,self.ids,max_pages=2,max_total_input_bytes=self.input_budget,max_total_output_bytes=self.output_budget)
        (self.root/"private"/"ocr"/"COLL_0001.json").write_bytes(self.receipt_raw+b" ")
        with self.assertRaises(run.RunError) as caught:
            run.execute_plan(self.db,self.out,self.ids,max_pages=2,max_total_input_bytes=self.input_budget,max_total_output_bytes=self.output_budget)
        self.assertEqual(caught.exception.code,"geometry_receipt_hash_mismatch")
        self.assertFalse((self.out/"manifest.json").exists())

    def test_image_bytes_are_verified_during_plan_and_execute(self):
        changed=io.BytesIO(); Image.new("RGB",(100,200),(18,31,47)).save(changed,format="PNG")
        self.assertNotEqual(hashlib.sha256(changed.getvalue()).hexdigest(),hashlib.sha256(self.image_raw).hexdigest())
        run.build_plan(self.db,self.out,self.ids,max_pages=2,max_total_input_bytes=self.input_budget,max_total_output_bytes=self.output_budget)
        (self.root/"private"/"scans"/"p.png").write_bytes(changed.getvalue())
        with self.assertRaises(run.RunError) as caught:
            run.execute_plan(self.db,self.out,self.ids,max_pages=2,max_total_input_bytes=self.input_budget,max_total_output_bytes=self.output_budget)
        self.assertEqual(caught.exception.code,"geometry_image_hash_mismatch")
        self.assertFalse((self.out/"manifest.json").exists())

    def test_plan_rejects_actual_image_dimension_mismatch(self):
        changed=io.BytesIO(); Image.new("RGB",(101,200),(17,31,47)).save(changed,format="PNG")
        image_raw=changed.getvalue(); image_sha=hashlib.sha256(image_raw).hexdigest()
        (self.root/"private"/"scans"/"p.png").write_bytes(image_raw)
        receipt=json.loads(self.receipt_raw.decode("utf-8")); receipt.update(image_sha256=image_sha,width=100)
        raw=json.dumps(receipt,separators=(",",":"),ensure_ascii=False).encode()
        (self.root/"private"/"ocr"/"COLL_0001.json").write_bytes(raw)
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE scan_pages SET image_sha256=?,ocr_sha256=? WHERE id=?",(image_sha,hashlib.sha256(raw).hexdigest(),"FIX:p0001"))
        with self.assertRaises(run.RunError) as caught:
            run.build_plan(self.db,self.out,self.ids,max_pages=2,max_total_input_bytes=self.input_budget,max_total_output_bytes=self.output_budget)
        self.assertEqual(caught.exception.code,"geometry_image_dimensions_mismatch")

    def test_ordered_selector_and_tampered_progress_are_rejected(self):
        receipt2=json.loads(self.receipt_raw.decode("utf-8")); receipt2["page_id"]="FIX:p0002"
        raw2=json.dumps(receipt2,separators=(",",":"),ensure_ascii=False).encode()
        (self.root/"private"/"ocr"/"COLL_0002.json").write_bytes(raw2)
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO scan_pages VALUES (?,?,?,?,?,?,?,?,?)",("FIX:p0002","COLL",2,"p.png",hashlib.sha256(self.image_raw).hexdigest(),100,200,"completed_unverified",hashlib.sha256(raw2).hexdigest()))
        ids2=self.ids.parent/"ids2.json"; ids2.write_text(json.dumps(["FIX:p0001","FIX:p0002"]),encoding="utf-8")
        run.build_plan(self.db,self.out,ids2,max_pages=3,max_total_input_bytes=self.input_budget,max_total_output_bytes=self.output_budget)
        raw=(self.out/"plan.json").read_bytes(); plan=json.loads(raw)
        plan["pages"].reverse(); plan_raw=run._json_bytes(plan); plan_sha=hashlib.sha256(plan_raw).hexdigest()
        (self.out/"plan.json").write_bytes(plan_raw)
        progress=run._progress_payload(plan["source_sha256"],plan_sha,plan["pages"],[],"planned")
        (self.out/"progress.json").write_bytes(run._json_bytes(progress))
        with self.assertRaises(run.RunError) as caught:
            run.execute_plan(self.db,self.out,ids2,max_pages=3,max_total_input_bytes=self.input_budget,max_total_output_bytes=self.output_budget)
        self.assertEqual(caught.exception.code,"geometry_plan_mismatch")
        self.assertFalse((self.out/"manifest.json").exists())

    def test_external_progress_change_is_not_overwritten(self):
        run.build_plan(self.db,self.out,self.ids,max_pages=2,max_total_input_bytes=self.input_budget,max_total_output_bytes=self.output_budget)
        progress_path=self.out/"progress.json"; progress_path.write_bytes(progress_path.read_bytes()+b" ")
        changed=progress_path.read_bytes()
        with self.assertRaises(run.RunError) as caught:
            run.execute_plan(self.db,self.out,self.ids,max_pages=2,max_total_input_bytes=self.input_budget,max_total_output_bytes=self.output_budget)
        self.assertEqual(caught.exception.code,"geometry_progress_invalid")
        self.assertEqual(progress_path.read_bytes(),changed)
        self.assertFalse((self.out/"manifest.json").exists())

    def test_input_budget_counts_image_and_receipt(self):
        with self.assertRaises(run.RunError) as caught:
            run.build_plan(self.db,self.out,self.ids,max_pages=2,max_total_input_bytes=len(self.receipt_raw)+len(self.image_raw)-1,max_total_output_bytes=self.output_budget)
        self.assertEqual(caught.exception.code,"geometry_input_budget_exceeded")

    def test_exclude_selector_remains_bound_to_plan(self):
        excluded=self.ids.parent/"excluded.json"; excluded.write_text(json.dumps(["FIX:excluded"]),encoding="utf-8")
        run.build_plan(self.db,self.out,self.ids,excluded,max_pages=2,max_total_input_bytes=self.input_budget,max_total_output_bytes=self.output_budget)
        excluded.write_text(json.dumps(["FIX:other"]),encoding="utf-8")
        with self.assertRaises(run.RunError) as caught:
            run.execute_plan(self.db,self.out,self.ids,excluded,max_pages=2,max_total_input_bytes=self.input_budget,max_total_output_bytes=self.output_budget)
        self.assertEqual(caught.exception.code,"geometry_plan_mismatch")
        self.assertFalse((self.out/"manifest.json").exists())

    def test_source_change_stops_publication(self):
        run.build_plan(self.db,self.out,self.ids,max_pages=2,max_total_input_bytes=self.input_budget,max_total_output_bytes=self.output_budget)
        with sqlite3.connect(self.db) as con: con.execute("UPDATE scan_collections SET book_key='changed-fixture' WHERE id='COLL'")
        with self.assertRaises(run.RunError) as caught:
            run.execute_plan(self.db,self.out,self.ids,max_pages=2,max_total_input_bytes=self.input_budget,max_total_output_bytes=self.output_budget)
        self.assertEqual(caught.exception.code,"geometry_source_changed")
        self.assertFalse((self.out/"manifest.json").exists())

    def test_selector_duplicates_and_boolean_coordinate_are_rejected_or_quarantined(self):
        with self.assertRaises(geometry.GeometryError): geometry.word_order([{"words":[{"x":True,"y":0,"w":1,"h":1}]}],10,10)
        dup=self.out.parent/"duplicate-ids.json";dup.write_text(json.dumps(["FIX:p0001","FIX:p0001"]),encoding="utf-8")
        with self.assertRaises(run.RunError):
            run.build_plan(self.db,self.out,dup,max_pages=3,max_total_input_bytes=self.input_budget,max_total_output_bytes=self.output_budget)

    def test_invalid_geometry_is_a_quarantined_reject_not_zero(self):
        bad=json.loads(self.receipt_raw.decode("utf-8"))
        bad["lines"][0]["words"][0]["x"]=True
        raw=json.dumps(bad,separators=(",",":"),ensure_ascii=False).encode()
        (self.root/"private"/"ocr"/"COLL_0001.json").write_bytes(raw)
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE scan_pages SET ocr_sha256=? WHERE id=?",(hashlib.sha256(raw).hexdigest(),"FIX:p0001"))
        run.build_plan(self.db,self.out,self.ids,max_pages=2,max_total_input_bytes=self.input_budget,max_total_output_bytes=self.output_budget)
        result=run.execute_plan(self.db,self.out,self.ids,max_pages=2,max_total_input_bytes=self.input_budget,max_total_output_bytes=self.output_budget)
        self.assertEqual((result["rejected_count"],result["unattempted_count"]),(1,0))
        candidate=json.loads((self.out/"geometry-000001.json").read_text(encoding="utf-8"))
        self.assertEqual(candidate["status"],"geometry_input_rejected")
        self.assertEqual(candidate["error_code"],"geometry_invalid")
        self.assertIsNone(candidate["raw_word_count"])
        self.assertIsNone(candidate["word_order"])
        self.assertEqual(candidate["requires_review"],1)
        self.assertEqual(candidate["accepted"],0)

    def test_budget_failure_never_publishes_complete_catalog(self):
        budget=3000
        run.build_plan(self.db,self.out,self.ids,max_pages=2,max_total_input_bytes=self.input_budget,max_total_output_bytes=budget)
        with self.assertRaises(run.RunError):
            run.execute_plan(self.db,self.out,self.ids,max_pages=2,max_total_input_bytes=self.input_budget,max_total_output_bytes=budget)
        self.assertFalse((self.out/"manifest.json").exists())

if __name__=="__main__": unittest.main()
