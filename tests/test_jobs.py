import argparse
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
root = pathlib.Path(__file__).parents[2]
with (root / 'trace').open('a') as stream: stream.write('capture\\n')
if (root / 'fail-capture').exists(): sys.exit(1)
state = pathlib.Path(sys.argv[sys.argv.index('--config') + 1]).parent
(state / 'capture-status.json').write_text(json.dumps({'success': True, 'counts': {'pending': 3 if (root / 'backlog').exists() else 0}}))
''')
        (self.repo / 'scripts/evidence.py').write_text('''import json, pathlib, sys
root = pathlib.Path(__file__).parents[2]
with (root / 'trace').open('a') as stream: stream.write('evidence\\n')
if (root / 'fail-evidence').exists(): sys.exit(1)
state = pathlib.Path(sys.argv[sys.argv.index('--config') + 1]).parent
(state / 'evidence-status.json').write_text(json.dumps({'success': True, 'counts': {'pending': 1 if (root / 'evidence-backlog').exists() else 0}}))
''')
        self.codex = self.root / 'codex'
        self.codex.write_text(f'''#!{sys.executable}
import pathlib, sys
generation = 'renamed' if pathlib.Path({str(self.root / 'renamed')!r}).exists() else 'current'
command = 'run' if generation == 'renamed' else 'exec'
json_flag = '--experimental-json' if generation == 'renamed' else '--json'
output_flag = '--output-last-message' if generation == 'renamed' else '-o'
if sys.argv[1:] == ['--help']:
    print('Commands:\\n  ' + command + '  Run Codex non-interactively')
    sys.exit(0)
if sys.argv[1:] == [command, '--help']:
    print('Options:\\n      --ephemeral\\n          Run without persisting session files\\n  -C, --cd <DIR>\\n          Set the working root\\n  -m, --model <MODEL>\\n          Model the agent should use\\n  -c, --config <key=value>\\n          Override a configuration value\\n  -s, --sandbox <MODE>\\n          Sandbox policy for model-generated commands\\n          read-only | workspace-write | danger-full-access\\n      ' + json_flag + '\\n          Print events as JSONL\\n  ' + output_flag + ' <FILE>\\n          File where the last message should be written')
    sys.exit(0)
assert sys.stdin.read() == 'Review evidence only.'
assert '--ephemeral' in sys.argv and sys.argv[-1] == '-'
assert sys.argv[1] == command and json_flag in sys.argv
assert sys.argv[sys.argv.index('--sandbox') + 1] == 'danger-full-access'
assert 'approval_policy="never"' in sys.argv and 'model_reasoning_effort="xhigh"' in sys.argv
with pathlib.Path({str(self.trace)!r}).open('a') as stream: stream.write('codex\\n')
if pathlib.Path({str(self.root / 'fail-review')!r}).exists(): sys.exit(1)
if not pathlib.Path({str(self.root / 'missing-report')!r}).exists():
    pathlib.Path(sys.argv[sys.argv.index(output_flag) + 1]).write_text('Verified review report.')
