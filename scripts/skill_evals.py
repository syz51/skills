#!/usr/bin/env python3
"""Opt-in, trace-scored Claude Code activation evaluations; importing never calls a model."""
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

import claude_cli
from read_guidance import SKILLS

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / 'tests/fixtures/skill_activation/cases.json'
REFERENCES = {
    'rust/cargo': ('rust', 'references/cargo-and-compatibility.md'),
    'rust/api': ('rust', 'references/api-and-errors.md'),
    'rust/async': ('rust', 'references/async-and-concurrency.md'),
    'rust/unsafe': ('rust', 'references/unsafe-and-ffi.md'),
    'typescript/tooling': ('typescript', 'references/tooling-and-public-types.md'),
    'typescript/map': ('typescript', 'references/skill-map.md'),
    'frontend-ui/vue-tooling': ('frontend-ui', 'references/vue-typescript-tooling.md'),
}
SAFETY = '''
Work directly without delegates. Read-only: do not edit files, install dependencies,
browse, connect accounts, or run project commands. This task is self-contained:
use only this fixture and applicable skill guidance; do not consult user memory
or unrelated saved history. Load a skill with the Skill tool. Read any further
guidance file with its own Read call, without offset or limit, and finish it
before reading later guidance. Do not claim loading from a failed read. Preserve
the configured skill selection and frontend-before-language order; loading is
not runtime proof. For project-file inspection in this bounded evaluation, read
single files by path rather than searching content. This keeps negative
read-scope evidence attributable and does not constrain ordinary work.
'''
# Read-only built-ins: Skill loads guidance and Read returns files. The session
# has no tool that writes, runs commands, browses or delegates.
TOOLS = ('Read', 'Skill')
# dontAsk denies anything that would prompt; no exposed tool can write.
PERMISSION_MODE = 'dontAsk'
CLI_FLAGS = ('--print', '--output-format', '--verbose', '--no-session-persistence', '--strict-mcp-config',
             '--tools', '--permission-mode', '--setting-sources', '--add-dir', '--model', '--effort')
# Project configuration a fixture or an ancestor directory could inject
# (the built-in AGENTS.md plugin also reads AGENTS.md).
PROJECT_CONFIG = ('.claude', 'CLAUDE.md', 'CLAUDE.local.md', 'AGENTS.md')
SKILL_BASE = 'Base directory for this skill: '
DISPATCH = '''For frontend/UI work, use the frontend-ui skill first.
For TypeScript implementation, review, debugging or configuration work, use the typescript skill. When that TypeScript task also involves frontend/UI work, complete frontend-ui first and apply both. Frontend work without TypeScript does not require the TypeScript skill.
For Rust implementation, review, debugging, migration or Cargo/toolchain configuration work, use the rust skill. For Tauri integration work, also use the tauri skill; Rust owns language and Cargo decisions. Unrelated work in a Rust repository does not require rust.
Read each skill reference a skill routes you to with its own complete Read call. Complete frontend-ui before reading TypeScript guidance for frontend work.
'''


def relative(value):
    if (not isinstance(value, str) or not value or value == '.' or '\\' in value
            or value != str(PurePosixPath(value)) or value.startswith('/')
            or any(p in ('.', '..') for p in PurePosixPath(value).parts)):
        raise ValueError('Invalid fixture path')
    return value


def target_spec(value):
    if value in SKILLS:
        return value, 'SKILL.md'
    if value in REFERENCES:
        return REFERENCES[value]
    parts = PurePosixPath(relative(value)).parts
    if len(parts) < 3 or parts[0] not in SKILLS or parts[1] != 'references' or not value.endswith('.md'):
        raise ValueError('Unknown reference target')
    return parts[0], '/'.join(parts[1:])


def load_cases(path=CASES):
    cases = json.loads(Path(path).read_text())
    if not isinstance(cases, list) or not cases:
        raise ValueError('Expected nonempty case list')
    names = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {'id', 'suite', 'invocation', 'prompt', 'files', 'required', 'forbidden', 'before'}:
            raise ValueError('Invalid case fields')
        name = relative(case['id'])
        if '/' in name or name in names:
            raise ValueError('Duplicate or nested case ID')
        names.add(name)
        if case['suite'] not in ('typescript', 'rust', 'managed') or case['invocation'] not in ('explicit', 'configured', 'negative'):
            raise ValueError('Invalid case mode')
        if not isinstance(case['prompt'], str) or not case['prompt'].strip() or not isinstance(case['files'], dict):
            raise ValueError('Invalid prompt/files')
        for name, content in case['files'].items():
            relative(name)
            if any(part in PROJECT_CONFIG for part in PurePosixPath(name).parts) or not isinstance(content, str):
                raise ValueError('Fixture may not override guidance/discovery')
        for field in ('required', 'forbidden'):
            if not isinstance(case[field], list) or any(not isinstance(t, str) for t in case[field]) or len(set(case[field])) != len(case[field]):
                raise ValueError('Unknown or duplicate target')
            for target in case[field]:
                target_spec(target)
        if set(case['required']) & set(case['forbidden']):
            raise ValueError('Conflicting target expectations')
        if not isinstance(case['before'], list) or any(not isinstance(p, list) or len(p) != 2 or p[0] == p[1] or any(t not in case['required'] for t in p) for p in case['before']):
            raise ValueError('Invalid ordering expectation')
    return cases


