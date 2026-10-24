import unittest
from ltr.project.learning_examples import ExampleError, select_examples
from ltr.project.storage import digest


class LearningExampleTests(unittest.TestCase):
    def setUp(self):
        self.source={'book_hash':'a','paragraphs':[{'id':'P1','text':'Target passage.'},{'id':'P2','text':'Different example.'}]}
        self.target={**self.source,'paragraphs':self.source['paragraphs'][:1]}
        self.example={'book_hash':'a','snapshot_digest':digest(self.source['paragraphs']),
                      'source_ids':['P2'],'target':'Synthetic translation.'}

    def test_non_target_example_allowed(self):
        self.assertEqual(len(select_examples({'a':self.source},self.target,[self.example])),1)

    def test_target_id_forbidden(self):
        with self.assertRaises(ExampleError):
            select_examples({'a':self.source},self.target,[{**self.example,'source_ids':['P1']}])

    def test_changed_source_forbidden(self):
        self.source['paragraphs'][1]['text']='Changed.'
        with self.assertRaises(ExampleError):
            select_examples({'a':self.source},self.target,[self.example])

    def test_target_copy_with_new_id_forbidden(self):
        self.source['paragraphs'][1]['text']='Target passage.'
        self.example['snapshot_digest']=digest(self.source['paragraphs'])
        with self.assertRaises(ExampleError):
            select_examples({'a':self.source},self.target,[self.example])