''')
        self.codex.chmod(0o755)
        self.config = self.state / 'config.json'
        self.config.write_text(json.dumps({'repo_path': str(self.repo), 'host_id': 'maintainer-host',
            'install_root': str(self.root / 'install'),
            'python_path': sys.executable, 'uv_path': sys.executable, 'codex_path': str(self.codex),
            'validator_path': sys.executable, 'role': 'maintainer'}))
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

    def test_prompt_review_follows_deployment_and_no_change_completes_due(self):
        self.initialize()
        self.enable_prompt_workflow()
        def improve(config, state_root, **kwargs):
            self.assertEqual(self.trace.read_text().splitlines()[-1], 'deploy')
            self.assertEqual(state_root, self.state.resolve())
            self.assertEqual(kwargs['due'], '2026-10-12T09:00:00+09:00')
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

    def test_first_run_does_not_catch_up_previous_week(self):
        self.initialize()
        self.assertNotIn('completed_due', self.state_data())

    def test_suspended_monday_catches_up_tuesday_once(self):
        self.initialize()
        tuesday = datetime(2026, 10, 13, 3, tzinfo=timezone.utc)
        self.assertEqual(jobs.execute(self.args, tuesday), 0)
        self.assertEqual(self.trace.read_text().splitlines(), ['capture', 'evidence', 'upload', 'collect', 'codex', 'publish', 'deploy'])
        status = json.loads((self.state / 'weekly-status.json').read_text())
        self.assertEqual(status['stages'], {name: {'success': True} for name in
                         ('capture', 'evidence', 'upload', 'collect', 'review', 'publish', 'deploy')})
        self.assertNotIn('error', status)
        self.assertEqual(self.state_data()['completed_due'], '2026-10-12T09:00:00+09:00')
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

    def test_cli_upgrade_changes_commands_and_aliases_without_changing_review_permissions(self):
        self.initialize()
        (self.root / 'renamed').touch()
        tuesday = datetime(2026, 10, 13, 3, tzinfo=timezone.utc)
        self.assertEqual(jobs.execute(self.args, tuesday), 0)
        self.assertEqual(self.trace.read_text().splitlines(), ['capture', 'evidence', 'upload', 'collect', 'codex', 'publish', 'deploy'])
        self.assertEqual((self.state / 'latest-weekly-report.md').read_text(), 'Verified review report.')
        self.assertIn('completed_due', self.state_data())

    def test_missing_report_or_review_failure_preserves_previous_report_and_due(self):
        self.initialize()
        report = self.state / 'latest-weekly-report.md'
        report.write_text('Previous successful report.')
        tuesday = datetime(2026, 10, 13, 3, tzinfo=timezone.utc)
        for marker in ('missing-report', 'fail-review'):
            with self.subTest(marker=marker):
                (self.root / marker).touch()
                self.assertEqual(jobs.execute(self.args, tuesday), 1)
                self.assertEqual(report.read_text(), 'Previous successful report.')
                self.assertNotIn('completed_due', self.state_data())
                self.assertNotIn('publish', self.trace.read_text().splitlines())
                (self.root / marker).unlink()
                self.trace.unlink()

    def test_missing_required_control_blocks_review_and_publication(self):
        self.initialize()
        tuesday = datetime(2026, 10, 13, 3, tzinfo=timezone.utc)
        with patch.object(jobs, 'help_flag', side_effect=RuntimeError('Capture CLI required control is unavailable')):
            self.assertEqual(jobs.execute(self.args, tuesday), 1)
        self.assertEqual(self.trace.read_text().splitlines(), ['capture', 'evidence', 'upload', 'collect'])
        self.assertNotIn('completed_due', self.state_data())
        self.assertIn('CLI adaptation failed', json.loads((self.state / 'weekly-status.json').read_text())['error'])

    def test_review_uses_advertised_reasoning_and_approval_controls(self):
        options = '''Options:
      --ephemeral
          Run without persisting session files
  -C, --cd <DIR>
          Set the working root
  -m, --model <MODEL>
          Model the agent should use
      --sandbox <MODE>
          Sandbox policy for model-generated commands: danger-full-access
      --events
          Print events as JSONL
      --report <FILE>
          File where the last message should be written
      --reasoning-effort <LEVEL>
          Set reasoning effort
      --ask-for-approval <POLICY>
          Configure when human approval is needed
'''
        with patch.object(jobs, 'cli_text', side_effect=['Commands:\n  run  Run Codex non-interactively', options]):
            command = jobs.review_command(str(self.codex), self.repo, self.state / 'report')
        self.assertEqual(command[1], 'run')
        self.assertIn('--events', command)
        self.assertIn('--report', command)
        self.assertEqual(command[command.index('--reasoning-effort') + 1], 'xhigh')
        self.assertEqual(command[command.index('--ask-for-approval') + 1], 'never')
        self.assertNotIn('model_reasoning_effort="xhigh"', command)
        self.assertNotIn('approval_policy="never"', command)

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
        self.assertEqual(self.trace.read_text().splitlines()[-7:], ['capture', 'evidence', 'upload', 'collect', 'codex', 'publish', 'deploy'])
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


if __name__ == '__main__':
    unittest.main()
