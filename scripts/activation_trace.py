"""Score observed skill content in agent traces, without replaying saved code.

``score_claude_trace`` reads Claude Code stream-json from live evaluations.
``score_trace`` and its helpers read Codex CLI JSONL; capture.py still uses
them for historical Codex rollouts.

The caller supplies immutable pre-run file snapshots. A requested path, a tool's
start event, or an assistant saying it used a skill is never loading evidence.
Unknown tool formats remain unverified rather than being inferred from prose.
"""
import ast
from dataclasses import dataclass
import hashlib
import fnmatch
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import warnings

from read_guidance import observed, request

SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


_STRING = r'''(?:"(?:[^"\\]|\\[\s\S])*"|'(?:[^'\\]|\\[\s\S])*')'''
_TRUNCATED = re.compile(r'(?im)^(?:warning:\s*)?(?:truncated output\b|output[^\n]*truncated\b|.*tokens truncated\b|.*lines omitted\b)')


def _digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


_JS_ESCAPES = {'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t', 'v': '\v'}
_JS_STRING = re.compile(r'\\(?:u\{([0-9A-Fa-f]{1,6})\}|u([0-9A-Fa-f]{4})|x([0-9A-Fa-f]{2})|(0)(?![0-9])|(\r\n?|[\n\u2028\u2029])|([^0-9xu]))')


def js_string(token):
    """Decode a JavaScript string literal from a tool call exactly as strict-mode JavaScript would, or raise ValueError.

    Escapes pair from the left, so '\\\\|' is an escaped backslash and a literal bar, and an escape with no
    meaning of its own, such as '\\|', is that character. Legacy octal and malformed escapes, unescaped
    quotes or line breaks and lone surrogates stay unknown rather than adopting another language's semantics.
    """
    if len(token) < 2 or token[0] not in '"\'' or token[-1] != token[0]:
        raise ValueError('Not a JavaScript string literal')
    body, position, parts = token[1:-1], 0, []
    for match in _JS_STRING.finditer(body):
        parts.append(body[position:match.start()])
        braced, unicode, hexadecimal, zero, continuation, character = match.groups()
        if braced or unicode or hexadecimal:
            value = int(braced or unicode or hexadecimal, 16)
            if value > 0x10FFFF:
                raise ValueError('Invalid JavaScript code point')
            parts.append(chr(value))
        else:
            parts.append('' if continuation else '\0' if zero else _JS_ESCAPES.get(character, character))
        position = match.end()
    parts.append(body[position:])
    # Unmatched text may hold no closing quote, raw line break or unpaired backslash.
    if any(re.search(r'[\n\r\\]|' + token[0], part) for part in parts[::2]):
        raise ValueError('Invalid JavaScript string')
    # JavaScript strings are UTF-16: join escaped surrogate pairs and refuse lone halves.
    text = re.sub('[\ud800-\udbff][\udc00-\udfff]',
                  lambda pair: chr(0x10000 + ((ord(pair[0][0]) - 0xD800) << 10) + ord(pair[0][1]) - 0xDC00), ''.join(parts))
    if re.search('[\ud800-\udfff]', text):
        raise ValueError('Lone surrogate in JavaScript string')
    return text


def quiet_parse(parse, text):
    """Parse transcript text that really is Python syntax, such as a tool envelope's
    repr or a saved Python heredoc, without warnings for invalid escapes like '\\|'.

    Python 3.12 emits SyntaxWarning for them, and older versions DeprecationWarning.
    """
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', SyntaxWarning)
        warnings.simplefilter('ignore', DeprecationWarning)
        return parse(text)


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


def _utility_name(value):
    """Bound utility contracts to bare names and standard system locations."""
    name = value.rsplit('/', 1)[-1]
    return name if '/' not in value or value in ('/bin/' + name, '/usr/bin/' + name) else None


def _shell_source(command):
    words = shlex.split(command)
    if len(words) == 3 and _utility_name(words[0]) in ('sh', 'bash', 'zsh') and words[1] in ('-c', '-lc'):
        return words[2]
    return command


def _shell(command):
    return shlex.split(_shell_source(command))


