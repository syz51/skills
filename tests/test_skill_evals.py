import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import skill_evals as ev


class SkillEvalsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skills = self.root / 'skills'
        for name in ev.SKILLS:
            p = self.skills / name / 'SKILL.md'
            p.parent.mkdir(parents=True)
            p.write_text('---\nname: ' + name + '\n---\n# Guidance\n')
        for owner, relative in ev.REFERENCES.values():
            p = self.skills / owner / relative
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('# Reference\n')
        self.folders = ev.skill_folders(self.skills)
        self.case = {'id': 'test', 'suite': 'typescript', 'invocation': 'configured',
                     'prompt': 'Review parser.ts', 'files': {'parser.ts': 'export const x=1;'},
                     'required': ['typescript'], 'forbidden': [], 'before': []}

    def fake(self, body):
        path = self.root / ('fake-' + str(len(list(self.root.glob('fake-*')))))
        path.write_text('#!' + sys.executable + '\n' + body)
        path.chmod(0o755)
        return str(path)

    def scorer(self, passed=True):
        return patch.dict(sys.modules, {'activation_trace': types.SimpleNamespace(
            score_trace=lambda *args: {'passed': passed, 'completed': passed,
                                      'reads': [], 'failures': [] if passed else ['Missing actual read']})})

    def test_case_matrix_covers_every_managed_skill_and_negative_controls(self):
        cases = ev.load_cases()
        self.assertTrue(set(ev.SKILLS) <= {t for c in cases for t in c['required']})
        self.assertTrue(any(c['before'] == [['frontend-ui', 'typescript']] for c in cases))
        self.assertGreaterEqual(sum('typescript' in c['forbidden'] for c in cases), 2)
        self.assertTrue(any('typescript/tooling' in c['required'] for c in cases))
        for skill in ev.SKILLS:
            with self.subTest(skill=skill):
                self.assertTrue(any(c['invocation'] == 'explicit' and skill in c['required'] for c in cases))
                self.assertTrue(any(c['invocation'] == 'configured' and skill in c['required'] and '$' not in c['prompt'] for c in cases))
                self.assertTrue(any(skill in c['forbidden'] for c in cases))

    def test_plan_never_invokes_model_or_creates_output(self):
        with patch.object(ev, 'check_cli', side_effect=AssertionError('live call')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.main(['plan', '--suite', 'all']), 0)

    def test_snapshots_preserve_crlf_bytes_and_moved_codex_home(self):
        content = b'---\r\nname: typescript\r\n---\r\n# TypeScript\r\n'
        (self.skills / 'typescript/SKILL.md').write_bytes(content)
        self.assertEqual(ev.snapshot_targets(self.folders)['typescript']['content'].encode(), content)
        home = self.root / 'custom-codex'
        home.mkdir()
        (home / 'skills').symlink_to(self.skills, target_is_directory=True)
        with patch.dict(os.environ, {'CODEX_HOME': str(home)}):
            installed = ev.skill_folders()
        self.assertEqual(set(installed), set(ev.SKILLS))
        self.assertTrue(all(path.parent == home / 'skills' for path in installed.values()))

    def test_empty_suite_cannot_pass_without_running_a_case(self):
        path = self.root / 'managed-only.json'
        path.write_text(json.dumps([dict(self.case, suite='managed')]))
        with patch.object(ev, 'check_cli', side_effect=AssertionError('must fail before CLI lookup')):
            for command in ('plan', 'run'):
                with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    ev.main([command, '--cases', str(path), '--output-dir', str(self.root / 'output')])
        self.assertFalse((self.root / 'output').exists())

    def test_invalid_case_paths_and_expectations_fail_before_execution(self):
        for changes in ({'id': '../escape'}, {'files': {'../secret': 'x'}},
                        {'files': {'.agents/skills/fake': 'x'}}, {'required': ['unknown']},
                        {'forbidden': ['typescript']}, {'before': [['typescript', 'tauri']]}):
            candidate = dict(self.case, **changes)
            path = self.root / 'cases.json'
            path.write_text(json.dumps([candidate]))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ev.load_cases(path)
        path.write_text(json.dumps([self.case, self.case]))
        with self.assertRaises(ValueError):
            ev.load_cases(path)

    def test_missing_cli_and_required_controls_fail_closed(self):
        with self.assertRaises(ValueError):
            ev.check_cli(str(self.root / 'missing'))
        fake = self.fake('print("--json --sandbox")\n')
        with self.assertRaises(ValueError):
            ev.check_cli(fake)

    def test_fake_cli_process_uses_safe_flags_and_retains_local_trace(self):
        cli = self.fake('import json,sys\nprint(json.dumps({"argv":sys.argv[1:]}))\n')
        with self.scorer():
            result = ev.run_case(self.case, self.root, self.folders, cli, model=None)
        args = json.loads((self.root / 'test/trace.jsonl').read_text())['argv']
        for flag in ('--ephemeral', '--json', '--skip-git-repo-check'):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index('--sandbox') + 1], 'read-only')
        self.assertNotIn('--model', args)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', args)
        self.assertTrue(result['passed'])
        self.assertTrue((self.root / 'test/result.json').is_file())
        saved = json.loads((self.root / 'test/targets.json').read_text())
        self.assertEqual(saved['typescript']['content'], (self.skills / 'typescript/SKILL.md').read_text())
        self.assertEqual(json.loads((self.root / 'test/case.json').read_text()), self.case)
        self.assertEqual((self.root / 'test').stat().st_mode & 0o777, 0o700)

    def test_process_failure_cannot_be_masked_by_passing_scorer(self):
        cli = self.fake('raise SystemExit(3)\n')
        with self.scorer():
            result = ev.run_case(self.case, self.root, self.folders, cli)
        self.assertFalse(result['passed'])
        self.assertEqual(result['exit_code'], 3)

    def test_missing_actual_read_fails_even_when_process_succeeds(self):
        cli = self.fake('print("{}")\n')
        with self.scorer(False):
            result = ev.run_case(self.case, self.root, self.folders, cli)
        self.assertFalse(result['passed'])
        self.assertIn('Missing actual read', result['failures'])

    def test_readonly_fixture_mutation_is_detected(self):
        cli = self.fake('from pathlib import Path\nimport sys\np=Path(sys.argv[sys.argv.index("-C")+1]); (p/"parser.ts").write_text("changed")\n')
        with self.scorer():
            result = ev.run_case(self.case, self.root, self.folders, cli)
        self.assertFalse(result['passed'])
        self.assertIn('Read-only fixture changed', result['failures'])

    def test_skill_mutation_is_detected(self):
        cli = self.fake('from pathlib import Path\nPath(' + repr(str(self.skills / 'typescript/SKILL.md')) + ').write_text("changed")\n')
        with self.scorer():
            result = ev.run_case(self.case, self.root, self.folders, cli)
        self.assertFalse(result['passed'])
        self.assertIn('Skill source changed during evaluation', result['failures'])

    def test_candidate_configuration_is_isolated_and_reported(self):
        cli = self.fake('print("{}")\n')
        with self.scorer():
            result = ev.run_case(self.case, self.root, self.folders, cli, candidate=True)
        fixture = self.root / 'test/fixture'
        self.assertEqual((fixture / '.agents/skills/typescript').resolve(), (self.skills / 'typescript').resolve())
        self.assertIn(str(self.skills / 'typescript/SKILL.md'), (fixture / 'AGENTS.md').read_text())
        self.assertEqual(result['configuration'], 'candidate-project-dispatch')

    def test_timeout_kills_descendants_after_leader_exits(self):
        pidfile = self.root / 'child.pid'
        cli = self.fake('import os,signal,time\npid=os.fork()\nif pid==0:\n signal.signal(signal.SIGTERM,signal.SIG_IGN)\n open(' + repr(str(pidfile)) + ',"w").write(str(os.getpid()))\n time.sleep(30)\nelse:\n time.sleep(30)\n')
        start = time.monotonic()
        code, out, err, timed = ev.run_process([cli], 1)
        self.assertTrue(timed)
        self.assertLess(time.monotonic() - start, 5)
        # Group termination is independently exercised: a surviving child would
        # retain the captured stdout pipe and block communicate for 30 seconds.
        self.assertTrue(pidfile.is_file())

    def test_cli_rejects_existing_output_and_unknown_cases(self):
        for argv in (['run', '--output-dir', str(self.root)], ['run', '--case', 'missing', '--output-dir', str(self.root/'new')]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                ev.main(argv)

    def test_multiple_cases_run_through_real_trace_scorer_and_subprocess(self):
        positive = dict(self.case, id='positive')
        negative = dict(self.case, id='negative', invocation='negative', required=[], forbidden=['typescript'])
        casefile = self.root / 'cases.json'
        casefile.write_text(json.dumps([positive, negative]))
        target = self.skills / 'typescript/SKILL.md'
        cli = self.fake('''import json,sys,shlex
from pathlib import Path
if '--help' in sys.argv:
 print('--ephemeral --json --sandbox --skip-git-repo-check --config --cd')
else:
 cwd=Path(sys.argv[sys.argv.index('-C')+1])
 if cwd.parent.name=='positive':
  path=Path(''' + repr(str(target)) + ''')
  print(json.dumps({'type':'item.completed','item':{'type':'command_execution','command':'cat '+shlex.quote(str(path)),'exit_code':0,'status':'completed','aggregated_output':path.read_text()}}))
 print(json.dumps({'type':'turn.completed'}))
''')
        output = self.root / 'results'
        with contextlib.redirect_stdout(io.StringIO()):
            code = ev.main(['run', '--cases', str(casefile), '--suite', 'all', '--skills-root', str(self.skills),
                            '--codex', cli, '--output-dir', str(output), '--jobs', '2'])
        self.assertEqual(code, 0)
        summary = json.loads((output / 'summary.json').read_text())
        self.assertTrue(summary['passed'])
        self.assertEqual([c['case'] for c in summary['cases']], ['positive', 'negative'])
        self.assertTrue(all(c['completed'] for c in summary['cases']))


if __name__ == '__main__':
    unittest.main()
