#!/usr/bin/env python3
"""Extract bounded, private feedback from completed local skill work; no chat writes."""
from datetime import datetime, timedelta, timezone
import argparse
import prompt_runtime
import ast
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import shlex
import subprocess
import sys
import tempfile
import time
import activation_trace
import claude_api
import claude_cli
from read_guidance import SKILL_PATTERN, SKILLS, guidance_path, observed, request, shell_words

SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

# Distinctive headings that identify each loaded SKILL.md besides its frontmatter name.
HEADINGS = {'frontend-ui': 'Frontend', 'content-research-writer': 'Content|Research', 'find-skills': 'Find Skills', 'tauri': 'Tauri', 'typescript': 'TypeScript', 'rust': 'Rust'}
SINCE = '2026-10-09T14:45:00+00:00'
# Claude Code records no turn-complete event; the newest finished reply waits this long for follow-on records.
CLAUDE_QUIET = timedelta(minutes=15)
MAX_RECORD_BYTES = 8_000_000
# Whole base64 strings carry no skill evidence; source hashes still use the original bytes.
INLINE_BASE64 = re.compile(rb'"(?:data:[\w.+-]+\\?/[\w.+-]+;base64,)?(?:[A-Za-z0-9+]|\\?/){4096,}={0,2}"')
MAX_INPUT = 48000
MAX_OUTPUT = 12000
MAX_TURNS = 20
MODEL_TIMEOUT = 240
CYCLE_TIMEOUT = 900
# A Claude usage limit defers model calls to its reset, or to the next hourly cycle when no reset is
# reported. Work that the limit has blocked for longer than the grace period fails capture and sources.
RATE_LIMIT_RETRY = timedelta(minutes=30)
RATE_LIMIT_GRACE = timedelta(hours=24)
# A rejection this soon after the recorded reset continues the same limit; a later one starts a new limit.
RATE_LIMIT_CONTINUES = timedelta(hours=2)
# CLI paths whose --help advertised every isolation control in this process.
VERIFIED_CLIS = set()
FIELDS = ('title', 'task', 'observation', 'evidence', 'cause', 'proposal')
SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['notes'], 'properties': {
    'notes': {'type': 'array', 'maxItems': 3, 'items': {'type': 'object', 'additionalProperties': False,
        'required': ['skill', *FIELDS], 'properties': {'skill': {'type': 'string', 'enum': list(SKILLS)},
            **{key: {'type': 'string', 'maxLength': 1000} for key in FIELDS}}}}}}
PROMPT = prompt_runtime.load('extract', root=prompt_runtime.DEFAULT_ROOT)


class CaptureBudgetExpired(RuntimeError):
    """The cycle ran out of time; evidence must remain eligible for the next run."""


class ExtractorFailure(RuntimeError):
    def __init__(self, reason, diagnostics):
        super().__init__(reason)
        self.reason = reason
        self.diagnostics = diagnostics


class RateLimited(ExtractorFailure):
    """The account's usage limit rejected a call. The work waits for the limit's reset; it is not a failed extraction."""
    def __init__(self, reason, diagnostics, limit):
        super().__init__(reason, diagnostics)
        self.limit = limit


def rate_limit_path(state_root, api=False):
    """The usage-limit record shared by every restricted call on one backend. The Claude API is billed apart
    from the Claude Code subscription, so each backend keeps its own record."""
    return Path(state_root) / ('rate-limit-api.json' if api else 'rate-limit.json')


def read_rate_limit(state_root, api=False):
    path = rate_limit_path(state_root, api)
    safe_path(path)
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    try:
        if (not isinstance(value, dict) or set(value) != {'since', 'retry_at', 'window'} or
                not all(isinstance(value[key], str) for key in ('since', 'retry_at')) or
                not (value['window'] is None or isinstance(value['window'], str))):
            raise ValueError
        timestamp(value['since'])
        timestamp(value['retry_at'])
    except ValueError:
        raise ValueError('Invalid rate limit state') from None
    return value


def record_rate_limit(state_root, facts, now, api=False):
    previous = read_rate_limit(state_root, api)
    continues = previous is not None and now - timestamp(previous['retry_at']) <= RATE_LIMIT_CONTINUES
    resets = facts.get('rate_limit_resets_at')
    reset = datetime.fromtimestamp(resets, timezone.utc) if resets is not None else None
    retry = max(reset, now) + timedelta(minutes=1) if reset and reset <= now + timedelta(days=8) else now + RATE_LIMIT_RETRY
    if continues:
        # A rejection that reports no reset never shortens a known wait.
        retry = max(retry, timestamp(previous['retry_at']))
    value = {'since': previous['since'] if continues else now.isoformat(), 'retry_at': retry.isoformat(),
             'window': facts.get('rate_limit_type') or (previous['window'] if continues else None)}
    atomic(rate_limit_path(state_root, api), json.dumps(value, indent=2) + '\n')
    return value


def rate_limit_summary(limit, count, now):
    """Status fields for work a usage limit deferred; overdue once it has blocked work past the grace period."""
    return {**limit, 'deferred': count, 'overdue': now - timestamp(limit['since']) > RATE_LIMIT_GRACE}


def report_rate_limit(status, limit, work):
    """A deferral within the grace period is a warning on a successful stage; past it, part of the error."""
    if not limit:
        return
    if limit['overdue']:
        status['error'] = ' '.join(filter(None, (status.get('error'),
                                                 f"The Claude usage limit has blocked {limit['deferred']} {work} for more than 24 hours.")))
    elif status['success']:
        status['warning'] = f"Claude usage limit reached; {limit['deferred']} {work} wait for its reset at {limit['retry_at']}."


def bounded_timeout(limit, deadline):
    remaining = limit if deadline is None else min(limit, deadline - time.monotonic())
    if remaining <= 0:
        raise CaptureBudgetExpired('Capture cycle budget exhausted')
    return remaining


def run_bounded(command, *, timeout, deadline=None, **kwargs):
    """Distinguish a helper failure from consuming the cycle's remaining time."""
    try:
        return subprocess.run(command, timeout=bounded_timeout(timeout, deadline), **kwargs)
    except subprocess.TimeoutExpired:
        bounded_timeout(timeout, deadline)
        raise


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Capture timestamps must include timezone')
    return result


# One pass, so a placeholder is never matched again. At one position the first
# alternative wins, so URLs and Git remotes are replaced before their user@host part
# can read as an email. Lookbehinds anchor each pattern at the start of its word, and
# no pattern backtracks more than once over a word, so the pass stays linear.
REDACTIONS = re.compile('|'.join([
    # A whole PEM or PGP private-key block, or everything after a header whose END is missing.
    r'(?P<credential>-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----'
    r'(?:[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|[\s\S]*)'
    # A bare, quoted, JSON-style or escaped-JSON assignment to a name containing a credential
    # word that ends there or at a separator or capital: token=…, API_KEY='…', "api_key": "…",
    # \"token\": \"…\", DB_PASSWORD=…, AWS_SECRET_ACCESS_KEY=… or githubTokenValue: …
    r'|(?P<quote>\\?["\']?)(?<![\w-])'
    r'(?=[\w-]*?(?i:api[_-]?key|token|passw(?:or)?d|secret|private[_-]?key)(?![a-z0-9]))[\w-]+(?P=quote)\s*[:=]\s*'
    r'(?:"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|\\"(?:[^"\\\n]|\\[^"\n])*\\"|(?:\\?["\'])?[^\s"\']+)'
    # A bearer token.
    r'|(?<![\w-])(?i:bearer)\s+[^\s"\']+'
    # Token formats the public-export scanner rejects.
    r'|\b(?:github_pat_[A-Za-z0-9_]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|sk-[A-Za-z0-9_-]{8,}|AKIA[A-Z0-9]{16})\b)',
    # Any scheme://… URL, credentials included, and scp-style remotes such as git@host:owner/repo.git.
    r'(?P<url>(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://[^\s<>"\']+|(?<![\w.+-])[\w.+-]+@[\w.-]+:(?=[\w~./-])[^\s<>"\']+)',
    r'(?P<email>(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})',
    # Pattern-based: a path containing a space is replaced only up to that space.
    r'(?P<path>/(?:Users|home)/[^\s<>"\']+)',
]))
PLACEHOLDERS = {'credential': '[redacted credential]', 'url': '[redacted URL]', 'email': '[redacted email]', 'path': '[local path]'}


def redact(text):
    """Replace credentials, URLs and Git remotes, email addresses and local paths before a model sees text."""
    return REDACTIONS.sub(lambda match: PLACEHOLDERS[next(name for name in PLACEHOLDERS if match[name] is not None)], text)


def text_content(payload):
    content = payload.get('content', [])
    return '\n'.join(item['text'] if isinstance(item.get('text'), str) else ''
                     for item in (content if isinstance(content, list) else []) if isinstance(item, dict))


