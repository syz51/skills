#!/usr/bin/env python3
"""Check both skill hosts and deliver failure/recovery notifications locally."""
import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

from jobs import latest_due, save

ROOT = Path.home() / '.local/share/codex-skills'
SYNC_STALE_AFTER = timedelta(minutes=90)


def read_object(path):
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError('Expected a JSON object')
    return data


def stamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('Timestamp needs a time zone')
    return result


def probe(root):
    config = read_object(root / 'config.json')
    if config.get('role') != {'client-host': 'client', 'maintainer-host': 'maintainer'}.get(config.get('host_id')):
        raise ValueError('Unexpected host role')
    result = {'host_id': config['host_id'], 'captured_at': datetime.now(timezone.utc).isoformat(),
              'jobs': {}, 'errors': [], 'cycle_busy': False}
    commands = ['sync', 'weekly'] if config['role'] == 'maintainer' else ['sync']
    for name in commands:
        target = f'gui/{os.getuid()}/org.example.skills.{name}'
        process = subprocess.run(['/bin/launchctl', 'print', target], capture_output=True, text=True, timeout=10)
        exit_code = re.search(r'last exit code = (-?\d+)', process.stdout)
        result['jobs'][name] = {'loaded': process.returncode == 0,
                                'running': bool(re.search(r'^\s*state = running$', process.stdout, re.M)),
                                'last_exit': int(exit_code[1]) if exit_code else None}
        path = root / (name + '-status.json')
        try:
            result[name] = read_object(path) if path.exists() else None
        except (OSError, ValueError):
            result[name] = None
            result['errors'].append(name + ' status is unreadable')
    if config['role'] == 'maintainer':
        try:
            result['weekly_state'] = read_object(root / 'weekly-state.json')
        except (OSError, ValueError):
            result['weekly_state'] = None
            result['errors'].append('weekly schedule state is unreadable')
    # Never block or change the worker's lock; it can legitimately span a weekly review.
    if not (root / 'cycle.lock').exists():
        result['errors'].append('cycle lock is missing')
    else:
        with (root / 'cycle.lock').open('rb') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result['cycle_busy'] = True
    try:
        result['capture'] = read_object(root / 'capture-status.json')
    except (OSError, ValueError):
        result['capture'] = None
        result['errors'].append('capture status is unreadable')
    result['jobs']['capture'] = {'loaded': result['jobs']['sync']['loaded'],
                                 'running': result['cycle_busy'] and (result['capture'] or {}).get('phase') == 'running',
                                 'last_exit': None}
    return result


def health(snapshot, now, grace=False):
    host = snapshot['host_id']
    problems = {}
    for error in snapshot['errors']:
        problems[host + ':data:' + error] = host + ': ' + error
    if abs((stamp(snapshot['captured_at']) - now).total_seconds()) > 300:
        return {host + ':clock': host + ': clock differs by more than five minutes'}
    weekly = snapshot.get('weekly') or {}
    active_weekly = (snapshot['cycle_busy'] and weekly.get('phase') == 'running'
                     and now - stamp(weekly['started_at']) <= timedelta(minutes=95))
    for command, job in snapshot['jobs'].items():
        key = host + ':' + command
        if not job['loaded']:
            problems[key + ':unloaded'] = host + ': ' + command + ' scheduler is not loaded'
        if job['last_exit'] not in (None, 0) and not job['running']:
            problems[key + ':exit'] = host + ': ' + command + ' process exited with an error'
        status = snapshot.get(command)
        if not status:
            if command in ('sync', 'capture') and not grace:
                problems[key + ':missing'] = host + ': ' + command + ' has no readable run status'
            continue
        if not isinstance(status, dict):
            raise ValueError('Invalid job status')
        for field in ('started_at', 'finished_at', 'last_success_finished_at', 'last_failure_finished_at'):
            if status.get(field) and stamp(status[field]) > now + timedelta(minutes=5):
                raise ValueError('Job status timestamp is in the future')
        success = status.get('last_success_finished_at')
        failure = status.get('last_failure_finished_at')
        if status.get('phase') != 'running' and status.get('success') is True:
            success = status['finished_at']
        elif status.get('phase') != 'running' and status.get('success') is False:
            failure = status['finished_at']
        if failure and (not success or stamp(failure) > stamp(success)):
            problems[key + ':failure'] = host + ': ' + command + ' failed; no successful retry yet'
        if status.get('phase') == 'running':
            if not grace and now - stamp(status['started_at']) > timedelta(minutes=95):
                problems[key + ':stuck'] = host + ': ' + command + ' has not finished within 95 minutes'
        if command in ('sync', 'capture') and not grace and not active_weekly:
            if not success or now - stamp(success) > SYNC_STALE_AFTER:
                problems[key + ':stale'] = host + ': ' + command + ' has not succeeded within 90 minutes'
        if command == 'capture':
            counts = status.get('counts') or {}
            oldest = counts.get('oldest_pending_at')
            if counts.get('pending', 0) and oldest and now - stamp(oldest) > timedelta(hours=24):
                problems[key + ':backlog'] = host + ': completed skill work has waited over 24 hours for feedback capture'
    if 'weekly' in snapshot['jobs'] and not grace and not active_weekly:
        state = snapshot.get('weekly_state')
        if state:
            due = latest_due(now)
            completed = state.get('completed_due')
            if (due >= stamp(state['setup_started_at']) and now - due > timedelta(hours=3)
                    and (not completed or stamp(completed) < due)):
                problems[host + ':weekly:overdue'] = host + ': Monday skill review is more than three hours overdue'
    return problems


