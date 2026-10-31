#!/usr/bin/env python3
"""Extract bounded, private feedback from completed local skill work; no chat writes."""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, HTTPServer
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
import threading
import time
from read_guidance import SKILL_PATTERN, SKILLS, guidance_path, observed, request, shell_words

SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

# Distinctive headings that identify each loaded SKILL.md besides its frontmatter name.
HEADINGS = {'frontend-ui': 'Frontend', 'content-research-writer': 'Content|Research', 'find-skills': 'Find Skills', 'tauri': 'Tauri', 'typescript': 'TypeScript'}
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
PRIVACY_REJECTIONS = {'privacy identifier rejected', 'privacy source quotation rejected', 'privacy source name rejected'}
STARTUP_DIAGNOSTIC = ('Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; '
                      'enable `features.code_mode_host` and install `codex-code-mode-host`.')
FIELDS = ('title', 'task', 'observation', 'evidence', 'cause', 'proposal')
TOOL_FEATURES = {'shell_tool', 'multi_agent', 'apps', 'plugins', 'hooks', 'code_mode_host',
                 'code_mode', 'multi_agent_v2', 'sleep_tool', 'computer_use', 'browser_use',
                 'image_generation', 'view_image', 'daemon_auto_start'}
EXTRACTOR_CONFIG = ('project_doc_max_bytes=0', 'model_reasoning_effort="xhigh"', 'approval_policy="never"',
                    'tools.update_plan.enabled=false', 'tools.experimental_request_user_input.enabled=false',
                    'web_search="disabled"', 'features.multi_agent_v2.non_code_mode_only=false',
                    'features.code_mode.excluded_tool_namespaces=["functions","clock","collaboration"]')
REQUIRED_CONFIG = ('project_doc_max_bytes=0', 'approval_policy="never"', 'web_search="disabled"')
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


def redact(text):
    text = re.sub(r'(?i)(?:bearer\s+|(?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s"\']+', '[redacted credential]', text)
    text = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,})\b', '[redacted credential]', text)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[redacted email]', text)
    text = re.sub(r'https?://[^\s<>"\']+', '[redacted URL]', text)
    text = re.sub(r'/(?:Users|home)/[^\s<>"\']+', '[local path]', text)
    return text


def text_content(payload):
    return '\n'.join(item.get('text', '') for item in payload.get('content', []) if isinstance(item, dict))


