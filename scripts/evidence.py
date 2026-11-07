#!/usr/bin/env python3
"""Check feedback against original turns, preserving linked assessment history."""
import argparse
import prompt_runtime
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import capture
import claude_cli
import read_guidance

from capture import (CYCLE_TIMEOUT, CaptureBudgetExpired, ExtractorFailure, RateLimited, FIELDS, ScanCache, SCHEMA as NOTE_SCHEMA, SINCE, SKILLS, atomic, claude_projects, claude_turns, codex_home,
                     completed_turns, message_context, model_response, safe_path, timestamp, validate_notes)

# Source checks need more room than extraction, which also carries duplicate-note context.
SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
MAX_INPUT = 96000
ORIGINAL_SCOPE = 'original-turn messages; artifacts and runtime outcomes not replayed'
FOLLOWUP_SCOPE = 'original-turn and subsequent same-chat messages; artifacts and runtime outcomes not replayed'

SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['status', 'summary'], 'properties': {
    'status': {'type': 'string', 'enum': ['supported', 'partial', 'unsupported']},
    'summary': {'type': 'string', 'minLength': 1, 'maxLength': 1000}}}
PROMPT = prompt_runtime.load('verify', root=prompt_runtime.DEFAULT_ROOT)
CORRECTION_SCHEMA = {**NOTE_SCHEMA, 'properties': {'notes': {**NOTE_SCHEMA['properties']['notes'], 'maxItems': 1}}}
CORRECTION_PROMPT = prompt_runtime.load('correct', root=prompt_runtime.DEFAULT_ROOT)
# A note whose source check settles nothing new (its source is missing or ambiguous, the check failed, or the
# assessment stays unresolved) waits 1, 2, 4, 8 and 16 hours, then a day, before its next scheduled attempt.
BACKOFF = 'evidence-backoff.json'
BACKOFF_REASONS = ('unavailable', 'ambiguous', 'error', 'unresolved')
MAX_BACKOFF = timedelta(hours=24)
# Evidence starts after capture, whose budget is 15 minutes, so its hourly start drifts; an entry due within this
# slack of a run is retried rather than skipped.
BACKOFF_SLACK = timedelta(minutes=30)


def field(note, name):
    values = re.findall(r'^- ' + re.escape(name) + r': (.+)$', note, re.M)
    value = values[0] if len(values) == 1 else None
    return None if name in ('Thread', 'Turn') and value == 'unavailable' else value