def recovered(key, snapshot, now, observed):
    if key == 'monitor:error':
        return True
    if snapshot is None:
        return False
    clock_ok = abs((stamp(snapshot['captured_at']) - now).total_seconds()) <= 300
    if key.endswith(':clock'):
        return clock_ok
    if not clock_ok:
        return False
    if key.endswith(':unreachable'):
        return True
    if ':data:' in key:
        return key.split(':data:', 1)[1] not in snapshot['errors']
    _, command, kind = key.split(':', 2)
    job = snapshot['jobs'].get(command) or {}
    if kind == 'unloaded':
        return job.get('loaded') is True
    status = snapshot.get(command) or {}
    success = status.get('last_success_finished_at')
    if status.get('success') is True:
        success = status.get('finished_at')
    if kind == 'failure':
        failure = status.get('last_failure_finished_at') or observed
        return bool(success and stamp(success) > stamp(failure))
    if kind == 'exit':
        return bool(success and stamp(success) > stamp(observed))
    if kind in ('stale', 'missing'):
        return bool(success and now - stamp(success) <= SYNC_STALE_AFTER)
    if kind == 'stuck':
        return bool(status.get('phase') == 'finished' and status.get('finished_at')
                    and stamp(status['finished_at']) > stamp(observed))
    if kind == 'backlog':
        counts = status.get('counts')
        return bool(status.get('success') is True and isinstance(counts, dict)
                    and (counts.get('pending') == 0 or (counts.get('oldest_pending_at')
                         and now - stamp(counts['oldest_pending_at']) <= timedelta(hours=24))))
    if kind == 'overdue':
        completed = (snapshot.get('weekly_state') or {}).get('completed_due')
        return bool(completed and stamp(completed) >= latest_due(now))
    return False


def evaluate(local, remote, previous, now):
    last = previous.get('checked_at')
    waking = not last or now - stamp(last) > timedelta(minutes=75)
    grace_until = (now + timedelta(minutes=15)).isoformat() if waking else previous.get('grace_until', now.isoformat())
    grace = now < stamp(grace_until)
    remote_grace_until = previous.get('remote_grace_until', now.isoformat())
    if remote is not None and previous.get('remote_reachable') is False:
        remote_grace_until = (now + timedelta(minutes=15)).isoformat()
    remote_grace = grace or now < stamp(remote_grace_until)
    previous_problems = previous.get('problems', {})
    running_observed = {}
    adjusted = {}
    for snapshot in (local, remote):
        if snapshot is None:
            continue
        copy = json.loads(json.dumps(snapshot))
        for command in snapshot['jobs']:
            status = snapshot.get(command) or {}
            if status.get('phase') != 'running':
                continue
            key = snapshot['host_id'] + ':' + command
            old = previous.get('running_observed', {}).get(key, {})
            since = now.isoformat() if waking or old.get('run_started_at') != status['started_at'] else old['observed_since']
            running_observed[key] = {'run_started_at': status['started_at'], 'observed_since': since}
            # Suspended Macs do not use up an observed running-time budget.
            copy[command]['started_at'] = since
        adjusted[snapshot['host_id']] = copy
    problems = health(adjusted['client-host'], now, grace)
    unreachable_since = None
    if remote is None:
        unreachable_since = now.isoformat() if waking else previous.get('unreachable_since') or now.isoformat()
        # An unobservable host cannot prove a previous failure recovered.
        problems.update({k: v for k, v in previous_problems.items() if k.startswith('maintainer-host:')})
        if not grace and now - stamp(unreachable_since) >= timedelta(minutes=15):
            problems['maintainer-host:unreachable'] = 'maintainer-host: health probe unavailable for 15 minutes (check Tailscale/SSH and remote setup)'
    else:
        problems.update(health(adjusted['maintainer-host'], now, remote_grace))
    # Suppression and missing observations cannot prove an incident recovered.
    for key, value in previous_problems.items():
        host = 'client-host' if key.startswith('client-host:') else 'maintainer-host'
        host_snapshot = adjusted.get(host)
        observed = previous.get('incident_since', {}).get(key, previous.get('checked_at', now.isoformat()))
        if key not in problems and not recovered(key, host_snapshot, now, observed):
            problems[key] = value
    return {'checked_at': now.isoformat(), 'grace_until': grace_until,
            'remote_grace_until': remote_grace_until, 'remote_reachable': remote is not None,
            'unreachable_since': unreachable_since, 'problems': problems,
            'incident_since': {key: previous.get('incident_since', {}).get(key, now.isoformat()) for key in problems},
            'running_observed': running_observed,
            'notified_problems': previous.get('notified_problems', {})}


