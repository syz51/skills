#!/usr/bin/env python3
# /// script
# dependencies = ["pyyaml"]
# ///
"""Check a skill folder's SKILL.md frontmatter and placeholders; exit 1 with a message on the first problem."""
import re
import sys
from pathlib import Path

import yaml

ALLOWED_KEYS = frozenset({'name', 'description', 'license', 'allowed-tools', 'metadata'})
MAX_NAME = 64
MAX_DESCRIPTION = 1024
FRONTMATTER = re.compile(r'---\n(.*?)\n---[ \t]*(?:\n|\Z)', re.DOTALL)
FENCE = re.compile(r'[ \t]*(?:(?:[-+*]|\d+[.)])[ \t]+)?(`{3,}|~{3,})(.*)')
TODO_LINE = re.compile(r' {0,3}\[TODO:.*\][ \t]*')


def problem(folder):
    """Return a description of the first rule the skill violates, or None when it is valid."""
    entry = Path(folder) / 'SKILL.md'
    if not entry.is_file():
        return 'SKILL.md not found'
    text = entry.read_text()
    match = FRONTMATTER.match(text)
    if not match:
        return 'SKILL.md must start with YAML frontmatter between --- lines'
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as error:
        return 'Invalid YAML in frontmatter: ' + str(error)
    if not isinstance(data, dict):
        return 'Frontmatter must be a YAML mapping'
    unexpected = sorted(str(key) for key in data if key not in ALLOWED_KEYS)
    if unexpected:
        return ('Unexpected frontmatter key(s): ' + ', '.join(unexpected)
                + '; allowed keys are ' + ', '.join(sorted(ALLOWED_KEYS)))

    name = data.get('name')
    if not isinstance(name, str) or not name.strip():
        return "Frontmatter 'name' must be a non-empty string"
    name = name.strip()
    if not re.fullmatch(r'[a-z0-9-]+', name):
        return f"Name '{name}' must be hyphen-case: lowercase letters, digits and hyphens"
    if name.startswith('-') or name.endswith('-') or '--' in name:
        return f"Name '{name}' cannot start or end with a hyphen or contain consecutive hyphens"
    if len(name) > MAX_NAME:
        return f'Name has {len(name)} characters; the maximum is {MAX_NAME}'

    description = data.get('description')
    if not isinstance(description, str) or not description.strip():
        return "Frontmatter 'description' must be a non-empty string"
    description = description.strip()
    if description.startswith('[TODO:'):
        return 'Description is an unfinished TODO placeholder'
    if '<' in description or '>' in description:
        return 'Description cannot contain angle brackets (< or >)'
    if len(description) > MAX_DESCRIPTION:
        return f'Description has {len(description)} characters; the maximum is {MAX_DESCRIPTION}'

    # A TODO placeholder line in the instructions is unfinished work unless it sits inside a code example.
    open_fence = None
    for line in text[match.end():].splitlines():
        fence = FENCE.fullmatch(line)
        if fence:
            marker, info = fence.groups()
            if open_fence is None:
                open_fence = marker
            elif marker[0] == open_fence[0] and len(marker) >= len(open_fence) and not info.strip():
                open_fence = None
        elif open_fence is None and TODO_LINE.fullmatch(line):
            return 'Skill instructions contain an unfinished TODO placeholder'
    return None


def main(argv):
    if len(argv) != 1:
        print('Usage: validate_skill.py <skill-directory>')
        return 1
    message = problem(argv[0])
    print(message or 'Skill is valid.')
    return 1 if message else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