def read_results(value):
    """Decode tool envelopes, without evaluating transcript code or consulting today's files."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            try:
                value = activation_trace.quiet_parse(ast.literal_eval, value)
            except (ValueError, SyntaxError, RecursionError):
                marker = re.search(r'Exit code: (\d+)\nOutput:\n', value)
                return [{'exit_code': int(marker[1]), 'output': value[marker.end():]}] if marker else []
    if isinstance(value, list):
        return [result for item in value for result in read_results(item)]
    if isinstance(value, dict):
        if isinstance(value.get('output'), str) and 'exit_code' in value:
            return [{**value, 'source_index': value['i']}] if type(value.get('i')) is int else [value]
        results = [result for key in ('text', 'value', 'result', 'content') if key in value
                   for result in read_results(value[key])]
        return [{**result, 'source_index': value['i']} for result in results] if type(value.get('i')) is int else results
    return []


def exec_arguments(raw, start=0):
    """Parse only flat literal exec options, never JavaScript expressions."""
    token = re.compile(r'\s*("(?:[^"\\]|\\[\s\S])*"|\x27(?:[^\x27\\]|\\[\s\S])*\x27|[A-Za-z_$][\w$]*|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|[{}:,])')
    def take(position):
        match = token.match(raw, position)
        if not match:
            raise ValueError('Not a literal exec argument')
        return match[1], match.end()
    opening, position = take(start)
    if opening != '{':
        raise ValueError('Expected literal exec options')
    values = {}
    key, position = take(position)
    while key != '}':
        if key[:1] in ('"', "'"):
            key = activation_trace.js_string(key)
        elif not re.fullmatch(r'[A-Za-z_$][\w$]*', key):
            raise ValueError('Computed exec option')
        if key in values or key == '__proto__':
            raise ValueError('Duplicate or inherited exec option')
        colon, position = take(position)
        if colon != ':':
            raise ValueError('Expected literal exec option value')
        value, position = take(position)
        if value[:1] in ('"', "'"):
            value = activation_trace.js_string(value)
        elif value in ('true', 'false', 'null'):
            value = {'true': True, 'false': False, 'null': None}[value]
        elif re.fullmatch(r'-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?', value):
            value = json.loads(value)
        else:
            raise ValueError('Nonliteral exec option value')
        values[key] = value
        separator, position = take(position)
        if separator == '}':
            break
        if separator != ',':
            raise ValueError('Invalid exec option separator')
        key, position = take(position)
    if not isinstance(values.get('cmd'), str):
        raise ValueError('Missing literal command')
    return values, position


def literal_batch(raw, commands):
    """Recognize literal tool calls without running any saved JavaScript."""
    batch = re.fullmatch(r'\s*(?:const|let)\s+(\w+)\s*=\s*await\s+Promise\.all(?:Settled)?\(\[([\s\S]*)\]\);\s*([\s\S]*?)\s*', raw)
    if not batch:
        return None
    body, position, actual = batch[2], 0, []
    try:
        while body[position:].strip():
            call = re.match(r'\s*tools\.exec_command\(', body[position:])
            if not call:
                return None
            arguments, position = exec_arguments(body, position + call.end())
            closing = re.match(r'\s*\)', body[position:])
            if not closing:
                return None
            position += closing.end()
            actual.append(arguments['cmd'])
            if body[position:].strip():
                separator = re.match(r'\s*,', body[position:])
                if not separator:
                    return None
                position += separator.end()
    except (ValueError, SyntaxError):
        return None
    if actual != commands:
        return None
    return batch[1], batch[3]


def indexed_output(variable, tail):
    variable = re.escape(variable)
    loop = r'for\s*\(\s*let\s+i\s*=\s*0\s*;\s*i\s*<\s*' + variable + r'\.length\s*;\s*i\+\+\s*\)\s*text\(\{\s*i\s*,\s*\.\.\.\s*' + variable + r'\[i\]\s*\}\);?'
    each = variable + r'\.forEach\(\(\s*(\w+)\s*,\s*i\s*\)\s*=>\s*text\(\{\s*i\s*,\s*(?:\.\.\.\s*\1|result\s*:\s*\1)\s*\}\)\);?'
    return bool(re.fullmatch(loop, tail) or re.fullmatch(each, tail))


def unchanged_reads(raw, commands):
    """Require direct tool output or a known wrapper that only carries results."""
    if len(commands) == 1:
        if raw.strip() == commands[0].strip():
            return True
        try:
            value = json.loads(raw)
            if isinstance(value, dict) and value.get('cmd') == commands[0]:
                return True
        except ValueError:
            pass
        direct = re.fullmatch(r'\s*text\(\s*await\s+tools\.exec_command\(([\s\S]*)\)\s*\)\s*;?\s*', raw)
        bound = re.fullmatch(r'\s*(?:const|let)\s+(\w+)\s*=\s*await\s+tools\.exec_command\(([\s\S]*)\)\s*;\s*text\(\s*\1\s*\)\s*;?\s*', raw)
        arguments = direct[1] if direct else bound[2] if bound else None
        if arguments is not None:
            try:
                value, end = exec_arguments(arguments)
                return not arguments[end:].strip() and [value['cmd']] == commands
            except (ValueError, SyntaxError):
                return False
    batch = literal_batch(raw, commands)
    if not batch:
        return False
    variable, tail = batch
    passthrough = re.escape(variable) + r'(?:\.reverse\(\))?\.forEach\(\s*text\s*\);?'
    return indexed_output(variable, tail) or bool(re.fullmatch(passthrough, tail))


def indexed_batch(raw, commands, results):
    """Legacy batch fingerprints additionally require explicit result indices."""
    batch = literal_batch(raw, commands)
    return bool(batch and indexed_output(*batch) and
                len(results) == len(commands) and {r.get('source_index') for r in results} == set(range(len(commands))))


def literal_commands(raw):
    commands = []
    try:
        command = json.loads(raw).get('cmd')
        if isinstance(command, str):
            commands = [command]
    except (ValueError, AttributeError):
        for match in re.finditer(r'\bcmd\s*:\s*("(?:[^"\\]|\\[\s\S])*"|\x27(?:[^\x27\\]|\\[\s\S])*\x27)', raw):
            try:
                commands.append(activation_trace.js_string(match[1]))
            except (ValueError, SyntaxError):
                return []  # An undecodable command stays unknown, as in exec_arguments.
    return commands


def template_matches(node, text, metadata_only=False):
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value == text
    if not isinstance(node, ast.JoinedStr) or len(node.values) > 40:
        return False
    literals = sum(len(item.value) for item in node.values if isinstance(item, ast.Constant))
    if metadata_only and literals < len(text) / 2:
        return False
    groups, position, previous_hole = {}, 0, False
    for index, item in enumerate(node.values):
        if isinstance(item, ast.Constant):
            if not text.startswith(item.value, position):
                return False
            position += len(item.value)
            previous_hole = not bool(item.value)
        else:
            if previous_hole or item.conversion != -1 or item.format_spec is not None:
                return False
            following = next((part.value for part in node.values[index + 1:] if isinstance(part, ast.Constant) and part.value), None)
            end = text.find(following, position) if following else len(text)
            value = text[position:end]
            if end < position or not 0 < len(value) <= 512 or '\n' in value:
                return False
            key = ast.dump(item.value)
            if key in groups and groups[key] != value:
                return False
            groups[key] = value
            position = end
            previous_hole = True
    if position != len(text):
        return False
    return not metadata_only or all(re.fullmatch(
        r'(?:[a-f0-9]{64}|\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d)|/(?:Users|home|tmp|var)/[^\n]+)', value)
        for value in groups.values())


def python_writes(statements, bindings=None, depth=0):
    """Inspect a small set of unconditional writes; never execute saved code."""
    if depth > 4:
        return
    bindings = dict(bindings or {})
    for statement in statements:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = statement.value
        elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            call = statement.value
            if isinstance(call.func, ast.Attribute) and call.func.attr == 'write_text' and len(call.args) == 1:
                yield call.func.value, call.args[0], bindings.copy()
        elif isinstance(statement, ast.For) and isinstance(statement.target, ast.Tuple) and isinstance(statement.iter, (ast.List, ast.Tuple)):
            names = statement.target.elts
            if len(statement.iter.elts) <= 16 and all(isinstance(name, ast.Name) for name in names):
                for values in statement.iter.elts:
                    if isinstance(values, ast.Tuple) and len(values.elts) == len(names):
                        local = {**bindings, **{name.id: value for name, value in zip(names, values.elts)}}
                        yield from python_writes(statement.body, local, depth + 1)
        elif isinstance(statement, ast.With) and len(statement.items) == 1:
            item = statement.items[0]
            call = item.context_expr
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == 'open' and
                    call.args and isinstance(call.args[0], ast.Constant) and call.args[0].value == 'x' and
                    isinstance(item.optional_vars, ast.Name)):
                for body in statement.body:
                    write = body.value if isinstance(body, ast.Expr) else None
                    if (isinstance(write, ast.Call) and isinstance(write.func, ast.Attribute) and write.func.attr == 'write' and
                            isinstance(write.func.value, ast.Name) and write.func.value.id == item.optional_vars.id and len(write.args) == 1):
                        yield call.func.value, write.args[0], bindings.copy()


def bound_node(node, bindings, depth=0):
    if depth > 20:
        return None
    if isinstance(node, ast.Name) and node.id in bindings:
        return bound_node(bindings[node.id], bindings, depth + 1)
    return node


def path_template(node, bindings, depth=0):
    if depth > 20:
        return None
    if isinstance(node, ast.Name):
        return path_template(bindings[node.id], bindings, depth + 1) if node.id in bindings else None
    if isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return node
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'Path' and len(node.args) == 1:
        return path_template(node.args[0], bindings, depth + 1)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left, right = path_template(node.left, bindings, depth + 1), path_template(node.right, bindings, depth + 1)
        if left is not None and right is not None:
            parts = (left.values if isinstance(left, ast.JoinedStr) else [left]) + [ast.Constant(value='/')] + (right.values if isinstance(right, ast.JoinedStr) else [right])
            return ast.JoinedStr(values=parts)
    return None


def atomic_writes(statements):
    """Recognize mkstemp → fdopen/write → link/retry → unlink, without running it."""
    bindings = {}
    same = lambda node, code: ast.dump(node) == ast.dump(ast.parse(code).body[0])
    for index, statement in enumerate(statements):
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target, call = statement.targets[0], statement.value
        if isinstance(target, ast.Name):
            bindings[target.id] = call
            continue
        if not (isinstance(target, ast.Tuple) and len(target.elts) == 2 and
                all(isinstance(n, ast.Name) for n in target.elts) and isinstance(call, ast.Call) and
                ast.unparse(call.func) == 'tempfile.mkstemp' and not call.args and
                {k.arg for k in call.keywords} == {'prefix', 'dir'}):
            continue
        fd, temporary = (n.id for n in target.elts)
        if fd == temporary or index + 1 >= len(statements):
            continue
        keywords = {k.arg: k.value for k in call.keywords}
        if not isinstance(keywords['prefix'], ast.Constant) or keywords['prefix'].value != '.tmp-':
            continue
        attempt = statements[index + 1]
        if not (isinstance(attempt, ast.Try) and len(attempt.body) == 2 and
                not attempt.handlers and not attempt.orelse and len(attempt.finalbody) == 1 and
                same(attempt.finalbody[0], f'os.unlink({temporary})')):
            continue
        opened, loop = attempt.body
        if not (isinstance(opened, ast.With) and len(opened.items) == 1 and len(opened.body) == 1):
            continue
        item, write = opened.items[0], opened.body[0]
        if not (isinstance(item.optional_vars, ast.Name) and
                same(ast.Expr(value=item.context_expr), f"os.fdopen({fd}, 'w')") and
                isinstance(write, ast.Expr) and isinstance(write.value, ast.Call) and
                isinstance(write.value.func, ast.Attribute) and
                ast.unparse(write.value.func) == item.optional_vars.id + '.write' and
                len(write.value.args) == 1 and not write.value.keywords):
            continue
        if not (isinstance(loop, ast.While) and isinstance(loop.test, ast.Constant) and loop.test.value is True and
                not loop.orelse and len(loop.body) == 2 and isinstance(loop.body[0], ast.Assign) and
                len(loop.body[0].targets) == 1 and isinstance(loop.body[0].targets[0], ast.Name)):
            continue
        destination = loop.body[0].targets[0].id
        if destination in (fd, temporary, item.optional_vars.id):
            continue
        expected = (f'try:\n os.link({temporary}, {destination})\n break\n'
                    'except FileExistsError:\n continue')
        if not same(loop.body[1], expected):
            continue
        yield keywords['dir'], loop.body[0].value, write.value.args[0], bindings.copy()


def atomic_metadata(node, bindings, note, depth=0):
    """Reconstruct only literal strings and recorded filename/body metadata, never Python code."""
    if depth > 20:
        return None
    resolve = lambda value: atomic_metadata(value, bindings, note, depth + 1)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return resolve(bindings[node.id]) if node.id in bindings else None
    if isinstance(node, ast.JoinedStr):
        parts = []
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                if part.conversion != -1 or part.format_spec is not None:
                    return None
                part = part.value
            value = resolve(part)
            if value is None:
                return None
            parts.append(value)
        return ''.join(parts) if sum(map(len, parts)) <= MAX_INPUT else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
        left, right = resolve(node.left), resolve(node.right)
        if left is not None and right is not None and len(left) + len(right) <= MAX_INPUT:
            return left + right if isinstance(node.op, ast.Add) else str(Path(left) / right)
        return None
    if not isinstance(node, ast.Call) or node.keywords:
        return None
    function = ast.unparse(node.func)
    if function == 'Path.home' and not node.args:
        return '/__source_home__'
    if function == 'Path' and len(node.args) == 1:
        return resolve(node.args[0])
    if function == 'socket.gethostname' and not node.args:
        machine = re.findall(r'^- Machine: ([A-Za-z0-9._-]{1,255})$', note['text'], re.M)
        return machine[0] if len(machine) == 1 else None
    if function == 'str' and len(node.args) == 1 and ast.unparse(node.args[0]) == 'uuid.uuid4()':
        suffix = re.search(r'-([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\.md$', note['name'])
        return suffix[1] if suffix else None
    if isinstance(node.func, ast.Attribute):
        base, method = node.func.value, node.func.attr
        if method == 'replace' and [ast.unparse(a) for a in node.args] == ["'/'", "'-'"]:
            value = resolve(base)
            return value.replace('/', '-') if value is not None else None
        clock = bound_node(base, bindings)
        if clock is not None and ast.unparse(clock) == 'datetime.datetime.now(datetime.timezone.utc)':
            recorded = re.findall(r'^- Recorded: (.+)$', note['text'], re.M)
            try:
                when = timestamp(recorded[0]) if len(recorded) == 1 else None
            except (ValueError, TypeError):
                return None
            if when is not None:
                if method == 'isoformat' and not node.args:
                    return when.isoformat()
                if method == 'strftime' and len(node.args) == 1 and ast.unparse(node.args[0]) == "'%Y%m%dT%H%M%SZ'":
                    return when.strftime('%Y%m%dT%H%M%SZ')
    return None


def creation_matches(raw, output, notes, recorded_at, tool_name='exec_command'):
    eligible = {}
    for digest, note in notes.items():
        recorded = re.findall(r'^- Recorded: (.+)$', note['text'], re.M)
        try:
            close = len(recorded) == 1 and abs((timestamp(recorded_at) - timestamp(recorded[0])).total_seconds()) <= 120
        except (ValueError, TypeError, AttributeError):
            close = False
        if close and len(note['text']) <= MAX_INPUT:
            eligible[digest] = note
    if not eligible:
        return []
    commands, results = literal_commands(raw), read_results(output)
    if (len(commands) != 1 or len(results) != 1 or type(results[0].get('exit_code')) is not int or
            results[0]['exit_code'] != 0 or re.search(r'(?i)(?:truncated output|output[^\n]*truncated|tokens truncated|lines omitted)', str(output))):
        return []
    try:
        if not isinstance(json.loads(raw), dict) or not re.search(r'(?:^|[._])exec_command$', tool_name):
            return []
    except ValueError:
        masked = re.sub(r'"(?:[^"\\]|\\[\s\S])*"|\x27(?:[^\x27\\]|\\[\s\S])*\x27', '""', raw)
        calls = re.findall(r'\b([\w.]+)\s*\(', masked)
        if calls.count('tools.exec_command') != 1 or any(call not in ('text', 'tools.exec_command') for call in calls):
            return []
        # The whole options object must be literal, so an expression such as "a" && "b" cannot borrow a command.
        if not unchanged_reads(raw, commands):
            return []
    command = commands[0]
    match = re.fullmatch(r"([^\n;&|`$]+)<<'([A-Za-z_][A-Za-z0-9_]*)'\n([\s\S]*)\n\2\s*", command)
    if not match:
        return []
    try:
        words = shlex.split(match[1])
    except ValueError:
        return []
    if len(words) < 2 or words[-1] != '-' or Path(words[-2]).name not in ('python', 'python3') or len(match[3]) > 262144:
        return []
    try:
        statements = activation_trace.quiet_parse(ast.parse, match[3]).body
    except (ValueError, SyntaxError, RecursionError):
        return []
    paths = []
    for line in results[0]['output'].splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            paths.extend(item for item in value.values() if isinstance(item, str) and item.startswith('/'))
    found = []
    # shortcut: inspect literal Python writes and metadata-only templates; extend when another saved writer format needs recovery.
    for target, content, bindings in python_writes(statements):
        target, content = path_template(target, bindings), bound_node(content, bindings)
        if target is None or content is None:
            continue
        for digest, note in eligible.items():
            if not template_matches(content, note['text'], metadata_only=True):
                continue
            candidates = [*paths]
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                candidates.append(target.value)
            if any(Path(path).parts[-3:] == (note['skill'], 'notes', note['name']) and template_matches(target, path) for path in candidates):
                found.append(digest)
    for folder, target, content, bindings in atomic_writes(statements):
        for digest, note in eligible.items():
            directory = atomic_metadata(folder, bindings, note)
            destination = atomic_metadata(target, bindings, note)
            if directory is None or destination is None:
                continue
            path = Path(destination)
            if (path.parent == Path(directory) and path.parts[-3:] == (note['skill'], 'notes', note['name']) and
                    atomic_metadata(content, bindings, note) == note['text']):
                found.append(digest)
    return sorted(set(found))


def literal_entrypoint(raw):
    """Identify one unchanged literal cat/sed request; substrings are not reads."""
    commands = literal_commands(raw)
    if not commands and re.match(r'^\s*(?:cat|sed)\s', raw):
        commands = [raw]
    if len(commands) != 1 or not unchanged_reads(raw, commands):
        return None
    try:
        words = shell_words(commands[0])
        valid = (len(words) == 2 and words[0] == 'cat') or (
            len(words) == 4 and words[:2] == ['sed', '-n'] and re.fullmatch(r'\d+,\d+p', words[2]))
        if not valid:
            return None
        path = guidance_path(words[-1])
        return path.split('/')[0] if path.endswith('/SKILL.md') else None
    except ValueError:
        return None


def loaded_reads(raw, output, turn):
    commands = literal_commands(raw)
    # A JavaScript literal may escape slashes, as in skills\/rust\/SKILL.md, so decoded commands count too.
    if not any(re.search(r'(?:' + SKILL_PATTERN + r')/', text) for text in (raw, *commands)):
        return []
    if not commands:
        try:
            request(raw)
            commands = [raw]
        except ValueError:
            if re.match(r'^\s*(?:cat|sed)\s', raw):
                commands = [raw]
    results = read_results(output)
    if not unchanged_reads(raw, commands):
        return []
    # Self-identifying envelopes bind each result to its literal request, even in an unordered batch.
    structured = []
    requests = []
    for command in commands:
        try:
            requests.append(request(command))
        except ValueError:
            requests.append(None)
    helper_only = all(expected is not None for expected in requests)
    mixed_indexed = not helper_only and indexed_batch(raw, commands, results)
    for index, expected in enumerate(requests):
        if expected is None:
            continue
        matching = []
        for result in results:
            if not helper_only and (not mixed_indexed or result.get('source_index') != index):
                continue
            if result.get('exit_code') != 0:
                continue
            try:
                matching.append(observed(json.loads(result['output']), expected, turn))
            except (ValueError, TypeError, KeyError):
                continue
        if len(matching) == 1:
            structured.extend(matching)
    # A verified envelope may itself explain truncation; only legacy reads rely on warning text.
    if re.search(r'(?i)(?:truncated output|output[^\n]*truncated|tokens truncated|lines omitted)', str(output)):
        return structured
    if len(commands) != 1 or len(results) != 1:
        if not indexed_batch(raw, commands, results):
            return structured
        results = sorted(results, key=lambda result: result['source_index'])
    reads = structured
    for command, result in zip(commands, results):
        try:
            words = shell_words(command)
        except ValueError:
            continue
        complete = len(words) == 2 and words[0] == 'cat'
        partial = len(words) == 4 and words[:2] == ['sed', '-n'] and re.fullmatch(r'\d+,\d+p', words[2])
        if (not (complete or partial) or result.get('exit_code') != 0
                or words[-1].startswith('-')):
            continue
        try:
            path = guidance_path(words[-1])
        except ValueError:
            continue
        content = result['output']
        if '\x00' in content or re.search(r'(?i)(?:output.*truncated|tokens truncated|lines omitted)', content):
            continue
        reads.append({'path': path, 'sha256': hashlib.sha256(content.encode()).hexdigest(),
                      'coverage': 'complete' if complete else 'partial', 'read_turn': turn})
    return reads


def parse_bytes(line):
    """Drop inline base64 payloads only from records that exceed the parsing limit."""
    return INLINE_BASE64.sub(b'"[inline base64 omitted]"', line) if len(line) > MAX_RECORD_BYTES else line


def completed_turns(paths, since, errors=None, eligible_only=True, source_notes=None):
    """Stream complete JSONL records; rescanning also handles appended/rollover files."""
    turns = {}
    for path in sorted(paths):
        meta, current, loaded, calls, reads, node_state = {}, None, set(), {}, [], {}
        with path.open('rb') as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.endswith(b'\n'):
                    break  # An actively appended final line is not yet a record.
                parse_line = parse_bytes(line)
                if len(parse_line) > MAX_RECORD_BYTES:
                    if errors is not None:
                        errors.append({'file': path.name[:200], 'line': line_number, 'reason': 'oversized rollout record'})
                    current = None
                    continue
                try:
                    record = json.loads(parse_line)
                except (ValueError, UnicodeError):
                    if errors is not None:
                        errors.append({'file': path.name[:200], 'line': line_number, 'reason': 'invalid rollout record'})
                    current = None
                    continue
                if not isinstance(record, dict) or not isinstance(record.get('payload', {}), dict):
                    if errors is not None:
                        errors.append({'file': path.name[:200], 'line': line_number, 'reason': 'invalid rollout object'})
                    current = None
                    continue
                payload = record.get('payload', {})
                kind = record.get('type')
                if kind == 'session_meta':
                    meta = payload
                    if 'subagent' in json.dumps(meta.get('source', '')) or meta.get('thread_source') == 'subagent':
                        break
                    continue
                if kind == 'event_msg' and payload.get('type') == 'task_started':
                    current = {'thread': meta.get('session_id') or meta.get('id'), 'turn': payload.get('turn_id'),
                               'messages': [], 'calls': [], 'verified_skills': [], 'loaded_hash': 'unknown'}
                    calls = {}
                    source_hash = hashlib.sha256()
                if current is None:
                    continue
                source_hash.update(line)
                if kind == 'event_msg' and payload.get('type') == 'user_message':
                    # Messages stay raw for local filters; message_context redacts them for models.
                    message = payload.get('message')
                    current['messages'].append({'role': 'user', 'text': message if isinstance(message, str) else ''})
                if kind == 'response_item':
                    item_type = payload.get('type')
                    if item_type in ('function_call', 'custom_tool_call'):
                        arguments = payload.get('arguments', payload.get('input', ''))
                        raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
                        name = payload.get('name', '')
                        skills = [s for s in SKILLS if re.search(r'(?<![\w.-])skills/' + re.escape(s) + r'/SKILL\.md\b', raw)]
                        calls[payload.get('call_id')] = (skills, name, raw)
                        current['calls'].append({'name': name})
                    elif item_type in ('function_call_output', 'custom_tool_call_output'):
                        skill_reads, name, raw = calls.get(payload.get('call_id'), ([], '', ''))
                        output = str(payload.get('output', ''))
                        if source_notes:
                            created = creation_matches(raw, payload.get('output', ''), source_notes, record.get('timestamp'), name)
                            if created:
                                current['created_notes'] = sorted(set(current.get('created_notes', [])) | set(created))
                        read = bool(re.search(r'\b(?:cat|sed|read_text|readFile|skills\.read|read_mcp_resource|read_guidance\.py)\b', raw))
                        successful = not re.search(r'(?i)(?:exit_code["\s:]+[1-9]|exit code: [1-9]|no such file|isError["\s:]+true)', output)
                        observed_reads = loaded_reads(raw, payload.get('output', ''), current['turn']) if read else []
                        if name == 'mcp__node_repl__reset' and successful:
                            node_state.clear()
                        if name == 'mcp__node_repl__js':
                            try:
                                arguments = json.loads(raw)
                                result = payload.get('output', '')
                                result = json.loads(result) if isinstance(result, str) else result
                                if not isinstance(result, dict) or result.get('isError') or result.get('error'):
                                    raise ValueError('Unsuccessful Node result')
                                text = '\n'.join(part['text'] for part in result.get('content', [])
                                                 if isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str))
                                observations = activation_trace.node_read_observations(arguments['code'], text, node_state)
                                if observations:
                                    read, successful = True, True
                                for observation in observations:
                                    path = guidance_path(observation['path'])
                                    content = observation['content']
                                    if '\x00' in content:
                                        continue
                                    observed_reads.append({'path': path, 'sha256': hashlib.sha256(content.encode()).hexdigest(),
                                                           'coverage': observation['coverage'], 'read_turn': current['turn']})
                            except (ValueError, TypeError, KeyError):
                                node_state.clear()
                        if read and (successful or observed_reads):
                            proven = {entry['path'].split('/')[0] for entry in observed_reads
                                      if entry['path'].endswith('/SKILL.md')}
                            # Old shell transcripts sometimes have no exact output envelope.
                            # Preserve eligibility only for one literal read and explicit success.
                            legacy = literal_entrypoint(raw)
                            if legacy and successful and re.match(r'^Exit code: 0\n', output):
                                proven.add(legacy)
                            loaded.update(skill for skill in proven if re.search(r'(?:name:\s*' + re.escape(skill) + r'\b|#\s*' + HEADINGS[skill] + ')', output, re.I))
                            for item in observed_reads:
                                if not any(all(previous[key] == item[key] for key in ('path', 'sha256', 'coverage')) for previous in reads):
                                    reads.append(item)
                        # Web/research output and file contents stay out of the model input.
                        if not skill_reads:
                            status = re.findall(r'(?:Exit code: |exit_code[\" :]+)([0-9]+)', output)
                            current['messages'].append({'role': 'tool', 'text': 'Tool returned; exit statuses: ' + ','.join(status[:8])})
                    elif item_type == 'message' and payload.get('role') == 'user':
                        text = text_content(payload)
                        if not text.startswith(('# AGENTS.md instructions', '<environment_context>')):
                            current['messages'].append({'role': 'user', 'text': text})
                    elif item_type == 'message' and payload.get('role') == 'assistant':
                        current['messages'].append({'role': 'assistant', 'text': text_content(payload)})
                if kind == 'event_msg' and payload.get('type') == 'task_complete':
                    finished = record.get('timestamp')
                    if payload.get('turn_id') != current['turn'] or not finished:
                        current = None
                        continue
                    current['completed_at'] = finished
                    current['verified_skills'] = sorted(loaded)
                    current['loaded_reads'] = list(reads)
                    current['source_sha256'] = source_hash.hexdigest()
                    keep_turn(turns, current, since, eligible_only)
                    current = None
    return sorted(turns.values(), key=lambda t: (t['completed_at'], t['evidence_id']))


def keep_turn(turns, current, since, eligible_only):
    thread, turn = current['thread'], current['turn']
    if not (thread and turn and (current['verified_skills'] or not eligible_only) and timestamp(current['completed_at']) >= since):
        return
    # Claude Code notification turns are judged by the latest typed prompt; Codex turns have no filter text.
    user = '\n'.join([m['text'] for m in current['messages'] if m['role'] == 'user'] + [current.pop('filter', '')])
    maintenance = re.search(r'(?i)(?:maintenance-prompt\.md|scripts/(?:jobs|capture|sync|monitor)\.py|skill-feedback/|capture-state\.json|synthetic (?:exercise|test|task)|test fixture|simulate (?:skill|work))', user)
    readonly = re.search(r'(?i)\b(?:read.only|do not (?:edit|change|write)|review only|planning only)\b', user)
    if eligible_only and (maintenance or readonly):
        return
    current['evidence_id'] = hashlib.sha256((thread + ':' + turn).encode()).hexdigest()
    key = (thread, turn)
    # Rollover copies can contain different amounts of context.
    if key not in turns or len(json.dumps(current)) > len(json.dumps(turns[key])):
        turns[key] = current


def claude_text(content):
    """Typed text of a Claude Code message, without harness reminders or notifications."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = '\n'.join(item['text'] if isinstance(item.get('text'), str) else '' for item in content
                         if isinstance(item, dict) and item.get('type') == 'text')
    else:
        return ''
    text = re.sub(r'<(system-reminder|task-notification)>[\s\S]*?</\1>', '', text)
    command = re.search(r'<command-name>(/[^<\n]+)</command-name>', text)
    if command:
        arguments = re.search(r'<command-args>([\s\S]*?)</command-args>', text)
        text = command[1] + (' ' + arguments[1] if arguments and arguments[1] else '')
    return text.strip()