def problem_job(key):
    parts = key.split(':', 2)
    if len(parts) < 2:
        return 'client-host', 'monitor'
    host, job = parts[:2]
    if job == 'data' and len(parts) == 3:
        job = parts[2].split()[0] if parts[2].split() else ''
    if host in ('client-host', 'maintainer-host') and job in ('sync', 'weekly', 'capture'):
        return host, job
    return 'client-host', 'monitor'


def log_job(root, host, job):
    if job != 'capture':
        return job
    # Capture writes to its enclosing sync or weekly worker's log.
    try:
        report = read_object(root / 'health.json')
        snapshot = report['local' if host == 'client-host' else 'remote']
        capture, weekly = snapshot['capture'], snapshot.get('weekly') or {}
        started = stamp(capture['started_at'])
        if weekly.get('started_at') and stamp(weekly['started_at']) <= started:
            if weekly.get('phase') == 'running' or started <= stamp(weekly['finished_at']):
                return 'weekly'
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return 'sync'


def job_log(root, job):
    names = ['monitor-launchd.log'] if job == 'monitor' else [job + '.log']
    names += [job + '-launchd-error.log', job + '-launchd.log']
    paths = [root / name for name in names if (root / name).is_file() and not (root / name).is_symlink()]
    if not paths:
        raise FileNotFoundError('No readable log for ' + job)
    # Preflight crashes can write only to launchd stdout/stderr, leaving an older worker log.
    return max(paths, key=lambda path: (bool(path.stat().st_size), path.stat().st_mtime_ns))


def open_job_log(config, root, host, job):
    if host == 'client-host':
        path = job_log(root, job)
    else:
        if config.get('remote_host') != 'maintainer-host':
            raise ValueError('Unexpected SSH host')
        folder = root / 'remote-logs' / host
        if folder.is_symlink() or folder.parent.is_symlink():
            raise ValueError('Remote log cache must not be a symlink')
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / (job + '.log')
        command = shlex.join([config['remote_python'], config['remote_script'], 'read-log', '--job', job])
        with tempfile.NamedTemporaryFile(dir=folder, prefix='.' + job + '-', delete=False) as output:
            temporary = Path(output.name)
            try:
                subprocess.run(['/usr/bin/ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                                '-o', 'ConnectTimeout=10', '-o', 'ConnectionAttempts=1',
                                '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
                                config['remote_host'], command], stdout=output, stderr=subprocess.PIPE,
                               check=True, timeout=30)
                output.flush()
                os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    subprocess.run(['/usr/bin/open', '-t', str(path)], check=True, timeout=10)
    return path


def notify(config, title, message, report, route=None, failure=False):
    notifier = config.get('notifier_path', str(Path.home() / 'Applications/terminal-notifier.app/Contents/MacOS/terminal-notifier'))
    group = 'codex-skills-health' + ('-' + '-'.join(route) if route else '')
    command = [notifier, '-title', title, '-message', message,
               '-group', group, '-sound', 'default']
    if failure:
        host, job = route
        action = [sys.executable, str(Path(__file__).resolve()), 'open-log',
                  '--config', str(report.parent / 'monitor-config.json'), '--host', host,
                  '--job', log_job(report.parent, host, job)]
        command += ['-execute', shlex.join(action)]
    else:
        command += ['-open', report.as_uri()]
    process = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if process.returncode:
        raise RuntimeError('macOS notification failed (exit ' + str(process.returncode) + '); check terminal-notifier permissions')


