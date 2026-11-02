#!/usr/bin/env python3
"""Daily maintenance and host sync; retain legacy weekly command/state names."""
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
import uuid
from zoneinfo import ZoneInfo

from capture import cli_text, help_command, help_flag, help_options, redact

TOKYO = ZoneInfo('Asia/Tokyo')


def latest_due(now):
    local = now.astimezone(TOKYO)
    due = local.replace(hour=9, minute=0, second=0, microsecond=0)
    return due if due <= local else due - timedelta(days=1)


def weekly_due(now, state):
    """Daily eligibility using the existing persisted-state/command contract."""
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


def review_learning_cycle(config, state_root, due, log, env, timeout):
    """Review in a recoverable private checkout; only validated commits reach main."""
    import investigations
    import sync

    began = time.monotonic()
    repo = Path(config['repo_path']).resolve()
    state_root = Path(state_root).resolve()
    attempts = state_root / 'investigations/attempts'
    attempts.mkdir(parents=True, exist_ok=True)
    active = state_root / 'investigations/active-attempt.json'

    def git(root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], timeout=30).decode().strip()

    def clean(root):
        return not git(root, 'status', '--porcelain', '--untracked-files=all')

    if not clean(repo) or git(repo, 'branch', '--show-current') != 'main':
        raise RuntimeError('Weekly maintenance requires a clean main checkout')
    base = git(repo, 'rev-parse', 'HEAD')
    attempt = json.loads(active.read_text()) if active.exists() else None
    recovery = None
    if attempt is not None:
        saved_work = Path(attempt['work'])
        work = saved_work.resolve()
        folder = work.parent
        if (not folder.is_relative_to(attempts) or work.name != 'work' or saved_work.is_symlink()
                or not (work / '.git').is_file()):
            raise ValueError('Unsafe saved investigation attempt')
        stale_cycle = False
        if attempt.get('cycle') is not None:
            cycle_path = Path(attempt['cycle'])
            if not cycle_path.resolve().is_relative_to(state_root / 'investigations/cycles') or cycle_path.is_symlink():
                raise ValueError('Unsafe saved investigation cycle')
            try:
                frozen = investigations.cycle_data(cycle_path)
                stale_cycle = investigations.bindings(work) != frozen['bindings']
            except ValueError:
                stale_cycle = True
        if attempt['base_commit'] != base or attempt['due'] != due.isoformat() or stale_cycle:
            attempt['status'] = 'superseded'
            save(folder / 'attempt.json', attempt)
            recovery = str(folder)
            attempt = None
    if attempt is None:
        folder = attempts / uuid.uuid4().hex
        folder.mkdir()
        work = folder / 'work'
        git(repo, 'worktree', 'add', '--detach', str(work), base)
        scoped = dict(config, repo_path=str(work))
        config_path = state_root / ('investigation-' + folder.name + '-config.json')
        save(config_path, scoped)
        attempt = {'base_commit': base, 'due': due.isoformat(), 'work': str(work),
                   'config': str(config_path), 'cycle': None, 'status': 'preparing',
                   'recovery': recovery}
        save(folder / 'attempt.json', attempt)
        save(active, attempt)
        try:
            cycle = investigations.prepare(scoped, state_root, due.isoformat(), trusted_base=base)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
            attempt['status'] = 'failed'
            attempt['failure'] = {'class': type(exc).__name__, 'detail': redact(str(exc))[:2000]}
            save(folder / 'attempt.json', attempt)
            save(active, attempt)
            raise
        attempt['cycle'] = str(cycle)
    else:
        config_path = Path(attempt['config'])
        if config_path.parent.resolve() != state_root or config_path.is_symlink():
            raise ValueError('Unsafe saved investigation config')
        scoped = json.loads(config_path.read_text())
        if scoped != dict(config, repo_path=str(work)):
            raise ValueError('Investigation configuration changed')
        if attempt['cycle'] is None:
            attempt['cycle'] = str(investigations.prepare(scoped, state_root, due.isoformat(), trusted_base=base))
        cycle = Path(attempt['cycle'])
        if not cycle.resolve().is_relative_to(state_root / 'investigations/cycles') or cycle.is_symlink():
            raise ValueError('Unsafe saved investigation cycle')
        frozen = investigations.cycle_data(cycle)
    attempt['status'] = 'running'
    save(folder / 'attempt.json', attempt)
    save(active, attempt)
    execution_env = dict(env, SKILL_INVESTIGATION_CYCLE=str(cycle),
                         SKILL_INVESTIGATION_CONFIG=str(config_path))
    prompt = maintenance_prompt(work, scoped) + (
        '\nCurrent host-local investigation context (paths are not publication content):\n'
        'SKILL_INVESTIGATION_CYCLE=' + str(cycle) + '\n'
        'SKILL_INVESTIGATION_CONFIG=' + str(config_path) + '\n'
        'Use uv run --no-project python scripts/investigations.py report --config '
        '"$SKILL_INVESTIGATION_CONFIG" --cycle "$SKILL_INVESTIGATION_CYCLE". '
        'Use run and record with the same config/cycle for actual experiments and dispositions. '
        'Complete required evidence and due investigations before finishing; keep future follow-ups visible. '
        'A saved attempt may contain your earlier failed work: inspect it, preserve evidence, '
        'and finish through scoped forward commits. Do not alter the canonical checkout.\n')
    if attempt.get('recovery'):
        prompt += ('Prior superseded attempt is read-only recovery context at ' + attempt['recovery']
                   + '. Reassess its evidence; do not blindly cherry-pick or reset from it.\n')
    try:
        review(config['codex_path'], work, folder, prompt, log, execution_env,
               timeout=max(1, timeout - (time.monotonic() - began)))
        summary = investigations.validate_cycle(scoped, state_root, cycle)
        _, heads = investigations.inventory(work)
        outstanding = sorted((v for v in heads.values() if v['state'] != 'resolved'),
                             key=lambda v: (v['follow_up']['next_due'], v['skill'], v['issue_id']))
        summary['outstanding'] = [{'skill': v['skill'], 'issue_id': v['issue_id'],
                                  'state': v['state'], **v['follow_up']} for v in outstanding]
        report = folder / 'latest-weekly-report.md'
        with report.open('a') as stream:
            stream.write('\n\nValidated investigation result: ' + str(summary['open'])
                         + ' outstanding issues (including provisional and blocked issues).\n')
            for issue in summary['outstanding']:
                stream.write('- ' + issue['skill'] + '/' + issue['issue_id'] + ' [' + issue['state']
                             + '], due ' + issue['next_due'] + ': ' + issue['next_action'] + '\n')
        if not clean(work):
            raise RuntimeError('Investigation review must commit its owned changes')
        candidate = git(work, 'rev-parse', 'HEAD')
        git(work, 'merge-base', '--is-ancestor', base, candidate)
        sync.publication_scope(work, base, candidate, bootstrap=False)
        sync.validate(work, config['validator_path'])
        if time.monotonic() - began >= timeout:
            raise TimeoutError('Investigation validation exceeded the job deadline')
        if not clean(repo) or git(repo, 'rev-parse', 'HEAD') != base:
            raise RuntimeError('Canonical checkout changed during investigation')
        git(repo, 'merge', '--ff-only', candidate)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError, sync.SyncError) as exc:
        attempt['status'] = 'failed'
        attempt['failure'] = {'class': type(exc).__name__, 'detail': redact(str(exc))[:2000]}
        save(folder / 'attempt.json', attempt)
        save(active, attempt)
        if isinstance(exc, sync.SyncError):
            raise RuntimeError(str(exc)) from exc
        raise
    attempt['status'] = 'validated'
    attempt['candidate_commit'] = candidate
    save(folder / 'attempt.json', attempt)
    # Cleanup is separate from acceptance: a cleanup failure must not relabel
    # an already accepted forward commit as a failed investigation.
    try:
        # Publish the report only after its machine-checked investigation succeeds.
        os.replace(report, state_root / 'latest-weekly-report.md')
        active.unlink()
        git(repo, 'worktree', 'remove', str(work))
        config_path.unlink()
    except (OSError, subprocess.SubprocessError) as exc:
        attempt['cleanup_pending'] = str(exc)
        save(folder / 'attempt.json', attempt)
        summary = dict(summary, cleanup_pending=str(folder))
    return summary


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
        raise ValueError('Daily maintenance is restricted to the remote maintainer')
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
                          'next_due': (due + timedelta(days=1)).isoformat()}, indent=2))
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

            def source_stage(name):
                began_at = datetime.now(timezone.utc)
                run([config['python_path'], str(repo / ('scripts/' + name + '.py')), '--config', str(config_path)],
                    log, env, timeout=1200)
                completed = json.loads((state_root / (name + '-status.json')).read_text())
                pending = completed['counts']['pending']
                finished = datetime.fromisoformat(completed['finished_at'])
                if (completed.get('success') is not True or type(pending) is not int or pending < 0 or
                        finished.tzinfo is None or not began_at <= finished <= datetime.now(timezone.utc)):
                    raise RuntimeError('Invalid or stale source stage result')
                return pending == 0 if args.command == 'weekly' else True

            def capture():
                return source_stage('capture')

            def evidence():
                return source_stage('evidence')

            try:
                stage('capture', capture, 'Feedback capture failed',
                      backlog_message='Feedback capture backlog remains; daily review deferred', defer=True)
                stage('evidence', evidence, 'Source feedback verification failed',
                      backlog_message='Source verification backlog remains; daily review deferred', defer=True)
                stage('upload', lambda: step('upload'), 'Feedback upload failed')
                if args.command == 'weekly' and not errors:
                    dirty = subprocess.run(['git', '-C', str(repo), 'status', '--porcelain'], capture_output=True, check=True).stdout
                    branch = subprocess.run(['git', '-C', str(repo), 'branch', '--show-current'], capture_output=True, check=True).stdout.strip()
                    if dirty or branch != b'main':
                        raise RuntimeError('Weekly maintenance requires a clean main checkout')
                    stage('collect', lambda: step('collect'), 'Feedback collection failed')
                    learning = (repo / 'scripts/investigations.py').is_file()
                    def run_review():
                        remaining = max(1, 5400 - (time.monotonic() - began))
                        if learning:
                            status['investigations'] = {'complete': False}
                            try:
                                status['investigations'] = review_learning_cycle(config, state_root, due, log, env, remaining)
                            finally:
                                active = state_root / 'investigations/active-attempt.json'
                                if not status['investigations'].get('complete') and active.exists():
                                    status['investigations']['recovery'] = json.loads(active.read_text())
                        else:
                            prompt = maintenance_prompt(repo, config)
                            review(config['codex_path'], repo, state_root, prompt, log, env, timeout=remaining)
                    stage('review', run_review,
                          'Maintenance review failed; CLI adaptation failed, execution failed, or a valid fresh report was unavailable')
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
        parser.error('--run-now is only for daily maintenance')
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
