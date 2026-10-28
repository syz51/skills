#!/usr/bin/env python3
"""Host-local synchronization and a single weekly maintenance cycle."""
import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from zoneinfo import ZoneInfo

from capture import cli_text, help_command, help_flag, help_options

TOKYO = ZoneInfo('Asia/Tokyo')


def latest_due(now):
    local = now.astimezone(TOKYO)
    due = (local - timedelta(days=local.weekday())).replace(hour=9, minute=0, second=0, microsecond=0)
    return due if due <= local else due - timedelta(days=7)


def weekly_due(now, state):
    due = latest_due(now)
    started = datetime.fromisoformat(state['setup_started_at'])
    completed = state.get('completed_due')
    return due >= started and (not completed or due > datetime.fromisoformat(completed))


def save(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(temporary, path)


def new_status(path, command, now):
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    status = {'command': command, 'started_at': now.isoformat(), 'phase': 'running', 'success': None}
    for name in ('last_success_finished_at', 'last_failure_finished_at'):
        if previous.get(name):
            status[name] = previous[name]
    if previous.get('finished_at') and isinstance(previous.get('success'), bool):
        status['last_success_finished_at' if previous['success'] else 'last_failure_finished_at'] = previous['finished_at']
    return status


def run(command, log, env, stdin=None, timeout=5400):
    with subprocess.Popen(command, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                          stdout=log, stderr=log, env=env, start_new_session=True, text=True) as process:
        try:
            process.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            # The leader can exit on SIGTERM while a descendant ignores it.
            # Finish the group cleanup even when waiting for the leader succeeded.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise RuntimeError('Command timed out: ' + Path(command[0]).name)
        if process.returncode:
            raise RuntimeError('Command failed: ' + Path(command[0]).name)


def review_command(cli, repo, report):
    """Discover the review invocation without changing its privileges or user configuration."""
    try:
        root_help = cli_text(cli, '--help')
        subcommand = help_command(root_help, ('exec', 'e'), r'(?:run|execute) Codex non.interactively\b')
        options = help_options(cli_text(cli, subcommand, '--help'))
        flags = {}
        for key, names, meaning in (
            ('ephemeral', ('--ephemeral',), r'without persisting session'),
            ('cd', ('-C', '--cd'), r'(?:working|workspace) root'),
            ('model', ('-m', '--model'), r'model.*(?:agent|use)|agent.*model'),
            ('config', ('-c', '--config'), r'override a configuration value'),
            ('sandbox', ('--sandbox', '-s'), r'sandbox policy.*model.generated'),
            ('output', ('-o', '--output-last-message'), r'file.*(?:last|final).*message|final.*message.*file'),
            ('json', ('--json', '--experimental-json'), r'print.*(?:JSONL|JSON events|newline.delimited JSON)')):
            flags[key] = help_flag(options, names, meaning, required=key != 'config')
        if 'danger-full-access' not in options[flags['sandbox']]:
            raise RuntimeError('Required review permissions are unavailable')
        effort = help_flag(options, ('--reasoning-effort', '--effort'), r'reasoning effort', required=False)
        approval = help_flag(options, ('--ask-for-approval', '-a'), r'when.*(?:approval|human approval)', required=False)
        if not flags['config'] and (not effort or not approval):
            raise RuntimeError('Required review model or approval controls are unavailable')
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        raise RuntimeError('Weekly review CLI adaptation failed; required command or controls are unavailable') from None
    command = [cli, subcommand, flags['ephemeral'], flags['cd'], str(repo), flags['model'], 'gpt-6.1-sol',
               flags['sandbox'], 'danger-full-access', flags['json'], flags['output'], str(report)]
    command += [effort, 'xhigh'] if effort else [flags['config'], 'model_reasoning_effort="xhigh"']
    command += [approval, 'never'] if approval else [flags['config'], 'approval_policy="never"']
    return command + ['-']


def review(cli, repo, state_root, prompt, log, env, timeout):
    began = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='.weekly-review-', dir=state_root) as folder:
        report = Path(folder) / 'report.md'
        command = review_command(cli, repo, report)
        remaining = timeout - (time.monotonic() - began)
        if remaining <= 0:
            raise RuntimeError('Weekly review command adaptation timed out')
        run(command, log, env, stdin=prompt, timeout=remaining)
        if report.is_symlink() or not report.is_file() or report.stat().st_size > 1000000:
            raise RuntimeError('Weekly review did not produce a valid fresh report; publication was deferred')
        os.replace(report, state_root / 'latest-weekly-report.md')