def _listing_prefix(words):
    """Only side-effect-free inventory commands may precede a final read."""
    if not words:
        return False
    name = _utility_name(words[0])
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


def _inspection_reads(command):
    """Bound possible reads in a literal inspection sequence, never loading.

    Newlines, semicolons and && may join standard inventory, cat and nl
    commands. No command changes shell state or interprets source. Keeping
    this grammar narrow lets disjoint project inspections establish absence
    without treating arbitrary compound programs as understood.
    """
    source = _shell_source(command)
    if any(char in source for char in '$`#'):
        return None
    # Split source before decoding argv: shlex's POSIX tokenization removes
    # the distinction between a real && operator and a quoted "&&" filename.
    groups, start, index, quote = [], 0, 0, None
    while index < len(source):
        char = source[index]
        if char == '\\' and quote != "'":
            index += 2
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char in '|()<>':
            return None
        elif char in ';&\n':
            width = 2 if source[index:index + 2] == '&&' else 1
            if char == '&' and width != 2:
                return None
            part = source[start:index].strip()
            if not part:
                return None
            groups.append(shlex.split(part))
            start = index + width
            index += width
            continue
        index += 1
    if quote or source.rstrip().endswith('&&'):
        return None
    if source[start:].strip():
        groups.append(shlex.split(source[start:]))
    if not groups:
        return None
    operands = []
    for words in groups:
        if _listing_prefix(words):
            continue
        name = _utility_name(words[0])
        if name not in ('cat', 'nl'):
            return None
        index = 1
        while index < len(words) and words[index].startswith('-'):
            option = words[index]
            index += 1
            if option == '--':
                break
            if not ((name == 'cat' and re.fullmatch(r'-[AbEenstTuv]+', option))
                    or (name == 'nl' and option in ('-ba', '-bt', '-bn'))):
                return None
        files = words[index:]
        if not files or any(not re.fullmatch(r'[\w ./,:@%+=-]+', path)
                            or path == '-' or any(part in ('.', '..') for part in path.split('/'))
                            for path in files):
            return None
        operands.extend(files)
    return operands


def _tool_text(result, include_error=False):
    if not isinstance(result, dict) or (result.get('isError') and not include_error):
        return None
    content = result.get('content')
    if not isinstance(content, list):
        return None
    texts = [block['text'] for block in content if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str)]
    return '\n'.join(texts)


def _node_attempts(code, state):
    """Identify possible read paths without evaluating code or granting proof.

    Unknown programs cannot establish absence or ordering. Literal bindings
    narrow paths only when the surrounding program preserves supported
    filesystem contracts; this never extends accepted positive evidence.
    """
    code = _without_comments(code)
    try:
        statements = _split(code)
    except ValueError:
        statements = [code]
    bindings, values, unresolved = dict(state['bindings']), set(), False
    for statement in statements:
        declaration = re.fullmatch(r'(?:var|let|const)\s+(\w+)\s*=\s*([\s\S]+)', statement)
        if declaration:
            try:
                bindings[declaration[1]] = _value(declaration[2], bindings)
            except (ValueError, SyntaxError, RecursionError):
                bindings.pop(declaration[1], None)
        # Keep offsets while removing strings so printed copies are not calls.
        bare = re.sub(_STRING, lambda m: ' ' * len(m[0]), statement)
        calls = list(re.finditer(r'\breadFile(?:Sync)?\s*\(', bare))
        for call in calls:
            depth, end = 1, call.end()
            while end < len(bare) and depth:
                if bare[end] == '(':
                    depth += 1
                elif bare[end] == ')':
                    depth -= 1
                end += 1
            value = None
            if not depth:
                try:
                    arguments = _split(statement[call.end():end - 1], ',')
                    value = _value(arguments[0], bindings) if arguments else None
                except (ValueError, SyntaxError, RecursionError):
                    pass
            if isinstance(value, str):
                values.add(value)
                if not value.startswith('/') and value.endswith('SKILL.md'):
                    unresolved = True
            else:
                unresolved = True
    # Narrow to unrelated literal paths only when the surrounding program
    # preserves the supported filesystem bindings. An arbitrary mutation
    # could redirect readFile even when its apparent operand is unrelated.
    # Literal conditionals are safe to unwrap for *possible* reads only;
    # this never grants positive evidence or claims the branch executed.
    narrowed = []
    for statement in statements:
        conditional = re.fullmatch(r'if\s*\(\s*(?:true|false)\s*\)\s*\{([\s\S]*)\}', statement)
        narrowed.append(conditional[1] if conditional else statement)
    try:
        _node(';'.join(narrowed), '', {'bindings': dict(state['bindings']), 'modules': set(state['modules'])})
    except (ValueError, SyntaxError, RecursionError):
        unresolved = True
    return values, unresolved


