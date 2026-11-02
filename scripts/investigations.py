#!/usr/bin/env python3
"""Persistent learning evidence. Copies isolate originals, not arbitrary code execution."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import tempfile
import time
from types import SimpleNamespace

import capture
import evidence

HEX = re.compile(r'[a-f0-9]{64}')
ID = re.compile(r'[a-z0-9][a-z0-9-]{0,79}')
ACTIONS = {'keep', 'revise', 'revert', 'investigate'}
STATES = {'open', 'provisional', 'resolved', 'blocked'}
RUNNER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else encoded(value)).hexdigest()


def bounded_bytes(path):
    with path.open('rb') as stream:
        return stream.read(1_000_000)


def read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
        raise ValueError('Unsafe or oversized evidence file')
    return json.loads(path.read_bytes())


def save(path, value):
    capture.safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    capture.atomic(path, encoded(value).decode(), exclusive=True)


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], timeout=30).decode().strip()


def bindings(root):
    return [{'skill': item['skill'], 'note_sha256': item['noteSha256'],
             'receipt_sha256': item['receiptSha256'], 'source_machine': item['sourceMachine']}
            for item in evidence.assessment_inventory(root / 'feedback')['latest']]


def instant(value, allow_date=False):
    if not isinstance(value, str): raise ValueError('Expected timestamp text')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        if allow_date and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value): return parsed.replace(tzinfo=timezone.utc)
        raise ValueError('Investigation timestamps require a timezone')
    return parsed


def validate_event(value):
    expected = {'schema_version', 'issue_id', 'skill', 'supersedes', 'evidence', 'action', 'state',
                'summary', 'baseline_commit', 'change', 'experiments', 'follow_up', 'cycle_id', 'recorded_at'}
    if set(value) != expected or value['schema_version'] != 1 or value['skill'] not in capture.SKILLS:
        raise ValueError('Invalid investigation event')
    if not isinstance(value['issue_id'], str) or not ID.fullmatch(value['issue_id']):
        raise ValueError('Invalid investigation identity')
    if value['action'] not in ACTIONS or value['state'] not in STATES:
        raise ValueError('Invalid investigation disposition')
    summary = value['summary']
    if not isinstance(summary, str) or not 10 <= len(summary) <= 1000 or '\n' in summary or capture.redact(summary) != summary or re.search(r'https?://|/(?:Users|home|tmp|var)/', summary):
        raise ValueError('Unsafe or uninformative abstract summary')
    if not isinstance(value['cycle_id'], str) or not HEX.fullmatch(value['cycle_id']): raise ValueError('Investigation must belong to a cycle')
    for field in ('supersedes', 'cycle_id'):
        if value[field] is not None and (not isinstance(value[field], str) or not HEX.fullmatch(value[field])):
            raise ValueError('Invalid investigation hash')
    if not re.fullmatch(r'[a-f0-9]{40,64}', value['baseline_commit']):
        raise ValueError('Invalid baseline revision')
    capture.timestamp(value['recorded_at'])
    if not isinstance(value['evidence'], list) or not isinstance(value['experiments'], list):
        raise ValueError('Invalid investigation evidence')
    for binding in value['evidence']:
        if set(binding) != {'note_sha256', 'receipt_sha256', 'source_machine'} or binding['source_machine'] not in ('client-host', 'maintainer-host') or any(not HEX.fullmatch(binding[k]) for k in ('note_sha256', 'receipt_sha256')):
            raise ValueError('Invalid source binding')
    if any(not isinstance(h, str) or not HEX.fullmatch(h) for h in value['experiments']):
        raise ValueError('Invalid execution hash')
    if value['state'] != 'resolved':
        follow = value['follow_up']
        if not isinstance(follow, dict) or set(follow) != {'next_action', 'next_due'} or not isinstance(follow['next_action'], str) or not 12 <= len(follow['next_action'].strip()) <= 1000 or '\n' in follow['next_action'] or re.search(r'https?://|/(?:Users|home|tmp|var)/', follow['next_action']) or capture.redact(follow['next_action']) != follow['next_action']:
            raise ValueError('Open investigation requires a concrete follow-up')
        instant(follow['next_due'])
    elif value['follow_up'] is not None:
        raise ValueError('Resolved investigations cannot retain an unresolved follow-up')
    return value


def heads_for(events):
    consumed = set()
    for h, value in events.items():
        prior = value['supersedes']
        if prior is not None:
            if prior not in events or prior in consumed or any(events[prior][k] != value[k] for k in ('issue_id', 'skill')) or capture.timestamp(events[prior]['recorded_at']) > capture.timestamp(value['recorded_at']):
                raise ValueError('Broken or branching investigation chain')
            consumed.add(prior)
    heads = {h: v for h, v in events.items() if h not in consumed}
    identities = [(v['skill'], v['issue_id']) for v in heads.values()]
    if len(identities) != len(set(identities)):
        raise ValueError('Unlinked investigation histories')
    return heads


def inventory(root):
    events = {}
    for skill in capture.SKILLS:
        directory = root / 'feedback' / skill / 'investigations'
        capture.safe_path(directory)
        for path in sorted(directory.glob('*.json')):
            value = validate_event(read(path)); h = digest(value)
            if path.name != value['issue_id'] + '--' + h + '.json' or skill != value['skill']:
                raise ValueError('Investigation identity mismatch')
            events[h] = value
    return events, heads_for(events)


def blob_hash(root, commit, name):
    info = git(root, 'ls-tree', commit, '--', name)
    if not info: return None
    if not info.startswith(('100644 blob ', '100755 blob ')):
        raise ValueError('Changed guidance must be a regular file')
    return digest(subprocess.check_output(['git', '-C', str(root), 'show', commit + ':' + name], timeout=30))


def validate_change(root, value, current=False):
    if value['action'] not in ('revise', 'revert'):
        if value['change'] is not None: raise ValueError('Non-revision cannot claim a change')
        return
    change = value['change']
    if not isinstance(change, dict) or set(change) != {'candidate_commit', 'paths', 'reverts_commit'} or not isinstance(change['paths'], dict) or not change['paths']:
        raise ValueError('Revision must bind its changes')
    commit = change['candidate_commit']
    if not isinstance(commit, str) or not re.fullmatch(r'[a-f0-9]{40,64}', commit): raise ValueError('Invalid candidate revision')
    git(root, 'merge-base', '--is-ancestor', value['baseline_commit'], commit)
    git(root, 'merge-base', '--is-ancestor', commit, 'HEAD')
    if commit == value['baseline_commit']: raise ValueError('Revision requires a forward commit')
    prefix = 'skills/' + value['skill'] + '/'
    changed = {n for n in git(root, 'diff', '--name-only', value['baseline_commit'], commit, '--', prefix).splitlines() if n}
    if changed != set(change['paths']): raise ValueError('Revision path inventory mismatch')
    for name, hashes in change['paths'].items():
        relative(name)
        if not name.startswith(prefix) or not isinstance(hashes, dict) or set(hashes) != {'before', 'after'}:
            raise ValueError('Revision exceeds owned skill scope')
        if hashes['before'] != blob_hash(root, value['baseline_commit'], name) or hashes['after'] != blob_hash(root, commit, name):
            raise ValueError('Revision hashes do not match Git evidence')
        if current:
            path = root / name; capture.safe_path(path)
            if hashes['after'] != (digest(path.read_bytes()) if path.is_file() else None): raise ValueError('Current guidance changed after candidate commit')
    if value['action'] == 'revert':
        bad = change['reverts_commit']
        if not isinstance(bad, str) or not re.fullmatch(r'[a-f0-9]{40,64}', bad): raise ValueError('Revert must identify a prior candidate')
        git(root, 'merge-base', '--is-ancestor', bad, value['baseline_commit'])
        parent = git(root, 'rev-parse', bad + '^')
        if any(hashes['after'] != blob_hash(root, parent, name) for name, hashes in change['paths'].items()): raise ValueError('Revert must restore the prior candidate parent bytes')
    elif change['reverts_commit'] is not None:
        raise ValueError('Ordinary revision cannot claim a revert')


def event_records(root):
    root = Path(root)
    if root.name == 'feedback': root = root.parent
    events, _ = inventory(root)
    return [{'sha256': h, 'path': 'feedback/' + v['skill'] + '/investigations/' + v['issue_id'] + '--' + h + '.json', 'event': v} for h, v in events.items()]


def validate_events(root):
    root = Path(root)
    if root.name == 'feedback': root = root.parent
    events, _ = inventory(root)
    known = {}
    for skill in capture.SKILLS:
        for path in (root / 'feedback' / skill / 'verifications').rglob('*.json'):
            data = path.read_bytes()
            value = evidence.parse_receipt(path.name, data, skill=skill)
            known[digest(data)] = value
    evidence.assessment_inventory(root / 'feedback')
    for event in events.values():
        validate_change(root, event)
        for binding in event['evidence']:
            receipt = known.get(binding['receipt_sha256'])
            if receipt is None or (receipt['skill'], receipt['noteSha256'], receipt['sourceMachine']) != (event['skill'], binding['note_sha256'], binding['source_machine']):
                raise ValueError('Investigation references missing or mismatched source evidence')
    return len(events)


def prepare(config, state_root, due, trusted_base=None):
    root = Path(config['repo_path']).resolve(); state = Path(state_root)
    validate_events(root)
    current = bindings(root)
    base = trusted_base or git(root, 'rev-parse', 'HEAD')
    git(root, 'merge-base', '--is-ancestor', base, 'HEAD')
    # Failed drafts must never become trusted history when evidence is refreshed.
    events = {}; reviews = {skill: [] for skill in capture.SKILLS}
    for name in git(root, 'ls-tree', '-r', '--name-only', base, '--', 'feedback').splitlines():
        parts = PurePosixPath(name).parts
        if len(parts) < 4 or parts[1] not in capture.SKILLS: continue
        if parts[2] == 'investigations' and len(parts) == 4 and name.endswith('.json'):
            value = validate_event(json.loads(git(root, 'show', base + ':' + name)))
            h = digest(value)
            if parts[3] != value['issue_id'] + '--' + h + '.json' or value['skill'] != parts[1]:
                raise ValueError('Invalid committed investigation identity')
            events[h] = value
        elif parts[2] == 'reviews' and name.endswith('.md'):
            reviews[parts[1]].append(git(root, 'show', base + ':' + name))
    heads = heads_for(events)
    required = []
    for binding in current:
        texts = '\n'.join(reviews[binding['skill']])
        covered = any(v['skill'] == binding['skill'] and {k: binding[k] for k in ('note_sha256', 'receipt_sha256', 'source_machine')} in v['evidence'] for v in events.values())
        if not covered and not all(re.search(r'(?<![a-f0-9])' + binding[k] + r'(?![a-f0-9])', texts) for k in ('note_sha256', 'receipt_sha256')):
            required.append(binding)
    if digest(Path(__file__).read_bytes()) != RUNNER_SHA256: raise ValueError('Investigation runner changed; start a fresh cycle')
    due_at = instant(str(due), allow_date=True)
    outstanding = {h: v for h, v in heads.items() if v['state'] != 'resolved'}
    value = {'schema_version': 1, 'runner_sha256': RUNNER_SHA256, 'due': str(due), 'base_commit': base,
             'created_at': datetime.now(timezone.utc).isoformat(), 'bindings': current, 'required': required,
             'pending': {h: v for h, v in outstanding.items() if instant(v['follow_up']['next_due']) <= due_at},
             'outstanding': outstanding, 'prior_events': sorted(events)}
    value['cycle_id'] = digest(value)
    path = state / 'investigations/cycles' / (value['cycle_id'] + '.json'); save(path, value)
    return path


def cycle_data(path):
    value = read(Path(path)); original = {k: v for k, v in value.items() if k != 'cycle_id'}
    if value.get('runner_sha256') != RUNNER_SHA256 or digest(Path(__file__).read_bytes()) != RUNNER_SHA256:
        raise ValueError('Investigation runner changed; start a fresh cycle')
    if value['cycle_id'] != digest(original):
        raise ValueError('Cycle snapshot was modified')
    return value


def local_receipt(state, h, cycle):
    private = Path(state) / 'investigations'
    value = read(private / 'runs' / (h + '.json'))
    expected = {'schema_version', 'cycle_id', 'issue_id', 'skill', 'plan_sha256', 'fixture_sha256',
                'fixture_files', 'runner_sha256', 'guidance_tree_sha256', 'guidance_sha256', 'guidance_changed',
                'originals_verified_unchanged', 'project_snapshot_sha256', 'variant', 'evidence_scope',
                'exit_code', 'timed_out', 'output_limit_exceeded', 'process_group_incomplete', 'outcome',
                'passed', 'stdout_sha256', 'stderr_sha256', 'recorded_at'}
    if not isinstance(value, dict) or set(value) != expected or value['schema_version'] != 1:
        raise ValueError('Incomplete execution receipt')
    if digest(value) != h or value['cycle_id'] != cycle['cycle_id'] or value['runner_sha256'] != cycle['runner_sha256']:
        raise ValueError('Execution receipt was modified or belongs to another cycle')
    if value['skill'] not in capture.SKILLS or not ID.fullmatch(value['issue_id']) or value['variant'] not in ('baseline', 'candidate', 'contrast'):
        raise ValueError('Invalid execution identity')
    for field in ('plan_sha256', 'fixture_sha256', 'guidance_tree_sha256', 'guidance_sha256', 'stdout_sha256', 'stderr_sha256'):
        if not isinstance(value[field], str) or not HEX.fullmatch(value[field]): raise ValueError('Invalid execution fingerprint')
    if type(value['exit_code']) is not int or any(type(value[k]) is not bool for k in ('timed_out', 'output_limit_exceeded', 'process_group_incomplete', 'guidance_changed', 'passed')):
        raise ValueError('Invalid execution outcome types')
    capture.timestamp(value['recorded_at'])
    plan = read(private / 'plans' / cycle['cycle_id'] / (value['skill'] + '--' + value['issue_id'] + '.json'))
    if digest(plan) != value['plan_sha256'] or plan['case']['skill'] != value['skill'] or plan['case']['evidence_scope'] != value['evidence_scope']:
        raise ValueError('Execution does not match its frozen plan')
    files = value['fixture_files']
    if not isinstance(files, dict) or set(files) != set(plan['case']['files']) or digest(files) != value['fixture_sha256']:
        raise ValueError('Execution fixture inventory mismatch')
    for name, fingerprint in files.items():
        relative(name)
        path = private / 'artifacts' / h / name; capture.safe_path(path)
        if not isinstance(fingerprint, str) or not HEX.fullmatch(fingerprint) or digest(path.read_bytes()) != fingerprint:
            raise ValueError('Execution fixture evidence changed')
    if any(files.get(name) != fingerprint for name, fingerprint in plan['files'].items()):
        raise ValueError('Execution changed a frozen oracle')
    for name in ('stdout', 'stderr'):
        path = private / 'logs' / h / name; capture.safe_path(path)
        if not path.is_file() or path.stat().st_size > 1_000_000 or digest(path.read_bytes()) != value[name + '_sha256']:
            raise ValueError('Execution logs are missing or changed')
    snapshot_name = plan['case'].get('project_snapshot')
    if snapshot_name is not None:
        snapshot = read(private / 'artifacts' / h / snapshot_name)
        if value['project_snapshot_sha256'] != digest(snapshot) or type(value['originals_verified_unchanged']) is not bool:
            raise ValueError('Execution project snapshot mismatch')
    elif value['project_snapshot_sha256'] is not None or value['originals_verified_unchanged'] is not None:
        raise ValueError('Unexpected project snapshot claim')
    expected_pass = (value['exit_code'] in plan['case']['success_codes'][value['variant']]
                     and not any(value[k] for k in ('timed_out', 'output_limit_exceeded', 'process_group_incomplete', 'guidance_changed'))
                     and value['originals_verified_unchanged'] is not False)
    if value['passed'] != expected_pass or value['outcome'] != execution_outcome(value):
        raise ValueError('Execution outcome disagrees with frozen success criteria')
    return value


def guidance_hashes(root, skill):
    tree = root / 'skills' / skill
    hashes = {}
    for path in sorted(tree.rglob('*')):
        capture.safe_path(path)
        if path.is_file(): hashes[str(path.relative_to(tree))] = digest(path.read_bytes())
    return digest(hashes), digest((tree / 'SKILL.md').read_bytes())


def execution_outcome(value):
    if value['exit_code'] == 127: return 'missing_dependency'
    if value['timed_out']: return 'timeout'
    if value['output_limit_exceeded']: return 'output_limit'
    if value['process_group_incomplete']: return 'incomplete_process_group'
    if value['guidance_changed']: return 'guidance_changed'
    if value['originals_verified_unchanged'] is False: return 'original_changed'
    return 'passed' if value['passed'] else 'assertion_failed'


def closure_proof(root, value, runs):
    if value['state'] != 'resolved': return
    if not runs: raise ValueError('Closing an issue requires behavior evidence')
    current_tree, current_entry = guidance_hashes(root, value['skill'])
    good = [r for r in runs if r['passed'] and r['evidence_scope'] in ('synthetic_behavior', 'project_snapshot')
            and r['guidance_tree_sha256'] == current_tree and r['guidance_sha256'] == current_entry]
    if not any(a['variant'] == 'candidate' and b['variant'] == 'contrast'
               and a['plan_sha256'] == b['plan_sha256'] and a['fixture_sha256'] == b['fixture_sha256']
               and any(r['variant'] == 'baseline' and r['plan_sha256'] == a['plan_sha256'] for r in runs)
               for a in good for b in good):
        raise ValueError('Closing an issue requires baseline, candidate and contrast behavior evidence for current guidance')


def record(config, state_root, cycle_path, decision):
    cycle = cycle_data(cycle_path); root = Path(config['repo_path']).resolve()
    git(root, 'merge-base', '--is-ancestor', cycle['base_commit'], 'HEAD')
    _, heads = inventory(root)
    value = dict(decision)
    value.update(schema_version=1, cycle_id=cycle['cycle_id'], recorded_at=datetime.now(timezone.utc).isoformat())
    validate_event(value)
    previous = {h: v for h, v in heads.items() if v['skill'] == value['skill'] and v['issue_id'] == value['issue_id']}
    if value['supersedes'] != next(iter(previous), None):
        raise ValueError('Disposition must extend the latest issue head')
    for item in value['evidence']:
        if {'skill': value['skill'], **item} not in cycle['bindings']:
            raise ValueError('Disposition source binding is not current')
    runs = [local_receipt(state_root, h, cycle) for h in value['experiments']]
    if any(r['skill'] != value['skill'] or r['issue_id'] != value['issue_id'] for r in runs):
        raise ValueError('Execution belongs to another issue')
    closure_proof(root, value, runs)
    if value['baseline_commit'] != cycle['base_commit']:
        raise ValueError('Disposition baseline is not the frozen cycle base')
    validate_change(root, value, current=True)
    save(root / 'feedback' / value['skill'] / 'investigations' / (value['issue_id'] + '--' + digest(value) + '.json'), value)
    return value


def validate_cycle(config, state_root, cycle_path):
    root = Path(config['repo_path']).resolve(); cycle = cycle_data(cycle_path)
    if bindings(root) != cycle['bindings']:
        raise ValueError('Evidence changed during investigation cycle')
    validate_events(root)
    git(root, 'merge-base', '--is-ancestor', cycle['base_commit'], 'HEAD')
    events, heads = inventory(root)
    new = {h: v for h, v in events.items() if h not in cycle['prior_events']}
    if any(v['cycle_id'] != cycle['cycle_id'] for v in new.values()):
        raise ValueError('New event belongs to another cycle')
    for binding in cycle['required']:
        if not any(v['skill'] == binding['skill'] and {k: binding[k] for k in ('note_sha256', 'receipt_sha256', 'source_machine')} in v['evidence'] for v in new.values()):
            raise ValueError('New evidence has no investigation disposition')
    for h in cycle['pending']:
        descendants = {h}
        for _ in range(len(new) + 1):
            descendants |= {k for k, v in new.items() if v['supersedes'] in descendants}
        if descendants == {h}:
            raise ValueError('Pending investigation was not revisited')
    for v in new.values():
        if v['baseline_commit'] != cycle['base_commit']: raise ValueError('Disposition baseline is not the frozen cycle base')
        validate_change(root, v)
        if any({'skill': v['skill'], **b} not in cycle['bindings'] for b in v['evidence']):
            raise ValueError('Disposition source binding is not current')
        runs = [local_receipt(state_root, h, cycle) for h in v['experiments']]
        if any(r['skill'] != v['skill'] or r['issue_id'] != v['issue_id'] for r in runs):
            raise ValueError('Execution belongs to another issue')
        # Historical events retain their original evidence. Only the newest issue
        # head must match today's files, allowing a later forward revision/revert.
        if v in heads.values():
            closure_proof(root, v, runs)

    return {'cycle_id': cycle['cycle_id'], 'dispositions': len(new),
            'open': sum(v['state'] != 'resolved' for v in heads.values()), 'complete': True}


def relative(name):
    p = PurePosixPath(name)
    if not isinstance(name, str) or not name or p.is_absolute() or '..' in p.parts or str(p) != name or '\\' in name:
        raise ValueError('Unsafe fixture path')
    return p


def run(config, state_root, cycle_path, case_path, variant, issue_id=None):
    cycle = cycle_data(cycle_path); root = Path(config['repo_path']).resolve(); case_path = Path(case_path).resolve()
    private_case = case_path.is_relative_to((Path(state_root) / 'investigations/cases').resolve())
    if not private_case and (not case_path.is_relative_to(root / 'skills') or 'evaluations' not in case_path.parts):
        raise ValueError('Execution requires a reviewed owned or private evaluation case')
    case = read(case_path); skill = case['skill']; issue_id = issue_id or case['id']
    if skill not in capture.SKILLS or (not private_case and not case_path.is_relative_to(root / 'skills' / skill / 'evaluations')) or not ID.fullmatch(issue_id) or variant not in ('baseline', 'candidate', 'contrast'):
        raise ValueError('Invalid evaluation scope')
    command = case['commands'][variant]
    if not isinstance(command, list) or not command or command[0] not in ('uv', 'bun') or any(not isinstance(s, str) or '\0' in s for s in command):
        raise ValueError('Only reviewed uv/bun argument vectors are supported')
    if case['evidence_scope'] not in ('synthetic_behavior', 'artifact_check', 'decision_proxy', 'guidance_loading', 'project_snapshot') or (case['evidence_scope'] == 'project_snapshot' and not private_case):
        raise ValueError('Portable fixture cannot claim original-project execution')
    prefix = ['uv', 'run', '--no-project', 'python'] if command[0] == 'uv' else ['bun', 'run']
    if command[:len(prefix)] != prefix or len(command) <= len(prefix) or command[len(prefix)] not in case['files']:
        raise ValueError('Execution must invoke a copied first-party fixture script')
    if any(arg.startswith('/') or '..' in PurePosixPath(arg).parts for arg in command[len(prefix)+1:]):
        raise ValueError('Fixture arguments cannot escape the copied workspace')
    timeout = case['timeout_seconds']
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise ValueError('Invalid evaluation deadline')
    if not isinstance(case['files'], list) or not 1 <= len(case['files']) <= 128 or len(set(case['files'])) != len(case['files']):
        raise ValueError('Invalid fixture inventory')
    if set(case['success_codes']) != {'baseline', 'candidate', 'contrast'} or any(
        not isinstance(codes, list) or not codes or any(type(code) is not int or not 0 <= code < 127 for code in codes)
        for codes in case['success_codes'].values()):
        raise ValueError('Invalid frozen success criteria')
    files = {}
    for name in case['files']:
        relative(name); source = case_path.parent / name; capture.safe_path(source)
        if not source.is_file() or source.stat().st_size > 2_000_000: raise ValueError('Invalid fixture file')
        files[name] = source.read_bytes()
    if sum(map(len, files.values())) > 20_000_000: raise ValueError('Fixture exceeds total size limit')
    mutable = case.get('mutable_files', [])
    if not isinstance(mutable, list) or len(set(mutable)) != len(mutable) or any(n not in files for n in mutable):
        raise ValueError('Mutable implementation files must be a subset of copied fixture files')
    if set(case['commands']) != {'baseline', 'candidate', 'contrast'}:
        raise ValueError('Case requires three frozen variant commands')
    for argv in case['commands'].values():
        if not isinstance(argv, list) or not argv: raise ValueError('Invalid variant command')
        invocation = ['uv', 'run', '--no-project', 'python'] if argv[0] == 'uv' else ['bun', 'run']
        if (argv[:len(invocation)] != invocation or len(argv) <= len(invocation)
                or argv[len(invocation)] not in files or argv[len(invocation)] in mutable):
            raise ValueError('Every invoked checker must remain an immutable oracle')
    snapshot = None
    if case.get('project_snapshot') is not None:
        name = case['project_snapshot']; relative(name)
        if not private_case or name not in files or name in mutable: raise ValueError('Project snapshot metadata must be private and immutable')
        snapshot = json.loads(files[name])
        validate_snapshot(config, snapshot)
        if variant == 'baseline' and any(n not in files or digest(files[n]) != h for n, h in snapshot['files'].items()):
            raise ValueError('Baseline does not match the selected project snapshot')
    elif case['evidence_scope'] == 'project_snapshot':
        raise ValueError('Project execution scope requires a verified snapshot')
    plan = {'case': case, 'files': {n: digest(b) for n, b in files.items() if n not in mutable}}
    fixture_hash = digest({n: digest(b) for n, b in files.items()})
    plan_hash = digest(plan); directory = Path(state_root) / 'investigations/plans' / cycle['cycle_id']
    frozen = directory / (skill + '--' + issue_id + '.json')
    if frozen.exists():
        if read(frozen) != plan: raise ValueError('Frozen oracle or fixture changed')
    else:
        if variant != 'baseline': raise ValueError('Freeze and run baseline before candidate')
        save(frozen, plan)
    private = Path(state_root) / 'investigations'; private.mkdir(parents=True, exist_ok=True)
    guidance_tree, guidance_entry = guidance_hashes(root, skill)
    with tempfile.TemporaryDirectory(prefix='run-', dir=private) as folder:
        work = Path(folder)
        for name, content in files.items():
            p = work / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(content)
        allowed = ('PATH', 'HOME', 'CODEX_HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'UV_CACHE_DIR', 'UV_PYTHON', 'BUN_INSTALL')
        env = {k: v for k, v in os.environ.items() if k in allowed}; env['TMPDIR'] = str(work)
        stdout = work / '.stdout'; stderr = work / '.stderr'
        with stdout.open('wb') as out, stderr.open('wb') as err:
            incomplete = False
            if shutil.which(command[0], path=env.get('PATH')) is None:
                err.write(b'Required fixture runner is unavailable.\n')
                process = SimpleNamespace(returncode=127)
                timed_out = False
            else:
                try:
                    child = subprocess.Popen(command, cwd=work, env=env, stdout=out, stderr=err, start_new_session=True)
                except OSError:
                    err.write(b'Required fixture runner could not start.\n')
                    process = SimpleNamespace(returncode=127)
                    timed_out = False
                else:
                    with child as process:
                        timed_out = False
                        deadline = time.monotonic() + timeout
                        try:
                            while process.poll() is None:
                                if time.monotonic() >= deadline or stdout.stat().st_size > 1_000_000 or stderr.stat().st_size > 1_000_000:
                                    timed_out = time.monotonic() >= deadline
                                    break
                                time.sleep(.01)
                        finally:
                            # A successful leader is insufficient when its workers
                            # remain alive. Always clean the entire owned group.
                            try:
                                os.killpg(process.pid, 0)
                                incomplete = process.poll() is not None
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            process.wait()
        oversized = stdout.stat().st_size > 1_000_000 or stderr.stat().st_size > 1_000_000
        source_unchanged = True
        if snapshot is not None:
            try: validate_snapshot(config, snapshot)
            except ValueError: source_unchanged = False
        guidance_changed = guidance_hashes(root, skill) != (guidance_tree, guidance_entry)
        value = {'schema_version': 1, 'cycle_id': cycle['cycle_id'], 'issue_id': issue_id, 'skill': skill,
                 'plan_sha256': plan_hash, 'fixture_sha256': fixture_hash, 'runner_sha256': RUNNER_SHA256,
                 'fixture_files': {n: digest(b) for n, b in files.items()},
                 'guidance_tree_sha256': guidance_tree, 'guidance_changed': guidance_changed,
                 'originals_verified_unchanged': source_unchanged if snapshot is not None else None,
                 'project_snapshot_sha256': digest(snapshot) if snapshot is not None else None,
                 'guidance_sha256': guidance_entry,
                 'variant': variant, 'evidence_scope': case['evidence_scope'], 'exit_code': process.returncode,
                 'timed_out': timed_out, 'output_limit_exceeded': oversized,
                 'process_group_incomplete': incomplete,
                 'passed': process.returncode in case['success_codes'][variant] and not timed_out and not oversized and not incomplete and not guidance_changed and source_unchanged,
                 'stdout_sha256': digest(bounded_bytes(stdout)), 'stderr_sha256': digest(bounded_bytes(stderr)),
                 'recorded_at': datetime.now(timezone.utc).isoformat()}
        value['outcome'] = execution_outcome(value)
        h = digest(value)
        logs = private / 'logs' / h; logs.mkdir(parents=True)
        for p in (stdout, stderr): (logs / p.name[1:]).write_bytes(bounded_bytes(p))
        for name, content in files.items():
            artifact = private / 'artifacts' / h / name
            artifact.parent.mkdir(parents=True, exist_ok=True); artifact.write_bytes(content)
        save(private / 'runs' / (h + '.json'), value)
    return {'run_sha256': h, **value}


def project_root(config, project_id):
    registered = config.get('investigation_projects', {})
    if project_id in registered:
        root = Path(registered[project_id]).expanduser().resolve()
    else:
        source = Path(project_id).expanduser()
        if not source.is_absolute(): raise ValueError('Unknown project registration')
        root = source.resolve()
        allowed = [Path(p).expanduser().resolve() for p in config.get('investigation_roots', [])]
        if not any(root.is_relative_to(parent) for parent in allowed): raise ValueError('Project is outside authorized investigation roots')
    if not (root / '.git').exists() or Path(git(root, 'rev-parse', '--show-toplevel')).resolve() != root:
        raise ValueError('Investigation source must be a Git project root')
    return root


def validate_snapshot(config, snapshot):
    root = project_root(config, snapshot['project_id'])
    if str(root) != snapshot['source_root']: raise ValueError('Project registration changed')
    for name, h in snapshot['files'].items():
        relative(name); path = root / name; capture.safe_path(path)
        if not path.is_file() or digest(path.read_bytes()) != h:
            raise ValueError('Original project changed since snapshot')
    return root


def snapshot_project(config, project_id, files, destination):
    root = project_root(config, project_id)
    destination = Path(destination)
    if destination.resolve().is_relative_to(root): raise ValueError('Project copies must not write into the original project')
    if Path(git(root, 'rev-parse', '--show-toplevel')).resolve() != root: raise ValueError('Investigation source must be a Git project root')
    if destination.exists() or not (root / '.git').exists(): raise ValueError('Project snapshot needs a new destination and registered Git root')
    snapshots = {}
    for name in files:
        relative(name)
        if any(p in ('.git', 'node_modules', '.ssh') or p.startswith('.env') or 'credential' in p.lower() for p in PurePosixPath(name).parts): raise ValueError('Private or dependency files cannot be snapshotted')
        path = root / name; capture.safe_path(path)
        if not path.is_file() or path.stat().st_size > 2_000_000: raise ValueError('Invalid project source file')
        snapshots[name] = path.read_bytes()
    destination.mkdir(parents=True)
    for name, data in snapshots.items():
        target = destination / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
    for name, data in snapshots.items():
        capture.safe_path(root / name)
        if (root / name).read_bytes() != data: raise ValueError('Original project changed during snapshot')
    result = {'scope': 'project_snapshot', 'project_id': project_id, 'source_root': str(root),
            'revision': git(root, 'rev-parse', 'HEAD'),
            'dirty': bool(git(root, 'status', '--porcelain')), 'originals_verified_unchanged': True,
            'files': {n: digest(b) for n, b in snapshots.items()}}
    save(destination / '.snapshot.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('command', choices=('prepare', 'report', 'run', 'record', 'snapshot'))
    parser.add_argument('--config', type=Path, required=True); parser.add_argument('--cycle', type=Path)
    parser.add_argument('--due', default=datetime.now(timezone.utc).isoformat()); parser.add_argument('--case', type=Path)
    parser.add_argument('--variant', choices=('baseline', 'candidate', 'contrast')); parser.add_argument('--issue'); parser.add_argument('--decision', type=Path)
    parser.add_argument('--project'); parser.add_argument('--files', nargs='+'); parser.add_argument('--destination', type=Path)
    args = parser.parse_args(); config = read(args.config); state = args.config.resolve().parent
    if args.command == 'prepare': result = {'cycle': str(prepare(config, state, args.due))}
    elif args.command == 'snapshot': result = snapshot_project(config, args.project, args.files, args.destination)
    elif args.command == 'report':
        cycle = cycle_data(args.cycle)
        result = {k: cycle[k] for k in ('cycle_id', 'due', 'base_commit', 'required', 'pending', 'outstanding')}
    elif args.command == 'run': result = run(config, state, args.cycle, args.case, args.variant, args.issue)
    else: result = record(config, state, args.cycle, read(args.decision))
    print(json.dumps(result, indent=2))
    return 1 if args.command == 'run' and result['outcome'] not in ('passed', 'assertion_failed') else 0


if __name__ == '__main__':
    raise SystemExit(main())
