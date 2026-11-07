#!/usr/bin/env python3
"""Propose source-checked rejected frontend defaults to the user's list as a draft pull request.

Candidates are frontend-ui notes titled with the rejected-default marker whose effective source checks are all
settled: supported with complete input, or carrying a supported correction that keeps the marker. Patterns
already in the list, proposed before (recorded locally, and read from earlier proposal pull requests, open,
merged or closed), or whose notes such a pull request cites are skipped, and so is any pattern that is not
plain words. New patterns go to the open proposal when there is one, otherwise to a new draft pull request
from a fresh branch. The job never merges, never writes the base branch, and makes no model calls. Pull
request bodies carry only counts and note hashes; entries are the generic pattern text.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess

import capture
import evidence

BRANCH_PREFIX = 'skill-feedback/frontend-avoid-'
NOTES_MARKER = '<!-- codex-skills rejected-default notes -->'
SECTION = '## Patterns'
TITLE = 'Add rejected frontend defaults from skill feedback'
COMMIT = 'Add rejected frontend defaults from skill feedback'
GH_TIMEOUT = 60
MAX_PATTERN = 200
# Entries reach a public repository, so a pattern must be plain words: no dots, slashes, colons or at signs,
# which rules out addresses, paths, domains, file names and mentions.
PLAIN = re.compile(r"[\w ,'&()+%\-，、（）·]+")
# Host identifiers and the private repository; propose() adds the local login, host and repository owner.
PRIVATE = ('client-host', 'maintainer-host', 'codex-skills', 'skill-feedback')


def normalize(pattern):
    return re.sub(r'\s+', ' ', pattern).strip().rstrip('.').strip().casefold()


def safe(pattern, private=PRIVATE):
    """Plain words with nothing the redactor or the private-identifier check would catch."""
    folded = pattern.casefold()
    return (3 <= len(pattern) <= MAX_PATTERN and PLAIN.fullmatch(pattern) is not None and '_' not in pattern
            and capture.redact(pattern) == pattern and not any(word.casefold() in folded for word in private if word))


def candidates(feedback):
    """Accepted rejected-default notes as {note sha256: pattern}, from the repository's reviewed feedback."""
    titles = {}
    for path in sorted((feedback / capture.REJECTED_DEFAULT_SKILL / 'notes').rglob('*.md')):
        capture.safe_path(path)
        data = path.read_bytes()
        titles[hashlib.sha256(data).hexdigest()] = data.decode('utf-8').split('\n', 1)[0].removeprefix('# ').strip()
    assessments = {}
    for value in evidence.assessment_inventory(feedback)['latest']:
        if value['skill'] == capture.REJECTED_DEFAULT_SKILL:
            assessments.setdefault(value['noteSha256'], []).append(value)
    accepted = {}
    for digest, title in titles.items():
        latest = assessments.get(digest, [])
        # Every host's current assessment must be settled; an unchecked or unresolved note waits.
        if not latest or any(evidence.unresolved(value) for value in latest):
            continue
        corrections = [value['correction']['note']['title'] for value in latest if value.get('correction')]
        pattern = capture.rejected_default(corrections[-1] if corrections else title)
        if pattern:
            accepted[digest] = pattern
    return accepted


def section_bounds(text):
    lines = text.split('\n')
    starts = [index for index, line in enumerate(lines) if line.strip() == SECTION]
    if len(starts) != 1:
        raise ValueError('The list must have exactly one patterns section')
    end = next((index for index in range(starts[0] + 1, len(lines)) if lines[index].startswith('## ')), len(lines))
    return lines, starts[0], end


def entries(text):
    lines, start, end = section_bounds(text)
    return [line[2:].strip() for line in lines[start + 1:end] if line.startswith('- ')]


def append_entries(text, patterns):
    """Adds entries after the section's last entry, or after its last text when it has none yet."""
    lines, start, end = section_bounds(text)
    items = [index for index in range(start + 1, end) if lines[index].startswith('- ')]
    last = items[-1] if items else max([start, *(index for index in range(start + 1, end) if lines[index].strip())])
    result = lines[:last + 1] + ['- ' + pattern for pattern in patterns] + lines[last + 1:]
    joined = '\n'.join(result)
    return joined if joined.endswith('\n') else joined + '\n'


def added_entries(patch):
    """Pattern lines a pull request added to the list file."""
    return [line[3:].strip() for line in (patch or '').split('\n') if line.startswith('+- ')]