def deliver(state, config, root):
    current = state['problems']
    notified = state['notified_problems']
    added, cleared = sorted(set(current) - set(notified)), sorted(set(notified) - set(current))
    if not added and not cleared:
        return
    routes = sorted({problem_job(key) for key in added + cleared})
    for route in routes:
        new = [key for key in added if problem_job(key) == route]
        done = [key for key in cleared if problem_job(key) == route]
        active = [key for key in current if problem_job(key) == route]
        lines = [current[key] for key in active] + ['Recovered: ' + notified[key] for key in done]
        title = 'Codex skills: needs attention' if active else 'Codex skills: recovered'
        title += ' — ' + ' / '.join(route)
        caption = 'Click to open this job’s log.' if active else 'Click for current health.'
        notify(config, title, '\n'.join(lines)[:900] + '\n' + caption, root / 'health.txt',
               route=route, failure=bool(active))
        with (root / 'monitor-events.jsonl').open('a') as stream:
            stream.write(json.dumps({'at': state['checked_at'], 'title': title, 'messages': lines}) + '\n')
        for key in new:
            notified[key] = current[key]
        for key in done:
            del notified[key]


def inspect(config, root, previous, now):
    local = probe(root)
    if local['host_id'] != 'client-host':
        raise ValueError('Notifications must run on client-host')
    if config['remote_host'] != 'maintainer-host':
        raise ValueError('Unexpected SSH host')
    command = shlex.join([config['remote_python'], config['remote_script'], 'probe'])
    try:
        process = subprocess.run(['/usr/bin/ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                                  '-o', 'ConnectTimeout=10', '-o', 'ConnectionAttempts=1',
                                  '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
                                  config['remote_host'], command], capture_output=True, text=True, timeout=30)
        if process.returncode:
            remote = None
        else:
            remote = json.loads(process.stdout)
            if not isinstance(remote, dict) or remote.get('host_id') != 'maintainer-host':
                raise ValueError('Invalid remote probe')
            health(remote, now)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        remote = None
    state = evaluate(local, remote, previous, now)
    state['remote_reachable'] = remote is not None
    report = {'monitor': state, 'local': local, 'remote': remote}
    save(root / 'health.json', report)
    lines = ['Codex skills health — ' + now.astimezone().isoformat(),
             'Local monitor checks both hosts every hour.',
             'Remote reachable: ' + str(remote is not None),
             'Wake/login grace until: ' + state['grace_until'], '']
    lines.insert(-1, 'Remote reconnection grace until: ' + state['remote_grace_until'])
    lines += list(state['problems'].values()) or ['No detected problems.']
    lines += ['', 'Local logs: ' + str(root),
              'Remote logs: maintainer-host:~/.local/share/codex-skills/',
              'Details: ' + str(root / 'health.json'),
              'If this report is older than 75 minutes while this Mac is awake, the monitor itself needs checking.']
    (root / 'health.txt').write_text('\n'.join(lines) + '\n')
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('probe', 'check', 'test-notification', 'read-log', 'open-log'))
    parser.add_argument('--config', type=Path, default=ROOT / 'monitor-config.json')
    parser.add_argument('--host', choices=('client-host', 'maintainer-host'))
    parser.add_argument('--job', choices=('sync', 'weekly', 'monitor'))
    args = parser.parse_args()
    if args.command in ('read-log', 'open-log') and not args.job:
        parser.error('--job is required for log commands')
    if args.command == 'open-log' and not args.host:
        parser.error('--host is required for open-log')
    if args.command == 'read-log':
        with job_log(args.config.resolve().parent, args.job).open('rb') as stream:
            shutil.copyfileobj(stream, sys.stdout.buffer)
        return 0
    if args.command == 'probe':
        print(json.dumps(probe(ROOT)))
        return 0
    try:
        config = read_object(args.config)
    except (OSError, ValueError):
        config = {}
    root = args.config.resolve().parent
    if args.command == 'open-log':
        try:
            open_job_log(config, root, args.host, args.job)
            return 0
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            notify(config, 'Codex skills: log unavailable',
                   'Could not open ' + args.host + ' / ' + args.job + ' log. Check SSH and log availability.',
                   root / 'health.txt')
            return 1
    if args.command == 'test-notification':
        notify(config, 'Codex skills: monitoring test', 'Test notification. Both Macs are monitored from this Mac.', root / 'health.txt')
        return 0
    with (root / 'monitor.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        path = root / 'monitor-state.json'
        try:
            previous = read_object(path) if path.exists() else {}
            state = inspect(config, root, previous, datetime.now(timezone.utc))
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            state = dict(locals().get('previous', {}))
            state.update(checked_at=datetime.now(timezone.utc).isoformat(),
                         problems={**state.get('problems', {}), 'monitor:error': 'Health monitor could not complete its checks'},
                         notified_problems=state.get('notified_problems', {}), error=type(error).__name__)
            (root / 'health.txt').write_text('Health monitor failed; inspect monitor-state.json and monitor-launchd-error.log.\n')
        try:
            deliver(state, config, root)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            state['notification_error'] = str(error)
        save(path, state)
        print(json.dumps(state))
        return 1 if state['problems'] or state.get('notification_error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