def _shell_substitution(command):
    """Recognize executable substitutions, excluding quotes and comments."""
    source = _shell_source(command)
    quote, index = None, 0
    while index < len(source):
        char = source[index]
        if quote == "'":
            if char == "'":
                quote = None
        elif char == '\\':
            index += 1
        elif char == "'" and quote is None:
            quote = "'"
        elif char == '"':
            quote = None if quote == '"' else '"'
        elif char == '#' and quote is None and (index == 0 or source[index - 1].isspace() or source[index - 1] in ';&|()'):
            end = source.find('\n', index)
            if end < 0:
                return False
            index = end
        elif char == '`' or char == '$' and source[index + 1:index + 2] == '(':
            return True
        index += 1
    return False


def _shell_attempts(command):
    """Find possible consuming operands, excluding clear print/inventory forms.

    Only bounded literal reader commands can establish disjoint file operands.
    Unknown executables may compute paths or run embedded source; absence of
    a literal target operand does not establish that their reads are unrelated.
    """
    lexer = shlex.shlex(_shell_source(command), posix=False, punctuation_chars=';&|()<>\n')
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    lexer.commenters = ''
    groups, current, inputs, input_next, comment, complex_program = [], [], [], False, False, False
    for token in lexer:
        if token.startswith('#'):
            comment = True
        if comment and '\n' not in token:
            continue
        if token and all(char in ';&|()<>\n' for char in token):
            complex_program = True
            comment = False
            if current:
                groups.append(current)
            current = []
            input_next = token in ('<', '<>')
        else:
            value = shlex.split(token)
            value = value[0] if len(value) == 1 else token
            if input_next:
                inputs.append(value)
                input_next = False
            else:
                current.append(value)
    if current:
        groups.append(current)
    operands, inert, opaque_program = list(inputs), not inputs, False
    for words in groups:
        while words and re.match(r'^[A-Za-z_][\w]*=', words[0]):
            words = words[1:]
        if not words:
            continue
        executable = words[0].rsplit('/', 1)[-1]
        # A custom executable named head/printf need not implement the utility.
        standard = _utility_name(words[0]) is not None
        if standard and executable in ('echo', 'printf', 'pwd', 'true', 'false', ':', 'ls', 'stat',
                          'test', '[', 'readlink', 'realpath', 'dirname', 'basename'):
            continue
        if standard and executable == 'find' and not any(word in ('-exec', '-execdir', '-ok', '-okdir', '-files0-from') for word in words):
            continue
        if standard and executable == 'rg' and _listing_prefix(words):
            continue
        inert = False
        # These utilities consume literal operands without interpreting program
        # source. Everything else is opaque unless it is the shared reader's
        # validated invocation. In particular, quoting Python/JS/awk source as
        # one argv value must not turn computed reads into unrelated operands.
        literal_reader = standard and executable in ('cat', 'head', 'tail', 'nl', 'fold', 'cut')
        literal_reader |= (standard and executable == 'sed' and len(words) == 4 and words[1] == '-n'
                           and re.fullmatch(r'\d+,\d+p', words[2]) is not None)
        if not literal_reader:
            try:
                if not standard:
                    raise ValueError('Untrusted interpreter location')
                request(shlex.join(words))
            except ValueError:
                opaque_program = True
        operands.extend(words[1:])
    return operands, complex_program, inert, opaque_program


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
        return js_string(expression)
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
            result = js_string(token)
            while self.pos < len(self.text) and self.text[self.pos:].strip() and self.peek() == '+':
                self.take()
                following = self.take()
                if following[0] not in ('"', "'"):
                    raise ValueError('Inspect concatenation is not literal')
                result += js_string(following)
            return result
        if token in ('[', '{'):
            closing, result = (']', []) if token == '[' else ('}', {})
            if self.peek() == closing:
                self.take()
                return result
            while True:
                if token == '{':
                    key = self.take()
                    key = js_string(key) if key[0] in ('"', "'") else key
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
                value = quiet_parse(ast.literal_eval, value)
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