def cited_notes(body):
    after = (body or '').split(NOTES_MARKER, 1)
    # Bodies edited on GitHub come back with CRLF line endings.
    return set(re.findall(r'^- ([a-f0-9]{64})\r?$', after[1], re.M)) if len(after) == 2 else set()


def pr_body(notes, entry_count):
    """Only counts and note hashes: no note, transcript or repository text."""
    return '\n'.join([
        'Adds frontend patterns that the user rejected as defaults in real sessions, as recorded by skill feedback capture.',
        '',
        'Each entry comes from a frontend-ui feedback note whose source check against the original session is settled. '
        'Merge to accept the entries, edit them first, or close to decline; patterns and notes proposed here are not proposed again.',
        '',
        f'Entries in this proposal: {entry_count}. Source notes: {len(notes)}.',
        '',
        NOTES_MARKER,
        *('- ' + digest for digest in sorted(notes)),
        '',
    ])


def plan(list_text, accepted, proposals, proposed=(), private=PRIVATE):
    """New entries as [(pattern, [note sha256])], skipping known patterns, cited notes and unsafe text."""
    known = {normalize(entry) for entry in entries(list_text)} | set(proposed)
    cited = set()
    for proposal in proposals:
        known.update(normalize(entry) for entry in proposal['entries'])
        cited |= proposal['notes']
    grouped, unsafe = {}, 0
    for digest, pattern in sorted(accepted.items()):
        if digest in cited or normalize(pattern) in known:
            continue
        if not safe(pattern, private):
            unsafe += 1
            continue
        grouped.setdefault(normalize(pattern), (pattern, []))[1].append(digest)
    return list(grouped.values()), unsafe


class GitHub:
    def __init__(self, gh, repository):
        self.gh, self.repository = gh, repository

    def __call__(self, method, endpoint, payload=None, paginate=False):
        command = [str(self.gh), 'api', '--method', method, f'repos/{self.repository}/{endpoint}']
        if paginate:
            command += ['--paginate', '--slurp']
        if payload is not None:
            command += ['--input', '-']
        result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                                capture_output=True, text=True, timeout=GH_TIMEOUT)
        if result.returncode:
            # gh errors can echo request details; keep only the endpoint family.
            raise RuntimeError('GitHub request failed: ' + method + ' ' + endpoint.split('/', 1)[0])
        if paginate:
            return [item for page in json.loads(result.stdout) for item in page]
        return json.loads(result.stdout) if result.stdout.strip() else None


def read_file(api, path, ref):
    value = api('GET', f'contents/{path}?ref={ref}')
    return base64.b64decode(value['content']).decode('utf-8'), value['sha']


def proposals(api, path):
    result = []
    for pull in api('GET', 'pulls?state=all&per_page=100', paginate=True):
        head = pull['head']
        if (not head['ref'].startswith(BRANCH_PREFIX)
                or ((head.get('repo') or {}).get('full_name') or '').casefold() != api.repository.casefold()):
            continue
        files = api('GET', f"pulls/{pull['number']}/files?per_page=100", paginate=True)
        result.append({'number': pull['number'], 'state': pull['state'], 'branch': head['ref'], 'url': pull['html_url'],
                       'created_at': pull['created_at'], 'notes': cited_notes(pull['body']), 'body': pull['body'] or '',
                       'entries': [entry for item in files if item['filename'] == path
                                   for entry in added_entries(item.get('patch'))]})
    return result


def encoded(text):
    return base64.b64encode(text.encode()).decode()


def remove_orphans(api, known):
    """Deletes proposal branches that no pull request uses, as when a run was killed before opening one."""
    used = {item['branch'] for item in known}
    for ref in api('GET', 'git/matching-refs/heads/' + BRANCH_PREFIX, paginate=True):
        branch = ref['ref'].removeprefix('refs/heads/')
        if branch.startswith(BRANCH_PREFIX) and branch not in used:
            api('DELETE', 'git/refs/heads/' + branch)