def user_skills_dir():
    """Claude Code's user skill directory, following a relocated configuration."""
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude').expanduser() / 'skills'


def skill_folders(skills_root=None):
    folders = {}
    for name in SKILLS:
        folder = (Path(skills_root) if skills_root else user_skills_dir()) / name
        if not (folder / 'SKILL.md').is_file():
            raise ValueError('Required skill is not installed: ' + name)
        folders[name] = folder.absolute()
    return folders


def snapshot_targets(folders, requested=()):
    specs = {name: (name, 'SKILL.md') for name in SKILLS} | REFERENCES
    specs.update({name: target_spec(name) for name in requested})
    result = {}
    for key, (owner, relative_path) in specs.items():
        path = folders[owner] / relative_path
        if not path.resolve().is_relative_to(folders[owner].resolve()) or not path.is_file():
            raise ValueError('Missing or escaping guidance target: ' + key)
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


def run_process(command, timeout, *, input=None, cwd=None, env=None):
    with subprocess.Popen(command, text=True, stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env,
                          start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
            return process.returncode, stdout, stderr, False
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except (ProcessLookupError, PermissionError):
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
        raise ValueError('Claude Code executable unavailable')
    code, stdout, stderr, timed_out = run_process([resolved, '--help'], 20, env=claude_cli.child_env())
    if code or timed_out or claude_cli.missing_flags(stdout, CLI_FLAGS) or PERMISSION_MODE not in stdout:
        raise ValueError('Claude Code CLI lacks required safe evaluation controls')
    return resolved


def isolated_location(output):
    """Claude Code loads CLAUDE.md and .claude/ from the cwd's ancestors too.

    The user's own Claude Code home (~/.claude or CLAUDE_CONFIG_DIR) is user configuration, not a project.
    """
    home = user_skills_dir().parent.resolve()
    for parent in (output, *output.parents):
        for name in PROJECT_CONFIG:
            path = parent / name
            if path.exists() and not (name == '.claude' and path.resolve() == home):
                raise ValueError('Output directory is inside a Claude Code project: ' + str(parent))


def injected_skill_dirs(stdout):
    """Base directories of the skills the Skill tool injected during the session."""
    found = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get('message') if isinstance(event, dict) and event.get('isSynthetic') is True else None
        content = message.get('content') if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else ():
            text = block.get('text') if isinstance(block, dict) else None
            if isinstance(text, str) and text.startswith(SKILL_BASE):
                found.append(text.partition('\n\n')[0][len(SKILL_BASE):])
    return found


def evaluation_command(executable, folders, model=None, candidate=False, effort=None):
    command = [executable, '--print', '--output-format', 'stream-json', '--verbose', '--no-session-persistence',
               '--strict-mcp-config', '--tools', ','.join(TOOLS), '--permission-mode', PERMISSION_MODE]
    if candidate:
        # Project skills and CLAUDE.md only: user skills, CLAUDE.md, settings and plugins cannot shadow candidates.
        command += ['--setting-sources', 'project']
    if model:
        command += ['--model', model]
    if effort:
        command += ['--effort', effort]
    # Read outside the fixture would otherwise be denied, including through the
    # candidate discovery links, which resolve to the skill tree.
    for directory in dict.fromkeys(str(path.resolve()) for path in folders.values()):
        command += ['--add-dir', directory]
    return command


def session_failures(stdout, candidate, folders=None):
    """Check the tools and skills the session reported before any turn ran.

    The scorer reports malformed lines; a long trace may exceed the bound that
    claude_cli.events applies to structured calls, so only the first init is decoded.
    """
    init = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') == 'system' and event.get('subtype') == 'init':
            init = event
            break
    if init is None:
        return ['Claude session did not start']
    failures = []
    if not isinstance(init.get('tools'), list) or set(init['tools']) - set(TOOLS):
        failures.append('Claude session exposed tools beyond ' + ', '.join(TOOLS))
    if init.get('mcp_servers'):
        failures.append('Claude session exposed MCP servers')
    if init.get('permissionMode') != PERMISSION_MODE:
        failures.append('Claude session did not use the ' + PERMISSION_MODE + ' permission mode')
    skills = set(init.get('skills') or ())
    if set(SKILLS) - skills:
        failures.append('Maintained skills not exposed: ' + ', '.join(sorted(set(SKILLS) - skills)))
    if candidate:
        directory = user_skills_dir()
        user = {path.name for path in directory.iterdir()} if directory.is_dir() else set()
        if (user - set(SKILLS)) & skills:
            failures.append('User skills leaked into the candidate session')
        # Same-named user skills hide behind the candidates' names; their injected base directory gives them away.
        installed = {(directory / name).resolve() for name in user}
        candidates = {Path(folder).resolve() for folder in (folders or {}).values()}
        leaked = sorted({base for base in injected_skill_dirs(stdout) if Path(base).resolve() in installed - candidates})
        if leaked:
            failures.append('Skills loaded from the user skill folder instead of the candidates: ' + ', '.join(leaked))
    return failures


def run_case(case, output_dir, folders, executable, timeout=240, model=None, candidate=False, effort=None):
    from activation_trace import score_claude_trace
    targets = snapshot_targets(folders, [*case['required'], *case['forbidden']])
    directory = output_dir / case['id']
    directory.mkdir(mode=0o700)
    fixture = directory / 'fixture'
    fixture.mkdir()
    for name, content in case['files'].items():
        path = fixture / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    if candidate:
        # Candidate trees become the session's only non-built-in skills; the
        # project CLAUDE.md stands in for the user's dispatch instructions.
        discovery = fixture / '.claude/skills'
        discovery.mkdir(parents=True)
        for owner, source in folders.items():
            (discovery / owner).symlink_to(source, target_is_directory=True)
        (fixture / 'CLAUDE.md').write_text(DISPATCH)
    before_fixture = tree_snapshot(fixture)
    before_skills = {name: tree_snapshot(path.resolve()) for name, path in folders.items()}
    if candidate:
        for key in targets:
            owner, relative_path = target_spec(key)
            # macOS may canonicalize /var to /private/var in the CLI's cwd.
            # Resolve the fixture parent, retaining the discovery-link path.
            for base in (fixture, fixture.resolve()):
                alias = str(base / '.claude/skills' / owner / relative_path)
                if alias not in targets[key]['aliases']:
                    targets[key]['aliases'].append(alias)
    (directory / 'targets.json').write_text(json.dumps(targets, indent=2) + '\n')
    (directory / 'case.json').write_text(json.dumps(case, indent=2) + '\n')
    command = evaluation_command(executable, folders, model, candidate, effort)
    # The prompt goes through stdin: --add-dir and --tools take variadic values.
    code, stdout, stderr, timed_out = run_process(command, timeout, input=case['prompt'] + SAFETY, cwd=fixture,
                                                  env=claude_cli.child_env())
    (directory / 'trace.jsonl').write_text(stdout)
    (directory / 'stderr.txt').write_text(stderr)
    result = score_claude_trace(stdout, targets, case['required'], case['forbidden'], case['before'])
    failures = list(result['failures']) + session_failures(stdout, candidate, folders)
    if code:
        failures.append('Claude Code exited unsuccessfully')
    if timed_out:
        failures.append('Claude Code timed out; process group terminated')
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
    parser.add_argument('--suite', choices=('typescript', 'rust', 'all'), default='typescript')
    parser.add_argument('--case', action='append', default=[])
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--skills-root', type=Path)
    parser.add_argument('--claude', default=str(claude_cli.DEFAULT_CLI))
    parser.add_argument('--model')
    parser.add_argument('--effort', choices=('low', 'medium', 'high', 'xhigh', 'max'))
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
        isolated_location(output)
        executable = check_cli(args.claude)
        folders = skill_folders(args.skills_root)
        for case in selected:
            snapshot_targets(folders, [*case['required'], *case['forbidden']])
        output.mkdir(parents=True, mode=0o700)
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            records = list(pool.map(lambda c: run_case(c, output, folders, executable, args.timeout, args.model, args.skills_root is not None, args.effort), selected))
        summary = {'passed': all(r['passed'] for r in records), 'cases': records}
        (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        print(json.dumps(summary, indent=2))
        return 0 if summary['passed'] else 1
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(2, 'Evaluation failed: ' + str(error) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
