"""Regression coverage for real schema relationships, using synthetic fixtures."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from ltr import workbench as wb
from tests.ltr.workbench.test_store import WorkbenchTestCase


class SourceBindingTests(WorkbenchTestCase):
    def test_forward_alignment_uses_chinese_dialogue_id(self):
        con=sqlite3.connect(self.root/'dialogue.sqlite')
        con.execute("INSERT INTO dialogues VALUES ('ja1','ja',0,'Japanese test',NULL,NULL,'unresolved',0,0,'ja-hash')")
        con.execute("INSERT INTO alignments VALUES ('d1','[\"ja1\"]','model_matched','candidate',0.7)")
        con.commit();con.close()
        detail=self.store.dialogue({'corpus':'c1','id':'d1'})
        self.assertTrue(detail['item']['has_alignment'])
        self.assertEqual(detail['alignment']['japanese'][0]['text'],'Japanese test')
        self.assertFalse(detail['item']['reference_eligible'])

    def test_candidate_filter_uses_current_candidate_not_legacy_value(self):
        self.assertEqual(self.store.dialogues({'corpus':'c1','speaker':'OldSpeaker'})['items'],[])
        rows=self.store.dialogues({'corpus':'c1','status':'candidate'})['items']
        self.assertEqual([r['id'] for r in rows],['d5'])

    def test_absolute_scan_inside_configured_source_is_supported(self):
        scans=self.root/'private/scans';scans.mkdir(parents=True)
        image=scans/'sample.png';image.write_bytes(b'synthetic-image')
        con=sqlite3.connect(self.root/'dialogue.sqlite');con.execute('update scan_pages set image_path=? where id=?',(str(image),'p1'));con.commit();con.close()
        self.assertEqual(self.store.scan_image({'corpus':'c1','page':'p1'})[0],b'synthetic-image')

    def test_unknown_optional_flag_does_not_zero_all_metrics(self):
        overview=self.store.overview()['corpora'][0]
        self.assertTrue(overview['available'])
        self.assertEqual(overview['counts']['dialogues'],5)
        self.assertEqual(overview['attribution']['total'],1)

    def test_detail_includes_quote_paragraph(self):
        detail=self.store.dialogue({'corpus':'c1','id':'d2'})
        self.assertIn(2,[row['ordinal'] for row in detail['context']])

    def test_context_anchor_uses_paragraphs_instead_of_dialogue_sequence(self):
        con=sqlite3.connect(self.root/'dialogue.sqlite')
        con.execute("UPDATE dialogues SET ordinal=999 WHERE id='d2'");con.commit();con.close()
        detail=self.store.dialogue({'corpus':'c1','id':'d2'})
        self.assertEqual(detail['item']['ordinal'],999)
        self.assertEqual(detail['context_anchor']['start_para'],2)
        self.assertEqual(detail['context_anchor']['end_para'],2)

    def test_long_context_keeps_last_endpoint_and_declares_gap(self):
        con=sqlite3.connect(self.root/'dialogue.sqlite')
        con.execute("UPDATE dialogues SET end_para=40 WHERE id='d2'")
        con.execute("INSERT INTO paragraphs VALUES ('far','ed1',40,'Synthetic final endpoint','{}')")
        con.commit();con.close()
        detail=self.store.dialogue({'corpus':'c1','id':'d2'})
        self.assertIn(40,[r['ordinal'] for r in detail['context']])
        self.assertEqual(detail['context_anchor']['mode'],'endpoint_windows')
        self.assertLessEqual(len(detail['context']),10)

    def test_api_unknown_sql_error_is_visible_as_sanitised_failure(self):
        _,db=self.store.database('c1')
        with self.assertRaises(wb.WorkbenchError) as error:
            db.query('SELECT not_a_column FROM dialogues')
        self.assertEqual(error.exception.code,'database_query_failed')


class ExternalConfigurationTests(unittest.TestCase):
    def test_relative_paths_resolve_against_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);config=root/'config.json'
            config.write_text(json.dumps({'corpora':[{'id':'c1','path':'data'}],'review_dir':'reviews'}))
            parsed=wb.WorkbenchConfig.load(config)
            self.assertEqual(parsed.corpora[0].root,root/'data')
            self.assertEqual(parsed.review_dir,root/'reviews')

    def test_review_directory_inside_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            config=Path(temporary)/'config.json'
            config.write_text(json.dumps({'corpora':[{'id':'c1','path':'data'}],'review_dir':'data/reviews'}))
            with self.assertRaises(wb.WorkbenchError):wb.WorkbenchConfig.load(config)


if __name__=='__main__':unittest.main()