def write_matches(result, notes, recorded_at):
    """Match a note written by Claude Code's Write tool, directly or as the temporary file it hard-links."""
    if not isinstance(result, dict) or result.get('type') != 'create' or not isinstance(result.get('filePath'), str):
        return []
    found = []
    for digest, note in notes.items():
        recorded = re.findall(r'^- Recorded: (.+)$', note['text'], re.M)
        try:
            close = len(recorded) == 1 and abs((timestamp(recorded_at) - timestamp(recorded[0])).total_seconds()) <= 120
        except (ValueError, TypeError, AttributeError):
            close = False
        if close and result.get('content') == note['text'] and Path(result['filePath']).parts[-3:-1] == (note['skill'], 'notes'):
            found.append(digest)
    return found


def claude_turns(paths, since, errors=None, eligible_only=True, source_notes=None, now=None):
    """Group Claude Code transcript records into completed prompt turns with the Codex turn shape."""
    now = now or datetime.now(timezone.utc)
    turns = {}
    for path in sorted(paths):
        if 'subagents' in path.parts:
            continue  # Subagent work is excluded, as in Codex capture.
        try:
            quiet = now - datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) >= CLAUDE_QUIET
        except OSError:
            continue
        # As in Codex capture, a skill read stays in effect for the rest of the session file.
        scan = {'finished': [], 'current': None, 'uses': {}, 'loaded': set(), 'reads': [], 'context': ''}
        with path.open('rb') as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.endswith(b'\n'):
                    break  # An actively appended final line is not yet a record.
                try:
                    parse_line = parse_bytes(line)
                    if len(parse_line) > MAX_RECORD_BYTES:
                        raise ValueError('oversized transcript record')
                    record = json.loads(parse_line)
                    if not isinstance(record, dict):
                        raise ValueError('invalid transcript object')
                    claude_record(scan, record, line, source_notes)
                except (ValueError, UnicodeError, TypeError, AttributeError, KeyError) as error:
                    if errors is not None:
                        reason = str(error) if str(error).startswith(('oversized', 'invalid transcript')) else 'invalid transcript record'
                        errors.append({'file': path.name[:200], 'line': line_number, 'reason': reason})
                    if scan['current']:
                        scan['current']['broken'] = True
        if scan['current']:
            scan['current']['last'] = True
            close_claude_turn(scan)
        for current in scan['finished']:
            # Only a real final reply completes a turn; the newest also waits for the file to go quiet.
            if current.pop('broken') or current.pop('stop') != 'end_turn' or (current.pop('last', False) and not quiet):
                continue
            try:
                keep_turn(turns, current, since, eligible_only)
            except (ValueError, TypeError, AttributeError):
                if errors is not None:
                    errors.append({'file': path.name[:200], 'line': 0, 'reason': 'invalid transcript timestamp'})
    return sorted(turns.values(), key=lambda t: (t['completed_at'], t['evidence_id']))


