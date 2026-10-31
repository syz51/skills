"""Score observed skill content in Codex CLI JSONL, without replaying saved code.

The caller supplies immutable pre-run file snapshots. A requested path, a tool's
start event, or an assistant saying it used a skill is never loading evidence.
Unknown tool formats remain unverified rather than being inferred from prose.
"""
import ast
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shlex

from read_guidance import observed, request

SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


_STRING = r'''(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')'''
_TRUNCATED = re.compile(r'(?im)^(?:warning:\s*)?(?:truncated output\b|output[^\n]*truncated\b|.*tokens truncated\b|.*lines omitted\b)')


def _digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _string(token):
    if token.startswith('"'):
        return json.loads(token)
    if re.search(r'\\(?![\\\x27"bfnrtv])', token):
        raise ValueError('Unsupported JavaScript string escape')
    return ast.literal_eval(token)


def _split(text, separator=';'):
    """Split a small literal syntax, retaining nested expressions and strings."""
    parts, start, stack, quote, escape = [], 0, [], None, False
    pairs = {')': '(', ']': '[', '}': '{'}
    for i, char in enumerate(text):
        if quote:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
        elif char in '([{':
            stack.append(char)
        elif char in ')]}':
            if not stack or stack.pop() != pairs[char]:
                raise ValueError('Unbalanced literal syntax')
        elif char == separator and not stack:
            parts.append(text[start:i].strip())
            start = i + 1
    if quote or stack:
        raise ValueError('Incomplete literal syntax')
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def _without_comments(code):
    # Comments cannot manufacture read expressions. Template literals are not
    # part of the supported static grammar, including their interpolation.
    pattern = re.compile(_STRING + r'|//[^\n]*|/\*[\s\S]*?\*/')
    return pattern.sub(lambda m: m[0] if m[0][0] in ('"', "'") else ' ', code)


def _shell_source(command):
    words = shlex.split(command)
    if len(words) == 3 and words[0].rsplit('/', 1)[-1] in ('sh', 'bash', 'zsh') and words[1] in ('-c', '-lc'):
        return words[2]
    return command


def _shell(command):
    return shlex.split(_shell_source(command))


def _listing_prefix(words):
    """Only side-effect-free inventory commands may precede a final read."""
    if not words:
        return False
    name = words[0].rsplit('/', 1)[-1]
    if name == 'pwd':
        return len(words) == 1
    if name == 'ls':
        return True
    if name != 'rg' or '--files' not in words[1:]:
        return False
    index = 1
    while index < len(words):
        word = words[index]
        if word in ('-g', '--glob', '--iglob'):
            index += 2
            if index > len(words):
                return False
            continue
        if word.startswith('-') and word not in ('--files', '--hidden', '--no-ignore', '--no-ignore-vcs', '--no-ignore-parent') and not word.startswith(('--glob=', '--iglob=')):
            return False
        index += 1
    return True


def _prefixed_read(command):
    """Return a final literal read after a successful read-only && prefix.

    Substitution, redirection, pipelines, fallbacks and arbitrary commands are
    not accepted. shlex retains quoted globs while exposing real operators.
    """
    source = _shell_source(command)
    if '$' in source or '`' in source:
        return None
    lexer = shlex.shlex(source, posix=True, punctuation_chars=';&|()<>')
    lexer.whitespace_split = True
    lexer.commenters = ''
    commands, current = [], []
    for token in lexer:
        if token == '&&':
            if not current:
                return None
            commands.append(current)
            current = []
        elif token in (';', '|', '||', '&', '(', ')', '<', '>', '>>', '<<'):
            return None
        else:
            current.append(token)
    commands.append(current)
    if len(commands) < 2 or not all(_listing_prefix(words) for words in commands[:-1]):
        return None
    return commands[-1]


def _tool_text(result):
    if not isinstance(result, dict) or result.get('isError'):
        return None
    content = result.get('content')
    if not isinstance(content, list):
        return None
    texts = [block['text'] for block in content if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str)]
    return '\n'.join(texts)