def read_results(value):
    """Decode tool envelopes, without evaluating transcript code or consulting today's files."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            try:
                value = ast.literal_eval(value)
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
    token = re.compile(r'\s*("(?:[^"\\]|\\.)*"|\x27(?:[^\x27\\]|\\.)*\x27|[A-Za-z_$][\w$]*|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|[{}:,])')
    def take(position):
        match = token.match(raw, position)
        if not match:
            raise ValueError('Not a literal exec argument')
        return match[1], match.end()
    def string(value):
        if value.startswith('"'):
            return json.loads(value)
        # Unsupported JS escapes stay unknown rather than adopting Python semantics.
        if re.search(r'\\(?![\\\x27"bfnrtv])', value):
            raise ValueError('Unsupported string escape')
        return ast.literal_eval(value)
    opening, position = take(start)
    if opening != '{':
        raise ValueError('Expected literal exec options')
    values = {}
    key, position = take(position)
    while key != '}':
        if key[:1] in ('"', "'"):
            key = string(key)
        elif not re.fullmatch(r'[A-Za-z_$][\w$]*', key):
            raise ValueError('Computed exec option')
        if key in values or key == '__proto__':
            raise ValueError('Duplicate or inherited exec option')
        colon, position = take(position)
        if colon != ':':
            raise ValueError('Expected literal exec option value')
        value, position = take(position)
        if value[:1] in ('"', "'"):
            value = string(value)
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
        for match in re.finditer(r'\bcmd\s*:\s*("(?:[^"\\]|\\.)*"|\x27(?:[^\x27\\]|\\.)*\x27)', raw):
            try:
                commands.append(ast.literal_eval(match[1]))
            except (ValueError, SyntaxError):
                return []
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
        masked = re.sub(r'"(?:[^"\\]|\\.)*"|\x27(?:[^\x27\\]|\\.)*\x27', '""', raw)
        calls = re.findall(r'\b([\w.]+)\s*\(', masked)
        if calls.count('tools.exec_command') != 1 or any(call not in ('text', 'tools.exec_command') for call in calls):
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
        statements = ast.parse(match[3]).body
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


def loaded_reads(raw, output, turn):
    if not re.search(r'(?:' + SKILL_PATTERN + r')/', raw):
        return []
    commands = literal_commands(raw)
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
        meta, current, loaded, calls, reads = {}, None, set(), {}, []
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
                    current['messages'].append({'role': 'user', 'text': redact(payload.get('message', ''))})
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
                        if read and (successful or observed_reads):
                            loaded.update(skill for skill in skill_reads if re.search(r'(?:name:\s*' + re.escape(skill) + r'\b|#\s*' + HEADINGS[skill] + ')', output, re.I))
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
                            current['messages'].append({'role': 'user', 'text': redact(text)})
                    elif item_type == 'message' and payload.get('role') == 'assistant':
                        current['messages'].append({'role': 'assistant', 'text': redact(text_content(payload))})
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
        text = '\n'.join(item.get('text', '') for item in content if isinstance(item, dict) and item.get('type') == 'text')
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
                current['messages'].append({'role': 'user', 'text': redact(text)})
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
                current['messages'].append({'role': 'assistant', 'text': redact(item['text'])})
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
        text = '\n'.join(item.get('text', '') for item in content if isinstance(item, dict)) if isinstance(content, list) else str(content or '')
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
            current['messages'].append({'role': 'user', 'text': redact(text)})


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
                scan['loaded'].update(s for s in skills if re.search(r'(?:name:\s*' + re.escape(s) + r'\b|#\s*' + HEADINGS[s] + ')', output, re.I))
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
        if any(redact(note[k]) != note[k] or re.search(r'(?:\+?\d[\d ()-]{8,}\d|\b\d+\s+\w+\s+(?:Street|Road|Avenue)\b)', note[k], re.I) for k in FIELDS):
            raise ValueError('Capture output contains private identifiers')
    source_text = '\n'.join(m.get('text', '') for m in turn.get('messages', []))
    source_words = re.findall(r'\w+', source_text)
    source_phrases = {' '.join(source_words[i:i + 7]).lower() for i in range(max(0, len(source_words) - 6))}
    sentences = [part.strip(' \n\t\"\'“”‘’') for part in re.split(r'[.!?。！？\n]+', source_text)]
    sentences += re.findall(r'[\"“]([^\"”]{20,})[\"”]', source_text)
    sentences = [re.sub(r'\s+', ' ', sentence).casefold() for sentence in sentences if len(sentence) >= 20 or (len(sentence) >= 5 and re.fullmatch(r'[\u3000-\u9fff\uf900-\ufaff]+', sentence))]
    names = re.findall(r'(?i:\b(?:customer|client|company|project|organization|named|called)\s*[:=]?\s+)[\"“]?((?:[A-Z][\w-]*)(?:\s+[A-Z][\w-]*){0,3})', source_text)
    names += re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}\b', source_text)
    generic_names = {'Frontend UI', 'Content Research Writer', 'Find Skills', 'JSON Schema', 'Python Standard Library',
                     'Google Docs', 'Google Sheets', 'Google Slides', 'Microsoft Excel', 'GitHub Actions'}
    names = [name.casefold() for name in names if name not in generic_names]
    for note in value['notes']:
        words = re.findall(r'\w+', ' '.join(note[k] for k in FIELDS))
        if any(' '.join(words[i:i + 7]).lower() in source_phrases for i in range(max(0, len(words) - 6))):
            raise ValueError('Capture output quotes source text')
        for field_text in (note[k] for k in FIELDS):
            normalized = re.sub(r'\s+', ' ', field_text).casefold()
            if any(sentence in normalized for sentence in sentences):
                raise ValueError('Capture output quotes a private source sentence')
            if any(re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', normalized) for name in names):
                raise ValueError('Capture output includes a source identifying name')
            for phrase in re.findall(r'[\u3000-\u9fff\uf900-\ufaff]{12,}', field_text):
                if any(phrase[i:i + 12] in source_text for i in range(len(phrase) - 11)):
                    raise ValueError('Capture output quotes private source text')
    return value['notes']


def message_context(messages, compact=False):
    indexed = [(i, m) for i, m in enumerate(messages) if m['role'] in ('user', 'assistant')]
    selected = indexed
    if compact:
        keep = set()
        for role, limit in (('user', 6), ('assistant', 2)):
            keep.update(i for i, _ in [(i, m) for i, m in indexed if m['role'] == role][-limit:])
        selected = [(i, m) for i, m in indexed if i in keep]
    included = {i for i, _ in selected}
    omitted = [i for i, _ in indexed if i not in included]
    result = [{'role': m['role'], 'text': m['text'][:2000] if compact else m['text'],
               'source_index': i, 'original_chars': len(m['text'])} for i, m in selected]
    return {'messages': result, 'omitted_message_indices': omitted,
            'truncated': bool(omitted) or any(len(m['text']) < m['original_chars'] for m in result)}


def extract(config, state_root, turn, existing, *, deadline=None):
    relevant = []
    for skill in turn['verified_skills']:
        matching = sorted((note for note in existing if note['skill'] == skill), key=lambda note: note.get('order', 0))
        relevant.extend(matching[-max(2, 8 // len(turn['verified_skills'])):])
    existing = relevant
    compact = dict(turn)
    compact.update(message_context(turn.get('messages', [])))
    compact['calls'] = turn.get('calls', [])[-60:]
    compact['truncated'] |= len(compact['calls']) != len(turn.get('calls', []))
    data = json.dumps({'turn': compact, 'existing_notes': existing}, ensure_ascii=False)
    if len(data) > MAX_INPUT:
        compact.update(message_context(turn.get('messages', []), compact=True))
        compact['truncated'] = True
        smaller = [note for skill in turn['verified_skills'] for note in [n for n in existing if n['skill'] == skill][-2:]]
        data = json.dumps({'turn': compact, 'existing_notes': smaller}, ensure_ascii=False)
    if len(data) > MAX_INPUT:
        raise ValueError('Capture input exceeds safety bound')
    return validate_notes(model_response(config, state_root, prompt_runtime.load('extract', config), data, SCHEMA, deadline=deadline), turn)


def run_extractor(command, *, env, input, timeout):
    """Reap the isolated CLI and its children together, including inherited output pipes."""
    with subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired('restricted extractor', timeout, output=stdout, stderr=stderr) from None
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def extractor_events(stdout, *, partial=False):
    """Validate control events; retain no model, reasoning, or error text in diagnostics."""
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors='replace')
    stdout = stdout or ''
    if len(stdout) > 1000000:
        raise RuntimeError('Capture event output exceeds safety bound')
    started, completed, errors = False, False, []
    lines = stdout.splitlines()
    for index, line in enumerate(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if partial and index == len(lines) - 1 and not stdout.endswith('\n'):
                break
            raise
        if not isinstance(event, dict):
            raise ValueError('Invalid capture output')
        if event.get('type') == 'turn.started':
            started = True
        if event.get('type') == 'turn.completed':
            completed = True
        item = event.get('item', {})
        if not isinstance(item, dict):
            raise ValueError('Invalid capture output')
        if not started and item.get('type') == 'error' and item.get('message') == STARTUP_DIAGNOSTIC:
            continue
        if (item and item.get('type') not in ('agent_message', 'reasoning', 'error')) or event.get('type') in ('tool_call', 'function_call'):
            raise RuntimeError('Capture model attempted tool execution')
        if item.get('type') == 'error' and isinstance(item.get('message'), str):
            errors.append(item['message'])
        if event.get('type') == 'error' and isinstance(event.get('message'), str):
            errors.append(event['message'])
        error = event.get('error')
        if isinstance(error, dict) and isinstance(error.get('message'), str):
            errors.append(error['message'])
    return ('completed' if completed else 'inference' if started else 'startup'), '\n'.join(errors)


def extractor_error_class(stderr, event_errors):
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors='replace')
    text = (stderr or '') + '\n' + event_errors
    for label, pattern in (
            ('authentication', r'\b401\b|unauthori[sz]ed|not logged in|authentication (?:failed|required)|refresh.token.*expired'),
            ('rate limit', r'\b429\b|rate.?limit|usage limit|quota exceeded'),
            ('connection', r'connection (?:reset|refused|closed)|stream disconnected|error sending request|network (?:unreachable|error)|timed out|unexpected eof|\b50[234]\b')):
        if re.search(pattern, text, re.I):
            return label
    return 'CLI or output'


def model_response(config, state_root, prompt, data, response_schema, *, deadline=None):
    with tempfile.TemporaryDirectory(prefix='.capture-model-', dir=state_root) as folder:
        root = Path(folder)
        schema, output = root / 'schema.json', root / 'output.json'
        schema.write_text(json.dumps(response_schema))
        bounded_timeout(MODEL_TIMEOUT, deadline)
        profile = compatible_profile(config['codex_path'], state_root, deadline=deadline)
        command = extractor_command(config['codex_path'], profile, root, schema, output, deadline=deadline)
        child_env = extractor_env()
        for attempt in range(1, 3):
            timeout = bounded_timeout(MODEL_TIMEOUT, deadline)
            output.unlink(missing_ok=True)
            began = time.monotonic()
            try:
                result = run_extractor(command, env=child_env, input=prompt + '\n' + data, timeout=timeout)
            except subprocess.TimeoutExpired as error:
                phase, event_errors = extractor_events(error.output, partial=True)
                category = extractor_error_class(error.stderr, event_errors)
                if category == 'CLI or output':
                    category = 'timeout'
            else:
                phase, event_errors = extractor_events(result.stdout)
                failed = result.returncode != 0 or not output.is_file() or output.stat().st_size > MAX_OUTPUT
                category = extractor_error_class(getattr(result, 'stderr', ''), event_errors)
                if not failed:
                    return json.loads(output.read_text())
            diagnostics = {'elapsed_seconds': round(time.monotonic() - began, 3),
                           'phase': phase, 'failure_class': category, 'attempts': attempt}
            # Keep recovered failures diagnosable without logging CLI or source text.
            print(json.dumps({'event': 'extractor_attempt_failed', **diagnostics}), file=sys.stderr, flush=True)
            # Deadline exhaustion is scheduling, not a failed assessment. Never settle its evidence.
            bounded_timeout(MODEL_TIMEOUT, deadline)
            if attempt == 1 and category in ('timeout', 'connection'):
                continue
            reason = 'extractor timeout' if category == 'timeout' else 'extractor ' + category + ' failure'
            raise ExtractorFailure(reason, diagnostics)


def extractor_env():
    return {key: value for key, value in os.environ.items() if not key.startswith('CODEX_') or key == 'CODEX_HOME'}


def cli_text(cli, *args, deadline=None):
    result = run_bounded([cli, *args], env={**extractor_env(), 'LC_ALL': 'C'},
                         capture_output=True, text=True, timeout=10, deadline=deadline)
    if result.returncode or len(result.stdout) > 100000:
        raise RuntimeError('Capture CLI capability discovery failed')
    return result.stdout


def help_options(text):
    options = {}
    current = ()
    for line in text.splitlines():
        match = re.match(r'^\s+(-(?:-[a-z][a-z0-9-]*|[A-Za-z])(?:,\s*-(?:-[a-z][a-z0-9-]*|[A-Za-z]))*)(?:\s|$)', line)
        if match:
            current = tuple(re.findall(r'--[a-z][a-z0-9-]*|-[A-Za-z]', match[1]))
            for option in current:
                options[option] = ''
        elif current:
            for option in current:
                options[option] += ' ' + line.strip()
    return options


def help_flag(options, names, meaning, *, required=True):
    for name in names:
        if name in options:
            return name
    matches = [name for name, description in options.items()
               if name.startswith('--') and re.search(meaning, description, re.I)]
    if len(matches) == 1:
        return matches[0]
    if required:
        raise RuntimeError('Capture CLI required control is unavailable')
    return None


def help_command(text, names, meaning):
    commands = dict(re.findall(r'^\s{2,}([a-z][a-z0-9-]*)\s{2,}([^\n]+)$', text, re.M))
    for name in names:
        if name in commands:
            return name
    matches = [name for name, description in commands.items() if re.search(meaning, description, re.I)]
    if len(matches) == 1:
        return matches[0]
    raise RuntimeError('Capture CLI capability discovery failed')


def discover_profile(cli, *, deadline=None):
    root_help = cli_text(cli, '--help', deadline=deadline)
    command = help_command(root_help, ('exec', 'e'), r'(?:run|execute) Codex non.interactively\b')
    root_options = help_options(root_help)
    options = help_options(cli_text(cli, command, '--help', deadline=deadline))
    profile = {'command': command,
        'standalone': help_flag(root_options, ('--no-daemon',), r'without.*shared background server'),
        'strict': help_flag(options, ('--strict-config',), r'fields.*not recognized|fields.*does not recognize', required=False)}
    for key, names, meaning in (
        ('ignore_config', ('--ignore-user-config',), r'do not load.*config\.toml'),
        ('ignore_rules', ('--ignore-rules',), r'do not load.*(?:execpolicy|\.rules)'),
        ('ephemeral', ('--ephemeral',), r'without persisting session'),
        ('cd', ('-C', '--cd'), r'(?:working|workspace) root'),
        ('model', ('-m', '--model'), r'model.*(?:agent|use)|agent.*model'),
        ('config', ('-c', '--config'), r'override a configuration value'),
        ('sandbox', ('--sandbox', '-s'), r'sandbox policy.*model.generated'),
        ('schema', ('--output-schema',), r'JSON Schema.*(?:response|output)'),
        ('output', ('-o', '--output-last-message'), r'file.*(?:last|final).*message|final.*message.*file'),
        ('json', ('--json', '--experimental-json'), r'print.*(?:JSONL|JSON events|newline.delimited JSON)'),
        ('skip_git', ('--skip-git-repo-check',), r'outside.*Git repository')):
        profile[key] = help_flag(options, names, meaning, required=key != 'skip_git')
    if 'read-only' not in options[profile['sandbox']]:
        raise RuntimeError('Capture CLI required control is unavailable')
    feature_command = help_command(root_help, ('features',), r'(?:inspect|manage) feature flags\b')
    listing = help_command(cli_text(cli, feature_command, '--help', deadline=deadline), ('list',), r'(?:list|show) known features\b')
    features = cli_text(cli, feature_command, listing, deadline=deadline)
    active = {match[1] for match in re.finditer(
        r'^([a-z][a-z0-9_.-]*)\s+(?!removed\b)[a-z][a-z ]+\s+(?:true|false)\s*$', features, re.M)}
    if not active:
        raise RuntimeError('Capture CLI capability discovery failed')
    profile['overrides'] = list(EXTRACTOR_CONFIG) + ['features.' + name + '=false' for name in sorted(active & TOOL_FEATURES)]
    return profile


def extractor_command(cli, profile, root, schema, output, *, deadline=None):
    command = [cli, profile['standalone'], profile['command'], profile['ignore_config'],
               profile['ignore_rules'], profile['ephemeral'], profile['cd'], str(root),
               profile['model'], 'gpt-6.1-sol', profile['sandbox'], 'read-only',
               profile['schema'], str(schema), profile['output'], str(output), profile['json']]
    for key in ('strict', 'skip_git'):
        if profile[key]:
            command.append(profile[key])
    if not profile['skip_git']:
        run_bounded(['git', 'init', '-q', str(root)], env=extractor_env(), check=True, capture_output=True,
                    timeout=10, deadline=deadline)
    for option in profile['overrides']:
        command.extend([profile['config'], option])
    return command + ['-']


def audit_profile(cli, profile, root, *, deadline=None):
    requests = []
    class AuditEndpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            self.connection.settimeout(5)
            size = int(self.headers.get('Content-Length', '0'))
            if self.path == '/v1/responses' and 0 < size <= 1000000:
                try:
                    requests.append(json.loads(self.rfile.read(size)))
                except (ValueError, OSError):
                    pass
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"Local capture compatibility check complete"}}')
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), AuditEndpoint)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix='.capture-audit-', dir=root) as folder:
            directory = Path(folder)
            schema = directory / 'schema.json'
            schema.write_text(json.dumps(SCHEMA))
            overrides = ('model_provider="capture_audit"', 'model_providers.capture_audit.name="Local capture audit"',
                'model_providers.capture_audit.base_url="http://127.0.0.1:' + str(server.server_port) + '/v1"',
                'model_providers.capture_audit.wire_api="responses"', 'model_providers.capture_audit.requires_openai_auth=false',
                'model_providers.capture_audit.supports_websockets=false', 'model_providers.capture_audit.request_max_retries=0')
            for _ in range(len(profile['overrides']) + 1):
                command = extractor_command(cli, profile, directory, schema, directory / 'output.json', deadline=deadline)
                for option in overrides:
                    command[-1:-1] = [profile['config'], option]
                result = run_bounded(command, env=extractor_env(), input='Compatibility check only. Return {"notes":[]}.',
                                     capture_output=True, text=True, timeout=30, deadline=deadline)
                if requests:
                    if any(not isinstance(request, dict) or request.get('model') != 'gpt-6.1-sol'
                           or not isinstance(request.get('input'), list) or not request['input'] for request in requests):
                        break
                    if any(request.get('tools') not in (None, []) for request in requests):
                        raise RuntimeError('Capture CLI isolation check exposed tools')
                    return
                removed = re.search(r'unknown configuration field `([a-zA-Z0-9_.-]+)` in -c/--config override', result.stderr)
                if not removed or not profile['strict']:
                    break
                key = removed[1]
                if any(value.startswith(key + '=') for value in REQUIRED_CONFIG):
                    break
                remaining = [value for value in profile['overrides'] if value.split('=', 1)[0] != key]
                if remaining == profile['overrides']:
                    break
                profile['overrides'] = remaining
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()
    raise RuntimeError('Capture CLI isolation check failed')


@lru_cache(maxsize=4)
def verified_profile(cli, root, identity, deadline=None):
    profile = discover_profile(cli, deadline=deadline)
    with Path(cli).open('rb') as binary:
        digest = hashlib.file_digest(binary, 'sha256').hexdigest()
    fingerprint = hashlib.sha256((digest + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()).encode()).hexdigest()
    cache = Path(root) / 'capture-cli.json'
    safe_path(cache)
    try:
        saved = json.loads(cache.read_text()) if cache.is_file() else {}
    except (OSError, ValueError):
        saved = {}
    discovered = {**profile, 'overrides': list(profile['overrides'])}
    if (isinstance(saved, dict) and saved.get('fingerprint') == fingerprint
            and saved.get('discovered') == discovered and saved.get('tools') == []):
        adapted = saved.get('profile')
        overrides = adapted.get('overrides') if isinstance(adapted, dict) else None
        if (isinstance(overrides, list) and adapted == {**profile, 'overrides': overrides}
                and all(value in profile['overrides'] for value in overrides)
                and all(value in overrides for value in REQUIRED_CONFIG)):
            return adapted
    audit_profile(cli, profile, root, deadline=deadline)
    atomic(cache, json.dumps({'fingerprint': fingerprint, 'discovered': discovered, 'profile': profile, 'tools': [],
                             'verified_at': datetime.now(timezone.utc).isoformat()}) + '\n')
    return profile


def compatible_profile(cli, root, *, deadline=None):
    info = Path(cli).stat()
    return verified_profile(cli, str(root), (info.st_ino, info.st_size, info.st_mtime_ns), deadline)


def cli_version(config):
    """Bounded diagnostic metadata; version checks never gate capture."""
    try:
        result = subprocess.run([config['codex_path'], '--version'], env=extractor_env(),
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return 'unknown'
    if result.returncode or len(result.stdout) > 256:
        return 'unknown'
    version = result.stdout.strip()
    return version if re.fullmatch(r'codex-cli [0-9][0-9A-Za-z._+-]*', version) else 'unknown'


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
        'Capture output contains private identifiers': 'privacy identifier rejected',
        'Capture output quotes source text': 'privacy source quotation rejected',
        'Capture output quotes a private source sentence': 'privacy source quotation rejected',
        'Capture output quotes private source text': 'privacy source quotation rejected',
        'Capture output includes a source identifying name': 'privacy source name rejected',
        'Invalid capture output': 'model schema rejected',
        'Invalid capture note field': 'model schema rejected',
        'Invalid capture note scope': 'model scope rejected',
        'Invalid capture transaction': 'invalid capture state',
        'Invalid capture host configuration': 'invalid host configuration',
        'Capture paths must not contain user symlinks': 'unsafe local path',
        'Capture input exceeds safety bound': 'input bound exceeded',
        'Capture event output exceeds safety bound': 'output bound exceeded',
        'Capture model attempted tool execution': 'model tool event rejected',
        'Feedback extraction failed; completed turn remains pending': 'extractor CLI or output failure',
        'Capture CLI capability discovery failed': 'CLI capability discovery failed',
        'Capture CLI required control is unavailable': 'CLI safety control unavailable',
        'Capture CLI isolation check failed': 'CLI isolation verification failed',
        'Capture CLI isolation check exposed tools': 'CLI isolation verification exposed tools',
    }
    return reasons.get(str(error), 'model or schema extraction failure')


def capture(config, state_root, *, now=None):
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
    for turn in pending:
        if time.monotonic() >= deadline or attempted >= MAX_TURNS:
            break
        evidence = turn['evidence_id']
        deferred = state['deferred'].get(evidence, {})
        clock = now or datetime.now(timezone.utc)
        if deferred.get('retry_at') and clock < timestamp(deferred['retry_at']):
            errors += 1
            continue
        attempted += 1
        try:
            if evidence not in state['pending']:
                candidate = dict(turn)
                if deferred.get('reason') in PRIVACY_REJECTIONS:
                    candidate['previous_capture_rejection'] = deferred['reason']
                lessons = extract(config, state_root, candidate, notes, deadline=deadline)
                state['pending'][evidence] = {'lessons': lessons, 'deployed': deployed, 'machine': machine, 'recorded_at': clock.isoformat()}
                atomic(state_path, json.dumps(state, indent=2) + '\n')
            transaction = state['pending'][evidence]
            if not isinstance(transaction, dict) or set(transaction) != {'lessons', 'deployed', 'machine', 'recorded_at'} or not isinstance(transaction['machine'], str) or not re.fullmatch(r'[A-Za-z0-9.-]+', transaction['machine']) or not isinstance(transaction['deployed'], str) or not re.fullmatch(r'(?:unavailable|[a-f0-9]{40,64})', transaction['deployed']):
                raise ValueError('Invalid capture transaction')
            timestamp(transaction['recorded_at'])
            lessons = validate_notes({'notes': transaction['lessons']}, turn)
        except CaptureBudgetExpired:
            break
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
            notes.append({'skill': lesson['skill'], 'text': content[:1500], 'hash': content_hash, 'order': time.time_ns()})
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
    summary = {'eligible': len(pending), 'processed': processed, 'notes': written,
               'pending': len(remaining) + len(unavailable), 'errors': errors,
               'oldest_pending_at': min(pending_dates, key=timestamp) if pending_dates else None,
               'unavailable_sources': len(unavailable),
               'scanner_error_count': len(scan_errors), 'scanner_diagnostics': scan_errors[:10]}
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path.home() / '.local/share/codex-skills/config.json')
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
                if config.get('host_id') not in ('client-host', 'maintainer-host') or not Path(config.get('codex_path', '')).is_file():
                    raise ValueError('Invalid capture host configuration')
                status['cli_version'] = cli_version(config)
                status['counts'] = capture(config, root)
                status['success'] = status['counts']['errors'] == 0
                if not status['success']:
                    status['error'] = 'Capture has deferred evidence; source remains pending.'
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
                status.update(success=False, error='Capture failed: ' + failure_reason(error) + '. Source evidence remains pending.')
            finished = datetime.now(timezone.utc).isoformat()
            status.update(phase='finished', finished_at=finished)
            status['last_success_finished_at' if status['success'] else 'last_failure_finished_at'] = finished
            atomic(status_path, json.dumps(status, indent=2) + '\n')
            summary = {'success': status['success'], 'counts': status.get('counts', {})}
            if status.get('error'):
                summary['error'] = status['error']
            print(json.dumps(summary))
            return 0 if status['success'] else 1
    except BlockingIOError:
        return 0
    except (OSError, ValueError):
        print('Capture preflight failed; evidence remains pending.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