def close_claude_turn(scan):
    current = scan['current']
    current['verified_skills'] = sorted(scan['loaded'])
    current['loaded_reads'] = list(scan['reads'])
    current['source_sha256'] = current.pop('hash').hexdigest()
    scan['finished'].append(current)


def claude_record(scan, record, line, source_notes):
    kind, current = record.get('type'), scan['current']
    if kind == 'attachment' and current and isinstance(record.get('attachment'), dict):
        # Input typed while a turn runs is queued into that turn.
        queued = record['attachment']
        if queued.get('type') == 'queued_command' and queued.get('commandMode') != 'task-notification':
            text = claude_text(queued.get('prompt'))
            if text:
                current['hash'].update(line)
                current['messages'].append({'role': 'user', 'text': text})
                scan['context'] = current['filter'] = current['filter'] + '\n' + text
        return
    message = record.get('message')
    # Forked sessions copy earlier records, which stay attributed to their original session.
    if kind not in ('user', 'assistant') or record.get('isSidechain') or record.get('forkedFrom') or not isinstance(message, dict):
        return
    content = message.get('content')
    results = [item for item in content if isinstance(item, dict) and item.get('type') == 'tool_result'] if isinstance(content, list) else []
    prompt = record.get('promptId')
    if kind == 'user' and not results and not record.get('isMeta') and not record.get('isCompactSummary') and \
            isinstance(prompt, str) and (current is None or prompt != current['turn']):
        if current:
            close_claude_turn(scan)
        text = claude_text(content)
        if text:
            scan['context'] = text
        # Notification turns have no typed text; the latest prompt still decides read-only or maintenance scope.
        current = scan['current'] = {'thread': record.get('sessionId'), 'turn': prompt, 'messages': [], 'calls': [],
            'verified_skills': [], 'loaded_hash': 'unknown', 'loaded_reads': [], 'hash': hashlib.sha256(),
            'stop': None, 'completed_at': None, 'broken': False, 'filter': scan['context']}
        scan['uses'] = {}
    if current is None or (kind == 'user' and prompt != current['turn']):
        return  # Compaction summaries and local-command output belong to no prompt turn.
    current['hash'].update(line)
    if kind == 'assistant':
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'text' and isinstance(item.get('text'), str) and item['text'].strip():
                current['messages'].append({'role': 'assistant', 'text': item['text']})
            elif item.get('type') == 'tool_use':
                scan['uses'][item.get('id')] = (item.get('name', ''), item.get('input') if isinstance(item.get('input'), dict) else {})
                current['calls'].append({'name': item.get('name', '')})
        synthetic = message.get('model') == '<synthetic>' or record.get('isApiErrorMessage')
        current['stop'] = 'synthetic' if synthetic else message.get('stop_reason')
        if not isinstance(record.get('timestamp'), str):
            raise ValueError('invalid transcript timestamp')
        current['completed_at'] = record['timestamp']
    elif results:
        claude_result(scan, record, results, source_notes)
    elif record.get('isMeta'):
        # A block without text is skipped, so it cannot shift the injected skill's header.
        text = ('\n'.join(item['text'] for item in content if isinstance(item, dict) and isinstance(item.get('text'), str))
                if isinstance(content, list) else str(content or ''))
        injected = re.match(r'Base directory for this skill: ([^\n]+)\n\n', text)
        source = record.get('sourceToolUseID')
        skill = Path(injected[1].strip()).name if injected else None
        if skill in SKILLS and (source is None or scan['uses'].get(source, ('',))[0] == 'Skill'):
            guidance = text[injected.end():]
            marker = guidance.rfind('\n\n\nARGUMENTS: ')
            guidance = guidance[:marker] if marker >= 0 else guidance
            if guidance.strip():
                scan['loaded'].add(skill)
                # Injected skills omit frontmatter, so the hash covers an excerpt of SKILL.md.
                add_read(scan, {'path': skill + '/SKILL.md', 'sha256': hashlib.sha256(guidance.encode()).hexdigest(),
                                'coverage': 'partial', 'read_turn': current['turn']})
    elif not record.get('isCompactSummary'):
        text = claude_text(content)
        if text:
            current['messages'].append({'role': 'user', 'text': text})


