#!/usr/bin/env python3
"""Opt-in, trace-scored Codex activation evaluations; importing never calls a model."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import subprocess
import sys
import tempfile

from read_guidance import SKILLS

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / 'tests/fixtures/skill_activation/cases.json'
REFERENCES = {
    'typescript/tooling': ('typescript', 'references/tooling-and-public-types.md'),
    'typescript/map': ('typescript', 'references/skill-map.md'),
    'frontend-ui/vue-tooling': ('frontend-ui', 'references/vue-typescript-tooling.md'),
}
SAFETY = '\nWork directly without delegates. Read-only: do not edit files, install dependencies, browse, connect accounts, or run project commands. Use local files and applicable guidance only.'


def relative(value):
    if (not isinstance(value, str) or not value or value == '.' or '\\' in value
            or value != str(PurePosixPath(value)) or value.startswith('/')
            or any(p in ('.', '..') for p in PurePosixPath(value).parts)):
        raise ValueError('Invalid fixture path')
    return value


def load_cases(path=CASES):
    cases = json.loads(Path(path).read_text())
    if not isinstance(cases, list) or not cases:
        raise ValueError('Expected nonempty case list')
    names, targets = set(), set(SKILLS) | set(REFERENCES)
    for case in cases:
        if not isinstance(case, dict) or set(case) != {'id', 'suite', 'invocation', 'prompt', 'files', 'required', 'forbidden', 'before'}:
            raise ValueError('Invalid case fields')
        name = relative(case['id'])
        if '/' in name or name in names:
            raise ValueError('Duplicate or nested case ID')
        names.add(name)
        if case['suite'] not in ('typescript', 'managed') or case['invocation'] not in ('explicit', 'configured', 'negative'):
            raise ValueError('Invalid case mode')
        if not isinstance(case['prompt'], str) or not case['prompt'].strip() or not isinstance(case['files'], dict):
            raise ValueError('Invalid prompt/files')
        for name, content in case['files'].items():
            relative(name)
            if 'AGENTS.md' in PurePosixPath(name).parts or any(part in ('.agents', '.codex') for part in PurePosixPath(name).parts) or not isinstance(content, str):
                raise ValueError('Fixture may not override guidance/discovery')
        for field in ('required', 'forbidden'):
            if not isinstance(case[field], list) or any(not isinstance(t, str) or t not in targets for t in case[field]) or len(set(case[field])) != len(case[field]):
                raise ValueError('Unknown or duplicate target')
        if set(case['required']) & set(case['forbidden']):
            raise ValueError('Conflicting target expectations')
        if not isinstance(case['before'], list) or any(not isinstance(p, list) or len(p) != 2 or p[0] == p[1] or any(t not in case['required'] for t in p) for p in case['before']):
            raise ValueError('Invalid ordering expectation')
    return cases


def skill_folders(skills_root=None):
    folders = {}
    codex_home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()
    for name in SKILLS:
        choices = [Path(skills_root) / name] if skills_root else [codex_home / 'skills' / name, Path.home() / '.agents/skills' / name]
        folder = next((p for p in choices if (p / 'SKILL.md').is_file()), None)
        if folder is None:
            raise ValueError('Required skill is not installed: ' + name)
        folders[name] = folder.absolute()
    return folders


def snapshot_targets(folders):
    specs = {name: (name, 'SKILL.md') for name in SKILLS} | REFERENCES
    result = {}
    for key, (owner, relative_path) in specs.items():
        path = folders[owner] / relative_path
        result[key] = {'path': str(path), 'content': path.read_bytes().decode('utf-8'), 'aliases': [str(path.resolve())]}
    return result


def tree_snapshot(root):
    result = {}
    for path in root.rglob('*'):
        if path.is_symlink():
            result[str(path.relative_to(root))] = ('link', os.readlink(path))
        elif path.is_file():
            result[str(path.relative_to(root))] = ('file', hashlib.sha256(path.read_bytes()).hexdigest())
    return result


def run_process(command, timeout):
    with subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return process.returncode, stdout, stderr, False
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
                if sig == signal.SIGTERM:
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
            stdout, stderr = process.communicate()
            return process.returncode, stdout, stderr, True


def check_cli(executable):
    resolved = shutil.which(str(executable))
    if resolved is None:
        raise ValueError('Codex executable unavailable')
    code, stdout, stderr, timed_out = run_process([resolved, 'exec', '--help'], 20)
    required = ('--ephemeral', '--json', '--sandbox', '--skip-git-repo-check', '--config', '--cd')
    if code or timed_out or any(flag not in stdout for flag in required):
        raise ValueError('Codex CLI lacks required safe evaluation controls')
    return resolved


def run_case(case, output_dir, folders, executable, timeout=240, model=None, candidate=False):
    from activation_trace import score_trace
    directory = output_dir / case['id']
    directory.mkdir(mode=0o700)
    fixture = directory / 'fixture'
    fixture.mkdir()
    for name, content in case['files'].items():
        path = fixture / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    if candidate:
        discovery = fixture / '.agents/skills'
        discovery.mkdir(parents=True)
        for owner, source in folders.items():
            (discovery / owner).symlink_to(source, target_is_directory=True)
        (fixture / 'AGENTS.md').write_text(
            f'For frontend/UI work, read {folders["frontend-ui"] / "SKILL.md"} first.\n'
            f'For TypeScript implementation, review, debugging or configuration work, read {folders["typescript"] / "SKILL.md"}; for frontend work apply both.\n')
    before_fixture = tree_snapshot(fixture)
    before_skills = {name: tree_snapshot(path.resolve()) for name, path in folders.items()}
    targets = snapshot_targets(folders)
    if candidate:
        specs = {name: (name, 'SKILL.md') for name in SKILLS} | REFERENCES
        for key, (owner, relative_path) in specs.items():
            targets[key]['aliases'].append(str(fixture / '.agents/skills' / owner / relative_path))
    (directory / 'targets.json').write_text(json.dumps(targets, indent=2) + '\n')
    (directory / 'case.json').write_text(json.dumps(case, indent=2) + '\n')
    command = [executable, 'exec', '--ephemeral', '--json', '--sandbox', 'read-only', '--skip-git-repo-check',
               '-C', str(fixture), '-c', 'log_dir=' + json.dumps(str(directory / 'logs'))]
    if model:
        command += ['--model', model]
    command += [case['prompt'] + SAFETY]
    code, stdout, stderr, timed_out = run_process(command, timeout)
    (directory / 'trace.jsonl').write_text(stdout)
    (directory / 'stderr.txt').write_text(stderr)
    result = score_trace(stdout, targets, case['required'], case['forbidden'], case['before'])
    failures = list(result['failures'])
    if code:
        failures.append('Codex exited unsuccessfully')
    if timed_out:
        failures.append('Codex timed out; process group terminated')
    if before_fixture != tree_snapshot(fixture):
        failures.append('Read-only fixture changed')
    if any(before_skills[name] != tree_snapshot(path.resolve()) for name, path in folders.items()):
        failures.append('Skill source changed during evaluation')
    record = {'case': case['id'], 'invocation': case['invocation'],
              'configuration': 'candidate-project-dispatch' if candidate else 'installed-user-configuration',
              'passed': result['passed'] and not failures, 'completed': result['completed'],
              'reads': result['reads'], 'failures': failures, 'exit_code': code, 'timed_out': timed_out}
    (directory / 'result.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('list', 'plan', 'run'))
    parser.add_argument('--cases', type=Path, default=CASES)
    parser.add_argument('--suite', choices=('typescript', 'all'), default='typescript')
    parser.add_argument('--case', action='append', default=[])
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--skills-root', type=Path)
    parser.add_argument('--codex', default='codex')
    parser.add_argument('--model')
    parser.add_argument('--timeout', type=int, default=240)
    parser.add_argument('--jobs', type=int, default=2)
    args = parser.parse_args(argv)
    try:
        cases = load_cases(args.cases)
        known = {case['id'] for case in cases}
        if set(args.case) - known:
            raise ValueError('Unknown selected case')
        selected = [c for c in cases if c['id'] in args.case] if args.case else [c for c in cases if args.suite == 'all' or c['suite'] == args.suite]
        if not selected:
            raise ValueError('No evaluation cases selected')
        if args.command != 'run':
            print(json.dumps([{'id': c['id'], 'invocation': c['invocation'], 'required': c['required'], 'forbidden': c['forbidden'], 'before': c['before']} for c in selected], indent=2))
            return 0
        if args.output_dir is None or not 1 <= args.jobs <= 4 or not 1 <= args.timeout <= 900:
            raise ValueError('Run requires output directory, jobs 1..4 and timeout 1..900')
        output = args.output_dir.expanduser().absolute()
        if output.exists():
            raise ValueError('Output directory must be new')
        executable = check_cli(args.codex)
        folders = skill_folders(args.skills_root)
        output.mkdir(parents=True, mode=0o700)
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            records = list(pool.map(lambda c: run_case(c, output, folders, executable, args.timeout, args.model, args.skills_root is not None), selected))
        summary = {'passed': all(r['passed'] for r in records), 'cases': records}
        (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        print(json.dumps(summary, indent=2))
        return 0 if summary['passed'] else 1
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(2, 'Evaluation failed: ' + str(error) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