def _node_attempts(code, bindings):
    """Identify uncertain reads for negative controls, without granting proof."""
    code = _without_comments(code)
    # Keep offsets intact while removing string payloads from executable text.
    # A printed path or a comment containing readFile is not a read attempt.
    bare = re.sub(_STRING, lambda m: ' ' * len(m[0]), code)
    calls = list(re.finditer(r'\breadFile(?:Sync)?\s*\(', bare))
    values = set()
    for call in calls:
        depth, end = 1, call.end()
        while end < len(bare) and depth:
            if bare[end] == '(':
                depth += 1
            elif bare[end] == ')':
                depth -= 1
            end += 1
        if depth:
            continue
        try:
            arguments = _split(code[call.end():end - 1], ',')
            value = _value(arguments[0], bindings) if arguments else None
        except (ValueError, SyntaxError, RecursionError):
            continue
        if isinstance(value, str):
            values.add(value)
    return values, bool(calls)


@dataclass(frozen=True)
class _Read:
    path: str
    start: int | None = None
    end: int | None = None


def _slice_range(content, start, end):
    """Translate JS UTF-16 slice offsets to intact Python text boundaries."""
    boundaries, units = {0: 0}, 0
    for index, char in enumerate(content):
        units += 2 if ord(char) > 0xFFFF else 1
        boundaries[units] = index + 1
    begin, stop, _ = slice(start, end).indices(units)
    if begin not in boundaries or stop not in boundaries:
        return None
    return boundaries[begin], boundaries[stop]


def _value(expression, bindings):
    expression = expression.strip()
    if re.fullmatch(_STRING, expression):
        return _string(expression)
    if re.fullmatch(r'[A-Za-z_$][\w$]*', expression):
        return bindings.get(expression)
    if expression == 'nodeRepl.cwd':
        return None
    if expression.startswith('[') and expression.endswith(']'):
        return [_value(piece, bindings) for piece in _split(expression[1:-1], ',')]
    pieces = _split(expression, '+')
    if len(pieces) > 1:
        values = [_value(piece, bindings) for piece in pieces]
        return ''.join(values) if all(isinstance(v, str) for v in values) else None
    raise ValueError('Not a literal binding')


def _node_read(expression, bindings, modules):
    expression = expression.strip()
    match = re.fullmatch(r'await\s+(\w+)\.readFile\((.*)\)', expression, re.S)
    sliced = re.fullmatch(r'\((await\s+\w+\.readFile\(.*\))\)\.slice\((-?\d+)(?:\s*,\s*(-?\d+))?\)', expression, re.S)
    if sliced:
        base = _node_read(sliced[1], bindings, modules)
        return _Read(base.path, int(sliced[2]), int(sliced[3]) if sliced[3] is not None else None) if base else None
    if not match or match[1] not in modules:
        return None
    args = _split(match[2], ',')
    if len(args) != 2 or _value(args[1], bindings) not in ('utf8', 'utf-8'):
        return None
    path = _value(args[0], bindings)
    return _Read(path) if isinstance(path, str) else None


class _Inspect:
    """Parse data-only Node inspect output, never JavaScript expressions."""
    token = re.compile(r'\s*(' + _STRING + r'|-?\d+(?:\.\d+)?|[A-Za-z_$][\w$]*|[{}\[\]:,+])')

    def __init__(self, text):
        self.text, self.pos = text, 0

    def take(self):
        match = self.token.match(self.text, self.pos)
        if not match:
            raise ValueError('Unsupported inspect output')
        self.pos = match.end()
        return match[1]

    def peek(self):
        position = self.pos
        try:
            return self.take()
        finally:
            self.pos = position

    def value(self, depth=0):
        if depth > 30:
            raise ValueError('Inspect output nesting exceeds bound')
        token = self.take()
        if token[0] in ('"', "'"):
            result = _string(token)
            while self.pos < len(self.text) and self.text[self.pos:].strip() and self.peek() == '+':
                self.take()
                following = self.take()
                if following[0] not in ('"', "'"):
                    raise ValueError('Inspect concatenation is not literal')
                result += _string(following)
            return result
        if token in ('[', '{'):
            closing, result = (']', []) if token == '[' else ('}', {})
            if self.peek() == closing:
                self.take()
                return result
            while True:
                if token == '{':
                    key = self.take()
                    key = _string(key) if key[0] in ('"', "'") else key
                    if not re.fullmatch(r'[A-Za-z_$][\w$]*', key) or key in result or self.take() != ':':
                        raise ValueError('Invalid inspect object key')
                    result[key] = self.value(depth + 1)
                else:
                    result.append(self.value(depth + 1))
                delimiter = self.take()
                if delimiter == closing:
                    return result
                if delimiter != ',':
                    raise ValueError('Invalid inspect delimiter')
        if token in ('true', 'false', 'null', 'undefined'):
            return {'true': True, 'false': False, 'null': None, 'undefined': None}[token]
        if re.fullmatch(r'-?\d+(?:\.\d+)?', token):
            return json.loads(token)
        raise ValueError('Unsupported inspect value')


