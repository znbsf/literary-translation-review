import copy, hashlib, importlib.util, tempfile, unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location("check",Path(__file__).parents[1]/"scripts/check_work_understanding.py")
check=importlib.util.module_from_spec(spec); spec.loader.exec_module(check)
class ValidatorTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
  raw="alpha\nbeta\n".encode(); (self.root/"source.txt").write_bytes(raw)
  self.record={"id":"r1","dimension":"events","kind":"fact","statement":"x","evidence":[{"start_line":1,"end_line":1}],"entities":[],"story_time":None,"disclosed_at":None,"uncertainty":None,"status":"candidate"}
  self.data={"schema":"work-understanding-records-v1","work_id":"w","source":{"edition_id":"e","sha256":hashlib.sha256(raw).hexdigest(),"path":"source.txt","language":"zh","locator_kind":"source_txt_line_1based_inclusive"},"reading":{"machine_read_ranges":[[1,2]],"human_confirmed":False,"limitations":[]},"records":[self.record],"relations":[],"coverage":[{"dimension":d,"status":"candidate_covered" if d=="events" else "not_started","record_ids":["r1"] if d=="events" else [],"gaps":[]} for d in check.DIMS],"questions":[]}
 def errors(self): return check.validate(self.data,self.root)[0]
 def test_valid(self): self.assertEqual(self.errors(),[])
 def test_hash(self): self.data["source"]["sha256"]="bad"; self.assertIn("source_hash",self.errors())
 def test_range(self): self.record["evidence"][0]["end_line"]=3; self.assertIn("evidence_range",self.errors())
 def test_anchor_bound_to_cited_lines(self):
  self.record["evidence"][0]["anchor"]="alpha"; self.assertEqual(self.errors(),[])
  self.record["evidence"][0]["anchor"]="beta"; self.assertIn("evidence_anchor",self.errors())
 def test_anchor_empty_or_nonstring(self):
  for value in ["", " ", None, []]:
   self.record["evidence"][0]["anchor"]=value; self.assertIn("evidence_anchor",self.errors())
 def test_multiline_anchor(self):
  self.record["evidence"][0].update(end_line=2,anchor="alpha\nbeta"); self.assertEqual(self.errors(),[])
 def test_duplicate_and_endpoint(self):
  self.data["records"].append(copy.deepcopy(self.record)); self.data["relations"].append({"id":"x","from":"r1","to":"missing","type":"causes","statement":"x","evidence":[{"start_line":1,"end_line":1}],"kind":"interpretation","status":"candidate"}); self.assertIn("duplicate_id",self.errors()); self.assertIn("relation_endpoint",self.errors())
 def test_false_coverage(self):
  c=next(x for x in self.data["coverage"] if x["dimension"]=="characters"); c.update(status="candidate_covered",record_ids=["r1"]); self.assertIn("coverage_records",self.errors())
 def test_human_flag(self): self.data["reading"]["human_confirmed"]=True; self.assertIn("human_confirmation",self.errors())
 def test_flags_and_disclosure(self):
  self.record["accepted"]=True; self.record["disclosed_at"]=3
  self.assertIn("candidate_confirmation",self.errors()); self.assertIn("disclosed_at",self.errors())
 def test_locator_and_overlap(self):
  self.data["source"]["locator_kind"]="line"; self.data["reading"]["machine_read_ranges"]=[[1,2],[2,2]]
  errors,counts=check.validate(self.data,self.root); self.assertIn("locator_kind",errors); self.assertIn("reading_overlap",errors); self.assertEqual(counts["machine_read_lines"],2)
 def test_relation_types_and_endpoint_safety(self):
  self.data["relations"].append({"id":"x","from":[],"to":"r1","type":"bad","statement":" ","evidence":[{"start_line":1,"end_line":1}],"kind":"bad","status":"candidate","human_verified":True})
  e=self.errors(); self.assertIn("relation_endpoint",e); self.assertIn("relation_type",e); self.assertIn("relation_kind",e); self.assertIn("candidate_confirmation",e)
 def test_manifest_identity(self):
  import json
  src=self.data["source"]; manifest=self.root/"manifest.json"; manifest.write_text(json.dumps({"sources":[{"path":src["path"],"sha256":src["sha256"],"edition_id":"wrong","language":"zh","work_id":"w"}]}),encoding="utf-8")
  self.assertIn("manifest_source",check.validate(self.data,self.root,manifest)[0])
if __name__=="__main__": unittest.main()