def _inert_exec(code):
    """Recognize only literal text outputs; unknown orchestration is not inert."""
    try:
        for statement in _split(_without_comments(code)):
            call = re.fullmatch(r'text\(([\s\S]*)\)', statement)
            if not call:
                return False
            parser = _Inspect(call[1])
            parser.value()
            if parser.text[parser.pos:].strip():
                return False
        return True
    except (ValueError, SyntaxError, RecursionError):
        return False


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


def _target_paths(trace_text, targets, required, forbidden, before):
    """Validate snapshots and map each observed path or alias to its targets."""
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
    return known, paths


def score_trace(trace_text, targets, required, forbidden=(), before=()):
    """Return a deterministic activation verdict for supplied pre-run snapshots.

    Complete coverage can comprise multiple verified continuation chunks. Their
    individual hashes remain partial; no synthetic historical full-file hash is
    reported. Ordering requires complete earlier guidance before any later read.
    """
    known, paths = _target_paths(trace_text, targets, required, forbidden, before)
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
            if not words or _utility_name(words[0]) is None:
                return
            expected_request = request(normalized)
            envelope = json.loads(output)
            observed(envelope, expected_request, 'activation')
            path = words[-3] if len(words) >= 2 and words[-2] == '--start' else words[-1]
            add(path, envelope['content'], index, envelope['start'], envelope['start'] + len(envelope['content']))
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
            return

    def failed_output_uncertainty(output):
        # Merged diagnostics and decorated output cannot establish absence.
        # File identity/disjointness comes from the read analysis, not matching
        # rendered text against snapshot lines. This never grants positive proof.
        return set(known) if isinstance(output, str) and output.strip() else set()

    def shell(command, output, index, successful=True):
        if successful:
            shell_evidence(command, output, index)
        elif not failed_output_uncertainty(output):
            return
        try:
            if _shell_substitution(command):
                mark_uncertain(known, index)
            operands = _inspection_reads(command)
            if operands is None:
                operands, complex_program, inert, opaque_program = _shell_attempts(command)
                if opaque_program:
                    mark_uncertain(known, index)
                if complex_program and not inert and _prefixed_read(command) is None:
                    mark_uncertain(known, index)
        except ValueError:
            mark_uncertain(known, index)
            return
        uncertain = {name for path, names in paths.items() if any(
            re.search(r"(?<![\w./-])" + re.escape(path) + r"(?=$|[\s\x27\"),;])", operand)
            or fnmatch.fnmatchcase(path, operand.split('=', 1)[-1])
            for operand in operands) for name in names}
        # Substitution and a basename-only skill read have no attributable path.
        # A successful reader can expose a forbidden guide even with no output.
        if any(re.search(r'\$(?:[A-Za-z_{]|\()|`', operand) or re.search(
                r'(?:^|/)SKILL\.md(?:$|[\s\x27\"),;])', operand)
                and not operand.startswith('/') for operand in operands):
            uncertain.update(known)
        mark_uncertain((name for name in uncertain if not any(
            read['target'] == name and read['event'] == index for read in reads)), index)

    def shell_result(command, result, index):
        exit_code = result.get('exit_code')
        if type(exit_code) is int:
            shell(command, result['output'], index, exit_code == 0)
        else:
            # Unknown success metadata may constrain absence, never prove a read.
            shell(command, '', index)

    def wrapped_shell(code, output, index):
        try:
            commands, indexed = _exec_commands(code)
        except (ValueError, SyntaxError, RecursionError):
            # Unknown orchestration may hide computed or aliased tool calls.
            # Only bounded literal output programs can establish no read.
            if not _inert_exec(code):
                mark_uncertain(known, index)
            return
        results = _results(output)
        if len(commands) == len(results) == 1:
            result = results[0]
            shell_result(commands[0], result, index)
        elif indexed and len(commands) == len(results) and {r.get('source_index') for r in results} == set(range(len(commands))):
            for command, result in zip(commands, sorted(results, key=lambda r: r['source_index'])):
                shell_result(command, result, index)
        else:
            if not results or any(type(result.get('exit_code')) is not int or result['exit_code'] == 0 or failed_output_uncertainty(result.get('output', '')) for result in results):
                for command in commands:
                    shell(command, '', index)
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
            exit_code = item.get('exit_code')
            command = item.get('command')
            if isinstance(command, str) and command.strip():
                output = item.get('aggregated_output')
                shell(command, output if type(exit_code) is int and isinstance(output, str) else '', index,
                      not (type(exit_code) is int and exit_code != 0))
            else:
                mark_uncertain(known, index)
            continue
        if item.get('status') != 'completed' or item.get('error') or (isinstance(item.get('result'), dict) and item['result'].get('isError')):
            result = item.get('result')
            if isinstance(result, dict):
                visible = _tool_text(result, include_error=True) or ''
                arguments = item.get('arguments')
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        arguments = None
                if isinstance(arguments, dict) and item.get('tool') == 'exec_command' and isinstance(arguments.get('cmd'), str):
                    shell(arguments['cmd'], visible, index, False)
                elif (isinstance(arguments, dict) and item.get('server') in ('node_repl', 'mcp__node_repl')
                      and isinstance(arguments.get('code'), str)):
                    attempts, unresolved = _node_attempts(arguments['code'], node_state)
                    possible = known if unresolved else {name for path in attempts for name in paths.get(path, [])}
                    mark_uncertain(failed_output_uncertainty(visible) & set(possible), index)
                    node_state = {'bindings': {}, 'modules': set()}
                else:
                    mark_uncertain(failed_output_uncertainty(visible), index)
            if item.get('server') in ('node_repl', 'mcp__node_repl'):
                node_state = {'bindings': {}, 'modules': set()}
            continue
        output = _tool_text(item.get('result'))
        arguments = item.get('arguments')
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = None
        if output is None or not isinstance(arguments, dict):
            mark_uncertain(known, index)
            continue
        if item.get('server') in ('functions', 'exec', 'shell', 'codex') and item.get('tool') == 'exec_command' and isinstance(arguments.get('cmd'), str):
            # Only the visible result payload is loading evidence. Hidden
            # transport metadata containing stdout is not a substitute.
            results = _results(output)
            if len(results) == 1 and type(results[0].get('exit_code')) is int:
                shell_result(arguments['cmd'], results[0], index)
            elif len(results) != 1 or type(results[0].get('exit_code')) is not int:
                shell(arguments['cmd'], '', index)
            continue
        if item.get('server') in ('functions', 'exec') and item.get('tool') == 'exec' and isinstance(arguments.get('code'), str):
            wrapped_shell(arguments['code'], output, index)
            continue
        if item.get('server') not in ('node_repl', 'mcp__node_repl') or item.get('tool') not in ('js', 'node_repl') or not isinstance(arguments.get('code'), str):
            mark_uncertain(known, index)
            continue
        attempts, unresolved = _node_attempts(arguments['code'], node_state)
        try:
            writes, labelled, candidates = _node(arguments['code'], output, node_state)
        except (ValueError, SyntaxError, RecursionError):
            node_state = {'bindings': {}, 'modules': set()}
            uncertain = {name for path in attempts for name in paths.get(path, [])}
            if unresolved:
                # No output length can prove absence when the read path is
                # unresolved. Do not infer safety from a short or empty slice.
                uncertain.update(known)
            mark_uncertain(uncertain, index)
            continue
        if _TRUNCATED.search(output) and not any(output == target['content'] for target in targets.values()):
            mark_uncertain(known if unresolved else (name for path in attempts for name in paths.get(path, [])), index)
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
        if unresolved:
            mark_uncertain(known, index)
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


