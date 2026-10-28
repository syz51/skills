#!/usr/bin/env python3
"""Immutable feedback transport and validated, atomic skill releases (stdlib only)."""
import argparse
import prompt_runtime
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote

from evidence import latest_receipt, parse_receipt
from capture import SKILLS, codex_home

HOSTS = ('client-host', 'maintainer-host')

class SyncError(Exception):
    pass

def git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True)
    if result.returncode:
        # Remote output can contain credential-bearing URLs; never relay it.
        raise SyncError('Git operation failed: ' + args[0])
    return result.stdout

def digest(data):
    return hashlib.sha256(data).hexdigest()

def clean(repo):
    if git(repo, 'status', '--porcelain').strip():
        raise SyncError('Maintenance checkout has uncommitted changes')
    if git(repo, 'branch', '--show-current').decode().strip() != 'main':
        raise SyncError('Maintenance checkout must be on main')

def fetch(repo):
    try:
        git(repo, 'fetch', 'origin', '+refs/heads/*:refs/remotes/origin/*', '--prune')
        return True
    except SyncError:
        print('Remote unavailable; local notes and current release are preserved.')
        return False

@contextlib.contextmanager
def worktree(repo, ref):
    with tempfile.TemporaryDirectory(prefix='skill-sync-') as folder:
        path = Path(folder) / 'checkout'
        git(repo, 'worktree', 'add', '--detach', str(path), ref)
        try:
            yield path
        finally:
            git(repo, 'worktree', 'remove', '--force', str(path))

def remote_ref(repo, branch):
    ref = 'refs/remotes/origin/' + branch
    try:
        git(repo, 'rev-parse', '--verify', ref)
        return ref
    except SyncError:
        return 'refs/remotes/origin/main'

def safe_tree(root):
    if root.is_symlink() or any(path.is_symlink() for path in root.rglob('*')):
        raise SyncError('Feedback symlinks are prohibited')

def safe_local_parents(path):
    # macOS system aliases are legitimate; user-controlled symlinks are not.
    system_aliases = {Path('/var'), Path('/tmp'), Path('/etc')}
    if any(parent.is_symlink() for parent in (path, *path.parents) if parent not in system_aliases):
        raise SyncError('Local feedback symlink is prohibited')