def _rendered_values(text):
    """Plain write strings plus decoded data-only inspect values."""
    values = []
    parser = _Inspect(text)
    try:
        while parser.text[parser.pos:].strip():
            values.append(parser.value())
    except (ValueError, SyntaxError, RecursionError):
        return [text]
    return [text, *values]


def _strings_in_data(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (dict, list)):
        for child in value.values() if isinstance(value, dict) else value:
            yield from _strings_in_data(child)


def _results(value, source_index=None, depth=0):
    """Decode result-only containers; never inspect or execute code strings."""
    if depth > 30:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            try:
                value = ast.literal_eval(value)
            except (ValueError, SyntaxError, RecursionError):
                return []
    if isinstance(value, list):
        return [item for child in value for item in _results(child, source_index, depth + 1)]
    if not isinstance(value, dict):
        return []
    if type(value.get('i')) is int:
        source_index = value['i']
    if 'exit_code' in value and isinstance(value.get('output'), str):
        return [{**value, 'source_index': source_index}]
    return [item for key in ('text', 'value', 'result', 'content') if key in value
            for item in _results(value[key], source_index, depth + 1)]


def _exec_commands(code):
    """Known wrappers preserve literal exec results without arbitrary JS."""
    direct = re.fullmatch(r'\s*text\(\s*await\s+tools\.exec_command\(([\s\S]*)\)\s*\)\s*;?\s*', code)
    bound = re.fullmatch(r'\s*(?:const|let|var)\s+(\w+)\s*=\s*await\s+tools\.exec_command\(([\s\S]*)\)\s*;\s*text\(\s*\1\s*\)\s*;?\s*', code)
    batch = re.fullmatch(r'\s*(?:const|let|var)\s+(\w+)\s*=\s*await\s+Promise\.all(?:Settled)?\(\[([\s\S]*)\]\);\s*([\s\S]*?)\s*', code)

    def command(options):
        parser = _Inspect(options)
        value = parser.value()
        if parser.text[parser.pos:].strip() or not isinstance(value, dict) or not isinstance(value.get('cmd'), str):
            raise ValueError('Not literal exec options')
        if '__proto__' in value or any(type(v) not in (str, int, float, bool, type(None)) for v in value.values()):
            raise ValueError('Nonliteral exec option')
        return value['cmd']

    if direct or bound:
        return [command(direct[1] if direct else bound[2])], False
    if not batch:
        raise ValueError('Unknown exec output wrapper')
    commands = []
    for expression in _split(batch[2], ','):
        call = re.fullmatch(r'\s*tools\.exec_command\(([\s\S]*)\)\s*', expression)
        if not call:
            raise ValueError('Unknown exec batch call')
        commands.append(command(call[1]))
    variable, tail = re.escape(batch[1]), batch[3]
    indexed = bool(re.fullmatch(
        r'for\s*\(\s*let\s+i\s*=\s*0\s*;\s*i\s*<\s*' + variable + r'\.length\s*;\s*i\+\+\s*\)\s*text\(\{\s*i\s*,\s*\.\.\.\s*' + variable + r'\[i\]\s*\}\);?', tail)
        or re.fullmatch(variable + r'\.forEach\(\(\s*(\w+)\s*,\s*i\s*\)\s*=>\s*text\(\{\s*i\s*,\s*(?:\.\.\.\s*\1|result\s*:\s*\1)\s*\}\)\);?', tail))
    if not indexed and not re.fullmatch(variable + r'(?:\.reverse\(\))?\.forEach\(text\);?', tail):
        raise ValueError('Unknown exec batch output transformation')
    return commands, indexed


