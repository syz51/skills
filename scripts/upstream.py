#!/usr/bin/env python3
"""Compare each recorded upstream source with its last reviewed state and write a patch for each change."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess

from read_guidance import SKILLS

REPOSITORY = re.compile(r'https://github\.com/([\w.-]+)/([\w.-]+)\.git')
OBJECT = re.compile(r'[0-9a-f]{40}')
IDENTIFIER = re.compile(r'[\w.-]+')
# Upstream repositories are untrusted: ignore inherited Git settings and user or system configuration, never prompt,
# and allow only HTTPS.
GIT_ENV = {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0'}
PROTOCOLS = ('-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always')
REGULAR = ('100644', '000000')  # Plain files and deletions; anything else is reported as special.
TIMEOUT = 300
MAX_COMMITS = 30
MAX_FILES = 400


class UpstreamError(Exception):
    pass


def git(*args, stdin=None):
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    try:
        result = subprocess.run(['git', *PROTOCOLS, *args], input=stdin, capture_output=True, timeout=TIMEOUT,
                                env={**environment, **GIT_ENV})
    except subprocess.TimeoutExpired:
        raise UpstreamError('Git command timed out') from None
    if result.returncode:
        lines = result.stderr.decode(errors='replace').strip().splitlines()
        raise UpstreamError((lines[-1] if lines else 'Git command failed')[:300])
    return result.stdout


def text(output):
    return output.decode(errors='replace').strip()


def entries(root):
    """Yield (skill, entry, error) for every upstreamReview record."""
    for skill in SKILLS:
        path = root / 'skills' / skill / 'sources.json'
        if not path.is_file():
            continue
        try:
            records = json.loads(path.read_text()).get('upstreamReview', [])
        except ValueError:
            yield skill, {}, 'sources.json is not valid JSON'
            continue
        if not isinstance(records, list):
            yield skill, {}, 'upstreamReview must be a list'
            continue
        seen = set()
        for entry in records:
            yield skill, entry if isinstance(entry, dict) else {}, invalid(entry, seen)


def relative(value):
    return isinstance(value, str) and not value.startswith('/') and '..' not in PurePosixPath(value).parts


def invalid(entry, seen):
    if not isinstance(entry, dict):
        return 'record must be an object'
    if not isinstance(entry.get('id'), str) or not IDENTIFIER.fullmatch(entry['id']) or entry['id'] in seen:
        return 'id must be a unique name'
    seen.add(entry['id'])
    if not isinstance(entry.get('repository'), str) or not REPOSITORY.fullmatch(entry['repository']):
        return 'repository must be an https://github.com/<owner>/<name>.git URL'
    if not relative(entry.get('path')):
        return 'path must be relative to the upstream repository root'
    if not relative(entry.get('local')):
        return 'local must be a path inside the skill'
    for key in ('revision', 'tree'):
        if entry.get(key) is not None and (not isinstance(entry[key], str) or not OBJECT.fullmatch(entry[key])):
            return key + ' must be a full Git object ID or null'
    if (entry.get('revision') is None) != (entry.get('tree') is None):
        return 'revision and tree must both be set or both be null'
    if entry.get('reviewedAt') is not None and not isinstance(entry['reviewedAt'], str):
        return 'reviewedAt must be a timestamp or null'
    return None


def refresh(url, cache):
    """Mirror only the default branch without file contents; diffs fetch the contents they need."""
    owner, name = REPOSITORY.fullmatch(url).groups()
    head = re.search(rb'^ref: refs/heads/(\S+)\tHEAD$', git('ls-remote', '--symref', url, 'HEAD'), re.M)
    if not head:
        raise UpstreamError('Upstream default branch is unavailable')
    branch = head[1].decode()
    directory = cache / (owner + '__' + name + '.git')
    if not (directory / 'HEAD').is_file():
        # Clone beside the mirror and rename it into place, so an interrupted clone never looks complete.
        partial = directory.with_name(directory.name + '.partial')
        shutil.rmtree(partial, ignore_errors=True)
        shutil.rmtree(directory, ignore_errors=True)
        git('clone', '--quiet', '--bare', '--filter=blob:none', '--single-branch', '--branch', branch, url, str(partial))
        os.replace(partial, directory)
    else:
        git('--git-dir=' + str(directory), 'fetch', '--quiet', '--filter=blob:none', url,
            '+refs/heads/' + branch + ':refs/heads/' + branch)
    git('--git-dir=' + str(directory), 'symbolic-ref', 'HEAD', 'refs/heads/' + branch)
    return directory


def items(output):
    """Split NUL-terminated Git output (-z) into decoded items."""
    return [item.decode(errors='replace') for item in output.split(b'\0') if item]


def local_edits(root, skill, entry, directory):
    """List where a local snapshot differs from the reviewed upstream files."""
    folder = root / 'skills' / skill / entry['local']
    upstream = {}
    for item in items(git('--git-dir=' + str(directory), 'ls-tree', '-r', '-z', entry['tree'])):
        meta, path = item.split('\t', 1)
        upstream[path] = meta.split()[2]
    files = sorted(p for p in folder.rglob('*') if p.is_file() and not p.is_symlink() and p.name != '.DS_Store')
    hashes = []
    # Literal argv paths preserve newlines and quotes; small batches bound argv size.
    for start in range(0, len(files), 64):
        hashes.extend(text(git('hash-object', '--no-filters', '--',
                               *map(str, files[start:start + 64]))).split())
    local = {str(p.relative_to(folder)): digest for p, digest in zip(files, hashes)}
    return {'modified': sorted(p for p in local if p in upstream and upstream[p] != local[p])[:MAX_FILES],
            'upstreamOnly': sorted(p for p in upstream if p not in local)[:MAX_FILES],
            'localOnly': sorted(p for p in local if p not in upstream)[:MAX_FILES]}


def check(root, skill, entry, directory, patches):
    run = lambda *args: text(git('--git-dir=' + str(directory), *args))
    path = entry['path']
    result = {'gitDir': str(directory), 'current': {'revision': run('rev-parse', 'HEAD^{commit}')}}
    try:
        result['current']['tree'] = run('rev-parse', 'HEAD:' + path if path else 'HEAD^{tree}')
    except UpstreamError:
        return {**result, 'status': 'removed'}
    current = result['current']['tree']
    if entry.get('tree') == current:
        return {**result, 'status': 'unchanged'}
    if entry.get('tree'):
        try:
            run('cat-file', '-e', entry['tree'] + '^{tree}')
        except UpstreamError:
            entry = {**entry, 'tree': None}
            result['note'] = 'The reviewed tree is unavailable upstream'
    if not entry.get('tree'):
        files = items(git('--git-dir=' + str(directory), 'ls-tree', '-r', '--name-only', '-z', current))
        return {**result, 'status': 'baseline-unknown', 'files': files[:MAX_FILES], 'fileCount': len(files)}
    # --raw -z alternates ":oldmode newmode ..." and path items; --numstat -z gives "added<TAB>deleted<TAB>path".
    raw = items(git('--git-dir=' + str(directory), 'diff', '--raw', '-z', '--no-renames', entry['tree'], current))
    modes = {raw[i + 1]: (raw[i].split()[0].lstrip(':'), raw[i].split()[1]) for i in range(0, len(raw) - 1, 2)}
    files = []
    for item in items(git('--git-dir=' + str(directory), 'diff', '--numstat', '-z', '--no-renames', entry['tree'], current)):
        added, deleted, name = item.split('\t', 2)
        before, after = modes.get(name, (None, None))
        files.append({'path': name, 'added': added, 'deleted': deleted, 'mode': after, 'previousMode': before, 'binary': added == '-'})
    # A change is special if either side is not a plain file, e.g. a deleted symlink or a dropped executable bit.
    special = [f['path'] for f in files if f['binary'] or f['mode'] not in REGULAR or f['previousMode'] not in REGULAR]
    # The patch carries only regular text files, so applying it can never create links, executables, or binaries.
    regular = [':(literal)' + f['path'] for f in files if f['path'] not in special]
    patch = patches / skill / (entry['id'] + '.patch')
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_bytes(git('--git-dir=' + str(directory), 'diff', '--full-index', '--no-renames', entry['tree'], current,
                          '--', *regular) if regular else b'')
    result.update(status='changed', patch=str(patch), files=files[:MAX_FILES], fileCount=len(files), special=special[:MAX_FILES])
    if entry['local'] != '.':
        result['localEdits'] = local_edits(root, skill, entry, directory)
    try:
        result['commits'] = run('log', '--format=%H %cs %s', '-n', str(MAX_COMMITS),
                                entry['revision'] + '..HEAD', '--', path or '.').splitlines()
    except UpstreamError:
        result['commits'] = []
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--cache', type=Path, default=Path.home() / '.cache/codex-skills/upstream')
    parser.add_argument('--skill', action='append', choices=SKILLS)
    args = parser.parse_args()
    root = args.repo.resolve()
    cache = args.cache.expanduser().resolve()
    patches = cache / 'patches'
    shutil.rmtree(patches, ignore_errors=True)
    cache.mkdir(parents=True, exist_ok=True)
    mirrors, sources = {}, []
    for skill, entry, error in entries(root):
        if args.skill and skill not in args.skill:
            continue
        record = {'skill': skill, **{key: entry.get(key) for key in ('id', 'repository', 'path', 'local', 'revision', 'tree', 'reviewedAt')}}
        if error:
            sources.append({**record, 'status': 'error', 'error': error})
            continue
        try:
            if entry['repository'] not in mirrors:
                try:
                    mirrors[entry['repository']] = refresh(entry['repository'], cache)
                except (OSError, UpstreamError) as failure:
                    mirrors[entry['repository']] = failure  # Report an unreachable repository once per source, not retry it.
            mirror = mirrors[entry['repository']]
            if isinstance(mirror, Exception):
                raise mirror
            record.update(check(root, skill, entry, mirror, patches))
        except (OSError, ValueError, UpstreamError) as failure:
            record.update(status='error', error=str(failure)[:300])
        sources.append(record)
    counts = {}
    for source in sources:
        counts[source['status']] = counts.get(source['status'], 0) + 1
    print(json.dumps({'checkedAt': datetime.now(timezone.utc).isoformat(), 'cache': str(cache),
                      'counts': counts, 'sources': sources}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
