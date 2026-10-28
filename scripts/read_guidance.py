#!/usr/bin/env python3
"""Return bounded guidance text with a fingerprint of exactly the returned content."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex

SKILLS = ('frontend-ui', 'content-research-writer', 'find-skills', 'tauri')
SKILL_PATTERN = '|'.join(map(re.escape, SKILLS))
PROTOCOL = 'codex-guidance-read/v1'
MAX_BYTES = 6000


def guidance_path(value):
    match = re.search(r'(?:^|/)((?:' + SKILL_PATTERN + r')/(?:SKILL\.md|references/[\w./-]+\.(?:md|json)))$', value)
    if not match or '..' in Path(match[1]).parts:
        raise ValueError('Expected a maintained skill or reference path')
    return match[1]


def shell_words(command):
    """Accept a single literal argv vector, without expansions or shell control."""
    command = command.strip()
    if '\n' in command or '\r' in command:
        raise ValueError('Not a literal shell command')
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ''
    words = list(lexer)
    if not words or any(not re.fullmatch(r'(?:~/)?[\w ./,:-]+', word) for word in words):
        raise ValueError('Not a literal shell argument')
    return words


def request(command):
    """Recognize a literal helper invocation; shell expressions are not evidence."""
    words = shell_words(command)
    if words[:3] == ['uv', 'run', '--script']:
        words = ['python', *words[3:]]
    elif words[:3] == ['uv', 'run', '--no-project']:
        words = words[3:]
    if len(words) not in (3, 5) or Path(words[0]).name not in ('python', 'python3'):
        raise ValueError('Not a guidance reader command')
    if words[1] != 'scripts/read_guidance.py' and not words[1].endswith('/codex-skills/repository/scripts/read_guidance.py'):
        raise ValueError('Not the shared guidance reader')
    start = 0
    if len(words) == 5:
        if words[3] != '--start' or not words[4].isdigit():
            raise ValueError('Invalid excerpt offset')
        start = int(words[4])
    return guidance_path(words[2]), start


def read(path, start=0):
    relative = guidance_path(str(path))
    if guidance_path(str(path.expanduser().resolve(strict=True))) != relative:
        raise ValueError('Guidance target differs from its requested path')
    content = path.expanduser().read_bytes().decode('utf-8')
    if start < 0 or start > len(content) or (start == len(content) and start != 0):
        raise ValueError('Excerpt offset is outside the guidance text')
    excerpt = content[start:].encode('utf-8')[:MAX_BYTES].decode('utf-8', errors='ignore')
    end = start + len(excerpt)
    return {'protocol': PROTOCOL, 'path': relative, 'start': start,
            'next_start': end if end < len(content) else None,
            'coverage': 'complete' if start == 0 and end == len(content) else 'partial',
            'sha256': hashlib.sha256(excerpt.encode('utf-8')).hexdigest(), 'content': excerpt}


def observed(record, expected, turn):
    if not isinstance(record, dict) or set(record) != {'protocol', 'path', 'start', 'next_start', 'coverage', 'sha256', 'content'}:
        raise ValueError('Invalid guidance envelope')
    if record['protocol'] != PROTOCOL or (record['path'], record['start']) != expected:
        raise ValueError('Guidance envelope differs from its command')
    content, start, following = record['content'], record['start'], record['next_start']
    if not isinstance(content, str) or len(content.encode('utf-8')) > MAX_BYTES or type(start) is not int or start < 0:
        raise ValueError('Invalid guidance content or offset')
    if not content and (start or following is not None):
        raise ValueError('Empty partial guidance read')
    if following is not None and (type(following) is not int or following != start + len(content) or following <= start):
        raise ValueError('Invalid continuation offset')
    coverage = 'complete' if start == 0 and following is None else 'partial'
    digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
    if record['coverage'] != coverage or record['sha256'] != digest:
        raise ValueError('Guidance content fingerprint differs')
    return {'path': record['path'], 'sha256': digest, 'coverage': coverage, 'read_turn': turn}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    parser.add_argument('--start', type=int, default=0, help='Character offset returned as next_start by the preceding read')
    args = parser.parse_args()
    try:
        print(json.dumps(read(args.path, args.start), ensure_ascii=False))
    except (OSError, ValueError) as error:
        parser.exit(1, 'Guidance read failed: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