def _node(code, text, state):
    """Recognize straight-line read/write dataflow and one labelled batch form.

    Unsupported programs produce no evidence and invalidate their persistent
    bindings. This intentionally does not pretend to interpret arbitrary JS.
    """
    bindings, modules = dict(state['bindings']), set(state['modules'])
    writes, batches, labelled = [], [], []
    code = _without_comments(code)
    for statement in _split(code):
        declaration = re.fullmatch(r'(?:var|let|const)\s+(\w+)\s*=\s*([\s\S]+)', statement)
        if declaration:
            name, expression = declaration[1], declaration[2]
            imported = re.fullmatch(r'await\s+import\((.*)\)', expression)
            if imported and _value(imported[1], bindings) == 'node:fs/promises':
                modules.add(name)
                bindings.pop(name, None)
                continue
            read = _node_read(expression, bindings, modules)
            if read:
                bindings[name] = read
                continue
            batch = re.fullmatch(r'await\s+Promise\.allSettled\((\w+)\.map\(\s*(\w+)\s*=>\s*(\w+)\.readFile\(\s*\2\s*,\s*(.*)\)\s*\)\s*\)', expression, re.S)
            if batch and batch[3] in modules and _value(batch[4], bindings) in ('utf8', 'utf-8') and isinstance(bindings.get(batch[1]), list):
                bindings[name] = [_Read(path) if isinstance(path, str) else None for path in bindings[batch[1]]]
                continue
            if name in modules:
                raise ValueError('Filesystem alias reassigned')
            bindings[name] = _value(expression, bindings)
            continue
        write = re.fullmatch(r'nodeRepl\.write\(([\s\S]*)\)', statement)
        if write:
            read = _node_read(write[1], bindings, modules)
            value = read if read else _value(write[1], bindings)
            if isinstance(value, _Read):
                writes.append(value)
            elif isinstance(value, list) and any(isinstance(v, _Read) for v in value):
                batches.append(value)
            else:
                writes.append(value)
            continue
        loop = re.fullmatch(r'for\s*\(\s*(?:var|let)\s+(\w+)\s*=\s*0\s*;\s*\1\s*<\s*(\w+)\.length\s*;\s*\1\+\+\s*\)\s*\{\s*nodeRepl\.write\(\s*[\x27"]FILE:\s*[\x27"]\s*\+\s*(\w+)\[\1\]\s*\)\s*;\s*nodeRepl\.write\(\s*\2\[\1\]\.status\s*===\s*[\x27"]fulfilled[\x27"]\s*\?\s*\2\[\1\]\.value\s*:\s*String\(\2\[\1\]\.reason\)\s*\)\s*;?\s*\}', statement, re.S)
        if loop:
            reads, paths = bindings.get(loop[2]), bindings.get(loop[3])
            if not isinstance(reads, list) or not isinstance(paths, list) or len(reads) != len(paths) or any(isinstance(r, _Read) and r.path != p for r, p in zip(reads, paths)):
                raise ValueError('Batch labels differ from read paths')
            labelled.extend(r for r in reads if isinstance(r, _Read))
            continue
        raise ValueError('Unsupported Node read program')
    state.update(bindings=bindings, modules=modules)
    candidates = []
    for batch in batches:
        for value in _rendered_values(text):
            if isinstance(value, list) and len(value) == len(batch):
                for read, result in zip(batch, value):
                    if isinstance(read, _Read) and isinstance(result, dict) and result.get('status') == 'fulfilled' and isinstance(result.get('value'), str):
                        candidates.append((read, result['value']))
    return writes, labelled, candidates