_SKILL_BASE = 'Base directory for this skill: '
_FRONTMATTER = re.compile(r'---\r?\n[\s\S]*?\r?\n---[ \t]*(?:\r?\n|$)')


def _skill_bodies(content):
    """Text the Claude Code Skill tool injects for a SKILL.md snapshot.

    The injection carries the body after the YAML frontmatter, whose name and
    description the session already lists as skill metadata. Observed
    injections drop the body's leading line breaks; nothing else may differ.
    """
    match = _FRONTMATTER.match(content)
    body = content[match.end():] if match else content
    return {text for text in (body, re.sub(r'^\r?\n', '', body), re.sub(r'^(?:\r?\n)+', '', body)) if text.strip()}


def _read_bounds(expected, file):
    """Character bounds of a Read result's raw content within a snapshot.

    Read numbers lines from 1 and counts the empty line after a final newline.
    A returned line implies its terminator, so line-chunked reads can tile the
    file. Any difference from the snapshot's lines yields no evidence.
    """
    start_line, count, total = file.get('startLine'), file.get('numLines'), file.get('totalLines')
    if any(type(value) is not int for value in (start_line, count, total)) or start_line < 1 or count < 1:
        return None
    lines = expected.split('\n')
    if total != len(lines) or start_line - 1 + count > len(lines):
        return None
    text = '\n'.join(lines[start_line - 1:start_line - 1 + count])
    if file.get('content') != text:
        return None
    start = sum(len(line) + 1 for line in lines[:start_line - 1])
    end = start + len(text)
    return start, end + 1 if end < len(expected) else end


