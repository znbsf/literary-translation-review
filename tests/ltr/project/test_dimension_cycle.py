"""Synthetic integrity checks for private runtime orchestration."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from ltr.project.dimension_cycle import (CycleError, evidence_anchors, hydrate_evidence,
    validate_quotes, validate_translation, revised_dimensions, validate_comparison, run_stage)
from ltr.project.storage import write_json, digest


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.rows=[{'id':'p1','text':'Synthetic source one.'},{'id':'p2','text':'Another synthetic sentence.'}]
        self.passage={'paragraphs':self.rows,'source_digest':digest(self.rows)}
        self.dimensions={'dimensions':[{'id':'D01','rule':'Keep qualification.'}]}

    def test_anchor_hydration_is_exact(self):
        value={'evidence':[{'anchor_id':'E0000_0000'}]}
        hydrate_evidence(value,evidence_anchors(self.rows))
        self.assertEqual(validate_quotes(value,self.rows),1)

    def test_unknown_anchor_rejected(self):
        with self.assertRaises(CycleError):
            hydrate_evidence({'anchor_id':'unknown'},evidence_anchors(self.rows))

    def test_approximate_quote_rejected(self):
        with self.assertRaises(CycleError):
            validate_quotes({'paragraph_id':'p1','quote':'synthetic source one.'},self.rows)

    def test_duplicate_translation_coverage_rejected(self):
        with self.assertRaises(CycleError):
            validate_translation({'units':[{'id':'p1','target':'a'},{'id':'p1','target':'b'}]},self.passage)

    def test_real_rule_is_replaced_without_mutating_original(self):
        changes={'dimension_changes':[{'dimension_id':'D01','new_rule':'Keep negative qualification.','reason':'synthetic'}]}
        result=revised_dimensions(self.dimensions,changes)
        self.assertEqual(result['dimensions'][0]['rule'],'Keep negative qualification.')
        self.assertEqual(self.dimensions['dimensions'][0]['rule'],'Keep qualification.')

    def test_invented_reference_id_rejected(self):
        result={'alignment':{'status':'matched','reference_ids':['R999']},'findings':[],'dimension_changes':[]}
        with self.assertRaises(CycleError):
            validate_comparison(result,{'paragraphs':[{'id':'R001'}]},self.dimensions,self.passage)

    def test_first_translation_does_not_receive_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            write_json(root/'passage.json',self.passage)
            write_json(root/'dimensions-v1.json',self.dimensions)
            write_json(root/'reference-window.json',{'secret_reference':'not permitted in first draft'})
            def mock_model(model,prompt,payload):
                self.assertNotIn('reference',payload)
                self.assertNotIn('not permitted',json.dumps(payload))
                return {'units':[{'id':r['id'],'target':'Synthetic translation'} for r in self.rows]},0.1
            with patch('ltr.project.dimension_cycle.call_model',side_effect=mock_model), patch('builtins.print'):
                run_stage(root,'translate','synthetic-model')
            with patch('ltr.project.dimension_cycle.call_model') as model:
                with self.assertRaises(CycleError): run_stage(root,'translate','synthetic-model')
                model.assert_not_called()

    def test_failed_attempt_preserved_and_retry_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            write_json(root/'passage.json',self.passage)
            with patch('ltr.project.dimension_cycle.call_model',side_effect=CycleError('synthetic_failure')):
                with self.assertRaises(CycleError): run_stage(root,'dimensions','synthetic-model')
            with patch('ltr.project.dimension_cycle.call_model') as model:
                with self.assertRaises(CycleError): run_stage(root,'dimensions','synthetic-model')
                model.assert_not_called()
            result={'dimensions':[{'evidence':[{'anchor_id':'E0000_0000'}]}]}
            with patch('ltr.project.dimension_cycle.call_model',return_value=(result,0.1)),patch('builtins.print'):
                run_stage(root,'dimensions','synthetic-model',new_attempt=True)
            self.assertTrue((root/'dimensions.attempt-1.claim.json').exists())
            self.assertEqual(json.loads((root/'dimensions.claim.json').read_text())['attempt'],2)


if __name__=='__main__': unittest.main()