def node_read_observations(code, output, state):
    """Return attributable historical Node read text, without a file snapshot.

    ``state`` is private to one transcript session. Only safe symbolic imports
    and bindings persist. Unsupported programs invalidate that state and return
    no observations. Unlabelled multiple scalar reads cannot be separated;
    settled batches require each observed result's explicit fulfilled status.
    Slice outputs remain partial and negative offsets have unknown starts.
    """
    if not isinstance(code, str) or not isinstance(output, str) or '\x00' in output:
        return []
    if not isinstance(state, dict):
        raise ValueError('Expected transcript-local Node state')
    if not state:
        state.update(bindings={}, modules=set())
    try:
        output.encode('utf-8')
        writes, _labelled, batches = _node(code, output, state)
    except (ValueError, SyntaxError, RecursionError, UnicodeError):
        state.clear()
        return []
    if _TRUNCATED.search(output):
        return []
    results = []

    def append(read, content):
        if not isinstance(content, str) or '\x00' in content:
            return
        # These are observed bytes. In particular, a caller must not replace
        # them with today's installed file when constructing a receipt.
        coverage = 'complete' if read.start is None and read.end is None else 'partial'
        results.append({'path': read.path, 'content': content,
                        'start': 0 if coverage == 'complete' else read.start if read.start is not None and read.start >= 0 else None,
                        'coverage': coverage})

    scalar = [i for i, value in enumerate(writes) if isinstance(value, _Read)]
    if len(scalar) == 1 and all(isinstance(value, (str, _Read)) for value in writes):
        index = scalar[0]
        prefix, suffix = ''.join(writes[:index]), ''.join(writes[index + 1:])
        if output.startswith(prefix) and output.endswith(suffix):
            end = len(output) - len(suffix) if suffix else len(output)
            if end >= len(prefix):
                append(writes[index], output[len(prefix):end])
    for read, content in batches:
        append(read, content)
    return results


