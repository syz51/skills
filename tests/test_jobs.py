import argparse
import contextlib
import io
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

scripts = Path(__file__).parents[1] / 'scripts'
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location('jobs', scripts / 'jobs.py')
jobs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jobs)


class DailyCadenceTests(unittest.TestCase):
    def test_daily_deadline_boundaries_and_timezone(self):
        cases = (
            ('2026-11-03T08:59:59+09:00', '2026-11-02T09:00:00+09:00'),
            ('2026-11-03T09:00:00+09:00', '2026-11-03T09:00:00+09:00'),
            ('2026-11-03T23:59:59+09:00', '2026-11-03T09:00:00+09:00'),
            ('2026-11-07T13:00:00+09:00', '2026-11-07T09:00:00+09:00'),
            ('2027-01-01T08:00:00+09:00', '2026-12-31T09:00:00+09:00'),
            ('2026-11-02T19:00:00-05:00', '2026-11-03T09:00:00+09:00'),
        )
        for now, expected in cases:
            with self.subTest(now=now):
                self.assertEqual(jobs.latest_due(datetime.fromisoformat(now)).isoformat(), expected)

    def test_existing_weekly_state_suppresses_today_then_allows_tomorrow(self):
        state = {'setup_started_at': '2026-10-09T14:26:04.220935+00:00',
                 'completed_due': '2026-11-02T09:00:00+09:00'}
        original = dict(state)
        for now, expected in (('2026-11-02T15:00:00+09:00', False),
                              ('2026-11-03T08:59:59+09:00', False),
                              ('2026-11-03T09:00:00+09:00', True),
                              ('2026-11-08T12:00:00+09:00', True)):
            with self.subTest(now=now):
                self.assertEqual(jobs.weekly_due(datetime.fromisoformat(now), state), expected)
        self.assertEqual(state, original)
        state['setup_started_at'] = '2026-11-03T10:00:00+09:00'
        self.assertFalse(jobs.weekly_due(datetime.fromisoformat('2026-11-03T12:00:00+09:00'), state))


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        (self.repo / 'scripts').mkdir(parents=True)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.trace = self.root / 'trace'
        (self.repo / 'maintenance-prompt.md').write_text('Review evidence only.')
        (self.repo / 'scripts/sync.py').write_text('''import pathlib, sys
root = pathlib.Path(__file__).parents[2]
with (root / 'trace').open('a') as stream: stream.write(sys.argv[1] + '\\n')
if ((root / 'fail').exists() and sys.argv[1] == 'publish') or (root / ('fail-' + sys.argv[1])).exists(): sys.exit(1)
''')
        (self.repo / 'scripts/capture.py').write_text('''import json, pathlib, sys
from datetime import datetime, timezone
root = pathlib.Path(__file__).parents[2]
with (root / 'trace').open('a') as stream: stream.write('capture\\n')
if (root / 'fail-capture').exists(): sys.exit(1)
state = pathlib.Path(sys.argv[sys.argv.index('--config') + 1]).parent
(state / 'capture-status.json').write_text(json.dumps({'finished_at': datetime.now(timezone.utc).isoformat(), 'success': True, 'counts': {'pending': 3 if (root / 'backlog').exists() else 0}}))
''')
        (self.repo / 'scripts/evidence.py').write_text('''import json, pathlib, sys
from datetime import datetime, timezone
root = pathlib.Path(__file__).parents[2]
with (root / 'trace').open('a') as stream: stream.write('evidence\\n')
if (root / 'fail-evidence').exists(): sys.exit(1)
state = pathlib.Path(sys.argv[sys.argv.index('--config') + 1]).parent
(state / 'evidence-status.json').write_text(json.dumps({'finished_at': datetime.now(timezone.utc).isoformat(), 'success': True, 'counts': {'pending': 1 if (root / 'evidence-backlog').exists() else 0}}))
''')
        (self.root / 'bin').mkdir()
        self.claude = self.root / 'bin/claude'
        self.calls = self.root / 'claude-calls.jsonl'
        self.claude.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys
