import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[3] / 'scripts'

from ltr.project.demo import demo
from ltr.project.preview import render
from ltr.project.storage import (StorageError, apply_edits, private_root, read_project,
                                 project_lock, write_json)


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.project = demo(self.root)

    def edits(self):
        return {"book_hash": self.project["book_hash"],
                "source_digest": self.project["source_digest"],
                "base_revision": self.project["revision"],
                "units": [{"id":u["id"], "target":"synthetic translation", "status":"accepted"}
                          for u in self.project["units"]]}

    def test_import_edit_save_confirm_reload(self):
        saved = apply_edits(self.root, self.edits())
        self.assertEqual(saved['revision'], 2)
        self.assertTrue(all(u['status']=='draft' for u in saved['units']))
        self.assertTrue((self.root/'history/revision-1.json').is_file())
        self.project = saved
        confirmed = apply_edits(self.root, self.edits())
        self.assertTrue(all(u['status']=='accepted' for u in confirmed['units']))
        render(self.root, read_project(self.root))
        self.assertEqual(read_project(self.root)['revision'], 3)

    def test_stale_edit_rejected_without_overwrite(self):
        edit=self.edits()
        apply_edits(self.root,edit)
        before=(self.root/'project.json').read_bytes()
        with self.assertRaisesRegex(StorageError,'stale_edit'):apply_edits(self.root,edit)
        self.assertEqual(before,(self.root/'project.json').read_bytes())

    def test_source_tamper_rejected_on_read(self):
        p=copy.deepcopy(self.project)
        p['source'][0]['text']='changed'
        write_json(self.root/'project.json',p)
        with self.assertRaisesRegex(StorageError,'source_changed'):read_project(self.root)

    def test_snapshot_tamper_rejected_on_read(self):
        write_json(self.root/'source-snapshot.json',{'changed':True})
        with self.assertRaisesRegex(StorageError,'source_snapshot_changed'):read_project(self.root)

    def test_duplicate_coverage_rejected_on_read(self):
        p=copy.deepcopy(self.project)
        p['units'][1]['id']=p['units'][0]['id']
        write_json(self.root/'project.json',p)
        with self.assertRaisesRegex(StorageError,'coverage_mismatch'):read_project(self.root)

    def test_missing_extra_duplicate_edits_rejected(self):
        for mode in ('missing','extra','duplicate'):
            edit=self.edits()
            if mode=='missing':edit['units'].pop()
            elif mode=='extra':edit['units'].append(dict(edit['units'][0],id='extra'))
            else:edit['units'][1]['id']=edit['units'][0]['id']
            with self.subTest(mode=mode),self.assertRaisesRegex(StorageError,'coverage_mismatch'):
                apply_edits(self.root,edit)

    def test_empty_accept_rejected(self):
        edit=self.edits();edit['units'][0]['target']=' '
        with self.assertRaisesRegex(StorageError,'empty_accepted_translation'):apply_edits(self.root,edit)

    def test_exclusive_lock_prevents_second_writer(self):
        with project_lock(self.root):
            with self.assertRaisesRegex(StorageError,'project_locked'):apply_edits(self.root,self.edits())
        self.assertEqual(read_project(self.root)['revision'],1)
        self.assertFalse((self.root/'.writer.lock').exists())

    def test_git_private_root_rejected(self):
        fake=Path(self.tmp.name)/'checkout';fake.mkdir();(fake/'.git').mkdir()
        with self.assertRaisesRegex(StorageError,'private_root_inside_git'):private_root(fake/'data')

    def test_preview_escapes_script_and_disables_network(self):
        p=copy.deepcopy(self.project)
        p['units'][0]['target']='</script><script>alert(1)</script>'
        render(self.root,p)
        html=(self.root/'review.html').read_text(encoding='utf-8')
        self.assertNotIn('</script><script>alert',html)
        self.assertIn('\\u003c/script\\u003e',html)
        self.assertIn("connect-src 'none'",html)
        self.assertNotIn('localStorage',html)

    def test_cli_error_does_not_echo_private_content(self):
        sentinel='SYNTHETIC_SECRET_NEVER_ECHO'
        path=self.root/'invalid.json';path.write_text(sentinel,encoding='utf-8')
        proc=subprocess.run([sys.executable,'-m','ltr.project','apply','--project',str(self.root),
                             '--edits',str(path)],capture_output=True,text=True,cwd=SCRIPTS)
        self.assertEqual(proc.returncode,2)
        self.assertNotIn(sentinel,proc.stdout+proc.stderr)
        self.assertNotIn(str(self.root),proc.stdout+proc.stderr)
        self.assertEqual(proc.stderr,'')

    def test_cli_unknown_arguments_do_not_echo_values(self):
        sentinel='SYNTHETIC_PRIVATE_ARGUMENT'
        proc=subprocess.run([sys.executable,'-m','ltr.project',sentinel],capture_output=True,text=True,cwd=SCRIPTS)
        self.assertEqual(proc.returncode,2)
        self.assertNotIn(sentinel,proc.stdout+proc.stderr)
        self.assertEqual(proc.stderr,'')

    def test_preflight_is_not_executable(self):
        proc=subprocess.run([sys.executable,'-m','ltr.project','preflight','--project',str(self.root)],capture_output=True,text=True,cwd=SCRIPTS)
        self.assertEqual(proc.returncode,0)
        plan=json.loads((self.root/'preflight.json').read_text())
        self.assertFalse(plan['execution_authorized'])
        self.assertFalse(plan['transport_implemented'])
        self.assertEqual(plan['model_calls'],0)


if __name__=='__main__':unittest.main()
