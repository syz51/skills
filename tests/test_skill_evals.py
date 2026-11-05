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

# Helpers a fake Claude Code CLI uses to emit stream-json in its cwd fixture.
PRELUDE = '''import json,os,sys
from pathlib import Path
fixture=Path(os.getcwd())
calls=[0]
def emit(event): print(json.dumps(event))
def init(skills=None, tools=('Read','Skill'), mode='dontAsk'):
 emit({'type':'system','subtype':'init','cwd':str(fixture),'tools':list(tools),'mcp_servers':[],
  'permissionMode':mode,'skills':list(skills if skills is not None else SKILLS)})
def use(name, arguments):
 calls[0]+=1
 identity='toolu_'+str(calls[0])
 emit({'type':'assistant','message':{'role':'assistant','content':[{'type':'tool_use','id':identity,'name':name,'input':arguments}]},'parent_tool_use_id':None})
 return identity
def result(identity, text, data=None, error=False):
 block={'type':'tool_result','tool_use_id':identity,'content':text}
 if error: block['is_error']=True
 emit({'type':'user','message':{'role':'user','content':[block]},'parent_tool_use_id':None,'tool_use_result':data})
def read(path):
 identity=use('Read',{'file_path':str(path)})
 content=Path(path).read_text()
 lines=content.split('\\n')
 result(identity,'(numbered)',{'type':'text','file':{'filePath':str(path),'content':content,'numLines':len(lines),'startLine':1,'totalLines':len(lines)}})
def denied(path):
 result(use('Read',{'file_path':str(path)}),'Permission to use Read has been denied.',error=True)
def skill(name, base):
 identity=use('Skill',{'skill':name})
 result(identity,'Launching skill: '+name,{'success':True,'commandName':name})
 body=Path(base,'SKILL.md').read_text().split('---\\n',2)[2]
 emit({'type':'user','message':{'role':'user','content':[{'type':'text','text':'Base directory for this skill: '+str(base)+'\\n\\n'+body}]},'parent_tool_use_id':None,'isSynthetic':True})
def say(text): emit({'type':'assistant','message':{'role':'assistant','content':[{'type':'text','text':text}]},'parent_tool_use_id':None})
def done(ok=True): emit({'type':'result','subtype':'success' if ok else 'error_during_execution','is_error':not ok})
'''.replace('SKILLS', repr(list(ev.SKILLS)))
HELP = ' '.join(ev.CLI_FLAGS) + ' choices: "default", "dontAsk"'


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

    def fake(self, body, prelude=True):
        path = self.root / ('fake-' + str(len(list(self.root.glob('fake-*')))))
        path.write_text('#!' + sys.executable + '\n' + (PRELUDE if prelude else '') + body)
        path.chmod(0o755)
        return str(path)

    def scorer(self, passed=True):
        return patch.dict(sys.modules, {'activation_trace': types.SimpleNamespace(
            score_claude_trace=lambda *args: {'passed': passed, 'completed': passed,
                                      'reads': [], 'failures': [] if passed else ['Missing actual read']})})

    def test_case_matrix_covers_every_managed_skill_and_negative_controls(self):
        cases = ev.load_cases()
        self.assertTrue(set(ev.SKILLS) <= {t for c in cases for t in c['required']})
        self.assertTrue(any(c['before'] == [['frontend-ui', 'typescript']] for c in cases))
        self.assertGreaterEqual(sum('typescript' in c['forbidden'] for c in cases), 2)
        self.assertTrue(any('typescript/tooling' in c['required'] for c in cases))
        for reference in ('rust/cargo', 'rust/api', 'rust/async', 'rust/unsafe'):
            self.assertTrue(any(reference in c['required'] for c in cases))
        self.assertTrue(any({'rust', 'tauri'} <= set(c['required']) for c in cases))
        for skill in ev.SKILLS:
            with self.subTest(skill=skill):
                self.assertTrue(any(c['invocation'] == 'explicit' and skill in c['required'] for c in cases))
                self.assertTrue(any(c['invocation'] == 'configured' and skill in c['required'] and '$' not in c['prompt'] for c in cases))
                self.assertTrue(any(skill in c['forbidden'] for c in cases))

    def test_generic_reference_target_rejects_unsafe_names_and_paths(self):
        for target in ('unknown/references/x.md', 'rust/references/../x.md',
                       'rust/references/x.txt', '/rust/references/x.md', 'rust/references//x.md'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                ev.target_spec(target)
        target = 'rust/references/libraries/new.md'
        with self.assertRaisesRegex(ValueError, 'Missing'):
            ev.snapshot_targets(self.folders, [target])
        folder = self.skills / 'rust/references/libraries'
        folder.mkdir(parents=True)
        outside = self.root / 'outside.md'
        outside.write_text('Private outside file')
        (folder / 'new.md').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'escaping'):
            ev.snapshot_targets(self.folders, [target])
        with patch.object(ev, 'run_process', side_effect=AssertionError('model call')):
            with self.assertRaises(ValueError):
                ev.run_case(dict(self.case, required=[target]), self.root, self.folders, 'unused')

    def test_new_owned_reference_case_scores_without_root_registry_enrollment(self):
        target = 'rust/references/libraries/new.md'
        path = self.skills / target
        path.parent.mkdir(parents=True)
        path.write_text('# New library contract\nUse version-matched APIs.\n')
        case = dict(self.case, suite='rust', required=[target], files={}, prompt='Review this new library API.')
        casefile = self.root / 'owned-cases.json'
        casefile.write_text(json.dumps([case]))
        self.assertEqual(ev.load_cases(casefile), [case])
        self.assertNotIn(target, ev.snapshot_targets(self.folders))
        cli = self.fake('''init()
read(fixture.resolve()/'.claude/skills/rust/references/libraries/new.md')
done()
''')
        with self.isolated_user():
            result = ev.run_case(case, self.root, self.folders, cli, candidate=True)
        self.assertTrue(result['passed'], result)
        saved = json.loads((self.root / 'test/targets.json').read_text())[target]
        self.assertEqual(saved['content'], path.read_text())
        self.assertIn(str((self.root / 'test/fixture').resolve() / '.claude/skills/rust/references/libraries/new.md'), saved['aliases'])

    def test_plan_never_invokes_model_or_creates_output(self):
        with patch.object(ev, 'check_cli', side_effect=AssertionError('live call')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.main(['plan', '--suite', 'all']), 0)

    def test_snapshots_preserve_crlf_bytes_and_moved_claude_config(self):
        content = b'---\r\nname: typescript\r\n---\r\n# TypeScript\r\n'
        (self.skills / 'typescript/SKILL.md').write_bytes(content)
        self.assertEqual(ev.snapshot_targets(self.folders)['typescript']['content'].encode(), content)
        home = self.root / 'custom-claude'
        home.mkdir()
        (home / 'skills').symlink_to(self.skills, target_is_directory=True)
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(home)}):
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
                        {'files': {'.claude/skills/fake/SKILL.md': 'x'}}, {'files': {'CLAUDE.md': 'x'}},
                        {'files': {'src/CLAUDE.local.md': 'x'}}, {'files': {'AGENTS.md': 'x'}},
                        {'required': ['unknown']},
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
        for text in ('--print --tools --permission-mode', HELP.replace('dontAsk', 'manual'),
                     HELP.replace('--strict-mcp-config', '--strict-mcp-config-x')):
            with self.subTest(text=text), self.assertRaises(ValueError):
                ev.check_cli(self.fake('print(' + repr(text) + ')\n', prelude=False))
        self.assertTrue(ev.check_cli(self.fake('print(' + repr(HELP) + ')\n', prelude=False)))

    def isolated_user(self, *names):
        home = self.root / 'user-config'
        for name in names:
            (home / 'skills' / name).mkdir(parents=True, exist_ok=True)
        return patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(home)})

    def invocation(self, **kwargs):
        record = self.root / 'invocation.json'
        cli = self.fake('json.dump({"argv":sys.argv[1:],"cwd":os.getcwd(),"env":dict(os.environ),"stdin":sys.stdin.read()},'
                        'open(' + repr(str(record)) + ',"w"))\ninit()\ndone()\n')
        parent = {'CLAUDECODE': '1', 'CLAUDE_CODE_ENTRYPOINT': 'cli', 'CLAUDE_AGENT_SDK_VERSION': '1', 'KEEP_ME': 'yes'}
        with self.scorer(), self.isolated_user(), patch.dict(os.environ, parent):
            result = ev.run_case(self.case, self.root, self.folders, cli, **kwargs)
        return result, json.loads(record.read_text())

    def test_fake_cli_process_uses_safe_flags_and_retains_local_trace(self):
        result, seen = self.invocation(model=None)
        args = seen['argv']
        for flag in ('--print', '--verbose', '--no-session-persistence', '--strict-mcp-config'):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index('--output-format') + 1], 'stream-json')
        self.assertEqual(args[args.index('--tools') + 1], 'Read,Skill')
        self.assertEqual(args[args.index('--permission-mode') + 1], 'dontAsk')
        added = [args[i + 1] for i, arg in enumerate(args) if arg == '--add-dir']
        self.assertEqual(added, [str(path.resolve()) for path in self.folders.values()])
        for absent in ('--model', '--effort', '--setting-sources', '--disable-slash-commands', '--dangerously-skip-permissions'):
            self.assertNotIn(absent, args)
        self.assertNotIn(self.case['prompt'], ' '.join(args))
        self.assertEqual(seen['stdin'], self.case['prompt'] + ev.SAFETY)
        self.assertEqual(Path(seen['cwd']).resolve(), (self.root / 'test/fixture').resolve())
        env = seen['env']
        self.assertEqual(env['CLAUDE_CODE_DISABLE_AUTO_MEMORY'], '1')
        self.assertEqual(env['KEEP_ME'], 'yes')
        for name in ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_AGENT_SDK_VERSION'):
            self.assertNotIn(name, env)
        self.assertTrue(result['passed'], result)
        self.assertTrue((self.root / 'test/result.json').is_file())
        saved = json.loads((self.root / 'test/targets.json').read_text())
        self.assertEqual(saved['typescript']['content'], (self.skills / 'typescript/SKILL.md').read_text())
        self.assertEqual(json.loads((self.root / 'test/case.json').read_text()), self.case)
        self.assertEqual((self.root / 'test').stat().st_mode & 0o777, 0o700)

    def test_model_and_effort_are_passed_only_when_requested(self):
        _, seen = self.invocation(model='claude-haiku-5-5', effort='low')
        self.assertEqual(seen['argv'][seen['argv'].index('--model') + 1], 'claude-haiku-5-5')
        self.assertEqual(seen['argv'][seen['argv'].index('--effort') + 1], 'low')

    def test_session_must_report_only_readonly_tools_and_every_skill(self):
        for body, failure in (("init(tools=('Read','Skill','Bash'))", 'exposed tools'),
                              ("init(mode='bypassPermissions')", 'permission mode'),
                              ("init(skills=['typescript'])", 'not exposed'),
                              ('pass', 'did not start')):
            with self.subTest(body=body), self.scorer():
                case = dict(self.case, id='session-' + str(len(list(self.root.glob('session-*')))))
                result = ev.run_case(case, self.root, self.folders, self.fake(body + '\ndone()\n'))
                self.assertFalse(result['passed'])
                self.assertTrue(any(failure in item for item in result['failures']), result)

    def test_process_failure_cannot_be_masked_by_passing_scorer(self):
        cli = self.fake('init()\nraise SystemExit(3)\n')
        with self.scorer():
            result = ev.run_case(self.case, self.root, self.folders, cli)
        self.assertFalse(result['passed'])
        self.assertEqual(result['exit_code'], 3)

    def test_missing_actual_read_fails_even_when_process_succeeds(self):
        cli = self.fake('init()\ndone()\n')
        with self.scorer(False):
            result = ev.run_case(self.case, self.root, self.folders, cli)
        self.assertFalse(result['passed'])
        self.assertIn('Missing actual read', result['failures'])

    def test_readonly_fixture_mutation_is_detected(self):
        cli = self.fake('init()\n(fixture/"parser.ts").write_text("changed")\ndone()\n')
        with self.scorer():
            result = ev.run_case(self.case, self.root, self.folders, cli)
        self.assertFalse(result['passed'])
        self.assertIn('Read-only fixture changed', result['failures'])

    def test_skill_mutation_is_detected(self):
        cli = self.fake('init()\nPath(' + repr(str(self.skills / 'typescript/SKILL.md')) + ').write_text("changed")\ndone()\n')
        with self.scorer():
            result = ev.run_case(self.case, self.root, self.folders, cli)
        self.assertFalse(result['passed'])
        self.assertIn('Skill source changed during evaluation', result['failures'])

    def test_candidate_configuration_is_isolated_and_reported(self):
        result, seen = self.invocation(candidate=True)
        fixture = self.root / 'test/fixture'
        self.assertEqual(sorted(p.name for p in (fixture / '.claude/skills').iterdir()), sorted(ev.SKILLS))
        for name in ev.SKILLS:
            self.assertEqual((fixture / '.claude/skills' / name).resolve(), (self.skills / name).resolve())
        self.assertEqual((fixture / 'CLAUDE.md').read_text(), ev.DISPATCH)
        self.assertFalse((fixture / 'AGENTS.md').exists())
        args = seen['argv']
        self.assertEqual(args[args.index('--setting-sources') + 1], 'project')
        self.assertEqual(result['configuration'], 'candidate-project-dispatch')
        self.assertTrue(result['passed'], result)

    def test_candidate_session_fails_when_user_skills_leak(self):
        cli = self.fake("init(skills=SKILLS_LIST+['headless-browser'])\ndone()\n".replace('SKILLS_LIST', repr(list(ev.SKILLS))))
        with self.scorer(), self.isolated_user('headless-browser', 'typescript'):
            installed = ev.run_case(dict(self.case, id='installed'), self.root, self.folders, cli)
            candidate = ev.run_case(dict(self.case, id='candidate'), self.root, self.folders, cli, candidate=True)
        self.assertTrue(installed['passed'], installed)
        self.assertIn('User skills leaked into the candidate session', candidate['failures'])

    def test_candidate_session_fails_when_same_named_user_skill_is_injected(self):
        installed = self.root / 'user-config' / 'skills' / 'typescript'
        installed.mkdir(parents=True)
        (installed / 'SKILL.md').write_text('---\nname: typescript\ndescription: installed copy\n---\n\n# Installed\n')
        cli = self.fake('init()\nskill("typescript", ' + repr(str(installed)) + ')\ndone()\n')
        with self.isolated_user():
            result = ev.run_case(self.case, self.root, self.folders, cli, candidate=True)
        self.assertIn('Skills loaded from the user skill folder instead of the candidates: ' + str(installed),
                      result['failures'])

    def test_output_may_live_under_the_users_claude_home_but_not_a_project(self):
        home = self.root / 'home'
        (home / '.claude').mkdir(parents=True)
        (self.root / 'project' / '.claude').mkdir(parents=True)
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(home / '.claude')}):
            ev.isolated_location(home / 'evals' / 'run')
            with self.assertRaisesRegex(ValueError, 'inside a Claude Code project'):
                ev.isolated_location(self.root / 'project' / 'evals' / 'run')

    def test_candidate_skill_injection_scores_through_discovery_link(self):
        cli = self.fake("init()\nskill('typescript', fixture.resolve()/'.claude/skills/typescript')\ndone()\n")
        with self.isolated_user():
            result = ev.run_case(self.case, self.root, self.folders, cli, candidate=True)
        self.assertTrue(result['passed'], result)
        self.assertEqual([(r['target'], r['coverage']) for r in result['reads']], [('typescript', 'complete')])

    def test_output_inside_claude_project_is_rejected_before_cli_lookup(self):
        project = self.root / 'project'
        (project / 'runs').mkdir(parents=True)
        (project / 'CLAUDE.md').write_text('Project instructions\n')
        with patch.object(ev, 'check_cli', side_effect=AssertionError('must fail first')), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ev.main(['run', '--skills-root', str(self.skills), '--output-dir', str(project / 'runs/new')])
        self.assertFalse((project / 'runs/new').exists())

    def test_candidate_canonical_discovery_alias_scores_actual_content(self):
        physical = self.root / 'physical-output'
        physical.mkdir()
        lexical = self.root / 'output-alias'
        lexical.symlink_to(physical, target_is_directory=True)
        cli = self.fake('''init()
read(fixture.resolve()/'.claude/skills/typescript/SKILL.md')
done()
''')
        with self.isolated_user():
            result = ev.run_case(self.case, lexical, self.folders, cli, candidate=True)
        self.assertTrue(result['passed'], result)
        target = json.loads((physical / 'test/targets.json').read_text())['typescript']
        self.assertIn(str(lexical / 'test/fixture/.claude/skills/typescript/SKILL.md'), target['aliases'])
        self.assertIn(str(physical.resolve() / 'test/fixture/.claude/skills/typescript/SKILL.md'), target['aliases'])

    def test_denied_read_requires_observed_retry_content(self):
        for retry in (False, True):
            with self.subTest(retry=retry):
                cli = self.fake('''init()
path=fixture.resolve()/'.claude/skills/typescript/SKILL.md'
denied(path)
''' + ("read(path)\n" if retry else "say('I loaded the typescript skill.')\n") + "done()\n")
                case = dict(self.case, id='retry-' + str(retry))
                with self.isolated_user():
                    result = ev.run_case(case, self.root, self.folders, cli, candidate=True)
                self.assertEqual(result['passed'], retry, result)
                self.assertEqual(bool(result['reads']), retry, result)

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
        cli = self.fake('''if '--help' in sys.argv:
 print(''' + repr(HELP) + ''')
else:
 init()
 if fixture.parent.name=='positive':
  read(Path(''' + repr(str(target)) + '''))
 done()
''')
        output = self.root / 'results'
        with contextlib.redirect_stdout(io.StringIO()), self.isolated_user():
            code = ev.main(['run', '--cases', str(casefile), '--suite', 'all', '--skills-root', str(self.skills),
                            '--claude', cli, '--output-dir', str(output), '--jobs', '2'])
        self.assertEqual(code, 0)
        summary = json.loads((output / 'summary.json').read_text())
        self.assertTrue(summary['passed'])
        self.assertEqual([c['case'] for c in summary['cases']], ['positive', 'negative'])
        self.assertTrue(all(c['completed'] for c in summary['cases']))


if __name__ == '__main__':
    unittest.main()