def upload(args):
    if not fetch(args.repo):
        return False
    ref = remote_ref(args.repo, 'inbox/' + args.host_id)
    history = list(note_records(args.repo, ref, args.host_id))
    historical_notes = {(meta['skill'], meta['sha256']) for _, _, meta in history}
    historical_copies = {name: data for name, data, _ in history}
    with worktree(args.repo, ref) as checkout:
        safe_tree(checkout / 'feedback')
        def existing_note(path):
            if path.exists():
                return path.read_bytes()
            return historical_copies.get(str(path.relative_to(checkout)))
        added = []
        revision = 'unknown'
        installed = args.install_root / 'current'
        if installed.is_symlink() and (installed / 'manifest.json').is_file():
            revision = json.loads((installed / 'manifest.json').read_text()).get('commit', 'unknown')
        for skill in SKILLS:
            source = args.feedback_dir / skill / 'notes'
            safe_local_parents(source)
            if any(parent.is_symlink() for parent in (source, source.parent, args.feedback_dir)):
                raise SyncError('Feedback directory must not be a symlink')
            # Capture receipt paths first: a concurrent note/receipt pair waits
            # for the next upload rather than outrunning this note snapshot.
            verification_dir = args.feedback_dir / skill / 'verifications'
            safe_local_parents(verification_dir)
            receipts = sorted(verification_dir.glob('*.json'))
            note_hashes = {sha for historical_skill, sha in historical_notes if historical_skill == skill}
            for note in sorted(source.glob('*.md')):
                if note.is_symlink() or not note.is_file():
                    raise SyncError('Feedback notes must be regular files')
                data = note.read_bytes()
                note_hashes.add(digest(data))
                destination = checkout / 'feedback' / skill / 'notes' / args.host_id / note.name
                prior = existing_note(destination)
                if prior is not None:
                    if prior == data:
                        continue
                    destination = destination.with_name(note.stem + '--' + digest(data) + '.md')
                prior = existing_note(destination)
                if prior is not None:
                    if prior != data:
                        raise SyncError('Immutable feedback collision')
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
                sidecar = destination.with_suffix('.json')
                metadata = {'filename': note.name, 'sha256': digest(data),
                    'sourceMachine': args.host_id, 'skill': skill, 'skillRevision': revision}
                if sidecar.exists():
                    prior = json.loads(sidecar.read_text())
                    if any(prior.get(key) != metadata[key] for key in ('filename', 'sha256', 'sourceMachine', 'skill')):
                        raise SyncError('Immutable provenance collision')
                else:
                    sidecar.write_text(json.dumps(metadata, indent=2) + '\n')
                added.extend([str(destination.relative_to(checkout)), str(sidecar.relative_to(checkout))])
            histories = {}
            for receipt in receipts:
                safe_local_parents(receipt)
                data = receipt.read_bytes()
                value = parse_receipt(receipt.name, data, args.host_id, skill)
                histories.setdefault(value['noteSha256'], {})[digest(data)] = value
                if value['noteSha256'] not in note_hashes:
                    raise SyncError('Source verification has no originating note')
                destination = checkout / 'feedback' / skill / 'verifications' / args.host_id / receipt.name
                if destination.exists():
                    if destination.read_bytes() != data:
                        raise SyncError('Immutable source verification collision')
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
                added.append(str(destination.relative_to(checkout)))
            for history in histories.values():
                latest_receipt(history)
        if not added:
            print('Feedback is already uploaded.')
            return True
        git(checkout, 'add', '--', *added)
        git(checkout, 'commit', '-m', 'Collect immutable feedback from ' + args.host_id)
        try:
            git(checkout, 'push', 'origin', 'HEAD:refs/heads/inbox/' + args.host_id)
        except SyncError:
            print('Upload deferred; source notes remain buffered locally.')
            return False
        print('Uploaded ' + str(len(added)) + ' feedback/provenance/source-verification files.')
        return True

def feedback_tree(repo, ref):
    """Keep Git paths literal, including Unicode, tabs, and newlines."""
    entries = {}
    for record in git(repo, 'ls-tree', '-r', '-z', ref, '--', 'feedback').split(b'\0'):
        if record:
            header, name = record.split(b'\t', 1)
            mode, kind, oid = header.split()
            entries[os.fsdecode(name)] = (mode, kind, oid.decode('ascii'))
    return entries

def provenance_note_name(metadata, sidecar):
    filename = metadata['filename']
    for candidate in (filename, Path(filename).stem + '--' + metadata['sha256'] + '.md'):
        if Path(candidate).with_suffix('.json').name == Path(sidecar).name:
            return candidate
    raise SyncError('Invalid feedback provenance filename')