def add_read(scan, entry):
    if not any(all(previous[key] == entry[key] for key in ('path', 'sha256', 'coverage')) for previous in scan['reads']):
        scan['reads'].append(entry)


def claude_result(scan, record, results, source_notes):
    """Record observed skill reads from one Claude Code tool result; tool contents stay out of messages."""
    current, result = scan['current'], record.get('toolUseResult')
    for item in results:
        name, arguments = scan['uses'].get(item.get('tool_use_id'), ('', {}))
        failed = bool(item.get('is_error'))
        output = item.get('content')
        output = output if isinstance(output, str) else '\n'.join(
            part.get('text', '') for part in output if isinstance(part, dict) and isinstance(part.get('text'), str)) if isinstance(output, list) else ''
        skill_read, created = False, []
        if name == 'Read' and not failed and isinstance(result, dict) and isinstance(result.get('file'), dict):
            read = result['file']
            try:
                path = guidance_path(str(read.get('filePath', '')))
            except ValueError:
                path = None
            if path and isinstance(read.get('content'), str) and '\x00' not in read['content']:
                skill_read = True
                complete = read.get('startLine') == 1 and read.get('numLines') == read.get('totalLines')
                add_read(scan, {'path': path, 'sha256': hashlib.sha256(read['content'].encode()).hexdigest(),
                                'coverage': 'complete' if complete else 'partial', 'read_turn': current['turn']})
                skill, relative = path.split('/', 1)
                if relative == 'SKILL.md' and re.search(r'(?:name:\s*' + re.escape(skill) + r'\b|#\s*' + HEADINGS[skill] + ')', read['content'], re.I):
                    scan['loaded'].add(skill)
        elif name == 'Bash' and isinstance(arguments.get('command'), str):
            command = arguments['command']
            raw, envelope = json.dumps({'cmd': command}), json.dumps({'exit_code': 1 if failed else 0, 'output': output})
            skills = [s for s in SKILLS if re.search(r'(?<![\w.-])skills/' + re.escape(s) + r'/SKILL\.md\b', command)]
            skill_read = bool(skills)
            if not failed and re.search(r'\b(?:cat|sed|read_text|read_guidance\.py)\b', command):
                entries = loaded_reads(raw, envelope, current['turn'])
                proven = {entry['path'].split('/')[0] for entry in entries
                          if entry['path'].endswith('/SKILL.md')}
                legacy = literal_entrypoint(command)
                if legacy:
                    proven.add(legacy)
                scan['loaded'].update(s for s in proven if re.search(r'(?:name:\s*' + re.escape(s) + r'\b|#\s*' + HEADINGS[s] + ')', output, re.I))
                # Claude Code trims and may wrap shell output, so only self-identifying guidance-reader envelopes are hashed.
                try:
                    request(command)
                except ValueError:
                    pass
                else:
                    skill_read = True
                    for entry in loaded_reads(raw, envelope, current['turn']):
                        add_read(scan, entry)
            if source_notes:
                created = creation_matches(raw, envelope, source_notes, record.get('timestamp'))
        elif name == 'Write' and source_notes and not failed:
            created = write_matches(result, source_notes, record.get('timestamp'))
        if created:
            current['created_notes'] = sorted(set(current.get('created_notes', [])) | set(created))
        if not skill_read:
            current['messages'].append({'role': 'tool', 'text': 'Tool returned; ' + ('error' if failed else 'success')})


