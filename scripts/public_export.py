#!/usr/bin/env python3
"""Export an explicit committed file inventory without sharing private Git history."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile

MANIFEST = '.public-export.json'
URL = re.compile(r'(?:https://github\.com/|git@github\.com:)[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+\.git')
SOURCE_ROOTS = {'skills', 'scripts', 'tests', 'prompts', 'prompt-evals', 'public'}
SOURCE_FILES = {'maintenance-prompt.md'}


class ExportError(RuntimeError):
    pass


def git(root, *args):
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(root), *args],
                            capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
                            env=dict(os.environ, GIT_TERMINAL_PROMPT='0'))
    if result.returncode:
        raise ExportError('Git operation failed: ' + args[0])
    return result.stdout


def relative(value):
    if (not isinstance(value, str) or not value or value == '.' or '\\' in value or
            value != str(PurePosixPath(value)) or value.startswith('/') or
            any(part in ('.', '..') or part.casefold() == '.git' for part in PurePosixPath(value).parts)):
        raise ExportError('Invalid inventory path')
    return value


def configuration(value):
    required = {'schema_version', 'source_repo', 'target_url', 'state_dir', 'files', 'author_name', 'author_email'}
    if not isinstance(value, dict) or set(value) - required - {'replacements', 'cycle_lock', 'fetch_source', 'json_drop_keys'} or not required <= value.keys():
        raise ExportError('Invalid export configuration')
    if value['schema_version'] != 1 or not isinstance(value['target_url'], str) or not URL.fullmatch(value['target_url']):
        raise ExportError('Invalid schema or GitHub destination')
    for key in ('author_name', 'author_email'):
        if not isinstance(value[key], str) or not value[key].strip() or any(char in value[key] for char in '\r\n<>'):
            raise ExportError('Invalid public author identity')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+', value['author_email']):
        raise ExportError('Invalid public author email')
    for key in ('source_repo', 'state_dir', 'cycle_lock'):
        if key in value and (not isinstance(value[key], str) or not value[key]):
            raise ExportError('Invalid configuration path')
    if not isinstance(value['files'], list) or not value['files']:
        raise ExportError('Empty or invalid inventory')
    sources, targets = set(), set()
    for entry in value['files']:
        if not isinstance(entry, dict) or set(entry) != {'source', 'target'}:
            raise ExportError('Invalid inventory entry')
        source, target = relative(entry['source']), relative(entry['target'])
        if source.split('/')[0] not in SOURCE_ROOTS and source not in SOURCE_FILES:
            raise ExportError('Source is outside publication scope')
        if source in sources or target.casefold() in targets or target.casefold() == MANIFEST:
            raise ExportError('Duplicate or reserved inventory entry')
        sources.add(source)
        targets.add(target.casefold())
    if any(a.startswith(b + '/') for a in targets for b in targets if a != b):
        raise ExportError('Overlapping inventory paths')
    if not isinstance(value.get('fetch_source', False), bool):
        raise ExportError('Invalid source fetch policy')
    drops = value.get('json_drop_keys', [])
    if not isinstance(drops, list) or any(not isinstance(key, str) or not key for key in drops):
        raise ExportError('Invalid metadata cleanup keys')
    replacements = value.get('replacements', {})
    if not isinstance(replacements, dict) or any(not isinstance(k, str) or not k or not isinstance(v, str) for k, v in replacements.items()):
        raise ExportError('Invalid replacements')
    return value


def scan(path, content):
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError:
        raise ExportError('Non-text publication file: ' + path) from None
    private_markers = ('/Us' + 'ers/', 'roy' + '-mac', 'sunmacbook' + '-air', 'syz51/' + 'codex-skills')
    rules = {'private identifier': any(marker in text for marker in private_markers),
             'credential': bool(re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-(?:proj-)?[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16})\b', text))}
    for rule, matched in rules.items():
        if matched:
            raise ExportError('Publication rejected: ' + path + ' (' + rule + ')')


def clean_source(repo):
    if git(repo, 'branch', '--show-current').strip() != b'main' or git(repo, 'status', '--porcelain').strip():
        raise ExportError('Source requires a clean committed main checkout')
    return git(repo, 'rev-parse', 'HEAD').decode().strip()


def snapshot(config, revision):
    repo = Path(config['source_repo']).expanduser()
    result = {}
    for entry in config['files']:
        source, target = entry['source'], entry['target']
        info = git(repo, 'ls-tree', '-z', revision, '--', source).split(b'\0')
        if len(info) != 2 or not info[0].startswith((b'100644 blob ', b'100755 blob ')):
            raise ExportError('Source is not a committed regular file: ' + source)
        # Every parent is a committed tree; a symlink parent cannot resolve here.
        content = git(repo, 'show', revision + ':' + source)
        if source.startswith('skills/') and source.endswith('/sources.json') and config.get('json_drop_keys'):
            def clean_metadata(value):
                if isinstance(value, dict):
                    return {key: clean_metadata(item) for key, item in value.items() if key not in config['json_drop_keys']}
                if isinstance(value, list):
                    return [clean_metadata(item) for item in value]
                return value
            try:
                content = encoded(clean_metadata(json.loads(content)))
            except (ValueError, UnicodeError):
                raise ExportError('Invalid source metadata: ' + source) from None
        for old, new in config.get('replacements', {}).items():
            content = content.replace(old.encode(), new.encode())
        scan(target, content)
        result[target] = content
    return result


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def manifest(files):
    return encoded({'schema_version': 1, 'files': {path: hashlib.sha256(data).hexdigest() for path, data in sorted(files.items())}})


def tree(repo, ref):
    result = {}
    for entry in git(repo, 'ls-tree', '-rz', ref).split(b'\0'):
        if entry:
            meta, name = entry.split(b'\t', 1)
            mode, kind, sha = meta.decode().split()
            result[os.fsdecode(name)] = (mode, kind, sha)
    return result


def read_manifest(repo, ref):
    entries = tree(repo, ref)
    if entries.get(MANIFEST, ('',))[0] != '100644':
        raise ExportError('Missing trusted publication manifest')
    try:
        value = json.loads(git(repo, 'show', ref + ':' + MANIFEST))
        if set(value) != {'schema_version', 'files'} or value['schema_version'] != 1 or not isinstance(value['files'], dict):
            raise ValueError()
        for path, digest in value['files'].items():
            relative(path)
            if path == MANIFEST or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                raise ValueError()
            if entries.get(path, ('',))[0] != '100644' or hashlib.sha256(git(repo, 'show', ref + ':' + path)).hexdigest() != digest:
                raise ValueError()
        return value['files']
    except (ValueError, TypeError, KeyError):
        raise ExportError('Invalid trusted publication manifest') from None


def prepare(repo, files, previous):
    refs = git(repo, 'for-each-ref', '--format=%(refname)', 'refs/remotes/origin/main').strip()
    if refs:
        git(repo, 'checkout', '-B', 'main', 'origin/main')
        current = tree(repo, 'HEAD')
        # Recovery is safe only if every desired managed file AND manifest match.
        exact = current.get(MANIFEST, ('',))[0] == '100644' and git(repo, 'show', 'HEAD:' + MANIFEST) == manifest(files)
        if exact:
            exact = all(current.get(path, ('',))[0] == '100644' and git(repo, 'show', 'HEAD:' + path) == data for path, data in files.items())
        if previous:
            git(repo, 'merge-base', '--is-ancestor', previous, 'HEAD')
            old = read_manifest(repo, previous)
            trusted = tree(repo, previous)
            if exact:
                # Previously managed files removed by this export must be absent.
                if any(path in current for path in old.keys() - files.keys()):
                    raise ExportError('Unexpected retained managed path')
                return False
            if current.get(MANIFEST) != trusted.get(MANIFEST):
                raise ExportError('Public manifest changed; reconcile publication state first')
            for path in old:
                if current.get(path) == trusted.get(path):
                    continue
                # An imported public contribution must agree with the committed
                # private snapshot before the exporter can resume ownership.
                imported = (path not in current and path not in files) or (
                    path in files and current.get(path, ('',))[0] == '100644'
                    and git(repo, 'show', 'HEAD:' + path) == files[path])
                if not imported:
                    raise ExportError('Public managed files changed; import and reconcile first')
        else:
            if exact:
                return False
            raise ExportError('Existing public repository requires trusted publication state')
    else:
        if previous:
            raise ExportError('Public main disappeared')
        git(repo, 'checkout', '--orphan', 'main')
        current, old = {}, {}
    for path in files:
        if path in current and path not in old:
            if current[path][0] != '100644' or git(repo, 'show', 'HEAD:' + path) != files[path]:
                raise ExportError('Publication collides with public-owned file: ' + path)
        if any(path.startswith(name + '/') or name.startswith(path + '/') for name in current if name not in old and name != MANIFEST):
            raise ExportError('Publication collides with public-owned path')
    for path in old.keys() - files.keys():
        (repo / path).unlink(missing_ok=True)
    for path, data in {**files, MANIFEST: manifest(files)}.items():
        destination = repo / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        destination.chmod(0o644)
    git(repo, 'add', '--all')
    return bool(git(repo, 'diff', '--cached', '--name-only').strip())


def save(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(encoded(value))
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def locks(paths):
    with contextlib.ExitStack() as stack:
        for path in paths:
            handle = stack.enter_context(path.open('a'))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ExportError('Publication deferred: another job holds the lock') from None
        yield


def execute(config, publish=False):
    config = configuration(config)
    source = Path(config['source_repo']).expanduser().resolve()
    state_dir = Path(config['state_dir']).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_paths = [state_dir / 'export.lock']
    if config.get('cycle_lock'):
        lock_paths.append(Path(config['cycle_lock']).expanduser())
    with locks(lock_paths):
        local_revision = clean_source(source)
        revision = local_revision
        if config.get('fetch_source'):
            git(source, 'fetch', 'origin', 'refs/heads/main:refs/remotes/origin/main')
            revision = git(source, 'rev-parse', 'refs/remotes/origin/main').decode().strip()
            git(source, 'merge-base', '--is-ancestor', local_revision, revision)
        files = snapshot(config, revision)
        state_path = state_dir / 'export-state.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state and (set(state) != {'target_url', 'public_commit', 'source_commit'} or state['target_url'] != config['target_url'] or not re.fullmatch('[0-9a-f]{40,64}', state['public_commit'])):
            raise ExportError('Publication state does not match destination')
        with tempfile.TemporaryDirectory(prefix='skill-public-export-') as temporary:
            repo = Path(temporary) / 'checkout'
            repo.mkdir()
            git(repo, 'init', '--initial-branch=main')
            git(repo, 'remote', 'add', 'origin', config['target_url'])
            git(repo, 'fetch', 'origin')
            changed = prepare(repo, files, state.get('public_commit'))
            if not publish:
                return {'changed': changed, 'file_count': len(files), 'paths': sorted(files), 'source_commit': revision}
            if changed:
                environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
                validation = Path(temporary) / 'validation'
                validation.mkdir()
                for path, content in files.items():
                    target = validation / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                runner = ('import os,sys,unittest; sys.path.insert(0,os.getcwd()); '
                          'suite=unittest.defaultTestLoader.discover(\"tests\"); '
                          'count=suite.countTestCases(); '
                          'print(\"Discovered tests:\",count); '
                          'sys.exit(0 if count and unittest.TextTestRunner().run(suite).wasSuccessful() else 1)')
                check = subprocess.run([sys.executable, '-I', '-c', runner], cwd=validation,
                                       env=environment, capture_output=True, timeout=600)
                diagnostics = check.stdout + check.stderr
                (state_dir / 'candidate-tests.log').write_bytes(diagnostics[-65536:])
                if check.returncode:
                    raise ExportError('Public candidate tests failed; publication stopped')
                if clean_source(source) != local_revision or (config.get('fetch_source') and git(source, 'rev-parse', 'refs/remotes/origin/main').decode().strip() != revision):
                    raise ExportError('Private source changed during publication')
                git(repo, 'config', 'user.name', config['author_name'])
                git(repo, 'config', 'user.email', config['author_email'])
                git(repo, '-c', 'commit.gpgSign=false', 'commit', '-m', 'Update public skills and evolution tools')
                git(repo, 'push', 'origin', 'HEAD:refs/heads/main')
            commit = git(repo, 'rev-parse', 'HEAD').decode().strip()
            save(state_path, {'target_url': config['target_url'], 'public_commit': commit, 'source_commit': revision})
            return {'changed': changed, 'file_count': len(files), 'public_commit': commit, 'source_commit': revision}


def rust_cli(arguments):
    """Preserve the old command entrypoint while Rust owns live publication."""
    binary = Path(os.environ.get('CODEX_SKILLS_BIN', '~/.local/bin/codex-skills')).expanduser()
    if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
        print('Install codex-skills or set CODEX_SKILLS_BIN to its absolute executable path.', file=sys.stderr)
        return 1
    try:
        os.execv(str(binary), [str(binary), *arguments])
    except OSError:
        print('Could not start the configured codex-skills executable.', file=sys.stderr)
        return 1


def main():
    return rust_cli(sys.argv[1:])


if __name__ == '__main__':
    raise SystemExit(main())