def provenance(repo, entries, name, parts):
    if entries[name][:2] != (b'100644', b'blob'):
        raise SyncError('Unsafe provenance file')
    metadata = json.loads(git(repo, 'cat-file', 'blob', entries[name][2]))
    if not isinstance(metadata, dict):
        raise SyncError('Invalid feedback provenance')
    filename = metadata.get('filename')
    if (metadata.get('sourceMachine') != parts[3] or metadata.get('skill') != parts[1]
            or not isinstance(metadata.get('sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', metadata['sha256'])
            or not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith('.md')):
        raise SyncError('Invalid feedback provenance')
    provenance_note_name(metadata, name)
    return metadata

def note_records(repo, ref, host=None):
    entries = feedback_tree(repo, ref)
    records, canonical, sidecars = [], {}, set()
    for name in entries:
        parts = Path(name).parts
        if len(parts) != 5 or parts[0] != 'feedback' or parts[1] not in SKILLS or parts[2] != 'notes' or parts[3] not in HOSTS or not name.endswith('.md'):
            continue
        sidecar = str(Path(name).with_suffix('.json'))
        if entries[name][:2] != (b'100644', b'blob') or sidecar not in entries:
            raise SyncError('Unsafe feedback file or missing provenance')
        data = git(repo, 'cat-file', 'blob', entries[name][2])
        metadata = provenance(repo, entries, sidecar, parts)
        if metadata['sha256'] != digest(data) or Path(name).name != provenance_note_name(metadata, sidecar):
            raise SyncError('Invalid feedback provenance')
        canonical[(parts[1], metadata['filename'], metadata['sha256'])] = data
        sidecars.add(sidecar)
        records.append((name, data, metadata))
    # Collection deduplicates note bytes across hosts but retains each host's
    # immutable provenance. An alias needs the exact validated canonical note.
    for name in entries:
        parts = Path(name).parts
        if (name in sidecars or len(parts) != 5 or parts[0] != 'feedback' or parts[1] not in SKILLS
                or parts[2] != 'notes' or parts[3] not in HOSTS or not name.endswith('.json')):
            continue
        metadata = provenance(repo, entries, name, parts)
        key = (parts[1], metadata['filename'], metadata['sha256'])
        if key not in canonical:
            raise SyncError('Feedback provenance alias has no originating note')
        logical = str(Path(name).with_name(provenance_note_name(metadata, name)))
        records.append((logical, canonical[key], metadata))
    yield from (record for record in records if not host or record[2]['sourceMachine'] == host)

def verification_records(repo, ref, host=None):
    notes = {(meta['sourceMachine'], meta['skill'], meta['sha256']) for _, _, meta in note_records(repo, ref)}
    entries = feedback_tree(repo, ref)
    records, histories = [], {}
    for name in entries:
        parts = Path(name).parts
        if len(parts) != 5 or parts[0] != 'feedback' or parts[1] not in SKILLS or parts[2] != 'verifications' or parts[3] not in HOSTS or not name.endswith('.json'):
            continue
        if host and parts[3] != host:
            continue
        if entries[name][:2] != (b'100644', b'blob'):
            raise SyncError('Unsafe source verification file')
        data = git(repo, 'cat-file', 'blob', entries[name][2])
        value = parse_receipt(Path(name).name, data, parts[3], parts[1])
        if (parts[3], parts[1], value['noteSha256']) not in notes:
            raise SyncError('Source verification has no originating note')
        histories.setdefault((parts[3], parts[1], value['noteSha256']), {})[digest(data)] = value
        records.append((name, data))
    for history in histories.values():
        latest_receipt(history)
    yield from records

def collect(args):
    clean(args.repo)
    if not fetch(args.repo):
        return False
    git(args.repo, 'merge', '--ff-only', 'refs/remotes/origin/main')
    safe_tree(args.repo / 'feedback')
    existing = {(Path(name).parts[1], meta['filename'], digest(data)) for name, data, meta in note_records(args.repo, 'HEAD')}
    added = []
    for host in HOSTS:
        ref = remote_ref(args.repo, 'inbox/' + host)
        for name, data, meta in note_records(args.repo, ref, host):
            key = (meta['skill'], meta['filename'], digest(data))
            if key in existing:
                provenance = args.repo / Path(name).with_suffix('.json')
                if not provenance.exists():
                    provenance.parent.mkdir(parents=True, exist_ok=True)
                    provenance.write_text(json.dumps(meta, indent=2) + '\n')
                    added.append(str(provenance.relative_to(args.repo)))
                elif json.loads(provenance.read_text()) != meta:
                    raise SyncError('Immutable provenance collision')
                continue
            path = args.repo / name
            if path.exists() and path.read_bytes() != data:
                path = path.with_name(path.stem + '--' + digest(data) + '.md')
            if path.exists():
                raise SyncError('Unexpected feedback collision')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.with_suffix('.json').write_text(json.dumps(meta, indent=2) + '\n')
            added.extend([str(path.relative_to(args.repo)), str(path.with_suffix('.json').relative_to(args.repo))])
            existing.add(key)
        for name, data in verification_records(args.repo, ref, host):
            path = args.repo / name
            if path.exists():
                if path.read_bytes() != data:
                    raise SyncError('Immutable source verification collision')
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            added.append(name)
    if added:
        git(args.repo, 'add', '--', *added)
        git(args.repo, 'commit', '-m', 'Import host feedback without merging inbox branches')
    print('Imported ' + str(sum(name.endswith('.md') for name in added)) + ' unique feedback notes; preserved host provenance.')
    return True

def skills_hash(root):
    result = hashlib.sha256()
    for path in sorted((root / 'skills').rglob('*')):
        if path.is_symlink():
            raise SyncError('Skill symlinks are prohibited')
        if path.is_file():
            result.update(str(path.relative_to(root)).encode() + b'\0' + path.read_bytes() + b'\0')
    return result.hexdigest()

def prompt_hash(root):
    try:
        return prompt_runtime.bundle_hash(root)
    except (ValueError, OSError, UnicodeError) as exc:
        raise SyncError('Invalid prompt bundle') from exc

def validate(root, validator=None):
    prompt_hash(root)
    if (root / 'skills').is_symlink():
        raise SyncError('Skill symlinks are prohibited')
    for skill in SKILLS:
        folder = root / 'skills' / skill
        if folder.is_symlink():
            raise SyncError('Skill symlinks are prohibited')
        if not (folder / 'SKILL.md').is_file():
            raise SyncError('Missing SKILL.md for ' + skill)
        for path in folder.rglob('*'):
            if path.is_symlink():
                raise SyncError('Skill symlinks are prohibited')
            if path.is_file() and path.suffix == '.md':
                markdown = re.sub(r'(?ms)^\s*(```|~~~).*?^\s*\1[^\n]*$', '', path.read_text())
                markdown = re.sub(r'`[^`]*`', '', markdown)
                for target in re.findall(r'\]\(([^)]+)\)', markdown):
                    target = target.strip().split(' "')[0].strip('<>')
                    if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', target) or target.startswith('#'):
                        continue
                    target = unquote(target.split('#')[0])
                    destination = (path.parent / target).resolve()
                    if not destination.is_relative_to(folder.resolve()) or not destination.exists():
                        raise SyncError('Broken or unsafe local reference in ' + str(path.relative_to(root)))
        if validator:
            result = subprocess.run(['uv', 'run', '--with', 'pyyaml', '--python', sys.executable, '--script', str(validator), str(folder)], capture_output=True)
            if result.returncode:
                raise SyncError('Skill validation command failed for ' + skill)
    return skills_hash(root)

def verify_release(root, validator=None):
    checksum = validate(root, validator)
    marker = root / 'release.json'
    if marker.is_symlink() or not marker.is_file():
        raise SyncError('Missing trusted release review')
    review = json.loads(marker.read_text())
    if review.get('schema_version') != 1 or review.get('validator_passed') is not True or review.get('skillsSha256') != checksum:
        raise SyncError('Release review does not approve this skills tree')
    for name in review.get('reviews', []):
        parts = Path(name).parts
        if len(parts) < 4 or parts[0] != 'feedback' or parts[1] not in SKILLS or parts[2] != 'reviews' or '..' in parts or not (root / name).is_file() or (root / name).is_symlink():
            raise SyncError('Invalid referenced review record')
    return checksum

def publish(args):
    clean(args.repo)
    if not args.validator:
        raise SyncError('Publication requires --validator')
    if not fetch(args.repo):
        return False
    git(args.repo, 'merge-base', '--is-ancestor', 'refs/remotes/origin/main', 'HEAD')
    fields = git(args.repo, 'diff', '--no-renames', '--name-status', '-z', 'refs/remotes/origin/main', 'HEAD').split(b'\0')
    changes = [(status.decode('ascii'), os.fsdecode(name)) for status, name in zip(fields[0:-1:2], fields[1:-1:2])]
    if not changes and (not args.bootstrap or (args.repo / 'release.json').exists()):
        with worktree(args.repo, 'HEAD') as checkout:
            safe_tree(checkout / 'feedback')
            verify_release(checkout, args.validator)
        print('Validated release is already published.')
        return True
    changed_skills, reviewed_skills, reviews = set(), set(), []
    for status, name in changes:
        parts = Path(name).parts
        skill_path = len(parts) >= 3 and parts[0] == 'skills' and parts[1] in SKILLS
        feedback_path = len(parts) >= 4 and parts[0] == 'feedback' and parts[1] in SKILLS and parts[2] in ('notes', 'reviews', 'verifications')
        if not (skill_path or feedback_path or name == 'release.json'):
            raise SyncError('Out-of-scope publication path: ' + name)
        if skill_path:
            changed_skills.add(parts[1])
        if feedback_path and parts[2] == 'notes' and status != 'A':
            raise SyncError('Feedback notes are immutable')
        if feedback_path and parts[2] == 'verifications' and status != 'A':
            raise SyncError('Source verifications are immutable')
        if feedback_path and parts[2] == 'reviews' and status in ('A', 'M'):
            reviewed_skills.add(parts[1])
            reviews.append(name)
    if not args.bootstrap and changed_skills - reviewed_skills:
        raise SyncError('Changed skills require a review record: ' + ', '.join(sorted(changed_skills - reviewed_skills)))
    with worktree(args.repo, 'HEAD') as checkout:
        checksum = validate(checkout, args.validator)
        safe_tree(checkout / 'feedback')
    list(verification_records(args.repo, 'HEAD'))
    manifest = {'schema_version': 1, 'skillsSha256': checksum,
                'validator_passed': True, 'reviews': sorted(reviews)}
    marker = args.repo / 'release.json'
    if marker.is_symlink() or (marker.exists() and not marker.is_file()):
        raise SyncError('Release manifest must be a regular file')
    serialized = json.dumps(manifest, indent=2) + '\n'
    if not marker.exists() or marker.read_text() != serialized:
        marker.write_text(serialized)
        git(args.repo, 'add', '--', 'release.json')
        git(args.repo, 'commit', '-m', 'Record validated skill release')
    git(args.repo, 'push', 'origin', 'HEAD:refs/heads/main')
    print('Published validated main release.')
    return True

def review_name(name):
    if not isinstance(name, str):
        return False
    parts = Path(name).parts
    return (str(Path(name)) == name and len(parts) >= 4 and parts[0] == 'feedback'
            and parts[1] in SKILLS and parts[2] == 'reviews' and '..' not in parts)

def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def atomic_write(path, data, prefix):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=prefix, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, path)
            sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