def codex_home():
    # The user-selected home may be an alias; feedback paths below it still reject symlinks.
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser().resolve()


def claude_projects(config):
    return Path(config.get('claude_projects_root') or
                Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude') / 'projects').expanduser().resolve()


def safe_path(path):
    if any(p.is_symlink() for p in (path, *path.parents) if str(p) not in ('/tmp', '/var', '/etc')):
        raise ValueError('Capture paths must not contain user symlinks')


def atomic(path, data, exclusive=False):
    safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.capture-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_text() != data:
                    raise ValueError('Immutable capture note collision')
        else:
            os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_notes(value, turn):
    if not isinstance(value, dict) or set(value) != {'notes'} or not isinstance(value['notes'], list) or len(value['notes']) > 3:
        raise ValueError('Invalid capture output')
    for note in value['notes']:
        if not isinstance(note, dict) or set(note) != {'skill', *FIELDS} or note['skill'] not in turn['verified_skills']:
            raise ValueError('Invalid capture note scope')
        if any(not isinstance(note[k], str) or not note[k].strip() or len(note[k]) > 1000 or '\n' in note[k] for k in FIELDS):
            raise ValueError('Invalid capture note field')
    return value['notes']


def validate_summary(summary, skill='frontend-ui'):
    """Source-check summaries are committed as a note field, so the same field rules apply."""
    note = {'skill': skill, **{key: 'Source evidence assessment' for key in FIELDS}, 'evidence': summary}
    validate_notes({'notes': [note]}, {'verified_skills': [skill]})


def message_context(messages, compact=False):
    """The model-facing view of turn messages: each text is redacted in full before any bound applies."""
    indexed = [(i, m) for i, m in enumerate(messages) if m['role'] in ('user', 'assistant')]
    selected = indexed
    if compact:
        keep = set()
        for role, limit in (('user', 6), ('assistant', 2)):
            keep.update(i for i, _ in [(i, m) for i, m in indexed if m['role'] == role][-limit:])
        selected = [(i, m) for i, m in indexed if i in keep]
    included = {i for i, _ in selected}
    omitted = [i for i, _ in indexed if i not in included]
    result = []
    for i, m in selected:
        text = redact(m['text'])
        result.append({'role': m['role'], 'text': text[:2000] if compact else text,
                       'source_index': i, 'original_chars': len(text)})
    return {'messages': result, 'omitted_message_indices': omitted,
            'truncated': bool(omitted) or any(len(m['text']) < m['original_chars'] for m in result)}


def extract(config, state_root, turn, existing, *, deadline=None, wait_for_rate_limit=True):
    relevant = []
    for skill in turn['verified_skills']:
        matching = sorted((note for note in existing if note['skill'] == skill), key=lambda note: note.get('order', 0))
        relevant.extend(matching[-max(2, 8 // len(turn['verified_skills'])):])
    existing = relevant
    compact = dict(turn)
    compact.update(message_context(turn.get('messages', [])))
    compact['calls'] = turn.get('calls', [])[-60:]
    compact['truncated'] |= len(compact['calls']) != len(turn.get('calls', []))
    # The existing notes come first: consecutive turns of the same skills share them, so the API caches them as a
    # prefix. The text is the JSON of {'existing_notes': ..., 'turn': ...}, the order the prompt evaluations use.
    def encode(notes):
        head = '{"existing_notes": ' + json.dumps(notes, ensure_ascii=False) + ', '
        return head + '"turn": ' + json.dumps(compact, ensure_ascii=False) + '}', len(head) if notes else None
    data, prefix = encode(existing)
    if len(data) > MAX_INPUT:
        compact.update(message_context(turn.get('messages', []), compact=True))
        compact['truncated'] = True
        data, prefix = encode([note for skill in turn['verified_skills'] for note in [n for n in existing if n['skill'] == skill][-2:]])
    if len(data) > MAX_INPUT:
        raise ValueError('Capture input exceeds safety bound')
    return validate_notes(model_response(config, state_root, prompt_runtime.load('extract', config), data, SCHEMA,
                                         deadline=deadline, wait_for_rate_limit=wait_for_rate_limit, role='extract',
                                         cache_prefix=prefix), turn)


def run_extractor(command, *, env, input, timeout, cwd=None):
    """Reap the isolated CLI and its children together, including inherited output pipes."""
    with subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            expired = subprocess.TimeoutExpired('restricted extractor', timeout, output=stdout, stderr=stderr)
            # The CLI may have exited on its own while a child held the pipes open; keep its status.
            expired.returncode = process.returncode
            raise expired from None
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def verify_cli(cli, *, deadline=None):
    """Fail closed before a private prompt reaches a CLI that cannot enforce every isolation control.

    Verification is keyed by the executable's identity, so a CLI replaced in place is checked again.
    """
    status = os.stat(cli)
    identity = (cli, status.st_ino, status.st_size, status.st_mtime_ns)
    if identity in VERIFIED_CLIS:
        return
    if claude_cli.missing_flags(cli_text(cli, '--help', deadline=deadline), claude_cli.RESTRICTED_FLAGS):
        raise RuntimeError('Capture CLI required control is unavailable')
    VERIFIED_CLIS.add(identity)


def usable(output):
    return isinstance(output, dict) and len(json.dumps(output, ensure_ascii=False)) <= MAX_OUTPUT


def model_response(config, state_root, prompt, data, response_schema, *, deadline=None, wait_for_rate_limit=True,
                   role=None, cache_prefix=None):
    """Run one restricted call on the role's model: through the Claude API when the configuration names a key
    file, otherwise through the Claude Code CLI. Until a recorded usage limit's reset, raise RateLimited
    without calling, unless the caller asked for an immediate retry."""
    model = claude_cli.ROLE_MODELS.get(role, claude_cli.RESTRICTED_MODEL)
    api = bool(config.get('api_key_file'))
    limit = read_rate_limit(state_root, api)
    if wait_for_rate_limit and limit and datetime.now(timezone.utc) < timestamp(limit['retry_at']):
        raise RateLimited('extractor rate limit failure', {'phase': 'startup', 'failure_class': 'rate limit', 'attempts': 0},
                          limit)
    with tempfile.TemporaryDirectory(prefix='.capture-model-', dir=state_root) as folder:
        bounded_timeout(MODEL_TIMEOUT, deadline)
        if api:
            command, child_env = claude_api.command(config), claude_api.environment(config)
        else:
            cli = str(claude_cli.cli_path(config))
            verify_cli(cli, deadline=deadline)
            command = claude_cli.restricted_command(cli, response_schema, model=model)
            child_env = claude_cli.child_env()
        for attempt in range(1, 3):
            timeout = bounded_timeout(MODEL_TIMEOUT, deadline)
            began = time.monotonic()
            stdin = (claude_api.request(model, claude_cli.EFFORT, prompt, data, response_schema, timeout, cache_prefix) if api
                     else prompt + '\n' + data)
            try:
                result = run_extractor(command, env=child_env, input=stdin, timeout=timeout, cwd=folder)
                stdout, stderr, exit_code, expired = result.stdout, result.stderr, result.returncode, False
            except subprocess.TimeoutExpired as error:
                stdout, stderr, exit_code, expired = error.output, error.stderr, getattr(error, 'returncode', None), True
            if api:
                phase, output, category, facts, usage = claude_api.outcome(decoded(stdout))
            else:
                # A crashed or killed CLI can stop mid-line; keep what it wrote so the failure stays diagnosable.
                phase, output, event_errors, facts = claude_cli.structured_run(stdout, partial=expired or exit_code != 0)
                category, usage = claude_cli.failure_class(decoded(stderr) + '\n' + event_errors), None
            # A leftover child can hold the pipes open after the call answered; keep the finished answer.
            if phase == 'completed' and usable(output) and (expired or exit_code == 0):
                rate_limit_path(state_root, api).unlink(missing_ok=True)
                if usage:
                    print(json.dumps({'event': 'model_call', 'role': role or 'review', 'model': model, 'usage': usage}),
                          file=sys.stderr, flush=True)
                return output
            category = category or 'CLI or output'
            if expired and category == 'CLI or output':
                category = 'timeout'
            diagnostics = {'elapsed_seconds': round(time.monotonic() - began, 3), 'phase': phase,
                           'failure_class': category, 'attempts': attempt, 'exit_code': exit_code, **facts}
            # Keep recovered failures diagnosable without logging CLI or source text.
            print(json.dumps({'event': 'extractor_attempt_failed', **diagnostics}), file=sys.stderr, flush=True)
            # Only a structured signal, never matched text alone, proves the account's usage limit. It is
            # recorded even when the cycle's budget is gone, since it applies to every later call.
            limited = category == 'rate limit' and (facts['api_error_status'] == 429 or facts['rate_limit_type'] is not None
                                                    or facts['rate_limit_resets_at'] is not None)
            if limited:
                limit = record_rate_limit(state_root, facts, datetime.now(timezone.utc), api)
            # Deadline exhaustion is scheduling, not a failed assessment. Never settle its evidence.
            bounded_timeout(MODEL_TIMEOUT, deadline)
            if attempt == 1 and category in ('timeout', 'connection'):
                continue
            reason = 'extractor timeout' if category == 'timeout' else 'extractor ' + category + ' failure'
            if limited:
                raise RateLimited(reason, diagnostics, limit)
            raise ExtractorFailure(reason, diagnostics)


def decoded(value):
    return value.decode(errors='replace') if isinstance(value, bytes) else value or ''


def cli_text(cli, *args, deadline=None):
    result = run_bounded([cli, *args], env={**claude_cli.child_env(), 'LC_ALL': 'C'},
                         capture_output=True, text=True, timeout=10, deadline=deadline)
    if result.returncode or len(result.stdout) > 100000:
        raise RuntimeError('Capture CLI capability discovery failed')
    return result.stdout


def cli_version(config):
    """Bounded diagnostic metadata; version checks never gate capture."""
    try:
        result = subprocess.run([str(claude_cli.cli_path(config)), '--version'], env=claude_cli.child_env(),
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return 'unknown'
    if result.returncode or len(result.stdout) > 256:
        return 'unknown'
    version = result.stdout.strip()
    return version if re.fullmatch(r'[0-9][0-9A-Za-z._+-]* \(Claude Code\)', version) else 'unknown'


def failure_reason(error):
    """Only static labels leave the private extractor boundary."""
    if isinstance(error, ExtractorFailure):
        return error.reason
    if isinstance(error, subprocess.TimeoutExpired):
        return 'extractor timeout'
    if isinstance(error, json.JSONDecodeError):
        return 'invalid model JSON'
    if isinstance(error, OSError):
        return 'local input/output failure'
    if isinstance(error, KeyError):
        return 'invalid capture state'
    reasons = {
        'Invalid capture output': 'model schema rejected',
        'Invalid capture note field': 'model schema rejected',
        'Invalid capture note scope': 'model scope rejected',
        'Invalid capture transaction': 'invalid capture state',
        'Invalid rate limit state': 'invalid rate limit state',
        'Claude API key file is unavailable': 'API key file unavailable',
        'Claude API key file must be readable only by its owner': 'API key file permissions too open',
        'Claude API helper requires uv': 'uv unavailable for the API helper',
        'Invalid capture host configuration': 'invalid host configuration',
        'Capture paths must not contain user symlinks': 'unsafe local path',
        'Capture input exceeds safety bound': 'input bound exceeded',
        'Claude event output exceeds safety bound': 'output bound exceeded',
        'Invalid Claude output': 'extractor CLI or output failure',
        'Capture model attempted tool execution': 'model tool event rejected',
        'Feedback extraction failed; completed turn remains pending': 'extractor CLI or output failure',
        'Capture CLI capability discovery failed': 'CLI capability discovery failed',
        'Capture CLI required control is unavailable': 'CLI safety control unavailable',
        'Claude session exposed tools': 'CLI isolation verification exposed tools',
    }
    return reasons.get(str(error), 'model or schema extraction failure')


def capture(config, state_root, *, now=None, retry_now=False):
    deadline = time.monotonic() + CYCLE_TIMEOUT
    state_root = Path(state_root)
    safe_path(state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    sessions = Path(config.get('sessions_root', codex_home() / 'sessions'))
    archived = Path(config.get('archived_sessions_root', codex_home() / 'archived_sessions'))
    feedback = Path(config.get('feedback_dir', codex_home() / 'skill-feedback'))
    install = Path(config.get('install_root', Path.home() / '.local/share/codex-skills'))
    safe_path(feedback)
    state_path = state_root / 'capture-state.json'
    safe_path(state_path)
    state = json.loads(state_path.read_text()) if state_path.exists() else {'processed': {}, 'pending': {}}
    if not isinstance(state, dict):
        raise ValueError('Invalid capture state')
    state.setdefault('pending', {})
    state.setdefault('deferred', {})
    if not isinstance(state, dict) or any(not isinstance(state.get(key), dict) for key in ('processed', 'pending', 'deferred')):
        raise ValueError('Invalid capture state')
    notes = []
    recovered = {}
    for skill in SKILLS:
        safe_path(feedback / skill / 'notes')
        for path in sorted((feedback / skill / 'notes').glob('*.md')):
            safe_path(path)
            content = path.read_text()
            notes.append({'skill': skill, 'text': redact(content)[:1500], 'order': path.stat().st_mtime_ns, 'hash': (re.findall(r'^- Lesson hash: ([a-f0-9]{64})$', content, re.M) or [None])[0]})
            for evidence in re.findall(r'^- Capture evidence: ([a-f0-9]{64})$', content, re.M):
                parts = re.findall(r'^- Capture lesson: ([0-9]+)/([0-9]+)$', content, re.M)
                if parts:
                    index, total = map(int, parts[0])
                    recovered.setdefault(evidence, {}).setdefault(total, set()).add(index)
    for evidence, counts in recovered.items():
        if any(total > 0 and indices == set(range(1, total + 1)) for total, indices in counts.items()):
            state['processed'].setdefault(evidence, {'recovered': True})
    obsolete = set(state['processed']) & (set(state['pending']) | set(state['deferred']))
    for evidence in obsolete:
        state['pending'].pop(evidence, None)
        state['deferred'].pop(evidence, None)
    if obsolete:
        atomic(state_path, json.dumps(state, indent=2) + '\n')
    deployed = 'unavailable'
    manifest = install / 'current/manifest.json'
    if manifest.is_file():
        revision = json.loads(manifest.read_text()).get('commit')
        if isinstance(revision, str) and re.fullmatch(r'[a-f0-9]{40,64}', revision):
            deployed = revision
    scan_errors = []
    since = timestamp(config.get('capture_since', SINCE))
    turns = sorted(completed_turns([*sessions.rglob('*.jsonl'), *archived.rglob('*.jsonl')], since, scan_errors) +
                   claude_turns(claude_projects(config).rglob('*.jsonl'), since, scan_errors, now=now),
                   key=lambda t: (t['completed_at'], t['evidence_id']))
    pending = [turn for turn in turns if turn['evidence_id'] not in state['processed']]
    # A moved or temporarily unavailable source must not disappear from job health.
    unavailable = ((set(state['pending']) | set(state['deferred'])) - set(state['processed']) -
                   {turn['evidence_id'] for turn in turns})
    written = 0
    machine = re.sub(r'[^A-Za-z0-9.-]', '-', socket.gethostname())
    processed = 0
    errors = len(scan_errors) + len(unavailable)
    attempted = 0
    rate_limited = 0
    limit = None
    # --retry-now makes one call despite a recorded limit; after a new rejection the rest wait.
    wait = not retry_now
    for turn in pending:
        if time.monotonic() >= deadline or attempted >= MAX_TURNS:
            break
        evidence = turn['evidence_id']
        deferred = state['deferred'].get(evidence, {})
        clock = now or datetime.now(timezone.utc)
        # A rate-limited turn waits on the shared usage-limit record, not on a backoff of its own.
        if (not retry_now and deferred.get('retry_at') and not deferred.get('rate_limited')
                and clock < timestamp(deferred['retry_at'])):
            errors += 1
            continue
        attempted += 1
        try:
            if evidence not in state['pending']:
                lessons = extract(config, state_root, turn, notes, deadline=deadline, wait_for_rate_limit=wait)
                state['pending'][evidence] = {'lessons': lessons, 'deployed': deployed, 'machine': machine, 'recorded_at': clock.isoformat()}
                atomic(state_path, json.dumps(state, indent=2) + '\n')
            transaction = state['pending'][evidence]
            if not isinstance(transaction, dict) or set(transaction) != {'lessons', 'deployed', 'machine', 'recorded_at'} or not isinstance(transaction['machine'], str) or not re.fullmatch(r'[A-Za-z0-9.-]+', transaction['machine']) or not isinstance(transaction['deployed'], str) or not re.fullmatch(r'(?:unavailable|[a-f0-9]{40,64})', transaction['deployed']):
                raise ValueError('Invalid capture transaction')
            timestamp(transaction['recorded_at'])
            lessons = validate_notes({'notes': transaction['lessons']}, turn)
        except CaptureBudgetExpired:
            break
        except RateLimited as error:
            limit, wait = error.limit, True
            rate_limited += 1
            # Retained like other deferred work, so a source that disappears meanwhile stays a visible error,
            # but without a failure backoff: the turn is not at fault.
            entry = {'attempts': deferred.get('attempts', 0), 'reason': failure_reason(error),
                     'completed_at': turn['completed_at'], 'retry_at': limit['retry_at'], 'rate_limited': True}
            if deferred != entry:
                state['deferred'][evidence] = entry
                atomic(state_path, json.dumps(state, indent=2) + '\n')
            continue
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
            errors += 1
            attempts = deferred.get('attempts', 0) + 1
            state['deferred'][evidence] = {'attempts': attempts, 'reason': failure_reason(error),
                'completed_at': turn['completed_at'],
                'retry_at': (clock + timedelta(hours=min(24, 2 ** min(attempts - 1, 5)))).isoformat()}
            if isinstance(error, ExtractorFailure):
                state['deferred'][evidence]['diagnostics'] = error.diagnostics
            atomic(state_path, json.dumps(state, indent=2) + '\n')
            continue
        for index, lesson in enumerate(lessons, 1):
            content_hash = hashlib.sha256(json.dumps(lesson, sort_keys=True).encode()).hexdigest()
            # Content identity catches equivalent output on state-loss retries.
            if any(n.get('hash') == content_hash for n in notes):
                continue
            content = '\n'.join([f"# {lesson['title']}", f"- Recorded: {transaction['recorded_at']}", f"- Completed: {turn['completed_at']}",
                f"- Machine: {transaction['machine']}", f"- Thread: {turn['thread']}", f"- Turn: {turn['turn']}",
                f"- Skill: {lesson['skill']}; deployed-at-capture {transaction['deployed']}; loaded " + ('observed content hashes below' if any(item['path'].startswith(lesson['skill'] + '/') for item in turn['loaded_reads']) else 'unknown'),
                '- Loaded reads: ' + json.dumps([item for item in turn['loaded_reads'] if item['path'].startswith(lesson['skill'] + '/')], sort_keys=True),
                f"- Capture evidence: {turn['evidence_id']}", f'- Capture lesson: {index}/{len(lessons)}', f'- Lesson hash: {content_hash}',
                *[f'- {key.title()}: {lesson[key]}' for key in FIELDS if key != 'title'], ''])
            filename = transaction['recorded_at'].replace(':', '').replace('+', '-') + '-' + transaction['machine'] + '-' + content_hash[:24] + '.md'
            atomic(feedback / lesson['skill'] / 'notes' / filename, content, exclusive=True)
            notes.append({'skill': lesson['skill'], 'text': redact(content)[:1500], 'hash': content_hash, 'order': time.time_ns()})
            written += 1
        processed += 1
        state['processed'][turn['evidence_id']] = {'completed_at': turn['completed_at'], 'notes': len(lessons)}
        del state['pending'][evidence]
        state['deferred'].pop(evidence, None)
        atomic(state_path, json.dumps(state, indent=2) + '\n')
    remaining = [turn for turn in pending if turn['evidence_id'] not in state['processed']]
    pending_dates = [turn['completed_at'] for turn in remaining]
    for evidence in unavailable:
        entry = state['deferred'].get(evidence, {})
        completed = entry.get('completed_at') if isinstance(entry, dict) else None
        if isinstance(completed, str):
            try:
                timestamp(completed)
            except ValueError:
                continue
            pending_dates.append(completed)
    rate_limit = rate_limit_summary(limit, rate_limited, now or datetime.now(timezone.utc)) if rate_limited else None
    if rate_limit and rate_limit['overdue']:
        errors += rate_limited
    summary = {'eligible': len(pending), 'processed': processed, 'notes': written,
               'pending': len(remaining) + len(unavailable), 'errors': errors,
               'rate_limited': rate_limited, 'rate_limit': rate_limit,
               'oldest_pending_at': min(pending_dates, key=timestamp) if pending_dates else None,
               'unavailable_sources': len(unavailable),
               'scanner_error_count': len(scan_errors), 'scanner_diagnostics': scan_errors[:10]}
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path.home() / '.local/share/codex-skills/config.json')
    parser.add_argument('--retry-now', action='store_true',
                        help='Retry deferred evidence now, retaining all validation and cycle limits')
    args = parser.parse_args()
    root = args.config.expanduser().resolve().parent
    status_path = root / 'capture-status.json'
    now = datetime.now(timezone.utc).isoformat()
    try:
        safe_path(root)
        root.mkdir(parents=True, exist_ok=True)
        safe_path(root / 'capture.lock')
        with (root / 'capture.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            old = json.loads(status_path.read_text()) if status_path.exists() else {}
            status = {key: old[key] for key in ('last_success_finished_at', 'last_failure_finished_at') if key in old}
            status.update(phase='running', success=None, started_at=now)
            atomic(status_path, json.dumps(status) + '\n')
            try:
                config = json.loads(args.config.read_text())
                if config.get('host_id') not in ('client-host', 'maintainer-host') or not claude_cli.cli_path(config).is_file():
                    raise ValueError('Invalid capture host configuration')
                status['cli_version'] = cli_version(config)
                status['counts'] = capture(config, root, retry_now=args.retry_now)
                status['success'] = status['counts']['errors'] == 0
                if not status['success']:
                    status['error'] = 'Capture has deferred evidence; source remains pending.'
                report_rate_limit(status, status['counts']['rate_limit'], 'turns')
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
                status.update(success=False, error='Capture failed: ' + failure_reason(error) + '. Source evidence remains pending.')
            finished = datetime.now(timezone.utc).isoformat()
            status.update(phase='finished', finished_at=finished)
            status['last_success_finished_at' if status['success'] else 'last_failure_finished_at'] = finished
            atomic(status_path, json.dumps(status, indent=2) + '\n')
            summary = {'success': status['success'], 'counts': status.get('counts', {})}
            for key in ('error', 'warning'):
                if status.get(key):
                    summary[key] = status[key]
            print(json.dumps(summary))
            return 0 if status['success'] else 1
    except BlockingIOError:
        print('Capture busy; no completed result was produced.')
        return 75
    except (OSError, ValueError):
        print('Capture preflight failed; evidence remains pending.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