def score_trace(trace_text, targets, required, forbidden=(), before=()):
    """Return a deterministic activation verdict for supplied pre-run snapshots.

    Complete coverage can comprise multiple verified continuation chunks. Their
    individual hashes remain partial; no synthetic historical full-file hash is
    reported. Ordering requires complete earlier guidance before any later read.
    """
    if not isinstance(trace_text, str) or not isinstance(targets, dict):
        raise ValueError('Expected trace text and a target mapping')
    known = set(required) | set(forbidden) | {target for pair in before for target in pair}
    if known - targets.keys():
        raise ValueError('Unknown activation target')
    paths = {}
    for name, target in targets.items():
        if not isinstance(target, dict) or not isinstance(target.get('path'), str) or not isinstance(target.get('content'), str) or not target['content'] or '\x00' in target['content']:
            raise ValueError('Invalid target snapshot')
        try:
            target['content'].encode('utf-8')
        except UnicodeError as error:
            raise ValueError('Target snapshot must be UTF-8 text') from error
        aliases = target.get('aliases', [])
        if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
            raise ValueError('Invalid target aliases')
        for path in [target['path'], *aliases]:
            if any(part in ('.', '..') for part in path.split('/')):
                raise ValueError('Target paths must not contain traversal')
            paths.setdefault(path, []).append(name)
    failures, reads, intervals, first, complete_at, unverified, exposures = [], [], {}, {}, {}, [], []
    completed, failed, seen = False, False, {}
    node_state = {'bindings': {}, 'modules': set()}

    def mark_uncertain(names, event):
        for name in sorted(set(names)):
            unverified.append((name, event))
            if name in forbidden:
                failures.append(f'Unverified forbidden read attempt: {name}')

    def add(path, content, event, start=0, end=None):
        for name in paths.get(path, []):
            expected = targets[name]['content']
            end = len(expected) if end is None else end
            if not (0 <= start <= end <= len(expected)) or expected[start:end] != content:
                continue
            if not content:
                continue
            record = {'target': name, 'event': event, 'coverage': 'complete' if start == 0 and end == len(expected) else 'partial', 'sha256': _digest(content), 'start': start, 'end': end}
            if record in reads:
                continue
            reads.append(record)
            first.setdefault(name, event)
            intervals.setdefault(name, []).append((start, end))
            covered = 0
            for begin, stop in sorted(intervals[name]):
                if begin > covered:
                    break
                covered = max(covered, stop)
            if covered == len(expected):
                complete_at.setdefault(name, event)

    def shell_evidence(command, output, index):
        try:
            words = _shell(command)
        except ValueError:
            return
        try:
            final = _prefixed_read(command)
        except ValueError:
            final = None
        if final:
            if len(final) == 2 and final[0] == 'cat' and final[1] in paths:
                content = targets[paths[final[1]][0]]['content']
                if output.endswith(content):
                    add(final[1], content, index)
                return
            try:
                request(shlex.join(final))
            except ValueError:
                return
            # The shared reader emits exactly one JSON line. Prefix inventory
            # text is not mistaken for its content or used to forge its hash.
            shell_evidence(shlex.join(final), output.rstrip('\n').rsplit('\n', 1)[-1], index)
            return
        if len(words) >= 2 and words[0] == 'cat' and all(path in paths for path in words[1:]):
            expected = ''.join(targets[paths[path][0]]['content'] for path in words[1:])
            if output == expected:
                for path in words[1:]:
                    add(path, targets[paths[path][0]]['content'], index)
            return
        if len(words) == 4 and words[:2] == ['sed', '-n'] and words[3] in paths:
            lines = re.fullmatch(r'(\d+),(\d+)p', words[2])
            if lines and int(lines[1]) > 0 and int(lines[2]) >= int(lines[1]):
                expected = targets[paths[words[3]][0]]['content']
                chunks = expected.splitlines(keepends=True)
                start = len(''.join(chunks[:int(lines[1]) - 1]))
                end = len(''.join(chunks[:int(lines[2])]))
                add(words[3], output, index, start, end)
            return
        normalized = shlex.join(words)
        try:
            expected_request = request(normalized)
            envelope = json.loads(output)
            observed(envelope, expected_request, 'activation')
            path = words[-3] if len(words) >= 2 and words[-2] == '--start' else words[-1]
            add(path, envelope['content'], index, envelope['start'], envelope['start'] + len(envelope['content']))
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
            return

    def shell(command, output, index):
        shell_evidence(command, output, index)
        try:
            words = _shell(command)
        except ValueError:
            return
        executable = words[0].rsplit('/', 1)[-1] if words else ''
        read_command = executable in ('cat', 'sed') or (executable in ('uv', 'python', 'python3') and any(word.endswith('read_guidance.py') for word in words))
        if read_command:
            for path, names in paths.items():
                if path in words or any(word.startswith(path) and word[len(path):len(path) + 1] in (';', '&', '|') for word in words):
                    for name in names:
                        if name in forbidden and name not in first:
                            mark_uncertain([name], index)

    def wrapped_shell(code, output, index):
        try:
            commands, indexed = _exec_commands(code)
        except (ValueError, SyntaxError, RecursionError):
            return
        results = _results(output)
        if len(commands) == len(results) == 1:
            result = results[0]
            if type(result.get('exit_code')) is int and result['exit_code'] == 0:
                shell(commands[0], result['output'], index)
        elif indexed and len(commands) == len(results) and {r.get('source_index') for r in results} == set(range(len(commands))):
            for command, result in zip(commands, sorted(results, key=lambda r: r['source_index'])):
                if type(result.get('exit_code')) is int and result['exit_code'] == 0:
                    shell(command, result['output'], index)
        else:
            # Only self-identifying helper results can be attributed without
            # array indices. shell() validates their literal path/start pair.
            for command in commands:
                try:
                    request(command)
                except ValueError:
                    continue
                for result in results:
                    if type(result.get('exit_code')) is int and result['exit_code'] == 0:
                        shell(command, result['output'], index)

    for index, line in enumerate(trace_text.splitlines()):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError()
        except (ValueError, RecursionError):
            failures.append(f'Malformed JSONL at line {index + 1}')
            continue
        kind = event.get('type')
        if kind == 'turn.completed':
            completed = True
        elif kind in ('turn.failed', 'error'):
            failed = True
        if kind != 'item.completed' or not isinstance(event.get('item'), dict):
            continue
        item = event['item']
        if item.get('type') not in ('command_execution', 'mcp_tool_call'):
            continue
        # Unknown read syntax cannot make a negative or ordering assertion
        # pass merely because its positive-evidence parser is unsupported.
        # Full content exposed by a completed tool result is uncertainty, not
        # invented proof that a particular file was read.
        exposed = item.get('aggregated_output') if item['type'] == 'command_execution' else _tool_text(item.get('result'))
        if isinstance(exposed, str):
            strings = [text for value in _rendered_values(exposed) for text in _strings_in_data(value)]
            exposures.extend((name, index) for name in known if any(targets[name]['content'] in text for text in strings))
        identity = item.get('id')
        if identity is not None:
            if not isinstance(identity, str) or not identity:
                failures.append('Invalid completed tool identity')
                continue
            if identity in seen:
                if seen[identity] != item:
                    failures.append('Conflicting completed tool event')
                continue
            seen[identity] = item
        if item['type'] == 'command_execution':
            if type(item.get('exit_code')) is int and item['exit_code'] == 0 and isinstance(item.get('command'), str) and isinstance(item.get('aggregated_output'), str):
                shell(item['command'], item['aggregated_output'], index)
            continue
        if item.get('status') != 'completed' or item.get('error'):
            continue
        output = _tool_text(item.get('result'))
        arguments = item.get('arguments')
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = None
        if output is None or not isinstance(arguments, dict):
            continue
        if item.get('server') in ('functions', 'exec', 'shell', 'codex') and item.get('tool') == 'exec_command' and isinstance(arguments.get('cmd'), str):
            # Only the visible result payload is loading evidence. Hidden
            # transport metadata containing stdout is not a substitute.
            results = _results(output)
            if len(results) == 1 and type(results[0].get('exit_code')) is int and results[0]['exit_code'] == 0:
                shell(arguments['cmd'], results[0]['output'], index)
            continue
        if item.get('server') in ('functions', 'exec') and item.get('tool') == 'exec' and isinstance(arguments.get('code'), str):
            wrapped_shell(arguments['code'], output, index)
            continue
        if item.get('server') not in ('node_repl', 'mcp__node_repl') or item.get('tool') not in ('js', 'node_repl') or not isinstance(arguments.get('code'), str):
            continue
        attempts, has_read = _node_attempts(arguments['code'], node_state['bindings'])
        try:
            writes, labelled, candidates = _node(arguments['code'], output, node_state)
        except (ValueError, SyntaxError, RecursionError):
            node_state = {'bindings': {}, 'modules': set()}
            uncertain = {name for path in attempts for name in paths.get(path, [])}
            if has_read:
                # Unknown transformations must not make a negative pass when
                # the actual result contains complete forbidden guidance.
                uncertain.update(name for name in known if any(targets[name]['content'] in text for value in _rendered_values(output) for text in _strings_in_data(value)))
            mark_uncertain(uncertain, index)
            continue
        if _TRUNCATED.search(output) and not any(output == target['content'] for target in targets.values()):
            continue
        sequence, sequence_reads = [], []
        for value in writes:
            if isinstance(value, str):
                sequence.append(value)
            elif isinstance(value, _Read) and value.path in paths:
                expected = targets[paths[value.path][0]]['content']
                bounds = _slice_range(expected, value.start, value.end)
                if bounds is None:
                    sequence = None
                    break
                start, end = bounds
                sequence.append(expected[start:end])
                sequence_reads.append((value, expected[start:end], start, end))
            else:
                sequence = None
                break
        rendered = _rendered_values(output)
        if sequence and any(value in (''.join(sequence), '\n'.join(sequence)) for value in rendered if isinstance(value, str)):
            for read, content, start, end in sequence_reads:
                add(read.path, content, index, start, end)
        for read in labelled:
            for name in paths.get(read.path, []):
                content = targets[name]['content']
                marker = 'FILE: ' + read.path
                start = output.find(marker)
                if start >= 0:
                    payload = output[start + len(marker):]
                    if payload.startswith(content) and (len(payload) == len(content) or payload[len(content):].startswith('FILE: ')):
                        add(read.path, content, index)
        for read, value in candidates:
            for name in paths.get(read.path, []):
                expected = targets[name]['content']
                bounds = _slice_range(expected, read.start, read.end)
                if bounds is None:
                    continue
                start, end = bounds
                content = expected[start:end]
                if content and content == value:
                    add(read.path, content, index, start, end)
        for path in attempts:
            for name in paths.get(path, []):
                if not any(read['target'] == name and read['event'] == index for read in reads):
                    mark_uncertain([name], index)
    if not completed or failed:
        failures.append('Codex turn did not complete successfully')
    for name, event in exposures:
        if not any(read['target'] == name and read['event'] == event for read in reads):
            mark_uncertain([name], event)
    for name in required:
        if name not in complete_at:
            failures.append(f'Complete content was not observed: {name}')
    for name in forbidden:
        if name in first:
            failures.append(f'Forbidden guidance was read: {name}')
    for earlier, later in before:
        if (earlier not in complete_at or later not in first or complete_at[earlier] >= first[later]
                or any(name == later and event <= complete_at[earlier] for name, event in unverified)):
            failures.append(f'Read order not established: {earlier} before {later}')
    return {'passed': not failures, 'completed': completed and not failed, 'reads': reads, 'failures': failures}