def review_blob(repo, commit, name, published, trees):
    """Recovery proof is a regular blob in published history, never a hash alone."""
    if not isinstance(commit, str) or not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', commit):
        raise SyncError('Invalid review recovery commit')
    if commit not in trees:
        git(repo, 'merge-base', '--is-ancestor', commit, published)
        trees[commit] = feedback_tree(repo, commit)
    entry = trees[commit].get(name)
    if not entry or entry[0] not in (b'100644', b'100755') or entry[1] != b'blob':
        raise SyncError('Invalid review recovery blob')
    data = git(repo, 'cat-file', 'blob', entry[2])
    return {'commit': commit, 'sha256': digest(data)}, data

def review_recovery(args, path, previous, published, trees):
    if not path.exists() and not path.is_symlink():
        return {}
    safe_local_parents(path)
    try:
        if not path.is_file():
            raise ValueError()
        value = json.loads(path.read_text())
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'base_commit', 'target_commit', 'feedback_dir', 'reviews'}
                or value['schema_version'] != 1 or value['feedback_dir'] != str(args.feedback_dir.resolve())
                or not isinstance(value['reviews'], dict)):
            raise ValueError()
        base, target = value['base_commit'], value['target_commit']
        if not isinstance(target, str) or not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', target):
            raise ValueError()
        git(args.repo, 'merge-base', '--is-ancestor', target, published)
        if base is not None:
            if not isinstance(base, str) or not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', base):
                raise ValueError()
            git(args.repo, 'merge-base', '--is-ancestor', base, target)
        if target not in trees:
            trees[target] = feedback_tree(args.repo, target)
        if base != previous and target != previous:
            raise ValueError()
        recovery = {}
        for name, versions in value['reviews'].items():
            if not review_name(name) or not isinstance(versions, dict) or set(versions) != {'before', 'after'}:
                raise ValueError()
            if versions['before'] is None and versions['after'] is None:
                raise ValueError()
            candidates = []
            for label in ('before', 'after'):
                source = versions[label]
                if source is None:
                    continue
                if not isinstance(source, dict) or set(source) != {'commit', 'sha256'}:
                    raise ValueError()
                expected, data = review_blob(args.repo, source['commit'], name, target, trees)
                if source != expected or (label == 'after' and source['commit'] != target):
                    raise ValueError()
                candidates.append((expected, data))
            if versions['after'] is None and name in trees[target]:
                raise ValueError()
            # Once current switched, an old before-version is a user edit.
            # Only cached copies absent from that completed target retain proof.
            if target != previous or versions['after'] is None:
                recovery[name] = candidates
        return recovery
    except (OSError, ValueError, KeyError, TypeError, SyncError):
        raise SyncError('Invalid or unverifiable review recovery state') from None