def validate_receipt(value, note_sha=None, host=None, skill=None):
    required = {'schema_version', 'noteSha256', 'sourceMachine', 'skill', 'thread', 'turn',
                'sourceSha256', 'completed_at', 'checked_at', 'loadedReads', 'status', 'summary', 'scope'}
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] not in (1, 2, 3, 4):
        raise ValueError('Invalid source verification receipt')
    if value['schema_version'] >= 2:
        required |= {'checkerSha256', 'inputSha256', 'inputTruncated', 'supersedes'}
    if value['schema_version'] >= 3:
        required.add('correction')
    if value['schema_version'] == 4:
        required |= {'evidenceSha256', 'followUps'}
    if set(value) != required:
        raise ValueError('Invalid source verification receipt')
    if value['sourceMachine'] not in ('client-host', 'maintainer-host') or value['skill'] not in SKILLS:
        raise ValueError('Invalid source verification receipt')
    for key in ('noteSha256', 'sourceSha256'):
        if not isinstance(value[key], str) or not re.fullmatch(r'[a-f0-9]{64}', value[key]):
            raise ValueError('Invalid source verification receipt')
    if any(expected is not None and value[key] != expected for key, expected in
           [('noteSha256', note_sha), ('sourceMachine', host), ('skill', skill)]):
        raise ValueError('Source verification identity mismatch')
    for key in ('thread', 'turn'):
        if not isinstance(value[key], str) or not re.fullmatch(r'[A-Za-z0-9-]{1,100}', value[key]):
            raise ValueError('Invalid source verification receipt')
    for key in ('completed_at', 'checked_at'):
        timestamp(value[key])
    if value['schema_version'] >= 2:
        for key in ('checkerSha256', 'inputSha256'):
            if not isinstance(value[key], str) or not re.fullmatch(r'[a-f0-9]{64}', value[key]):
                raise ValueError('Invalid assessment fingerprint')
        if type(value['inputTruncated']) is not bool or (value['supersedes'] is not None and
                (not isinstance(value['supersedes'], str) or not re.fullmatch(r'[a-f0-9]{64}', value['supersedes']))):
            raise ValueError('Invalid assessment lineage')
    scope = FOLLOWUP_SCOPE if value.get('followUps') else ORIGINAL_SCOPE
    if value['status'] not in SCHEMA['properties']['status']['enum'] or value['scope'] != scope:
        raise ValueError('Invalid source verification receipt')
    if not isinstance(value['summary'], str) or not 0 < len(value['summary']) <= 1000 or '\n' in value['summary']:
        raise ValueError('Invalid source verification receipt')
    if value['schema_version'] >= 3:
        if value['status'] == 'supported' and (value['schema_version'] == 3 or value['correction'] is not None):
            raise ValueError('Supported notes do not need a correction receipt')
        correction = value['correction']
        if correction is not None:
            if (not isinstance(correction, dict) or set(correction) !=
                    {'note', 'status', 'summary', 'inputSha256', 'inputTruncated', 'generationInputSha256'} or
                    correction['status'] != 'supported' or correction['inputTruncated'] is not False or value['inputTruncated']):
                raise ValueError('Invalid verified correction')
            for key in ('inputSha256', 'generationInputSha256'):
                if not isinstance(correction[key], str) or not re.fullmatch(r'[a-f0-9]{64}', correction[key]):
                    raise ValueError('Invalid correction fingerprint')
            if (not isinstance(correction['summary'], str) or not 0 < len(correction['summary']) <= 1000 or
                    '\n' in correction['summary']):
                raise ValueError('Invalid correction summary')
            validate_notes({'notes': [correction['note']]}, {'verified_skills': [value['skill']]})
    if value['schema_version'] == 4:
        if not isinstance(value['evidenceSha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', value['evidenceSha256']):
            raise ValueError('Invalid evidence fingerprint')
        if not isinstance(value['followUps'], list):
            raise ValueError('Invalid follow-up evidence')
        seen, previous = {value['turn']}, None
        for item in value['followUps']:
            if (not isinstance(item, dict) or set(item) != {'thread', 'turn', 'sourceSha256', 'completed_at'} or
                    item['thread'] != value['thread'] or not isinstance(item['turn'], str) or
                    not re.fullmatch(r'[A-Za-z0-9-]{1,100}', item['turn']) or item['turn'] in seen or
                    not isinstance(item['completed_at'], str) or
                    not isinstance(item['sourceSha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', item['sourceSha256'])):
                raise ValueError('Invalid follow-up evidence')
            order = (timestamp(item['completed_at']), item['turn'])
            if order[0] <= timestamp(value['completed_at']) or (previous is not None and order <= previous):
                raise ValueError('Invalid follow-up chronology')
            seen.add(item['turn'])
            previous = order
    if not isinstance(value['loadedReads'], list):
        raise ValueError('Invalid source verification receipt')
    for item in value['loadedReads']:
        if not isinstance(item, dict) or set(item) != {'path', 'sha256', 'coverage', 'read_turn'}:
            raise ValueError('Invalid loaded read')
        path = Path(item['path'])
        if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != value['skill'] or not re.fullmatch(r'[\w./-]+', item['path']):
            raise ValueError('Invalid loaded read')
        if not re.fullmatch(r'[a-f0-9]{64}', item['sha256']) or item['coverage'] not in ('complete', 'partial') or not re.fullmatch(r'[A-Za-z0-9-]{1,100}', item['read_turn']):
            raise ValueError('Invalid loaded read')
    return value


def parse_receipt(name, data, host=None, skill=None):
    match = re.fullmatch(r'([a-f0-9]{64})(?:--([a-f0-9]{64}))?\.json', name)
    if not match:
        raise ValueError('Invalid source verification filename')
    value = validate_receipt(json.loads(data), match[1], host, skill)
    expected = hashlib.sha256(data).hexdigest() if value['schema_version'] >= 2 else None
    if match[2] != expected:
        raise ValueError('Source verification filename does not match its content')
    return value


def latest_receipt(records):
    if len({(v['sourceMachine'], v['skill'], v['noteSha256']) for v in records.values()}) > 1:
        raise ValueError('Source verification histories have different identities')
    for value in records.values():
        previous = value.get('supersedes')
        if previous is not None and previous not in records:
            raise ValueError('Source verification predecessor is missing')
    superseded = {v.get('supersedes') for v in records.values()}
    heads = [(digest, v) for digest, v in records.items() if digest not in superseded]
    if records and not heads:
        raise ValueError('Source verification history has no current assessment')
    return max(heads, key=lambda item: (timestamp(item[1]['checked_at']), item[0])) if heads else (None, None)


def assessment_inventory(feedback):
    notes, groups = {}, {}
    for skill in SKILLS:
        for path in (feedback / skill / 'notes').rglob('*.md'):
            safe_path(path)
            notes.setdefault((skill, hashlib.sha256(path.read_bytes()).hexdigest()), []).append(str(path.relative_to(feedback)))
        for path in (feedback / skill / 'verifications').rglob('*.json'):
            safe_path(path)
            data = path.read_bytes()
            parts = path.relative_to(feedback / skill / 'verifications').parts
            if len(parts) > 2 or (len(parts) == 2 and parts[0] not in ('client-host', 'maintainer-host')):
                raise ValueError('Invalid source verification host directory')
            value = parse_receipt(path.name, data, host=parts[0] if len(parts) == 2 else None, skill=skill)
            key = (skill, value['noteSha256'])
            if key not in notes:
                raise ValueError('Source verification has no originating note')
            group = groups.setdefault((*key, value['sourceMachine']), {})
            group[hashlib.sha256(data).hexdigest()] = (value, str(path.relative_to(feedback)))
    latest = []
    for group in groups.values():
        digest, value = latest_receipt({h: item[0] for h, item in group.items()})
        latest.append({**value, 'receiptSha256': digest, 'receipt': group[digest][1]})
    checked = {(item['skill'], item['noteSha256']) for item in latest}
    return {'notes': len(notes), 'assessments': sum(len(g) for g in groups.values()),
            'corrected': sum(v.get('correction') is not None for v in latest),
            'unresolved': sum(unresolved(v) for v in latest),
            'latest': sorted(latest, key=lambda v: (v['skill'], v['noteSha256'], v['sourceMachine'])),
            'unchecked': [{'skill': skill, 'noteSha256': digest, 'notes': paths}
                          for (skill, digest), paths in sorted(notes.items()) if (skill, digest) not in checked]}


def turn_identity(turn):
    return {'thread': turn['thread'], 'turn': turn['turn'], 'sourceSha256': turn['source_sha256'],
            'completed_at': turn['completed_at']}


def evidence_fingerprint(turn):
    records = [{**turn_identity(item), **message_context(item['messages'])}
               for item in (turn, *turn.get('follow_ups', []))]
    return hashlib.sha256(json.dumps(records, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def source_input(note, turn, **extra):
    # Notes are not privacy-checked, so the note under check, and the original a correction
    # replaces, are redacted like the transcript they are compared with.
    note = capture.redact(note)
    if 'correction_of' in extra:
        extra['correction_of'] = capture.redact(extra['correction_of'])
    follow_ups = turn.get('follow_ups', [])
    def build(compact):
        source = {**message_context(turn['messages'], compact=compact),
                  'scope': 'Original user/assistant messages only; tool contents and runtime outcomes absent.'}
        later = [{**turn_identity(item), **message_context(item['messages'], compact=compact)} for item in follow_ups]
        return source, later
    def serialize(source, later):
        additions = {'follow_ups': later, 'omitted_follow_up_turns': len(follow_ups) - len(later)} if follow_ups else {}
        return json.dumps({'note': note, 'original_turn': source, **additions, **extra}, ensure_ascii=False)
    source, later = build(False)
    data = serialize(source, later)
    if len(data) > MAX_INPUT:
        source, later = build(True)
        data = serialize(source, later)
        while len(data) > MAX_INPUT and later:
            later.pop(0)
            data = serialize(source, later)
    if len(data) > MAX_INPUT:
        raise ValueError('Capture input exceeds safety bound')
    return data, source['truncated'] or any(item['truncated'] for item in later) or len(later) != len(follow_ups)


def verify_note(config, state_root, note, turn, skill, correction_of=None, *, deadline=None, wait_for_rate_limit=True):
    data, truncated = source_input(note, turn, **({'correction_of': correction_of} if correction_of is not None else {}))
    result = model_response(config, state_root, prompt_runtime.load('verify', config), data, SCHEMA, deadline=deadline,
                            wait_for_rate_limit=wait_for_rate_limit, role='verify')
    if not isinstance(result, dict) or set(result) != {'status', 'summary'} or result['status'] not in SCHEMA['properties']['status']['enum']:
        raise ValueError('Invalid source verification result')
    capture.validate_summary(result['summary'], skill)
    return {**result, 'inputSha256': hashlib.sha256(data.encode()).hexdigest(),
            'inputTruncated': truncated}


def correct_note(config, state_root, note, turn, skill, assessment, *, deadline=None, wait_for_rate_limit=True):
    data, truncated = source_input(note, turn, skill=skill,
                                  assessment={key: assessment[key] for key in ('status', 'summary')})
    if truncated:
        return None
    candidates = validate_notes(model_response(config, state_root, prompt_runtime.load('correct', config), data,
                                               CORRECTION_SCHEMA, deadline=deadline, wait_for_rate_limit=wait_for_rate_limit,
                                               role='correct'),
                                {'verified_skills': [skill]})
    if len(candidates) > 1:
        raise ValueError('Too many correction candidates')
    if not candidates:
        return None
    candidate = candidates[0]
    text = '# ' + candidate['title'] + '\n' + '\n'.join(
        '- ' + key.title() + ': ' + candidate[key] for key in FIELDS if key != 'title') + '\n'
    checked = verify_note(config, state_root, text, turn, skill, correction_of=note, deadline=deadline,
                          wait_for_rate_limit=wait_for_rate_limit)
    if checked['status'] != 'supported' or checked['inputTruncated']:
        return None
    return {'note': candidate, **checked, 'generationInputSha256': hashlib.sha256(data.encode()).hexdigest()}


def unresolved(value):
    return value.get('correction') is None and (value['status'] != 'supported' or value.get('inputTruncated', False))


def count_assessment(counts, value):
    counts['current'] += 1
    counts[value['status']] += 1
    if value.get('correction') is not None:
        counts['corrected'] += 1
    elif unresolved(value):
        counts['unresolved'] += 1


def checker_fingerprint(config):
    digest = hashlib.sha256()
    for module, imported_hash in ((sys.modules[__name__], SOURCE_SHA256),
                                  (capture, capture.SOURCE_SHA256),
                                  (capture.claude_api, capture.claude_api.SOURCE_SHA256),
                                  (claude_cli, claude_cli.SOURCE_SHA256),
                                  (prompt_runtime, prompt_runtime.SOURCE_SHA256),
                                  (read_guidance, read_guidance.SOURCE_SHA256),
                                  (capture.activation_trace, capture.activation_trace.SOURCE_SHA256)):
        data = Path(module.__file__).read_bytes()
        if hashlib.sha256(data).hexdigest() != imported_hash:
            raise ValueError('Evidence checker source changed; restart with fresh imports')
        digest.update(data + b'\0')
    for role in ('extract', 'verify', 'correct'):
        digest.update(role.encode() + b'\0' + prompt_runtime.load(role, config).encode() + b'\0')
    return digest.hexdigest()


def load_backoff(path, checker_sha, now):
    """Per-note retry state, keyed by skill and note SHA-256. An entry written by another checker is void, so a
    changed checker makes every note due again; a changed note has a new key. No wait outlasts the cap, even one
    saved while the clock ran ahead."""
    safe_path(path)
    try:
        value = json.loads(path.read_text())
        entries = value['notes'] if value.get('version') == 1 and isinstance(value['notes'], dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        entries = {}
    valid = {}
    for key, entry in entries.items():
        try:
            if (entry['checker'] == checker_sha and entry['reason'] in BACKOFF_REASONS and type(entry['attempts']) is int
                    and entry['attempts'] >= 1):
                valid[key] = {**entry, 'retry_at': min(timestamp(entry['retry_at']), now + MAX_BACKOFF)}
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return valid


def stepped_backoff(entry, checker_sha, reason, now):
    """The next retry after an attempt that settled nothing; a different outcome than last time starts over."""
    attempts = entry['attempts'] + 1 if entry and entry['reason'] == reason else 1
    delay = min(MAX_BACKOFF, timedelta(hours=2 ** min(attempts - 1, 5)))
    return {'checker': checker_sha, 'reason': reason, 'attempts': attempts, 'retry_at': now + delay - BACKOFF_SLACK}


def save_backoff(path, entries):
    atomic(path, json.dumps({'version': 1, 'notes': {key: {**entry, 'retry_at': entry['retry_at'].isoformat()}
                                                     for key, entry in sorted(entries.items())}}, indent=2) + '\n')


def count_outcome(counts, reason, previous):
    """Count a note whose check settled nothing new, whether it ran now or is waiting for its retry."""
    if reason == 'error':
        counts['errors'] += 1
        counts['pending'] += 1
        return
    if reason in ('unavailable', 'ambiguous'):
        counts['unavailable'] += 1
        counts['ambiguous'] += reason == 'ambiguous'
    if previous:
        count_assessment(counts, previous)


def verify(config, state_root, note_sha=None, recheck=False, now=None):
    deadline = time.monotonic() + CYCLE_TIMEOUT
    now = now or datetime.now(timezone.utc)
    feedback = Path(config.get('feedback_dir', codex_home() / 'skill-feedback'))
    safe_path(feedback)
    targets = []
    recovery_notes = {}
    shared = None
    found = False
    counts = {'notes': 0, 'current': 0, 'checked': 0, 'pending': 0, 'unavailable': 0, 'errors': 0,
              'supported': 0, 'partial': 0, 'unsupported': 0, 'corrected': 0, 'unresolved': 0,
              'recovered': 0, 'ambiguous': 0, 'rate_limited': 0, 'rate_limit': None}
    checker_sha = checker_fingerprint(config)
    # A requested check runs at once and updates only its own note's schedule.
    backoff_path = Path(state_root) / BACKOFF
    backoff = load_backoff(backoff_path, checker_sha, now)
    schedule = {}
    for skill in SKILLS:
        for path in sorted((feedback / skill / 'notes').glob('*.md')):
            safe_path(path)
            note_bytes = path.read_bytes()
            # Preserve text-mode newline handling without reopening mutable bytes.
            note = note_bytes.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
            digest = hashlib.sha256(note_bytes).hexdigest()
            if note_sha and digest != note_sha:
                continue
            found = True
            counts['notes'] += 1
            directory = feedback / skill / 'verifications'
            safe_path(directory)
            records = {}
            for receipt in directory.glob(digest + '*.json'):
                safe_path(receipt)
                data = receipt.read_bytes()
                records[hashlib.sha256(data).hexdigest()] = parse_receipt(receipt.name, data, config['host_id'], skill)
            previous_sha, previous = latest_receipt(records)
            if (not previous and not recheck and not field(note, 'Thread') and not field(note, 'Turn') and
                    config.get('repo_path')):
                if shared is None:
                    shared = assessment_inventory(Path(config['repo_path']) / 'feedback')['latest']
                matches = [v for v in shared if v['skill'] == skill and v['noteSha256'] == digest]
                if matches and all(v['schema_version'] >= 2 and not unresolved(v) for v in matches):
                    count_assessment(counts, max(matches, key=lambda v: (timestamp(v['checked_at']), v['receiptSha256'])))
                    continue
            if previous and previous['schema_version'] >= 2 and not recheck and not unresolved(previous):
                count_assessment(counts, previous)
                continue
            key = skill + '/' + digest
            entry = backoff.get(key)
            if entry and entry['retry_at'] > now and not note_sha:
                schedule[key] = entry
                count_outcome(counts, entry['reason'], previous)
                continue
            targets.append((skill, note, digest, directory, previous_sha, previous))
            if not previous and (not field(note, 'Thread') or not field(note, 'Turn')):
                recovery_notes[digest] = {'skill': skill, 'name': path.name, 'text': note}
    if note_sha and not found:
        raise ValueError('Requested source note is unavailable on this host')
    if note_sha:
        # Other notes keep their entries; the requested note's entry is whatever this check leaves.
        schedule = {**{key: entry for key, entry in backoff.items() if not key.endswith('/' + note_sha)}, **schedule}
    if not targets:
        save_backoff(backoff_path, schedule)
        return counts
    # A check requested for one note is attempted at once; scheduled checks wait for a usage limit's reset.
    limit, wait = None, not note_sha
    sessions = Path(config.get('sessions_root', codex_home() / 'sessions'))
    archived = Path(config.get('archived_sessions_root', codex_home() / 'archived_sessions'))
    errors = []
    dates = [timestamp(config.get('capture_since', SINCE)),
             *(timestamp(previous['completed_at']) for *_, previous in targets if previous)]
    for item in recovery_notes.values():
        try:
            dates.append(timestamp(field(item['text'], 'Recorded')))
        except (ValueError, AttributeError):
            pass
    since = min(dates)
    # Matching a note without a recorded turn needs the note writes a full read finds; otherwise every unchanged
    # transcript is answered from the stored scan.
    cache = None if recovery_notes else ScanCache(Path(state_root) / 'evidence-scan.json', complete=True)
    turns = completed_turns([*sessions.rglob('*.jsonl'), *archived.rglob('*.jsonl')], since, errors,
                            eligible_only=False, source_notes=recovery_notes, cache=cache)
    turns += claude_turns(claude_projects(config).rglob('*.jsonl'), since, errors,
                          eligible_only=False, source_notes=recovery_notes, cache=cache)
    if cache:
        cache.save()
    by_id = {(turn['thread'], turn['turn']): turn for turn in turns}
    by_evidence = {turn['evidence_id']: turn for turn in turns}
    counts['errors'] += len(errors)
    by_thread = {}
    for item in turns:
        by_thread.setdefault(item['thread'], []).append(item)
    for skill, note, digest, directory, previous_sha, previous in targets:
        key = skill + '/' + digest

        def settled(reason):
            schedule[key] = stepped_backoff(backoff.get(key), checker_sha, reason, now)

        def deferred():
            if key in backoff:
                schedule[key] = backoff[key]
        thread = field(note, 'Thread') or (previous['thread'] if previous else '')
        turn_id = field(note, 'Turn') or (previous['turn'] if previous else '')
        recovered = False
        turn = by_id.get((thread, turn_id))
        if not turn:
            candidate = by_evidence.get(field(note, 'Capture evidence'))
            if candidate and (not thread or candidate['thread'] == thread) and (not turn_id or candidate['turn'] == turn_id):
                turn = candidate
                recovered = not thread or not turn_id
        if not turn and digest in recovery_notes:
            candidates = [item for item in turns if digest in item.get('created_notes', []) and
                          (not thread or item['thread'] == thread) and (not turn_id or item['turn'] == turn_id)]
            if len(candidates) == 1:
                turn, recovered = candidates[0], True
        if not turn:
            reason = 'ambiguous' if digest in recovery_notes and len(candidates) > 1 else 'unavailable'
            settled(reason)
            count_outcome(counts, reason, previous)
            continue
        follow_ups = sorted((item for item in by_thread[turn['thread']] if item['turn'] != turn['turn'] and
                            timestamp(item['completed_at']) > timestamp(turn['completed_at'])),
                           key=lambda item: (timestamp(item['completed_at']), item['turn']))
        turn = {**turn, 'follow_ups': follow_ups}
        fingerprint = evidence_fingerprint(turn)
        if (previous and not recheck and previous.get('checkerSha256') == checker_sha and
                previous.get('evidenceSha256') == fingerprint):
            settled('unresolved')
            count_assessment(counts, previous)
            continue
        if counts['checked'] >= 10 or time.monotonic() >= deadline:
            deferred()
            counts['pending'] += 1
            continue
        try:
            result = verify_note(config, state_root, note, turn, skill, deadline=deadline, wait_for_rate_limit=wait)
            value = {'schema_version': 4, 'noteSha256': digest, 'sourceMachine': config['host_id'],
                'skill': skill, 'thread': turn['thread'], 'turn': turn['turn'], 'sourceSha256': turn['source_sha256'],
                'completed_at': turn['completed_at'], 'checked_at': datetime.now(timezone.utc).isoformat(),
                'loadedReads': [item for item in turn['loaded_reads'] if item['path'].startswith(skill + '/')],
                'scope': FOLLOWUP_SCOPE if follow_ups else ORIGINAL_SCOPE,
                'checkerSha256': checker_sha, 'supersedes': previous_sha, 'correction': None,
                'evidenceSha256': fingerprint,
                'followUps': [turn_identity(item) for item in follow_ups], **result}
            if result['status'] != 'supported' and not result['inputTruncated']:
                value['correction'] = correct_note(config, state_root, note, turn, skill, result, deadline=deadline,
                                                   wait_for_rate_limit=wait)
            validate_receipt(value)
            data = json.dumps(value, indent=2) + '\n'
            receipt = directory / (digest + '--' + hashlib.sha256(data.encode()).hexdigest() + '.json')
            atomic(receipt, data, exclusive=True)
            counts['checked'] += 1
            counts['recovered'] += int(recovered)
            count_assessment(counts, value)
            if unresolved(value):
                settled('unresolved')
        except CaptureBudgetExpired:
            deferred()
            counts['pending'] += 1
        except RateLimited as error:
            deferred()
            limit, wait = error.limit, True
            counts['pending'] += 1
            counts['rate_limited'] += 1
            # A requested check that could not run is a failure of that request.
            counts['errors'] += int(bool(note_sha))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            settled('error')
            count_outcome(counts, 'error', None)
            if isinstance(error, ExtractorFailure) and len(counts.get('extractor_diagnostics', [])) < 10:
                counts.setdefault('extractor_diagnostics', []).append(error.diagnostics)
    if counts['rate_limited']:
        counts['rate_limit'] = capture.rate_limit_summary(limit, counts['rate_limited'], datetime.now(timezone.utc))
        if counts['rate_limit']['overdue'] and not note_sha:
            counts['errors'] += counts['rate_limited']
    save_backoff(backoff_path, schedule)
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path.home() / '.local/share/codex-skills/config.json')
    parser.add_argument('--note-sha', help='Verify a particular local note, including a case requested by the reviewer')
    parser.add_argument('--recheck', action='store_true', help='Append a new assessment of --note-sha, preserving prior receipts')
    parser.add_argument('--report', type=Path, help='Read-only effective assessments for a local or collected feedback directory')
    args = parser.parse_args()
    if args.note_sha and not re.fullmatch(r'[a-f0-9]{64}', args.note_sha):
        parser.error('Invalid note SHA-256')
    if args.recheck and not args.note_sha:
        parser.error('--recheck requires --note-sha')
    if args.report and (args.note_sha or args.recheck):
        parser.error('--report cannot be combined with verification options')
    root = args.config.expanduser().resolve().parent
    try:
        if args.report:
            print(json.dumps(assessment_inventory(args.report.expanduser().resolve()), indent=2))
            return 0
        config = json.loads(args.config.read_text())
        counts = verify(config, root, args.note_sha, args.recheck)
        status = {'finished_at': datetime.now(timezone.utc).isoformat(), 'success': counts['errors'] == 0, 'counts': counts}
        if counts['rate_limit'] and args.note_sha:
            status['error'] = ('The Claude usage limit rejected the requested source check; retry after '
                               + counts['rate_limit']['retry_at'] + '.')
        else:
            capture.report_rate_limit(status, counts['rate_limit'], 'source checks')
        capture.report_api_fallback(status, config, root)
        atomic(root / 'evidence-status.json', json.dumps(status, indent=2) + '\n')
        print(json.dumps(status))
        return 0 if status['success'] else 1
    except (OSError, ValueError, KeyError, TypeError):
        print('Source verification failed; source data and existing receipts are preserved.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