def create(api, path, base, head, list_text, list_sha, new, notes, now):
    branch = BRANCH_PREFIX + (now or datetime.now(timezone.utc)).strftime('%Y%m%d-%H%M%S')
    try:
        api('POST', 'git/refs', {'ref': 'refs/heads/' + branch, 'sha': head})
        api('PUT', f'contents/{path}', {'message': COMMIT, 'branch': branch, 'sha': list_sha,
                                        'content': encoded(append_entries(list_text, [p for p, _ in new]))})
        return api('POST', 'pulls', {'title': TITLE, 'head': branch, 'base': base, 'draft': True,
                                     'body': pr_body(notes, len(new))})['html_url']
    except (RuntimeError, ValueError, KeyError, subprocess.SubprocessError):
        # A request can time out after GitHub carried it out. Deleting the branch of an existing pull request
        # would close it, so keep the branch then; the next run finds the pull request, or removes the orphan.
        owner = api.repository.split('/')[0]
        if not api('GET', f'pulls?state=all&head={owner}:{branch}', paginate=True):
            api('DELETE', 'git/refs/heads/' + branch)
        raise


def propose(config, state_root, now=None):
    settings = config['frontend_avoid']
    repository, path, base = settings['repository'], settings['path'], settings.get('base', 'main')
    gh = Path(settings.get('gh_path', '/opt/homebrew/bin/gh'))
    if (not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9._-]+', repository) or not re.fullmatch(r'[\w.-]+(?:/[\w.-]+)*', path)
            or '..' in path.split('/') or not re.fullmatch(r'[\w.-]+(?:/[\w.-]+)*', base) or base.startswith(BRANCH_PREFIX)
            or not gh.is_absolute() or not gh.is_file()):
        raise ValueError('Invalid rejected-default proposal configuration')
    private = (*PRIVATE, repository.split('/')[0], Path.home().name, socket.gethostname().split('.')[0])
    feedback = Path(config['repo_path']).expanduser() / 'feedback'
    accepted = candidates(feedback)
    state_path = Path(state_root) / 'frontend-avoid-state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    handled, proposed = set(state.get('handled', [])), set(state.get('proposed', []))
    summary = {'candidates': len(accepted), 'entries': 0, 'unsafe': 0, 'outcome': 'unchanged', 'pull_request': None}
    if set(accepted) <= handled:
        return summary
    api = GitHub(gh, repository)
    head = api('GET', f'git/ref/heads/{base}')['object']['sha']
    list_text, list_sha = read_file(api, path, head)
    known = proposals(api, path)
    remove_orphans(api, known)
    new, summary['unsafe'] = plan(list_text, accepted, known, proposed, private)
    open_ = sorted((item for item in known if item['state'] == 'open'), key=lambda item: item['created_at'])
    notes = {digest for _, digests in new for digest in digests}
    if open_:
        target = open_[-1]
        listed = {normalize(entry) for entry in target['entries']}
        # Also repairs a body that lost citations, as when an earlier run updated the file but not the body.
        notes |= target['notes'] | {digest for digest, pattern in accepted.items() if normalize(pattern) in listed}
        if new:
            text, sha = read_file(api, path, target['branch'])
            api('PUT', f'contents/{path}', {'message': COMMIT, 'branch': target['branch'], 'sha': sha,
                                            'content': encoded(append_entries(text, [p for p, _ in new]))})
        if new or not notes <= target['notes']:
            api('PATCH', f"pulls/{target['number']}", {'body': pr_body(notes, len(target['entries']) + len(new))})
            summary.update(outcome='updated' if new else 'repaired', pull_request=target['url'])
    elif new:
        summary.update(outcome='created', pull_request=create(api, path, base, head, list_text, list_sha, new, notes, now))
    summary['entries'] = len(new)
    proposed |= {normalize(pattern) for pattern, _ in new}
    capture.atomic(state_path, json.dumps({'handled': sorted(handled | set(accepted)), 'proposed': sorted(proposed)},
                                          indent=2) + '\n')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    parser.add_argument('--config', type=Path, default=Path.home() / '.local/share/codex-skills/config.json')
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve()
    status = {'started_at': datetime.now(timezone.utc).isoformat(), 'success': False}
    try:
        status['counts'] = propose(json.loads(config_path.read_text()), config_path.parent)
        status['success'] = True
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        status['error'] = type(error).__name__ + ': ' + (str(error) if isinstance(error, (RuntimeError, ValueError)) else 'failed')
    status['finished_at'] = datetime.now(timezone.utc).isoformat()
    capture.atomic(config_path.parent / 'frontend-avoid-status.json', json.dumps(status, indent=2) + '\n')
    print(json.dumps(status))
    return 0 if status['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
