"""Synthetic orchestration; every model stage is mocked."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from ltr.project import series_workflow as workflow
from ltr.project.dimension_cycle import CycleError
from ltr.project.storage import write_json, digest


class SeriesWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = {"book_hash": "synthetic", "paragraphs": [
            {"id": f"P{i}", "text": "合成" * 500, "locator": {"start": 0}} for i in range(5)]}
        write_json(self.root / "source.json", self.source)

    def fake_stage(self, root, stage, model, **kwargs):
        passage = workflow._read(root / "passage.json")
        value=({'units':[{'id':p['id'],'target':'合成译文'} for p in passage['paragraphs']]}
               if stage=='retranslate' else {'synthetic':True})
        write_json(root / workflow.STAGES[stage], value)
        write_json(root / (stage + ".claim.json"), {"status": "completed",
            "source_digest": passage["source_digest"], 'result_digest':digest(value)})

    def test_partition_covers_every_id_in_order_once(self):
        chunks = workflow.partition_source(self.source)
        self.assertEqual(len(chunks), 3)
        self.assertEqual([p["id"] for c in chunks for p in c["paragraphs"]], [f"P{i}" for i in range(5)])
        self.assertTrue(all(c["paragraphs"] for c in chunks))
        self.assertEqual(sum(c["characters"] for c in chunks), 5000)

    def test_long_single_paragraph_remains_intact(self):
        chunks = workflow.partition_source(self.source, target_chars=100)
        self.assertEqual(len(chunks), 5)
        self.assertEqual(chunks[0]["paragraphs"], self.source["paragraphs"][:1])

    def test_blank_layout_preserved_outside_translation_units(self):
        rows=[{'id':'space1','text':'　'},self.source['paragraphs'][0],
              {'id':'space2','text':'\n'},self.source['paragraphs'][1],{'id':'space3','text':''}]
        chunks=workflow.partition_source({'book_hash':'s','paragraphs':rows},target_chars=500)
        self.assertEqual([p['id'] for c in chunks for p in c['paragraphs']],['P0','P1'])
        self.assertEqual([p['id'] for c in chunks for p in c['layout_units']],['space1','space2','space3'])
        self.assertEqual(sum(len(c['paragraphs'])+len(c['layout_units']) for c in chunks),len(rows))

    def test_invalid_ids_and_empty_source_rejected(self):
        for rows in ([], [self.source["paragraphs"][0]] * 2, [{"id": "x", "text": " "}]):
            with self.assertRaises(CycleError):
                workflow.partition_source({"book_hash": "s", "paragraphs": rows})

    def test_complete_sequence_budget_resume_and_empty_reference(self):
        with patch.object(workflow, "run_stage", side_effect=self.fake_stage) as stages, \
                patch.object(workflow, "render", return_value={"status": "synthetic"}), \
                contextlib.redirect_stdout(io.StringIO()):
            result = workflow.run_chapter(self.root, "synthetic-model")
            self.assertEqual(stages.call_count, 16)
            self.assertEqual([call.args[1] for call in stages.call_args_list],
                             ["context"] + list(workflow.STAGES)[1:] * 3)
            self.assertEqual(result["planned_calls"], 16)
            assembled=workflow._read(self.root/'chapter-translation-v2.json')
            self.assertEqual([u['id'] for u in assembled['units']],[p['id'] for p in self.source['paragraphs']])
            reference = workflow._read(self.root / "chunks/0000/reference-window.json")
            self.assertEqual(reference["paragraphs"], [])
            self.assertEqual(reference["scope"]["status"], "no_existing_translation")
            second = workflow.run_chapter(self.root, "synthetic-model")
            self.assertEqual(second["new_calls_reserved"], 0)
            self.assertEqual(stages.call_count, 16)

    def test_failed_claim_stops_without_retry(self):
        write_json(self.root / "context.claim.json", {"status": "failed"})
        with patch.object(workflow, "run_stage") as stages:
            with self.assertRaisesRegex(CycleError, "stage_claim_requires_resolution"):
                workflow.run_chapter(self.root, "synthetic")
            stages.assert_not_called()
        self.assertEqual(workflow._read(self.root / "workflow-progress.json")["status"], "stopped")

    def test_modified_completed_output_stops_resume(self):
        with patch.object(workflow,'run_stage',side_effect=self.fake_stage), \
                patch.object(workflow,'render',return_value={}),contextlib.redirect_stdout(io.StringIO()):
            workflow.run_chapter(self.root,'synthetic')
        write_json(self.root/'chapter-context.json',{'synthetic':False})
        with patch.object(workflow,'run_stage') as stages:
            with self.assertRaisesRegex(CycleError,'stage_claim_requires_resolution'):
                workflow.run_chapter(self.root,'synthetic')
            stages.assert_not_called()

    def test_chunk_mismatch_is_not_overwritten(self):
        write_json(self.root / "chapter-context.json", {"synthetic": True})
        passage = workflow.partition_source(self.source)[0]
        chunk = workflow.prepare_chunk(self.root, 0, passage)
        changed = dict(passage, horizon="changed")
        with self.assertRaisesRegex(CycleError, "immutable_artifact_mismatch"):
            workflow.prepare_chunk(self.root, 0, changed)
        self.assertEqual(workflow._read(chunk / "passage.json"), passage)

    def test_prepare_source_immutable_and_never_calls_models(self):
        with patch.object(workflow, "run_stage") as model:
            with self.assertRaisesRegex(CycleError, "source_already_prepared"):
                workflow.prepare_chapter("synthetic.epub", "synthetic", self.root)
            other = self.root / "fresh"
            with patch.object(workflow, "locate_chapter", return_value={}), \
                    patch.object(workflow, "extract_selected_chapter", return_value=self.source):
                result = workflow.prepare_chapter("synthetic.epub", "synthetic", other)
            self.assertEqual(result["chunks"], 3)
            model.assert_not_called()

    def test_failure_during_call_does_not_advance(self):
        with patch.object(workflow, "run_stage", side_effect=CycleError("synthetic_failure")) as stage:
            with self.assertRaises(CycleError):
                workflow.run_chapter(self.root, "synthetic")
            self.assertEqual(stage.call_count, 1)
        self.assertFalse((self.root / "aggregate.json").exists())


if __name__ == "__main__":
    unittest.main()
