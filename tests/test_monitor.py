from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import monitor

NOW = datetime(2026, 10, 12, 4, tzinfo=timezone.utc)


def snapshot(host='client-host'):
    status = {'phase': 'finished', 'success': True, 'finished_at': NOW.isoformat()}
    result = {'host_id': host, 'captured_at': NOW.isoformat(), 'errors': [], 'cycle_busy': False,
              'jobs': {'sync': {'loaded': True, 'running': False, 'last_exit': 0}}, 'sync': status}
    if host == 'maintainer-host':
        result['jobs']['weekly'] = {'loaded': True, 'running': False, 'last_exit': 0}
        result['weekly'] = deepcopy(status)
        result['weekly_state'] = {'setup_started_at': '2026-10-09T14:26:00+00:00',
                                  'completed_due': '2026-10-12T09:00:00+09:00'}
    return result


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.local, self.remote = snapshot(), snapshot('maintainer-host')
        self.previous = {'checked_at': (NOW - timedelta(minutes=5)).isoformat(),
                         'grace_until': (NOW - timedelta(hours=1)).isoformat(), 'problems': {}}

    def test_healthy_hosts(self):
        self.assertFalse(monitor.evaluate(self.local, self.remote, self.previous, NOW)['problems'])

    def test_hourly_poll_does_not_reset_grace_or_outage_tracking(self):
        self.previous['checked_at'] = (NOW - timedelta(hours=1)).isoformat()
        self.previous['unreachable_since'] = (NOW - timedelta(hours=1)).isoformat()
        self.local['sync']['finished_at'] = (NOW - timedelta(hours=2)).isoformat()
        state = monitor.evaluate(self.local, None, self.previous, NOW)
        self.assertEqual(state['grace_until'], self.previous['grace_until'])
        self.assertEqual(state['unreachable_since'], self.previous['unreachable_since'])
        self.assertIn('client-host:sync:stale', state['problems'])
        self.assertIn('maintainer-host:unreachable', state['problems'])

    def test_failed_retry_is_not_recovery_until_success(self):
        self.remote['sync'] = {'phase': 'finished', 'success': False, 'finished_at': NOW.isoformat()}
        first = monitor.evaluate(self.local, self.remote, self.previous, NOW)
        self.assertIn('maintainer-host:sync:failure', first['problems'])
        self.remote['sync'] = {'phase': 'running', 'success': None, 'started_at': NOW.isoformat(),
                               'last_failure_finished_at': NOW.isoformat()}
        retry = monitor.evaluate(self.local, self.remote, first, NOW + timedelta(minutes=5))
        self.assertIn('maintainer-host:sync:failure', retry['problems'])
        self.remote['sync'].update(phase='finished', success=True, finished_at=(NOW + timedelta(minutes=6)).isoformat())
        self.local['captured_at'] = self.remote['captured_at'] = (NOW + timedelta(minutes=6)).isoformat()
        done = monitor.evaluate(self.local, self.remote, retry, NOW + timedelta(minutes=6))
        self.assertNotIn('maintainer-host:sync:failure', done['problems'])

    def test_missing_status_and_bad_clock_do_not_recover_failures(self):
        self.previous['problems'] = {'maintainer-host:sync:failure': 'failed'}
        self.remote['sync'] = None
        result = monitor.evaluate(self.local, self.remote, self.previous, NOW)
        self.assertIn('maintainer-host:sync:failure', result['problems'])
        self.remote['sync'] = snapshot('maintainer-host')['sync']
        self.remote['captured_at'] = (NOW + timedelta(hours=1)).isoformat()
        result = monitor.evaluate(self.local, self.remote, self.previous, NOW)
        self.assertIn('maintainer-host:sync:failure', result['problems'])

    def test_active_weekly_does_not_recover_prior_overdue_or_stale(self):
        self.previous['problems'] = {'maintainer-host:weekly:overdue': 'overdue', 'maintainer-host:sync:stale': 'stale'}
        self.remote['weekly_state'].pop('completed_due')
        self.remote['cycle_busy'] = True
        self.remote['sync']['finished_at'] = (NOW - timedelta(hours=2)).isoformat()
        self.remote['weekly'] = {'phase': 'running', 'success': None, 'started_at': NOW.isoformat()}
        result = monitor.evaluate(self.local, self.remote, self.previous, NOW)
        self.assertIn('maintainer-host:weekly:overdue', result['problems'])
        self.assertIn('maintainer-host:sync:stale', result['problems'])
        self.remote['weekly_state']['completed_due'] = '2026-10-12T09:00:00+09:00'
        self.remote['sync']['finished_at'] = NOW.isoformat()
        result = monitor.evaluate(self.local, self.remote, result, NOW)
        self.assertNotIn('maintainer-host:weekly:overdue', result['problems'])
        self.assertNotIn('maintainer-host:sync:stale', result['problems'])

    def test_unloaded_and_crashed_jobs(self):
        self.local['jobs']['sync'].update(loaded=False, last_exit=9)
        problems = monitor.health(self.local, NOW)
        self.assertIn('client-host:sync:unloaded', problems)
        self.assertIn('client-host:sync:exit', problems)

    def test_hard_crash_requires_fresh_success_not_retry_or_lock_skip(self):
        self.local['jobs']['sync']['last_exit'] = 9
        self.local['sync'] = {'phase': 'running', 'success': None, 'started_at': NOW.isoformat(),
                              'last_success_finished_at': (NOW - timedelta(minutes=20)).isoformat()}
        state = monitor.evaluate(self.local, self.remote, self.previous, NOW)
        self.assertIn('client-host:sync:exit', state['problems'])
        self.local['jobs']['sync']['running'] = True
        state = monitor.evaluate(self.local, self.remote, state, NOW + timedelta(minutes=1))
        self.assertIn('client-host:sync:exit', state['problems'])
        self.local['jobs']['sync'].update(running=False, last_exit=0)
        state = monitor.evaluate(self.local, self.remote, state, NOW + timedelta(minutes=2))
        self.assertIn('client-host:sync:exit', state['problems'])
        self.local['sync'].update(phase='finished', success=True, finished_at=(NOW + timedelta(minutes=3)).isoformat())
        state = monitor.evaluate(self.local, self.remote, state, NOW + timedelta(minutes=3))
        self.assertNotIn('client-host:sync:exit', state['problems'])

    def test_stale_sync_and_active_weekly(self):
        self.remote['sync']['finished_at'] = (NOW - timedelta(hours=2)).isoformat()
        self.assertIn('maintainer-host:sync:stale', monitor.health(self.remote, NOW))
        self.remote['cycle_busy'] = True
        self.remote['weekly'] = {'phase': 'running', 'success': None, 'started_at': (NOW - timedelta(minutes=40)).isoformat()}
        self.assertNotIn('maintainer-host:sync:stale', monitor.health(self.remote, NOW))

    def test_capture_failure_cannot_hide_behind_successful_sync(self):
        self.remote['jobs']['capture'] = {'loaded': True, 'running': False, 'last_exit': None}
        self.remote['capture'] = {'phase': 'finished', 'success': False, 'finished_at': NOW.isoformat(),
                                  'last_success_finished_at': (NOW - timedelta(hours=1)).isoformat()}
        first = monitor.evaluate(self.local, self.remote, self.previous, NOW)
        self.assertIn('maintainer-host:capture:failure', first['problems'])
        self.remote['capture'] = {'phase': 'running', 'success': None, 'started_at': NOW.isoformat(),
                                  'last_failure_finished_at': NOW.isoformat()}
        retry = monitor.evaluate(self.local, self.remote, first, NOW + timedelta(minutes=1))
        self.assertIn('maintainer-host:capture:failure', retry['problems'])
        after = NOW + timedelta(minutes=2)
        self.remote['captured_at'] = self.local['captured_at'] = after.isoformat()
        self.remote['capture'].update(phase='finished', success=True, finished_at=after.isoformat())
        self.assertNotIn('maintainer-host:capture:failure', monitor.evaluate(self.local, self.remote, retry, after)['problems'])

    def test_capture_backlog_and_unobservable_recovery(self):
        self.local['jobs']['capture'] = {'loaded': True, 'running': False, 'last_exit': None}
        self.local['capture'] = {'phase': 'finished', 'success': True, 'finished_at': NOW.isoformat(),
                                 'counts': {'pending': 2, 'oldest_pending_at': (NOW - timedelta(hours=25)).isoformat()}}
        first = monitor.evaluate(self.local, self.remote, self.previous, NOW)
        self.assertIn('client-host:capture:backlog', first['problems'])
        self.local['capture'] = None
        self.assertIn('client-host:capture:backlog', monitor.evaluate(self.local, self.remote, first, NOW)['problems'])
        self.local['capture'] = {'phase': 'finished', 'success': True, 'finished_at': NOW.isoformat(), 'counts': {'pending': 0}}
        self.assertNotIn('client-host:capture:backlog', monitor.evaluate(self.local, self.remote, first, NOW)['problems'])

    def test_hourly_sync_is_healthy_and_clears_stale_alert(self):
        self.remote['sync']['finished_at'] = (NOW - timedelta(hours=1)).isoformat()
        self.previous['problems'] = {'maintainer-host:sync:stale': 'stale'}
        result = monitor.evaluate(self.local, self.remote, self.previous, NOW)
        self.assertNotIn('maintainer-host:sync:stale', result['problems'])
        self.remote['sync']['finished_at'] = (NOW - timedelta(minutes=91)).isoformat()
        self.assertIn('maintainer-host:sync:stale', monitor.health(self.remote, NOW))

    def test_stuck_cycle(self):
        self.local['sync'] = {'phase': 'running', 'success': None, 'started_at': (NOW - timedelta(hours=2)).isoformat()}
        self.assertIn('client-host:sync:stuck', monitor.health(self.local, NOW))

    def test_pending_weekly_deadline_and_setup_gate(self):
        del self.remote['weekly_state']['completed_due']
        self.assertIn('maintainer-host:weekly:overdue', monitor.health(self.remote, NOW))
        self.remote['weekly_state']['setup_started_at'] = NOW.isoformat()
        self.assertNotIn('maintainer-host:weekly:overdue', monitor.health(self.remote, NOW))

    def test_transient_disconnect_and_sustained_outage(self):
        state = monitor.evaluate(self.local, None, self.previous, NOW)
        self.assertNotIn('maintainer-host:unreachable', state['problems'])
        for minutes in (5, 10, 15):
            self.local['captured_at'] = (NOW + timedelta(minutes=minutes)).isoformat()
            state = monitor.evaluate(self.local, None, state, NOW + timedelta(minutes=minutes))
        self.assertIn('maintainer-host:unreachable', state['problems'])
        self.local['captured_at'] = self.remote['captured_at'] = (NOW + timedelta(minutes=16)).isoformat()
        state = monitor.evaluate(self.local, self.remote, state, NOW + timedelta(minutes=16))
        self.assertNotIn('maintainer-host:unreachable', state['problems'])

    def test_unreachable_host_cannot_recover_prior_failure(self):
        self.previous['problems'] = {'maintainer-host:sync:failure': 'failed'}
        state = monitor.evaluate(self.local, None, self.previous, NOW)
        self.assertEqual(state['problems']['maintainer-host:sync:failure'], 'failed')

    def test_remote_wake_gets_grace_while_local_remains_awake(self):
        state = monitor.evaluate(self.local, None, self.previous, NOW)
        state['unreachable_since'] = (NOW - timedelta(hours=2)).isoformat()
        state['problems']['maintainer-host:unreachable'] = 'unreachable'
        self.remote['sync']['finished_at'] = (NOW - timedelta(hours=2)).isoformat()
        after = monitor.evaluate(self.local, self.remote, state, NOW + timedelta(minutes=5))
        self.assertNotIn('maintainer-host:sync:stale', after['problems'])
        self.assertNotIn('maintainer-host:unreachable', after['problems'])
        self.remote['sync']['finished_at'] = (NOW + timedelta(minutes=6)).isoformat()
        self.local['captured_at'] = self.remote['captured_at'] = (NOW + timedelta(minutes=6)).isoformat()
        final = monitor.evaluate(self.local, self.remote, after, NOW + timedelta(minutes=6))
        self.assertFalse(final['problems'])

    def test_sleep_does_not_clear_known_stuck_retry(self):
        self.local['sync'] = {'phase': 'running', 'success': None,
                              'started_at': (NOW - timedelta(hours=3)).isoformat()}
        self.previous['checked_at'] = (NOW - timedelta(hours=2)).isoformat()
        self.previous['problems'] = {'client-host:sync:stuck': 'stuck'}
        state = monitor.evaluate(self.local, self.remote, self.previous, NOW)
        self.assertIn('client-host:sync:stuck', state['problems'])
        state = monitor.evaluate(self.local, self.remote, state, NOW + timedelta(minutes=5))
        self.assertIn('client-host:sync:stuck', state['problems'])

    def test_wake_grace_and_existing_stale_issue(self):
        self.local['sync']['finished_at'] = (NOW - timedelta(hours=2)).isoformat()
        self.previous['checked_at'] = (NOW - timedelta(hours=2)).isoformat()
        state = monitor.evaluate(self.local, None, self.previous, NOW)
        self.assertFalse(state['problems'])
        self.previous['problems'] = {'client-host:sync:stale': 'stale'}
        state = monitor.evaluate(self.local, self.remote, self.previous, NOW)
        self.assertIn('client-host:sync:stale', state['problems'])
        self.local['sync']['finished_at'] = NOW.isoformat()
        state = monitor.evaluate(self.local, self.remote, self.previous, NOW)
        self.assertNotIn('client-host:sync:stale', state['problems'])

    def test_clock_and_invalid_time(self):
        self.remote['captured_at'] = (NOW + timedelta(hours=1)).isoformat()
        self.assertIn('maintainer-host:clock', monitor.health(self.remote, NOW))
        self.remote['captured_at'] = NOW.isoformat()
        self.remote['sync']['finished_at'] = 'bad'
        with self.assertRaises(ValueError): monitor.health(self.remote, NOW)
        self.remote['sync']['finished_at'] = (NOW + timedelta(hours=1)).isoformat()
        with self.assertRaises(ValueError): monitor.health(self.remote, NOW)

    def test_sleep_does_not_count_toward_stuck_cycle(self):
        self.remote['cycle_busy'] = True
        self.remote['sync']['finished_at'] = (NOW - timedelta(hours=9)).isoformat()
        self.remote['weekly'] = {'phase': 'running', 'success': None,
                                 'started_at': (NOW - timedelta(hours=8)).isoformat()}
        self.previous['checked_at'] = (NOW - timedelta(hours=8)).isoformat()
        state = monitor.evaluate(self.local, self.remote, self.previous, NOW)
        for minutes in range(5, 101, 5):
            now = NOW + timedelta(minutes=minutes)
            self.local['captured_at'] = self.remote['captured_at'] = now.isoformat()
            self.local['sync']['finished_at'] = now.isoformat()
            state = monitor.evaluate(self.local, self.remote, state, now)
            if minutes <= 95:
                self.assertNotIn('maintainer-host:weekly:stuck', state['problems'])
                self.assertNotIn('maintainer-host:sync:stale', state['problems'])
        self.assertIn('maintainer-host:weekly:stuck', state['problems'])

    def test_notification_dedup_recovery_and_failed_delivery_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = {'checked_at': NOW.isoformat(), 'problems': {'client-host:sync:failure': 'failed'}, 'notified_problems': {}}
            with patch.object(monitor, 'notify', side_effect=RuntimeError('denied')):
                with self.assertRaises(RuntimeError): monitor.deliver(state, {}, root)
            self.assertFalse(state['notified_problems'])
            with patch.object(monitor, 'notify') as send:
                monitor.deliver(state, {}, root)
                monitor.deliver(state, {}, root)
                self.assertEqual(send.call_count, 1)
                state['problems'] = {}
                monitor.deliver(state, {}, root)
                self.assertEqual(send.call_count, 2)
                self.assertIn('recovered', send.call_args.args[1])
            self.assertEqual(len((root / 'monitor-events.jsonl').read_text().splitlines()), 2)

    def test_exception_state_can_recover_on_next_poll(self):
        previous = {'checked_at': (NOW - timedelta(minutes=5)).isoformat(),
                    'problems': {'monitor:error': 'failed'}, 'notified_problems': {'monitor:error': 'failed'}}
        result = monitor.evaluate(self.local, self.remote, previous, NOW)
        self.assertFalse(result['problems'])

    def test_broken_monitor_configuration_still_alerts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'monitor-config.json'
            config.write_text('invalid JSON')
            with patch.object(sys, 'argv', ['monitor.py', 'check', '--config', str(config)]), patch.object(monitor, 'notify') as send, patch('builtins.print'):
                self.assertEqual(monitor.main(), 1)
            self.assertEqual(send.call_count, 1)
            state = json.loads((root / 'monitor-state.json').read_text())
            self.assertIn('monitor:error', state['notified_problems'])

    def test_probe_rejects_wrong_host_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'config.json').write_text(json.dumps({'host_id': 'maintainer-host', 'role': 'client'}))
            with self.assertRaises(ValueError): monitor.probe(root)

    def test_probe_does_not_create_missing_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'config.json').write_text(json.dumps({'host_id': 'client-host', 'role': 'client'}))
            result = monitor.probe(root)
            self.assertFalse((root / 'cycle.lock').exists())
            self.assertIn('cycle lock is missing', result['errors'])

    def test_failure_click_routes_both_hosts_to_each_job_log(self):
        with tempfile.TemporaryDirectory(prefix='monitor logs ') as tmp:
            root = Path(tmp)
            for host in ('client-host', 'maintainer-host'):
                for job in ('sync', 'weekly', 'capture'):
                    with self.subTest(host=host, job=job), patch.object(monitor.subprocess, 'run') as run:
                        run.return_value.returncode = 0
                        monitor.notify({}, 'Failure', 'failed', root / 'health.txt', route=(host, job), failure=True)
                        command = run.call_args.args[0]
                        self.assertNotIn('-open', command)
                        action = shlex.split(command[command.index('-execute') + 1])
                        self.assertEqual(action[action.index('--host') + 1], host)
                        self.assertEqual(action[action.index('--job') + 1], 'sync' if job == 'capture' else job)
                        self.assertEqual(action[action.index('--config') + 1], str(root / 'monitor-config.json'))
                        self.assertIn(host + '-' + job, command[command.index('-group') + 1])

    def test_capture_failure_opens_enclosing_weekly_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = {'local': self.local, 'remote': self.remote}
            data['remote']['weekly'].update(started_at=(NOW - timedelta(minutes=10)).isoformat())
            data['remote']['capture'] = {'started_at': (NOW - timedelta(minutes=9)).isoformat()}
            (root / 'health.json').write_text(json.dumps(data))
            with patch.object(monitor.subprocess, 'run') as run:
                run.return_value.returncode = 0
                monitor.notify({}, 'Failure', 'failed', root / 'health.txt',
                               route=('maintainer-host', 'capture'), failure=True)
                command = run.call_args.args[0]
                action = shlex.split(command[command.index('-execute') + 1])
                self.assertEqual(action[action.index('--job') + 1], 'weekly')
            data['remote']['capture']['started_at'] = (NOW + timedelta(minutes=1)).isoformat()
            (root / 'health.json').write_text(json.dumps(data))
            self.assertEqual(monitor.log_job(root, 'maintainer-host', 'capture'), 'sync')

    def test_distinct_failure_alerts_and_partial_delivery_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            problems = {'client-host:sync:failure': 'local failed', 'client-host:sync:exit': 'local exited',
                        'maintainer-host:weekly:failure': 'remote failed'}
            state = {'checked_at': NOW.isoformat(), 'problems': problems, 'notified_problems': {}}
            with patch.object(monitor, 'notify', side_effect=[None, RuntimeError('denied')]) as send:
                with self.assertRaises(RuntimeError): monitor.deliver(state, {}, root)
                self.assertEqual(send.call_count, 2)
                self.assertEqual(send.call_args_list[0].kwargs['route'], ('client-host', 'sync'))
                self.assertEqual(send.call_args_list[1].kwargs['route'], ('maintainer-host', 'weekly'))
            self.assertEqual(set(state['notified_problems']), {'client-host:sync:failure', 'client-host:sync:exit'})
            with patch.object(monitor, 'notify') as send:
                monitor.deliver(state, {}, root)
                monitor.deliver(state, {}, root)
                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.kwargs['route'], ('maintainer-host', 'weekly'))
            self.assertEqual(state['notified_problems'], problems)

    def test_recovery_keeps_failed_jobs_clickable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = {'checked_at': NOW.isoformat(), 'problems': {'client-host:sync:exit': 'still failed'},
                     'notified_problems': {'client-host:sync:exit': 'still failed', 'client-host:sync:failure': 'old failure'}}
            with patch.object(monitor, 'notify') as send:
                monitor.deliver(state, {}, root)
                self.assertTrue(send.call_args.kwargs['failure'])
                state['problems'] = {}
                monitor.deliver(state, {}, root)
                self.assertFalse(send.call_args.kwargs['failure'])
                self.assertEqual(send.call_args.kwargs['route'], ('client-host', 'sync'))

    def test_local_click_opens_log_and_preflight_log_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fallback = root / 'sync-launchd-error.log'
            fallback.write_text('preflight failed')
            with patch.object(monitor.subprocess, 'run') as run:
                self.assertEqual(monitor.open_job_log({}, root, 'client-host', 'sync'), fallback)
                run.assert_called_once_with(['/usr/bin/open', '-t', str(fallback)], check=True, timeout=10)
            (root / 'sync.log').write_text('job failure details')
            self.assertEqual(monitor.job_log(root, 'sync'), root / 'sync.log')

    def test_recent_preflight_log_is_not_hidden_by_old_worker_or_empty_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, data, modified in [('sync.log', 'old job output', 100),
                                         ('sync-launchd.log', 'new preflight failure', 200),
                                         ('sync-launchd-error.log', '', 300)]:
                path = root / name
                path.write_text(data)
                os.utime(path, (modified, modified))
            self.assertEqual(monitor.job_log(root, 'sync'), root / 'sync-launchd.log')

    def test_unknown_problems_use_monitor_log_and_unreadable_job_state_keeps_its_route(self):
        for key in ('monitor:error', 'unknown', 'maintainer-host:unreachable', 'client-host:data:'):
            self.assertEqual(monitor.problem_job(key), ('client-host', 'monitor'))
        self.assertEqual(monitor.problem_job('maintainer-host:data:weekly status is unreadable'),
                         ('maintainer-host', 'weekly'))

    def test_failed_log_click_reports_unavailability_without_changing_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argv = ['monitor.py', 'open-log', '--config', str(root / 'monitor-config.json'),
                    '--host', 'maintainer-host', '--job', 'sync']
            with patch.object(sys, 'argv', argv), patch.object(monitor, 'notify') as send:
                self.assertEqual(monitor.main(), 1)
                self.assertIn('log unavailable', send.call_args.args[1])
            self.assertFalse((root / 'monitor-state.json').exists())

    def test_remote_click_fetches_exact_log_then_opens_local_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {'remote_host': 'maintainer-host', 'remote_python': '/remote/python runtime/python',
                      'remote_script': '/remote/scripts/monitor.py'}
            data = b'actual remote weekly log\n\xff\n'
            def run(command, **kwargs):
                if command[0] == '/usr/bin/ssh':
                    self.assertIn('StrictHostKeyChecking=yes', command)
                    self.assertEqual(shlex.split(command[-1]), [config['remote_python'], config['remote_script'],
                                                               'read-log', '--job', 'weekly'])
                    kwargs['stdout'].write(data)
                return subprocess.CompletedProcess(command, 0)
            with patch.object(monitor.subprocess, 'run', side_effect=run) as calls:
                path = monitor.open_job_log(config, root, 'maintainer-host', 'weekly')
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(calls.call_args.args[0], ['/usr/bin/open', '-t', str(path)])
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_remote_fetch_failure_preserves_cache_without_opening_stale_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'remote-logs/maintainer-host/sync.log'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'previous valid log')
            config = {'remote_host': 'maintainer-host', 'remote_python': '/remote/python', 'remote_script': '/remote/monitor.py'}
            def run(command, **kwargs):
                kwargs['stdout'].write(b'partial failed transfer')
                raise subprocess.CalledProcessError(255, command)
            with patch.object(monitor.subprocess, 'run', side_effect=run) as calls:
                with self.assertRaises(subprocess.CalledProcessError):
                    monitor.open_job_log(config, root, 'maintainer-host', 'sync')
                self.assertEqual(calls.call_count, 1)
            self.assertEqual(path.read_bytes(), b'previous valid log')
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_remote_click_rejects_symlink_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'elsewhere').mkdir()
            (root / 'remote-logs').symlink_to(root / 'elsewhere', target_is_directory=True)
            with patch.object(monitor.subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    monitor.open_job_log({'remote_host': 'maintainer-host'}, root, 'maintainer-host', 'sync')
                run.assert_not_called()

    def test_log_command_streams_bytes_without_host_state_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b'actual job output\n\xff\n'
            (root / 'sync.log').write_bytes(data)
            result = subprocess.run([sys.executable, str(Path(monitor.__file__)), 'read-log',
                                     '--config', str(root / 'monitor-config.json'), '--job', 'sync'], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, data)
            self.assertEqual([p.name for p in root.iterdir()], ['sync.log'])


if __name__ == '__main__':
    unittest.main()