def deploy(args):
    if not args.validator:
        raise SyncError('Deployment requires --validator')
    if not fetch(args.repo):
        return False
    commit = git(args.repo, 'rev-parse', 'refs/remotes/origin/main').decode().strip()
    root = args.install_root
    current = root / 'current'
    previous_commit = None
    if current.exists() or current.is_symlink():
        if not current.is_symlink() or not current.resolve().is_relative_to((root / 'releases').resolve()):
            raise SyncError('Current installation is not a managed release')
        old = current.resolve()
        previous_manifest = json.loads((old / 'manifest.json').read_text())
        if skills_hash(old) != previous_manifest['skillsSha256']:
            raise SyncError('Installed skills have local edits; deployment refused')
        if prompt_hash(old) != previous_manifest.get('promptsSha256'):
            raise SyncError('Installed prompts have local edits; deployment refused')
        previous_commit = previous_manifest.get('commit')
    with worktree(args.repo, commit) as checkout:
        checksum = verify_release(checkout, args.validator)
        prompts_checksum = prompt_hash(checkout)
        safe_tree(checkout / 'feedback')
        safe_local_parents(args.feedback_dir)
        journal = root / 'review-deploy.json'
        safe_local_parents(journal)
        trees = {}
        recovery = review_recovery(args, journal, previous_commit, commit, trees)
        review_changes = []
        journal_reviews = {}
        previous_reviews = {}
        if isinstance(previous_commit, str) and re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', previous_commit):
            try:
                git(args.repo, 'merge-base', '--is-ancestor', previous_commit, commit)
                previous_reviews = trees.setdefault(previous_commit, feedback_tree(args.repo, previous_commit))
            except SyncError:
                pass
        def local_source(name, local):
            if name in previous_reviews:
                source, data = review_blob(args.repo, previous_commit, name, commit, trees)
                if local == data:
                    return source
            return next((source for source, data in recovery.get(name, []) if local == data), None)
        for skill in SKILLS:
            for review in sorted((checkout / 'feedback' / skill / 'reviews').rglob('*')):
                if review.is_symlink():
                    raise SyncError('Review symlinks are prohibited')
                if review.is_file():
                    destination = args.feedback_dir / skill / 'reviews' / review.relative_to(checkout / 'feedback' / skill / 'reviews')
                    safe_local_parents(destination)
                    name = str(review.relative_to(checkout))
                    after, data = review_blob(args.repo, commit, name, commit, trees)
                    if review.read_bytes() != data:
                        raise SyncError('Published review does not match its Git blob')
                    before = None
                    if destination.exists():
                        if not destination.is_file():
                            raise SyncError('Local review edits would be overwritten')
                        local = destination.read_bytes()
                        if local == data:
                            journal_reviews[name] = {'before': after, 'after': after}
                            continue
                        before = local_source(name, local)
                        if before is None:
                            raise SyncError('Local review edits would be overwritten')
                    journal_reviews[name] = {'before': before, 'after': after}
                    review_changes.append((destination, data))
        # Reviews removed upstream remain local cached files. Retain only their
        # exact managed bytes so later reintroduction can still update them.
        for name in sorted(set(previous_reviews) | set(recovery)):
            if not review_name(name) or name in journal_reviews:
                continue
            destination = args.feedback_dir / Path(name).relative_to('feedback')
            safe_local_parents(destination)
            if destination.is_file():
                before = local_source(name, destination.read_bytes())
                if before is not None:
                    journal_reviews[name] = {'before': before, 'after': None}
        release = root / 'releases' / commit
        release.parent.mkdir(parents=True, exist_ok=True)
        if release.exists():
            manifest_path = release / 'manifest.json'
            existing_manifest = json.loads(manifest_path.read_text())
            if (skills_hash(release) != checksum or
                    existing_manifest.get('skillsSha256') != checksum or
                    existing_manifest.get('commit') != commit or
                    prompt_hash(release) != prompts_checksum or
                    existing_manifest.get('promptsSha256') != prompts_checksum):
                raise SyncError('Existing immutable release has changed')
        else:
            with tempfile.TemporaryDirectory(dir=release.parent, prefix='.stage-') as staging:
                stage = Path(staging) / 'release'
                stage.mkdir()
                shutil.copytree(checkout / 'skills', stage / 'skills')
                copied_prompts = prompt_runtime.copy_bundle(checkout, stage)
                if copied_prompts != prompts_checksum:
                    raise SyncError('Prompt bundle changed during deployment')
                manifest = {'commit': commit, 'skillsSha256': checksum}
                if copied_prompts is not None:
                    manifest['promptsSha256'] = copied_prompts
                (stage / 'manifest.json').write_text(json.dumps(manifest) + '\n')
                os.rename(stage, release)
        # The durable before/after proof must precede every review replacement.
        transaction = {'schema_version': 1, 'base_commit': previous_commit, 'target_commit': commit,
                       'feedback_dir': str(args.feedback_dir.resolve()), 'reviews': journal_reviews}
        if journal_reviews or journal.exists():
            atomic_write(journal, (json.dumps(transaction, indent=2) + '\n').encode(), '.review-deploy-')
        for destination, data in review_changes:
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(destination, data, '.review-')
        root.mkdir(parents=True, exist_ok=True)
        # main() serializes deployments. Reuse one managed staging link so a
        # killed process cannot leave a growing set of stale PID-named links.
        temporary = root / '.current-next'
        if temporary.is_symlink():
            temporary.unlink()
        elif temporary.exists():
            raise SyncError('Current staging path is not a managed symlink')
        try:
            temporary.symlink_to(release)
            os.replace(temporary, current)
            sync_directory(root)
        finally:
            temporary.unlink(missing_ok=True)
        transaction['reviews'] = {name: versions for name, versions in journal_reviews.items() if versions['after'] is None}
        if transaction['reviews']:
            atomic_write(journal, (json.dumps(transaction, indent=2) + '\n').encode(), '.review-deploy-')
        else:
            journal.unlink(missing_ok=True)
            sync_directory(root)
    print('Deployed validated release ' + commit[:12] + '.')
    return True

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('upload', 'collect', 'publish', 'deploy'))
    parser.add_argument('--repo', type=Path, default=Path.home() / '.local/share/codex-skills/repository')
    parser.add_argument('--host-id', choices=HOSTS, default='client-host')
    parser.add_argument('--feedback-dir', type=Path, default=codex_home() / 'skill-feedback')
    parser.add_argument('--install-root', type=Path, default=Path.home() / '.local/share/codex-skills')
    parser.add_argument('--validator', type=Path)
    parser.add_argument('--bootstrap', action='store_true', help='Allow initial skill population without prior reviews')
    args = parser.parse_args()
    try:
        common = Path(git(args.repo, 'rev-parse', '--git-common-dir').decode().strip())
        if not common.is_absolute():
            common = args.repo / common
        with (common / 'skill-sync.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            success = globals()[args.command](args)
        return 0 if success else 1
    except (SyncError, OSError, ValueError, KeyError) as error:
        print('Sync stopped: ' + str(error))
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
