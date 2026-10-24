from copy import deepcopy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from ltr.project.learning_finalize import ready, finish


class FinalizeTests(unittest.TestCase):
    def state(self):
        return {'planned_jobs':3,'planned_books':1,'counts':{
            'jobs':{'completed':3,'reserved':0,'failed':0},
            'merges':{'completed':1,'reserved':0,'failed':0}}}

    def test_complete_counts_with_failures_are_not_ready(self):
        self.assertTrue(ready(self.state()))
        for phase in ('jobs','merges'):
            for field in ('failed','reserved'):
                value=deepcopy(self.state());value['counts'][phase][field]=1
                self.assertFalse(ready(value))

    def test_stop_marker_prevents_synthesis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'STOP').write_text('synthetic pause')
            with patch('ltr.project.learning_finalize.synthesize') as model:
                self.assertEqual(finish(root,'synthetic')['status'],'stopped_by_marker')
                model.assert_not_called()

    def test_failed_finished_batch_does_not_dispatch_synthesis(self):
        value=self.state();value['counts']['jobs']['completed']=2;value['counts']['jobs']['failed']=1
        with tempfile.TemporaryDirectory() as tmp,patch('ltr.project.learning_finalize.status',return_value=value),\
                patch('ltr.project.learning_finalize.synthesize') as model:
            self.assertEqual(finish(Path(tmp),'synthetic')['status'],'incomplete_reading_requires_resolution')
            model.assert_not_called()

    def test_independent_merge_is_awaited_without_root_lock(self):
        pending=self.state();pending['counts']['merges'].update(completed=0,reserved=1)
        with tempfile.TemporaryDirectory() as tmp,patch('ltr.project.learning_finalize.status',side_effect=[pending,self.state()]),\
                patch('ltr.project.learning_finalize.time.sleep') as sleep,\
                patch('ltr.project.learning_finalize.export_learning',return_value={}),\
                patch('ltr.project.learning_finalize.synthesize',return_value={'completed_categories':6}) as model:
            self.assertEqual(finish(Path(tmp),'synthetic')['status'],'completed_model_proposals')
            sleep.assert_called_once_with(10)
            model.assert_called_once()