def prompt_workflow(repo):
    """Keep legacy checkouts usable, but reject an incomplete prompt workflow."""
    paths = ('scripts/prompt_runtime.py', 'scripts/prompt_review.py',
             'prompts/extract.md', 'prompts/verify.md', 'prompts/correct.md', 'prompts/maintain.md')
    present = [(repo / path).is_file() for path in paths]
    if any(present) and not all(present):
        raise RuntimeError('Prompt workflow is incomplete')
    return all(present)


def maintenance_prompt(repo, config):
    if not prompt_workflow(repo):
        return (repo / 'maintenance-prompt.md').read_text()
    from prompt_runtime import load
    return load('maintain', config=config)


def review_prompts(config, state_root, *, timeout, due):
    from prompt_review import run as run_review
    return run_review(config, state_root, timeout=timeout, due=due)


def execute(args, now=None):
    now = now or datetime.now(timezone.utc)
    config_path = args.config.expanduser().resolve()
    state_root = config_path.parent
    config = json.loads(config_path.read_text())
    required = ('repo_path', 'host_id', 'python_path', 'uv_path', 'codex_path', 'validator_path', 'role')
    if any(not isinstance(config.get(key), str) or not config[key] for key in required):
        raise ValueError('Missing or invalid host configuration')
    if config['host_id'] not in ('client-host', 'maintainer-host') or config['role'] not in ('client', 'maintainer'):
        raise ValueError('Invalid host or role')
    if args.command == 'weekly' and (config['role'] != 'maintainer' or config['host_id'] != 'maintainer-host'):
        raise ValueError('Weekly maintenance is restricted to the remote maintainer')
    repo = Path(config['repo_path']).expanduser().resolve()
    for key in ('python_path', 'uv_path', 'codex_path', 'validator_path'):
        if not Path(config[key]).is_file():
            raise ValueError('Missing configured executable or validator: ' + key)
    if any(not (repo / name).is_file() for name in ('scripts/sync.py', 'scripts/capture.py', 'scripts/evidence.py', 'maintenance-prompt.md')):
        raise ValueError('Repository workflow is missing')
    state_path = state_root / 'weekly-state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'setup_started_at': now.isoformat()}
    due = latest_due(now)
    if args.check:
        print(json.dumps({'host_id': config['host_id'], 'role': config['role'], 'repo_path': str(repo),
                          'weekly_due': weekly_due(now, state), 'latest_due': due.isoformat(),
                          'next_due': (due + timedelta(days=7)).isoformat()}, indent=2))
        return 0
    with (state_root / 'cycle.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        state = json.loads(state_path.read_text()) if state_path.exists() else {'setup_started_at': now.isoformat()}
        if args.command == 'weekly':
            save(state_path, state)
            if not args.run_now and not weekly_due(now, state):
                return 0
        env = dict(os.environ)
        env['PATH'] = os.pathsep.join([str(Path(config['uv_path']).parent), str(Path(config['python_path']).parent), '/usr/bin', '/bin', '/usr/sbin', '/sbin', env.get('PATH', '')])
        command = [config['python_path'], str(repo / 'scripts/sync.py'), '--repo', str(repo),
                   '--host-id', config['host_id'], '--validator', config['validator_path']]
        status_path = state_root / (args.command + '-status.json')
        status = new_status(status_path, args.command, now)
        save(status_path, status)
        began = time.monotonic()
        with (state_root / (args.command + '.log')).open('a') as log:
            def step(name):
                remaining = 5400 - (time.monotonic() - began)
                if remaining <= 0:
                    raise RuntimeError('Maintenance cycle timed out')
                run(command[:2] + [name] + command[2:], log, env, timeout=remaining)
            status['stages'] = {}
            errors = []

            def stage(name, action, message, backlog_message=None, defer=False):
                try:
                    if action() is False:
                        message = backlog_message
                        raise RuntimeError(message)
                except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
                    status['stages'][name] = {'success': False, 'error': message}
                    errors.append(message)
                    if not defer:
                        raise RuntimeError(message) from None
                else:
                    status['stages'][name] = {'success': True}

            def capture():
                run([config['python_path'], str(repo / 'scripts/capture.py'), '--config', str(config_path)],
                    log, env, timeout=1200)
                if args.command == 'weekly':
                    captured = json.loads((state_root / 'capture-status.json').read_text())
                    pending = captured['counts']['pending']
                    if captured.get('success') is not True or type(pending) is not int or pending < 0:
                        raise RuntimeError('Invalid completed capture status')
                    return pending == 0

            def evidence():
                run([config['python_path'], str(repo / 'scripts/evidence.py'), '--config', str(config_path)],
                    log, env, timeout=1200)
                if args.command == 'weekly':
                    verified = json.loads((state_root / 'evidence-status.json').read_text())
                    pending = verified['counts']['pending']
                    if verified.get('success') is not True or type(pending) is not int or pending < 0:
                        raise RuntimeError('Invalid source verification status')
                    return pending == 0

            try:
                stage('capture', capture, 'Feedback capture failed',
                      backlog_message='Feedback capture backlog remains; weekly review deferred', defer=True)
                stage('evidence', evidence, 'Source feedback verification failed',
                      backlog_message='Source verification backlog remains; weekly review deferred', defer=True)
                stage('upload', lambda: step('upload'), 'Feedback upload failed')
                if args.command == 'weekly' and not errors:
                    dirty = subprocess.run(['git', '-C', str(repo), 'status', '--porcelain'], capture_output=True, check=True).stdout
                    branch = subprocess.run(['git', '-C', str(repo), 'branch', '--show-current'], capture_output=True, check=True).stdout.strip()
                    if dirty or branch != b'main':
                        raise RuntimeError('Weekly maintenance requires a clean main checkout')
                    stage('collect', lambda: step('collect'), 'Feedback collection failed')
                    def run_review():
                        prompt = maintenance_prompt(repo, config)
                        review(config['codex_path'], repo, state_root, prompt, log, env,
                               timeout=max(1, 5400 - (time.monotonic() - began)))
                    stage('review', run_review,
                          'Weekly review failed; CLI adaptation failed, execution failed, or a valid fresh report was unavailable')
                    stage('publish', lambda: step('publish'), 'Feedback publication failed')
                stage('deploy', lambda: step('deploy'), 'Release deployment failed')
                if args.command == 'weekly' and not errors and prompt_workflow(repo):
                    result = {}
                    def improve_prompts():
                        remaining = 5400 - (time.monotonic() - began)
                        if remaining <= 0:
                            raise RuntimeError('Maintenance cycle timed out')
                        result.update(review_prompts(config, state_root, timeout=min(900, remaining),
                                                     due=due.isoformat()))
                        if result.get('outcome') not in ('no_change', 'promoted', 'already_reviewed'):
                            raise RuntimeError('Invalid prompt review outcome')
                        if result['outcome'] == 'promoted' and not result.get('commit'):
                            raise RuntimeError('Missing promoted prompt commit')
                    stage('prompt_review', improve_prompts, 'Prompt improvement review failed')
                    # A prior publication may need activation after a deployment failure.
                    # Deployment is idempotent; never trust review state alone to skip it.
                    if result.get('commit'):
                        stage('prompt_deploy', lambda: step('deploy'), 'Prompt release deployment failed')
                if not errors and args.command == 'weekly' and due >= datetime.fromisoformat(state['setup_started_at']):
                    state['completed_due'] = due.isoformat()
                    save(state_path, state)
                status['success'] = not errors
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
                status['success'] = False
                # Stage failures already have a static, sanitized message. Failures
                # outside a stage must not copy subprocess or filesystem details.
                if not errors:
                    errors.append('Weekly maintenance requires a clean main checkout' if
                                  isinstance(error, RuntimeError) and str(error) == 'Weekly maintenance requires a clean main checkout'
                                  else 'Maintenance cycle failed')
            if errors:
                status['error'] = '; '.join(errors)
            status['finished_at'] = datetime.now(timezone.utc).isoformat()
            status['phase'] = 'finished'
            status['last_success_finished_at' if status['success'] else 'last_failure_finished_at'] = status['finished_at']
            save(status_path, status)
        return 0 if status['success'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('sync', 'weekly'))
    parser.add_argument('--config', type=Path, default=Path.home() / '.local/share/codex-skills/config.json')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--run-now', action='store_true')
    args = parser.parse_args()
    if args.run_now and args.command != 'weekly':
        parser.error('--run-now is only for weekly maintenance')
    try:
        return execute(args)
    except (OSError, ValueError, KeyError) as error:
        print('Job stopped: ' + str(error))
        if not args.check:
            try:
                path = args.config.expanduser().resolve().parent / (args.command + '-status.json')
                now = datetime.now(timezone.utc)
                status = new_status(path, args.command, now)
                status.update(phase='finished', success=False, error=str(error),
                              finished_at=now.isoformat(), last_failure_finished_at=now.isoformat())
                save(path, status)
            except OSError:
                pass
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