def score_claude_trace(trace_text, targets, required, forbidden=(), before=()):
    """Score Claude Code stream-json with the same semantics as score_trace.

    Evidence is tool output returned in the session: a Skill tool injection of
    a SKILL.md body, attributed to the successful Skill call that produced it,
    or a Read result's raw content (``tool_use_result``; the tool_result text
    carries line-number prefixes). Requested paths and assistant text are never
    evidence. A failed, unattributed or unsupported request for a target, or any
    tool other than Read and Skill, leaves that target unverified: it fails a
    forbidden expectation and ordering, and never satisfies a required one.
    """
    known, paths = _target_paths(trace_text, targets, required, forbidden, before)
    real = {}
    for path, names in paths.items():
        real.setdefault(os.path.realpath(path), set()).update(names)
    skill_dirs = {path[:-len('/SKILL.md')]: names for path, names in paths.items() if path.endswith('/SKILL.md')}
    failures, reads, intervals, first, complete_at, unverified = [], [], {}, {}, {}, []
    completed, failed = False, False
    calls, answered, attempts, loaded_skills = {}, set(), {}, []

    def lookup(path):
        """Targets a path names; None when it could name any of them."""
        if not isinstance(path, str) or not path:
            return None
        if not path.startswith('/'):
            # A relative path resolves against an unrecorded cwd: suffixes bound it.
            relative = posixpath.normpath(path)
            if relative == '.' or relative.split('/')[0] == '..':
                return None
            return {name for candidate, names in paths.items() if candidate.endswith('/' + relative) for name in names}
        return set(paths.get(path, ())) | real.get(os.path.realpath(path), set())

    def mark_uncertain(names, event):
        for name in sorted(set(names)):
            unverified.append((name, event))
            if name in forbidden:
                failures.append(f'Unverified forbidden read attempt: {name}')

    def add(name, call, event, observed, start, end):
        expected = targets[name]['content']
        record = {'target': name, 'event': event,
                  'coverage': 'complete' if start == 0 and end == len(expected) else 'partial',
                  'sha256': _digest(observed), 'start': start, 'end': end}
        attempts.get(call, {}).pop(name, None)
        if record in reads:
            return
        reads.append(record)
        # Ordering counts from the request: a later guide requested before the
        # earlier one completed was not read after it, whenever it returned.
        first.setdefault(name, calls[call][2])
        intervals.setdefault(name, []).append((start, end))
        covered = 0
        for begin, stop in sorted(intervals[name]):
            if begin > covered:
                break
            covered = max(covered, stop)
        if covered == len(expected):
            complete_at.setdefault(name, event)

    def exposed(text, event):
        # Full target content in unattributed output is uncertainty, not proof.
        for name in known:
            if targets[name]['content'] in text and not any(r['target'] == name and r['event'] == event for r in reads):
                mark_uncertain([name], event)

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
        if kind == 'result':
            completed = event.get('subtype') == 'success' and event.get('is_error') is False
            failed |= not completed
            continue
        if kind not in ('assistant', 'user'):
            continue
        content = (event.get('message') or {}).get('content') if isinstance(event.get('message'), dict) else None
        if isinstance(content, str):
            content = [{'type': 'text', 'text': content}]
        if not isinstance(content, list) or event.get('parent_tool_use_id') is not None:
            # Unknown message shapes and subagent activity cannot establish absence.
            mark_uncertain(known, index)
            continue
        if kind == 'assistant':
            for block in content:
                if not isinstance(block, dict) or not str(block.get('type', '')).endswith('tool_use'):
                    continue
                identity, tool, arguments = block.get('id'), block.get('name'), block.get('input')
                if not isinstance(identity, str) or identity in calls or not isinstance(arguments, dict):
                    mark_uncertain(known, index)
                    continue
                calls[identity] = (tool, arguments, index)
                if tool == 'Read':
                    names = lookup(arguments.get('file_path'))
                elif tool == 'Skill' and isinstance(arguments.get('skill'), str):
                    skill = arguments['skill'].lstrip('/')
                    names = {skill} & set(targets)
                else:
                    names = None
                if names is None:
                    # Another tool or an unattributable request may read anything.
                    mark_uncertain(known, index)
                    continue
                attempts[identity] = {name: index for name in names}
            continue
        results = [block for block in content if isinstance(block, dict) and block.get('type') == 'tool_result']
        for block in results:
            identity = block.get('tool_use_id')
            if identity not in calls or identity in answered:
                mark_uncertain(known, index)
                continue
            answered.add(identity)
            tool, arguments, _ = calls[identity]
            data = event.get('tool_use_result')
            # Structured output belongs to a single result; never split it.
            if block.get('is_error') or len(results) != 1 or not isinstance(data, dict):
                continue
            if tool == 'Skill' and data.get('success') is True and isinstance(data.get('commandName'), str):
                loaded_skills.append((identity, data['commandName'], arguments.get('args')))
            elif tool == 'Read' and data.get('type') == 'text' and isinstance(data.get('file'), dict):
                file = data['file']
                if (file.get('filePath') != arguments.get('file_path') or not isinstance(file.get('content'), str)
                        or not str(file['filePath']).startswith('/')):
                    continue
                for name in lookup(file['filePath']) or ():
                    bounds = _read_bounds(targets[name]['content'], file)
                    if bounds:
                        add(name, identity, index, file['content'], *bounds)
                exposed(file['content'], index)
        if event.get('isSynthetic') is not True:
            continue
        for block in content:
            text = block.get('text') if isinstance(block, dict) and block.get('type') == 'text' else None
            if not isinstance(text, str) or not text.startswith(_SKILL_BASE):
                continue
            header, separator, body = text.partition('\n\n')
            names = skill_dirs.get(header[len(_SKILL_BASE):], [])
            for name in names:
                call = next((item for item in loaded_skills if item[1] == name), None)
                if call is None:
                    mark_uncertain([name], index)
                    continue
                loaded_skills.remove(call)
                identity, _, args = call
                for expected in _skill_bodies(targets[name]['content']):
                    if body == expected or isinstance(args, str) and body == expected + '\n\nARGUMENTS: ' + args:
                        add(name, identity, index, expected, 0, len(targets[name]['content']))
                        break
            if not names:
                for name in known:
                    if any(expected in body for expected in _skill_bodies(targets[name]['content'])):
                        mark_uncertain([name], index)
    for pending in attempts.values():
        for name, event in pending.items():
            mark_uncertain([name], event)
    if not completed or failed:
        failures.append('Claude session did not complete successfully')
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