root = pathlib.Path({str(self.root)!r})
def record(**fields):
    with (root / 'claude-calls.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(fields, argv=sys.argv[1:], cwd=os.getcwd(), env=dict(os.environ))) + '\\n')
if sys.argv[1:] == ['--help']:
    record(kind='help')
    missing = root / 'missing-flag'
    flags = [flag for flag in ('--print', '--no-session-persistence', '--model', '--effort', '--permission-mode',
                               '--output-format', '--verbose', '--json-schema', '--tools')
             if not missing.exists() or flag != missing.read_text()]
    print('Usage: claude [options] [prompt]\\n\\nOptions:\\n' + '\\n'.join('  ' + flag + ' <value>  Fixture option' for flag in flags))
    sys.exit(0)
record(kind='review', stdin=sys.stdin.read())
with pathlib.Path({str(self.trace)!r}).open('a') as stream: stream.write('claude\\n')
def emit(event): print(json.dumps(event), flush=True)
emit({{'type': 'system', 'subtype': 'init', 'session_id': 'fixture'}})
if (root / 'long-stream').exists():
    for _ in range(300):
        emit({{'type': 'assistant', 'message': {{'content': [{{'type': 'text', 'text': 'x' * 5000}}]}}}})
if (root / 'fail-review').exists():
    print('review crashed', file=sys.stderr)
    sys.exit(1)
if (root / 'error-result').exists():
    emit({{'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'result': 'Partial notes.'}})
elif not (root / 'missing-report').exists():
    emit({{'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'Verified review report.'}})
''')
        self.claude.chmod(0o755)
        self.config = self.state / 'config.json'
        self.config.write_text(json.dumps({'repo_path': str(self.repo), 'host_id': 'maintainer-host',
            'install_root': str(self.root / 'install'),
            'python_path': sys.executable, 'uv_path': sys.executable, 'claude_path': str(self.claude),
            'role': 'maintainer'}))
        for command in (['init', '-b', 'main'], ['add', '.'], ['-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-m', 'fixture']):
            subprocess.run(['git', '-C', str(self.repo), *command], check=True, capture_output=True)
        self.args = argparse.Namespace(command='weekly', config=self.config, check=False, run_now=False)

    def state_data(self):
        return json.loads((self.state / 'weekly-state.json').read_text())

    def initialize(self):
        friday = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
        self.assertEqual(jobs.execute(self.args, friday), 0)
        self.assertFalse(self.trace.exists())
        return friday

    def enable_prompt_workflow(self):
        for path in ('scripts/prompt_runtime.py', 'scripts/prompt_review.py',
                     'prompts/extract.md', 'prompts/verify.md', 'prompts/correct.md', 'prompts/maintain.md'):
            target = self.repo / path
            target.parent.mkdir(exist_ok=True)
            target.write_text('fixture')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test', '-c',
                        'user.email=test@example.test', 'commit', '-m', 'prompt fixture'],
                       check=True, capture_output=True)

    def test_busy_actual_capture_never_completes_sync_or_weekly(self):
        import shutil
        for command, old_success in (('sync', False), ('weekly', True)):
            with self.subTest(command=command):
                for source in scripts.glob('*.py'):
                    if source.name != 'jobs.py':
                        shutil.copy(source, self.repo / 'scripts' / source.name)
                # Keep downstream source verification and transport harmless fixtures.
                (self.repo / 'scripts/evidence.py').write_text("raise SystemExit(1)\n")
                old = {'finished_at': '2020-01-01T00:00:00+00:00', 'success': old_success,
                       'counts': {'pending': 0 if old_success else 1}}
                (self.state / 'capture-status.json').write_text(json.dumps(old))
                with (self.state / 'capture.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    args = argparse.Namespace(command=command, config=self.config, check=False, run_now=True)
                    self.assertEqual(jobs.execute(args), 1)
                current = json.loads((self.state / (command + '-status.json')).read_text())
                self.assertFalse(current['stages']['capture']['success'])
                self.assertEqual(json.loads((self.state / 'capture-status.json').read_text()), old)
                self.assertNotIn('review', current['stages'])

    def test_zero_exit_with_stale_capture_or_evidence_is_rejected(self):
        for name in ('capture', 'evidence'):
            with self.subTest(name=name):
                path = self.repo / 'scripts' / (name + '.py')
                original = path.read_text()
                path.write_text('raise SystemExit(0)\n')
                (self.state / (name + '-status.json')).write_text(json.dumps({
                    'finished_at': '2020-01-01T00:00:00+00:00', 'success': True, 'counts': {'pending': 0}}))
                args = argparse.Namespace(command='sync', config=self.config, check=False, run_now=False)
                self.assertEqual(jobs.execute(args), 1)
                self.assertFalse(json.loads((self.state / 'sync-status.json').read_text())['stages'][name]['success'])
                path.write_text(original)

    def test_prompt_review_follows_deployment_and_no_change_completes_due(self):
        self.initialize()
        self.enable_prompt_workflow()
        def improve(config, state_root, **kwargs):
            self.assertEqual(self.trace.read_text().splitlines()[-1], 'deploy')
            self.assertEqual(state_root, self.state.resolve())
            self.assertEqual(kwargs['due'], '2026-10-13T09:00:00+09:00')
            self.assertGreater(kwargs['timeout'], 0)
            self.assertLessEqual(kwargs['timeout'], 900)
            return {'outcome': 'no_change'}
        with patch.object(jobs, 'maintenance_prompt', return_value='Review evidence only.'), \
                patch.object(jobs, 'review_prompts', side_effect=improve) as improved:
            self.assertEqual(jobs.execute(self.args, datetime(2026, 10, 13, 3, tzinfo=timezone.utc)), 0)
        improved.assert_called_once()
        self.assertEqual(self.trace.read_text().splitlines().count('deploy'), 1)
        status = json.loads((self.state / 'weekly-status.json').read_text())
        self.assertEqual(status['stages']['prompt_review'], {'success': True})
        self.assertNotIn('prompt_deploy', status['stages'])
        self.assertIn('completed_due', self.state_data())

    def test_prompt_failure_preserves_normal_deployment_and_pending_due(self):
        self.initialize()
        self.enable_prompt_workflow()
        with patch.object(jobs, 'maintenance_prompt', return_value='Review evidence only.'), \
                patch.object(jobs, 'review_prompts', side_effect=RuntimeError('private candidate text')):
            self.assertEqual(jobs.execute(self.args, datetime(2026, 10, 13, 3, tzinfo=timezone.utc)), 1)
        status = json.loads((self.state / 'weekly-status.json').read_text())
        self.assertEqual(status['stages']['deploy'], {'success': True})
        self.assertFalse(status['stages']['prompt_review']['success'])
        self.assertNotIn('prompt_deploy', status['stages'])
        self.assertNotIn('private candidate', json.dumps(status))
        self.assertNotIn('completed_due', self.state_data())

    def test_promoted_and_already_published_prompts_are_deployed(self):
        self.initialize()
        self.enable_prompt_workflow()
        self.args.run_now = True
        for outcome in ('promoted', 'already_reviewed'):
            with self.subTest(outcome=outcome), \
                    patch.object(jobs, 'maintenance_prompt', return_value='Review evidence only.'), \
                    patch.object(jobs, 'review_prompts', return_value={'outcome': outcome, 'commit': 'a' * 40}):
                self.assertEqual(jobs.execute(self.args, datetime(2026, 10, 13, 3, tzinfo=timezone.utc)), 0)
                self.assertEqual(self.trace.read_text().splitlines()[-2:], ['deploy', 'deploy'])
                status = json.loads((self.state / 'weekly-status.json').read_text())
                self.assertEqual(status['stages']['prompt_deploy'], {'success': True})
                self.trace.unlink()

    def test_prompt_deployment_failure_retries_activation(self):
        self.initialize()
        self.enable_prompt_workflow()
        original = jobs.run
        deployments = 0
        def run(*args, **kwargs):
            nonlocal deployments
            if args[0][1].endswith('/sync.py') and args[0][2] == 'deploy':
                deployments += 1
                if deployments == 2:
                    raise RuntimeError('deployment failed')
            return original(*args, **kwargs)
        tuesday = datetime(2026, 10, 13, 3, tzinfo=timezone.utc)
        with patch.object(jobs, 'maintenance_prompt', return_value='Review evidence only.'), \
                patch.object(jobs, 'review_prompts', side_effect=[
                    {'outcome': 'promoted', 'commit': 'a' * 40},
                    {'outcome': 'already_reviewed', 'commit': 'a' * 40}]), \
                patch.object(jobs, 'run', side_effect=run):
            self.assertEqual(jobs.execute(self.args, tuesday), 1)
            self.assertNotIn('completed_due', self.state_data())
            status = json.loads((self.state / 'weekly-status.json').read_text())
            self.assertTrue(status['stages']['deploy']['success'])
            self.assertFalse(status['stages']['prompt_deploy']['success'])
            self.assertEqual(jobs.execute(self.args, tuesday), 0)
        self.assertEqual(deployments, 4)
        self.assertIn('completed_due', self.state_data())

    def test_prompt_review_never_runs_on_hourly_or_earlier_failure(self):
        self.initialize()
        self.enable_prompt_workflow()
        for command, marker in (('sync', None), ('weekly', 'fail-deploy'),
                                ('weekly', 'backlog'), ('weekly', 'fail-review')):
            self.args.command = command
            if marker:
                (self.root / marker).touch()
            with self.subTest(command=command, marker=marker), \
                    patch.object(jobs, 'maintenance_prompt', return_value='Review evidence only.'), \
                    patch.object(jobs, 'review_prompts') as improved:
                self.assertEqual(jobs.execute(self.args, datetime(2026, 10, 13, 3, tzinfo=timezone.utc)),
                                 0 if command == 'sync' else 1)
                improved.assert_not_called()
            if marker:
                (self.root / marker).unlink()

    def test_partial_prompt_workflow_fails_closed(self):
        (self.repo / 'scripts/prompt_runtime.py').touch()
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            jobs.maintenance_prompt(self.repo, {})

    def test_maintenance_prompt_uses_runtime_loader(self):
        self.enable_prompt_workflow()
        import types
        from unittest.mock import Mock
        load = Mock(return_value='protected and selected prompt')
        config = {'repo_path': str(self.repo)}
        with patch.dict(sys.modules, {'prompt_runtime': types.SimpleNamespace(load=load)}):
            self.assertEqual(jobs.maintenance_prompt(self.repo, config), 'protected and selected prompt')
        load.assert_called_once_with('maintain', config=config)

    def test_prompt_review_receives_only_remaining_cycle_budget(self):
        self.initialize()
        self.enable_prompt_workflow()
        clock = [0]
        def improve(config, state_root, **kwargs):
            self.assertEqual(kwargs['timeout'], 200)
            return {'outcome': 'no_change'}
        def deployed(*args, **kwargs):
            if args[0][1].endswith('/sync.py') and args[0][2] == 'deploy':
                clock[0] = 5200
            return original(*args, **kwargs)
        original = jobs.run
        with patch.object(jobs.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(jobs, 'run', side_effect=deployed), \
                patch.object(jobs, 'maintenance_prompt', return_value='Review evidence only.'), \
                patch.object(jobs, 'review_prompts', side_effect=improve):
            self.assertEqual(jobs.execute(self.args, datetime(2026, 10, 13, 3, tzinfo=timezone.utc)), 0)

    def test_daily_check_keeps_legacy_state_and_reports_next_day_without_work(self):
        self.initialize()
        saved = {'setup_started_at': '2026-10-09T14:26:04.220935+00:00',
                 'completed_due': '2026-11-02T09:00:00+09:00'}
        path = self.state / 'weekly-state.json'
        path.write_text(json.dumps(saved))
        before = path.read_bytes()
        self.args.check = True
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(jobs.execute(self.args, datetime.fromisoformat('2026-11-02T15:00:00+09:00')), 0)
        result = json.loads(output.getvalue())
        self.assertFalse(result['weekly_due'])
        self.assertEqual(result['next_due'], '2026-11-03T09:00:00+09:00')
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.trace.exists())

    def test_first_run_does_not_catch_up_previous_day(self):
        self.initialize()
        self.assertNotIn('completed_due', self.state_data())

    def test_missed_days_catch_up_latest_daily_deadline_once(self):
        self.initialize()
        tuesday = datetime(2026, 10, 13, 3, tzinfo=timezone.utc)
        self.assertEqual(jobs.execute(self.args, tuesday), 0)
        self.assertEqual(self.trace.read_text().splitlines(), ['capture', 'evidence', 'upload', 'collect', 'claude', 'publish', 'deploy'])
        status = json.loads((self.state / 'weekly-status.json').read_text())
        self.assertEqual(status['stages'], {name: {'success': True} for name in
                         ('capture', 'evidence', 'upload', 'collect', 'review', 'publish', 'deploy')})
        self.assertNotIn('error', status)
        self.assertEqual(self.state_data()['completed_due'], '2026-10-13T09:00:00+09:00')
        self.assertEqual(jobs.execute(self.args, tuesday), 0)
        self.assertEqual(len(self.trace.read_text().splitlines()), 7)

    def test_failure_preserves_due_for_retry(self):
        self.initialize()
        (self.root / 'fail').touch()
        tuesday = datetime(2026, 10, 13, 3, tzinfo=timezone.utc)
        self.assertEqual(jobs.execute(self.args, tuesday), 1)
        self.assertNotIn('completed_due', self.state_data())
        self.assertEqual(self.trace.read_text().splitlines()[-1], 'publish')
        self.assertFalse(json.loads((self.state / 'weekly-status.json').read_text())['success'])

    def calls_of(self, kind):
        lines = self.calls.read_text().splitlines() if self.calls.exists() else []
        return [call for call in map(json.loads, lines) if call['kind'] == kind]

    def test_review_runs_claude_with_full_access_and_publishes_result(self):
        self.initialize()
        tuesday = datetime(2026, 10, 13, 3, tzinfo=timezone.utc)
        with patch.dict(os.environ, {'CLAUDECODE': '1', 'CLAUDE_CODE_ENTRYPOINT': 'cli'}):
            self.assertEqual(jobs.execute(self.args, tuesday), 0)
        self.assertEqual(self.trace.read_text().splitlines(),
                         ['capture', 'evidence', 'upload', 'collect', 'claude', 'publish', 'deploy'])
        [help_call] = self.calls_of('help')
        self.assertEqual(help_call['env']['LC_ALL'], 'C')
        [call] = self.calls_of('review')
        argv = call['argv']
        self.assertEqual(call['stdin'], 'Review evidence only.')
        self.assertIn('--print', argv)
        self.assertIn('--no-session-persistence', argv)
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'bypassPermissions')
        self.assertEqual(argv[argv.index('--model') + 1], 'claude-opus-5-5')
        self.assertEqual(argv[argv.index('--effort') + 1], 'xhigh')
        self.assertEqual(argv[argv.index('--output-format') + 1], 'stream-json')
        self.assertNotIn('--setting-sources', argv)
        self.assertEqual(os.path.realpath(call['cwd']), os.path.realpath(self.repo))
        self.assertNotIn('CLAUDECODE', call['env'])
        self.assertNotIn('CLAUDE_CODE_ENTRYPOINT', call['env'])
        self.assertEqual(call['env']['CLAUDE_CODE_DISABLE_AUTO_MEMORY'], '1')
        self.assertEqual(call['env']['PATH'].split(os.pathsep)[2], str(self.claude.parent))
        self.assertEqual((self.state / 'latest-weekly-report.md').read_text(), 'Verified review report.')
        log = (self.state / 'weekly.log').read_text()
        self.assertIn('"subtype": "init"', log)
        self.assertIn('"result": "Verified review report."', log)
        self.assertEqual(list(self.state.glob('.weekly-review-*')), [])
        self.assertIn('completed_due', self.state_data())

    def test_review_keeps_investigation_environment_and_checkout(self):
        work = self.root / 'work'
        work.mkdir()
        env = dict(os.environ, SKILL_INVESTIGATION_CYCLE='/cycle', SKILL_INVESTIGATION_CONFIG='/config',
                   CLAUDECODE='1')
        with (self.root / 'review.log').open('a') as log:
            jobs.review(self.claude, work, self.state, 'Review evidence only.', log, env, 30)
        [call] = self.calls_of('review')
        self.assertEqual(os.path.realpath(call['cwd']), os.path.realpath(work))
        self.assertEqual(call['env']['SKILL_INVESTIGATION_CYCLE'], '/cycle')
        self.assertEqual(call['env']['SKILL_INVESTIGATION_CONFIG'], '/config')
        self.assertNotIn('CLAUDECODE', call['env'])
        self.assertEqual((self.state / 'latest-weekly-report.md').read_text(), 'Verified review report.')

    def test_long_event_stream_still_yields_final_report(self):
        (self.root / 'long-stream').touch()
        with (self.root / 'review.log').open('a') as log:
            jobs.review(self.claude, self.repo, self.state, 'Review evidence only.', log, dict(os.environ), 30)
        self.assertGreater((self.root / 'review.log').stat().st_size, 1000000)
        self.assertEqual((self.state / 'latest-weekly-report.md').read_text(), 'Verified review report.')

    def test_missing_report_or_review_failure_preserves_previous_report_and_due(self):
        self.initialize()
        report = self.state / 'latest-weekly-report.md'
        report.write_text('Previous successful report.')
        tuesday = datetime(2026, 10, 13, 3, tzinfo=timezone.utc)
        for marker in ('missing-report', 'error-result', 'fail-review'):
            with self.subTest(marker=marker):
                (self.root / marker).touch()
                self.assertEqual(jobs.execute(self.args, tuesday), 1)
                self.assertEqual(report.read_text(), 'Previous successful report.')
                self.assertNotIn('completed_due', self.state_data())
                self.assertEqual(self.trace.read_text().splitlines()[-1], 'claude')
                self.assertNotIn('publish', self.trace.read_text().splitlines())
                self.assertIn('"subtype": "init"', (self.state / 'weekly.log').read_text())
                self.assertEqual(list(self.state.glob('.weekly-review-*')), [])
                (self.root / marker).unlink()
                self.trace.unlink()

    def test_missing_required_flag_blocks_review_and_publication(self):
        self.initialize()
        report = self.state / 'latest-weekly-report.md'
        report.write_text('Previous successful report.')
        tuesday = datetime(2026, 10, 13, 3, tzinfo=timezone.utc)
        for flag in jobs.claude_cli.REVIEW_FLAGS:
            with self.subTest(flag=flag):
                (self.root / 'missing-flag').write_text(flag)
                self.assertEqual(jobs.execute(self.args, tuesday), 1)
                self.assertEqual(self.trace.read_text().splitlines(), ['capture', 'evidence', 'upload', 'collect'])
                self.assertEqual(self.calls_of('review'), [])
                self.assertEqual(report.read_text(), 'Previous successful report.')
                self.assertNotIn('completed_due', self.state_data())
                self.assertIn('CLI adaptation failed', json.loads((self.state / 'weekly-status.json').read_text())['error'])
                self.trace.unlink()

    def test_legacy_codex_keys_are_ignored_and_claude_path_is_optional(self):
        config = json.loads(self.config.read_text())
        config.update(codex_path=str(self.root / 'removed-codex'), validator_path=str(self.root / 'removed-validator'))
        self.config.write_text(json.dumps(config))
        self.args.check = True
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(jobs.execute(self.args), 0)
            del config['claude_path']
            self.config.write_text(json.dumps(config))
            with patch.object(jobs.claude_cli, 'DEFAULT_CLI', self.claude):
                self.assertEqual(jobs.execute(self.args), 0)
            with patch.object(jobs.claude_cli, 'DEFAULT_CLI', self.root / 'missing-claude'), \
                    self.assertRaisesRegex(ValueError, 'claude_path'):
                jobs.execute(self.args)
            for invalid in ('', None, 3):
                with self.subTest(claude_path=invalid):
                    self.config.write_text(json.dumps(dict(config, claude_path=invalid)))
                    with self.assertRaisesRegex(ValueError, 'invalid host configuration'):
                        jobs.execute(self.args)

    def test_sync_steps_use_the_repository_validator_default(self):
        self.args.command = 'sync'
        commands = []
        def recorded(command, *args, **kwargs):
            commands.append(command)
            stage = Path(command[1]).stem
            if stage in ('capture', 'evidence'):
                (self.state / (stage + '-status.json')).write_text(json.dumps({
                    'finished_at': datetime.now(timezone.utc).isoformat(),
                    'success': True, 'counts': {'pending': 0}}))
        with patch.object(jobs, 'run', side_effect=recorded):
            self.assertEqual(jobs.execute(self.args), 0)
        sync_commands = [command for command in commands if command[1].endswith('/sync.py')]
        self.assertEqual([command[2] for command in sync_commands], ['upload', 'deploy'])
        self.assertTrue(all('--validator' not in command for command in sync_commands))

    def test_dirty_or_non_main_checkout_blocks_review_and_publication(self):
        self.initialize()
        tuesday = datetime(2026, 10, 13, 3, tzinfo=timezone.utc)
        dirty = self.repo / 'untracked.txt'
        dirty.write_text('unrelated work')
        for condition in ('dirty', 'branch'):
            with self.subTest(condition=condition):
                if condition == 'branch':
                    dirty.unlink()
                    subprocess.run(['git', '-C', str(self.repo), 'checkout', '-b', 'topic'],
                                   check=True, capture_output=True)
                self.assertEqual(jobs.execute(self.args, tuesday), 1)
                self.assertEqual(self.trace.read_text().splitlines(), ['capture', 'evidence', 'upload'])
                status = json.loads((self.state / 'weekly-status.json').read_text())
                self.assertEqual(status['error'], 'Weekly maintenance requires a clean main checkout')
                self.assertNotIn('review', status['stages'])
                self.assertNotIn('completed_due', self.state_data())
                self.trace.unlink()

    def test_client_cannot_run_weekly(self):
        config = json.loads(self.config.read_text())
        config['role'] = 'client'
        self.config.write_text(json.dumps(config))
        with self.assertRaises(ValueError):
            jobs.execute(self.args)
        self.assertFalse(self.trace.exists())

    def test_sync_sequence(self):
        self.args.command = 'sync'
        self.assertEqual(jobs.execute(self.args), 0)
        self.assertEqual(self.trace.read_text().splitlines(), ['capture', 'evidence', 'upload', 'deploy'])

    def test_capture_failure_still_transports_buffer_and_deploys(self):
        self.args.command = 'sync'
        (self.root / 'fail-capture').touch()
        self.assertEqual(jobs.execute(self.args), 1)
        self.assertEqual(self.trace.read_text().splitlines(), ['capture', 'evidence', 'upload', 'deploy'])
        status = json.loads((self.state / 'sync-status.json').read_text())
        self.assertFalse(status['success'])
        self.assertIn('Feedback capture failed', status['error'])

    def test_preparation_failures_are_both_retained(self):
        self.args.command = 'sync'
        (self.root / 'fail-capture').touch()
        (self.root / 'fail-evidence').touch()
        self.assertEqual(jobs.execute(self.args), 1)
        self.assertEqual(self.trace.read_text().splitlines(), ['capture', 'evidence', 'upload', 'deploy'])
        status = json.loads((self.state / 'sync-status.json').read_text())
        self.assertEqual(status['stages'], {
            'capture': {'success': False, 'error': 'Feedback capture failed'},
            'evidence': {'success': False, 'error': 'Source feedback verification failed'},
            'upload': {'success': True}, 'deploy': {'success': True}})
        self.assertEqual(status['error'], 'Feedback capture failed; Source feedback verification failed')

    def test_transport_failure_retains_earlier_failures(self):
        self.args.command = 'sync'
        (self.root / 'fail-capture').touch()
        for name, message, expected in (
            ('upload', 'Feedback upload failed', ['capture', 'evidence', 'upload']),
            ('deploy', 'Release deployment failed', ['capture', 'evidence', 'upload', 'deploy'])):
            with self.subTest(stage=name):
                (self.root / ('fail-' + name)).touch()
                self.assertEqual(jobs.execute(self.args), 1)
                self.assertEqual(self.trace.read_text().splitlines(), expected)
                status = json.loads((self.state / 'sync-status.json').read_text())
                self.assertEqual(status['error'], 'Feedback capture failed; ' + message)
                self.assertEqual(status['stages'][name], {'success': False, 'error': message})
                self.assertEqual(status['stages']['capture']['success'], False)
                (self.root / ('fail-' + name)).unlink()
                self.trace.unlink()

    def test_stage_errors_do_not_include_exception_details(self):
        self.args.command = 'sync'
        with patch.object(jobs, 'run', side_effect=OSError('private-token-in-path')):
            self.assertEqual(jobs.execute(self.args), 1)
        raw = (self.state / 'sync-status.json').read_text()
        self.assertNotIn('private-token-in-path', raw)
        status = json.loads(raw)
        self.assertEqual(status['error'], 'Feedback capture failed; Source feedback verification failed; Feedback upload failed')

    def test_weekly_capture_failure_preserves_due_and_buffer(self):
        self.initialize()
        (self.root / 'fail-capture').touch()
        tuesday = datetime(2026, 10, 13, 3, tzinfo=timezone.utc)
        self.assertEqual(jobs.execute(self.args, tuesday), 1)
        self.assertEqual(self.trace.read_text().splitlines(), ['capture', 'evidence', 'upload', 'deploy'])
        self.assertNotIn('completed_due', self.state_data())

    def test_weekly_backlog_waits_for_complete_capture(self):
        self.initialize()
        (self.root / 'backlog').touch()
        tuesday = datetime(2026, 10, 13, 3, tzinfo=timezone.utc)
        self.assertEqual(jobs.execute(self.args, tuesday), 1)
        self.assertEqual(self.trace.read_text().splitlines(), ['capture', 'evidence', 'upload', 'deploy'])
        self.assertNotIn('completed_due', self.state_data())
        self.assertIn('backlog remains', json.loads((self.state / 'weekly-status.json').read_text())['error'])
        (self.root / 'backlog').unlink()
        self.assertEqual(jobs.execute(self.args, tuesday), 0)
        self.assertEqual(self.trace.read_text().splitlines()[-7:], ['capture', 'evidence', 'upload', 'collect', 'claude', 'publish', 'deploy'])
        self.assertIn('completed_due', self.state_data())

    def test_source_failure_or_backlog_defers_weekly_but_preserves_transport(self):
        self.initialize()
        for marker in ('fail-evidence', 'evidence-backlog'):
            with self.subTest(marker=marker):
                (self.root / marker).touch()
                self.assertEqual(jobs.execute(self.args, datetime(2026, 10, 13, 3, tzinfo=timezone.utc)), 1)
                self.assertEqual(self.trace.read_text().splitlines(), ['capture', 'evidence', 'upload', 'deploy'])
                self.assertNotIn('completed_due', self.state_data())
                (self.root / marker).unlink()
                self.trace.unlink()

    def test_weekly_backlogs_are_both_retained(self):
        self.initialize()
        (self.root / 'backlog').touch()
        (self.root / 'evidence-backlog').touch()
        self.assertEqual(jobs.execute(self.args, datetime(2026, 10, 13, 3, tzinfo=timezone.utc)), 1)
        status = json.loads((self.state / 'weekly-status.json').read_text())
        self.assertIn('Feedback capture backlog remains', status['error'])
        self.assertIn('Source verification backlog remains', status['error'])
        self.assertFalse(status['stages']['capture']['success'])
        self.assertFalse(status['stages']['evidence']['success'])
        self.assertNotIn('review', status['stages'])
        self.assertNotIn('completed_due', self.state_data())

    def test_busy_cycle_skips_without_work(self):
        self.args.command = 'sync'
        path = self.state / 'sync-status.json'
        path.write_text('previous status')
        with (self.state / 'cycle.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(jobs.execute(self.args), 0)
        self.assertFalse(self.trace.exists())
        self.assertEqual(path.read_text(), 'previous status')

    def test_timeout_kills_descendant_when_group_leader_exits_on_term(self):
        leader_file = self.root / 'leader.pid'
        child_file = self.root / 'child.pid'
        program = '''import os, pathlib, signal, time
pathlib.Path(%r).write_text(str(os.getpid()))
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    pathlib.Path(%r).write_text(str(os.getpid()))
while True:
    time.sleep(1)
''' % (str(leader_file), str(child_file))
        began = time.monotonic()
        try:
            with (self.root / 'process.log').open('w') as log:
                with self.assertRaisesRegex(RuntimeError, '^Command timed out: ' + Path(sys.executable).name + '$'):
                    jobs.run([sys.executable, '-c', program], log, dict(os.environ), timeout=1)
            self.assertLess(time.monotonic() - began, 8)
            self.assertTrue(child_file.is_file(), 'The descendant must install its SIGTERM handler before timeout')
            child = child_file.read_text()
            deadline = time.monotonic() + 2
            while True:
                state = subprocess.run(['ps', '-o', 'stat=', '-p', child], capture_output=True, text=True).stdout.strip()
                if not state or state.startswith('Z') or time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            self.assertTrue(not state or state.startswith('Z'), 'Timed-out descendant is still running: ' + state)
        finally:
            if leader_file.is_file():
                try:
                    os.killpg(int(leader_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_running_status_preserves_failure_until_success(self):
        path = self.state / 'sync-status.json'
        path.write_text(json.dumps({'success': False, 'finished_at': '2026-10-09T10:00:00+00:00',
                                    'last_success_finished_at': '2026-10-09T09:00:00+00:00'}))
        running = jobs.new_status(path, 'sync', datetime.now(timezone.utc))
        self.assertEqual(running['phase'], 'running')
        self.assertIsNone(running['success'])
        self.assertEqual(running['last_failure_finished_at'], '2026-10-09T10:00:00+00:00')
        self.assertEqual(running['last_success_finished_at'], '2026-10-09T09:00:00+00:00')
        self.args.command = 'sync'
        def assert_started(*args, **kwargs):
            observed = json.loads(path.read_text())
            self.assertEqual(observed['phase'], 'running')
            self.assertIsNone(observed['success'])
            stage = Path(args[0][1]).stem
            if stage in ('capture', 'evidence'):
                (self.state / (stage + '-status.json')).write_text(json.dumps({
                    'finished_at': datetime.now(timezone.utc).isoformat(),
                    'success': True, 'counts': {'pending': 0}}))
        from unittest.mock import patch
        with patch.object(jobs, 'run', side_effect=assert_started):
            self.assertEqual(jobs.execute(self.args), 0)
        final = json.loads(path.read_text())
        self.assertEqual(final['phase'], 'finished')
        self.assertEqual(final['last_success_finished_at'], final['finished_at'])
        self.assertEqual(final['last_failure_finished_at'], '2026-10-09T10:00:00+00:00')

    def test_preflight_failure_preserves_success_history(self):
        path = self.state / 'sync-status.json'
        path.write_text(json.dumps({'success': True, 'finished_at': '2026-10-09T09:00:00+00:00'}))
        config = json.loads(self.config.read_text())
        config['python_path'] = str(self.root / 'missing-python')
        self.config.write_text(json.dumps(config))
        from unittest.mock import patch
        with patch.object(sys, 'argv', ['jobs.py', 'sync', '--config', str(self.config)]), patch('builtins.print'):
            self.assertEqual(jobs.main(), 1)
        final = json.loads(path.read_text())
        self.assertFalse(final['success'])
        self.assertEqual(final['last_success_finished_at'], '2026-10-09T09:00:00+00:00')
        self.assertEqual(final['last_failure_finished_at'], final['finished_at'])


class LearningTransactionTests(unittest.TestCase):
    setUp = JobsTests.setUp
    def git_at(self, root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()

    def commit_at(self, root):
        self.git_at(root, 'add', '.')
        self.git_at(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-m', 'Reviewed change')

    def transaction(self, callback, gate=None, scope=None):
        import investigations
        import sync
        config = json.loads(self.config.read_text())
        with patch.object(jobs, 'review', side_effect=callback), \
                patch.object(investigations, 'validate_cycle', side_effect=gate or (lambda *a: {'complete': True, 'open': 0})), \
                patch.object(sync, 'validate', return_value='validated'), \
                patch.object(sync, 'publication_scope', side_effect=scope or (lambda *a, **k: None), create=True), \
                (self.root / 'transaction.log').open('a') as log:
            return jobs.review_learning_cycle(config, self.state, jobs.latest_due(datetime.now(timezone.utc)), log, dict(os.environ), 30)

    def accepted_review(self, cli, work, state, prompt, log, env, timeout):
        self.assertEqual(Path(env['SKILL_INVESTIGATION_CONFIG']).parent, self.state.resolve())
        self.assertIn('SKILL_INVESTIGATION_CYCLE', prompt)
        (work / 'skills/frontend-ui').mkdir(parents=True, exist_ok=True)
        (work / 'skills/frontend-ui/guide.md').write_text('Reviewed guidance')
        self.commit_at(work)
        (state / 'latest-weekly-report.md').write_text('Validated learning report')

    def test_validated_attempt_advances_main_and_removes_worktree(self):
        before = self.git_at(self.repo, 'rev-parse', 'HEAD')
        result = self.transaction(self.accepted_review)
        self.assertTrue(result['complete'])
        self.assertNotEqual(before, self.git_at(self.repo, 'rev-parse', 'HEAD'))
        self.assertEqual((self.repo / 'skills/frontend-ui/guide.md').read_text(), 'Reviewed guidance')
        self.assertFalse((self.state / 'investigations/active-attempt.json').exists())
        self.assertEqual(len(self.git_at(self.repo, 'worktree', 'list', '--porcelain').split('worktree ')), 2)

    def test_missing_disposition_preserves_candidate_and_canonical_main(self):
        before = self.git_at(self.repo, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(ValueError, 'missing disposition'):
            self.transaction(self.accepted_review, gate=lambda *a: (_ for _ in ()).throw(ValueError('missing disposition')))
        self.assertEqual(before, self.git_at(self.repo, 'rev-parse', 'HEAD'))
        saved = json.loads((self.state / 'investigations/active-attempt.json').read_text())
        self.assertTrue((Path(saved['work']) / 'skills/frontend-ui/guide.md').exists())
        self.assertEqual(saved['failure'], {'class': 'ValueError', 'detail': 'missing disposition'})
        self.assertEqual(self.git_at(self.repo, 'status', '--porcelain'), '')

    def test_weekly_requires_investigation_gate_before_publish(self):
        import investigations
        import sync
        (self.repo / 'scripts/investigations.py').write_text('# available learning runner\n')
        self.commit_at(self.repo)
        before = self.git_at(self.repo, 'rev-parse', 'HEAD')
        self.args.run_now = True
        with patch.object(jobs, 'review', side_effect=self.accepted_review), \
                patch.object(investigations, 'validate_cycle', side_effect=ValueError('missing disposition')), \
                patch.object(sync, 'validate'), patch.object(sync, 'publication_scope'):
            self.assertEqual(jobs.execute(self.args), 1)
        status = json.loads((self.state / 'weekly-status.json').read_text())
        self.assertFalse(status['investigations']['complete'])
        self.assertEqual(status['investigations']['recovery']['status'], 'failed')
        self.assertNotIn('publish', self.trace.read_text().splitlines())
        self.assertNotIn('deploy', self.trace.read_text().splitlines())
        self.assertEqual(before, self.git_at(self.repo, 'rev-parse', 'HEAD'))

    def test_advanced_canonical_archives_attempt_without_copying_its_edits(self):
        def crash(cli, work, state, prompt, log, env, timeout):
            (work / 'owned-draft').write_text('old baseline experiment')
            raise RuntimeError('review interrupted')
        with self.assertRaises(RuntimeError):
            self.transaction(crash)
        saved = json.loads((self.state / 'investigations/active-attempt.json').read_text())
        (self.repo / 'user-commit').write_text('new canonical baseline')
        self.commit_at(self.repo)
        def fresh(cli, work, state, prompt, log, env, timeout):
            self.assertNotEqual(work, Path(saved['work']))
            self.assertFalse((work / 'owned-draft').exists())
            self.assertIn('read-only recovery context', prompt)
            self.accepted_review(cli, work, state, prompt, log, env, timeout)
        self.transaction(fresh)
        self.assertEqual((Path(saved['work']) / 'owned-draft').read_text(), 'old baseline experiment')
        self.assertEqual(json.loads((Path(saved['work']).parent / 'attempt.json').read_text())['status'], 'superseded')

    def test_crashed_dirty_attempt_resumes_without_resetting_evidence(self):
        def crash(cli, work, state, prompt, log, env, timeout):
            (work / 'owned-draft').write_text('preserve failed experiment')
            raise RuntimeError('review interrupted')
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.transaction(crash)
        saved = json.loads((self.state / 'investigations/active-attempt.json').read_text())
        work = Path(saved['work'])
        def resume(cli, resumed, state, prompt, log, env, timeout):
            self.assertEqual(resumed, work)
            self.assertEqual((resumed / 'owned-draft').read_text(), 'preserve failed experiment')
            (resumed / 'owned-draft').unlink()
            self.accepted_review(cli, resumed, state, prompt, log, env, timeout)
        self.transaction(resume)
        self.assertTrue((self.repo / 'skills/frontend-ui/guide.md').exists())

    def test_changed_cycle_evidence_preserves_old_draft_and_starts_clean(self):
        import investigations
        def crash(cli, work, state, prompt, log, env, timeout):
            (work / 'owned-draft').write_text('failed-cycle evidence')
            raise RuntimeError('review interrupted')
        with self.assertRaises(RuntimeError): self.transaction(crash)
        saved = json.loads((self.state / 'investigations/active-attempt.json').read_text())
        binding = {'skill': 'frontend-ui', 'note_sha256': 'a'*64, 'receipt_sha256': 'b'*64, 'source_machine': 'client-host'}
        def fresh(cli, work, state, prompt, log, env, timeout):
            self.assertNotEqual(work, Path(saved['work']))
            self.assertFalse((work / 'owned-draft').exists())
            self.accepted_review(cli, work, state, prompt, log, env, timeout)
        with patch.object(investigations, 'bindings', return_value=[binding]): self.transaction(fresh)
        self.assertEqual((Path(saved['work'])/'owned-draft').read_text(), 'failed-cycle evidence')

    def test_changed_runner_archives_incompatible_cycle_for_clean_retry(self):
        import investigations
        def crash(*args, **kwargs): raise RuntimeError('review interrupted')
        with self.assertRaises(RuntimeError): self.transaction(crash)
        saved = json.loads((self.state / 'investigations/active-attempt.json').read_text())
        original = investigations.cycle_data
        def read_cycle(path):
            if str(path) == saved['cycle']: raise ValueError('Investigation runner changed')
            return original(path)
        with patch.object(investigations, 'cycle_data', side_effect=read_cycle): self.transaction(self.accepted_review)
        self.assertEqual(json.loads((Path(saved['work']).parent/'attempt.json').read_text())['status'], 'superseded')

    def test_failed_preparation_retains_a_retryable_attempt(self):
        import investigations
        with patch.object(investigations, 'prepare', side_effect=ValueError('invalid source evidence')):
            with self.assertRaisesRegex(ValueError, 'invalid source evidence'):
                self.transaction(self.accepted_review)
        saved = json.loads((self.state / 'investigations/active-attempt.json').read_text())
        self.assertIsNone(saved['cycle'])
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(saved['failure']['detail'], 'invalid source evidence')
        def resume(cli, work, state, prompt, log, env, timeout):
            self.assertEqual(work, Path(saved['work']))
            self.accepted_review(cli, work, state, prompt, log, env, timeout)
        self.transaction(resume)

    def test_concurrent_canonical_edits_are_preserved(self):
        def edit(cli, work, state, prompt, log, env, timeout):
            self.accepted_review(cli, work, state, prompt, log, env, timeout)
            (self.repo / 'user-work').write_text('never overwrite')
        with self.assertRaisesRegex(RuntimeError, 'Canonical checkout changed'):
            self.transaction(edit)
        self.assertEqual((self.repo / 'user-work').read_text(), 'never overwrite')
        self.assertFalse((self.repo / 'skills/frontend-ui/guide.md').exists())

    def test_scope_rejection_never_advances_canonical(self):
        import sync
        before = self.git_at(self.repo, 'rev-parse', 'HEAD')
        def forbidden(cli, work, state, prompt, log, env, timeout):
            (work / 'maintenance-prompt.md').write_text('forbidden automatic authority change')
            self.commit_at(work)
            (state / 'latest-weekly-report.md').write_text('Unsupported claim')
        with self.assertRaisesRegex(RuntimeError, 'Out-of-scope publication path'):
            self.transaction(forbidden, scope=sync.publication_scope)
        self.assertEqual(before, self.git_at(self.repo, 'rev-parse', 'HEAD'))
        self.assertEqual((self.repo / 'maintenance-prompt.md').read_text(), 'Review evidence only.')


if __name__ == '__main__':
    unittest.main()
