import unittest
from ltr.project.learning_repair import apply_replacements
from ltr.project.dimension_cycle import CycleError


class RepairTests(unittest.TestCase):
    def record(self,pid):
        return {'category':'characters','name':'Synthetic','description':'Synthetic fact',
                'certainty':'explicit','evidence_ids':[pid]}

    def test_only_bad_record_is_changed_with_exact_quote(self):
        initial={'records':[self.record('P0'),self.record('BAD')]}
        fixed=apply_replacements(initial,{'replacements':[{'index':1,'record':self.record('P1'),
            'evidence':[{'paragraph_id':'P1','quote':'second'}]}]},[1],
            [{'id':'P0','text':'first'},{'id':'P1','text':'second fact'}])
        self.assertEqual(fixed['records'][0],initial['records'][0])
        self.assertEqual(initial['records'][1]['evidence_ids'],['BAD'])

    def test_unsupported_record_can_be_removed_but_not_silently_reassigned(self):
        initial={'records':[self.record('BAD')]}
        self.assertEqual(apply_replacements(initial,{'replacements':[{'index':0,'record':None}]},[0],[])['records'],[])
        with self.assertRaises(CycleError):
            apply_replacements(initial,{'replacements':[{'index':0,'record':self.record('P0'),
                'evidence':[{'paragraph_id':'P0','quote':'invented'}]}]},[0],[{'id':'P0','text':'actual'}])
