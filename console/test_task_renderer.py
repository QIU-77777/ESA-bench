import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from harbor.models.task.task import Task
from console.app.render.task_renderer import render_tasks
from esa.reference import ReferenceFailure, validate_task

ROOT = Path(__file__).resolve().parents[1]

class ConsoleTaskDraftTests(unittest.TestCase):
    def test_esa_draft_uses_own_input_and_trusted_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows=[{"row_id":"draft-01","input":"查询电量","expected":"正确电量",
                   "rubric":{"reply_checks":[{"pattern":"电量"}]},"tags":[]}]
            task=render_tasks(rows,Path(tmp),'esa','example',1)/'draft-01'
            task_config=tomllib.loads((task/'task.toml').read_text())
            self.assertEqual(task_config['metadata']['rubric'], rows[0]['rubric'])
            self.assertEqual(Task(task).paths.discovered_solve_path, (task/'solution/solve.sh').resolve())
            initial=json.loads((task/'environment/esa/initial_state.json').read_text())
            self.assertEqual(['查询电量'],initial['user_messages'])
            self.assertEqual('draft-01',initial['case_id'])
            self.assertEqual([],initial['tool_profile'])
            self.assertNotIn('expected',initial)
            self.assertNotIn('rubric',initial)
            for name in ('score.py','state_score.py'):
                self.assertEqual((ROOT/'esa'/name).read_bytes(),(task/'tests'/name).read_bytes())
            entry=(task/'tests/test.sh').read_text()
            self.assertIn('ESA_RUBRIC_PATH=/tests/rubric.json',entry)
            self.assertIn('python3 /tests/score.py',entry)
            self.assertNotIn('echo 1',entry)
            self.assertTrue((task/'solution/reference.py').is_file())
            self.assertEqual((task/'tests/negative_tests.py').read_text(), 'CASES = {}\n')
            self.assertEqual((task/'solution/solve.sh').read_bytes(),
                             b'#!/bin/bash\nset -euo pipefail\npython3 /solution/runner.py\n')
            self.assertEqual((task/'solution/reference_runtime.py').read_bytes(), (ROOT/'esa/reference.py').read_bytes())
            self.assertEqual((task/'solution/runner.py').read_bytes(), (ROOT/'esa/oracle.py').read_bytes())
            class NeverCallTransport:
                def request(self,*args):
                    raise AssertionError('uncertified draft contacted deployment')
            with self.assertRaisesRegex(ReferenceFailure, 'nonempty CASES'):
                validate_task(task,NeverCallTransport())

if __name__=='__main__': unittest.main()
