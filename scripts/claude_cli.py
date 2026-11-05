"""Claude Code CLI invocations: isolated structured calls and the maintainer review."""
import hashlib
import json
import os
import re
from pathlib import Path

SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
RESTRICTED_MODEL = 'claude-opus-5-5'
REVIEW_MODEL = 'claude-opus-5-5'
EFFORT = 'xhigh'
# --json-schema answers through this synthetic tool; a restricted session may expose nothing else.
STRUCTURED_TOOL = 'StructuredOutput'
DEFAULT_CLI = Path.home() / '.local/bin/claude'
MAX_STREAM = 1000000
# Set by a running Claude Code session for its own process tree. CLAUDE_CODE_EFFORT_LEVEL would
# override the effort each command pins.
SESSION_VARIABLES = ('CLAUDECODE', 'CLAUDE_EFFORT', 'CLAUDE_PID', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_CHILD_SESSION',
                     'CLAUDE_CODE_EXECPATH', 'CLAUDE_CODE_SSE_PORT', 'CLAUDE_CODE_EFFORT_LEVEL')
SESSION_PREFIXES = ('CLAUDE_CODE_SESSION_', 'CLAUDE_CODE_MESSAGING_', 'CLAUDE_AGENT_SDK_')
# Without any of these flags a call would lose isolation or change its output format, so callers fail closed.
RESTRICTED_FLAGS = ('--print', '--setting-sources', '--tools', '--strict-mcp-config', '--disable-slash-commands',
                    '--no-session-persistence', '--model', '--effort', '--output-format', '--verbose', '--json-schema')
REVIEW_FLAGS = ('--print', '--no-session-persistence', '--model', '--effort', '--permission-mode',
                '--output-format', '--verbose')
FAILURE_PATTERNS = (
    ('authentication', r'\b401\b|\b403\b|unauthori[sz]ed|not logged in|/login\b|invalid api key|'
                       r'authentication[ _](?:failed|required|error)|oauth token'),
    ('rate limit', r'\b429\b|rate.?limit|usage limit|limit reached|quota exceeded|too many requests'),
    ('connection', r'connection (?:reset|refused|closed|error)|stream disconnected|network (?:unreachable|error)|'
                   r'timed out|unexpected eof|econnreset|etimedout|fetch failed|overloaded|\b50[0234]\b|\b529\b'))


def cli_path(config):
    """The configured Claude Code executable, defaulting to the native installer's location."""
    return Path(config.get('claude_path') or DEFAULT_CLI).expanduser()


def child_env(base=None):
    """Detach a child CLI from any Claude Code session that started this process, and keep it stateless.

    Authentication and provider settings (ANTHROPIC_*, CLAUDE_CONFIG_DIR, CLAUDE_CODE_OAUTH_TOKEN,
    CLAUDE_CODE_USE_*) pass through; variables a parent session sets for its own process tree do not.
    """
    source = os.environ if base is None else base
    env = {key: value for key, value in source.items()
           if key not in SESSION_VARIABLES and not key.startswith(SESSION_PREFIXES)}
    env['CLAUDE_CODE_DISABLE_AUTO_MEMORY'] = '1'
    # No auto-update, telemetry or error reporting mid-run; scheduled jobs must behave the same every hour.
    env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] = '1'
    return env


def missing_flags(help_text, flags):
    return [flag for flag in flags if not re.search(r'(?<![\w-])' + re.escape(flag) + r'(?![\w-])', help_text)]


def restricted_command(cli, schema, *, model=RESTRICTED_MODEL, effort=EFFORT):
    """No settings, CLAUDE.md, tools, MCP servers, skills, transcript or memory; the answer must match `schema`.

    `--setting-sources ""` also keeps user and project CLAUDE.md files out (verified with Claude Code 2.1.318).
    """
    return [str(cli), '--print', '--setting-sources', '', '--tools', '', '--strict-mcp-config',
            '--disable-slash-commands', '--no-session-persistence', '--model', model, '--effort', effort,
            '--output-format', 'stream-json', '--verbose', '--json-schema', json.dumps(schema, separators=(',', ':'))]


def review_command(cli, *, model=REVIEW_MODEL, effort=EFFORT):
    """The maintainer review keeps the user's Claude Code configuration, with full tool access and no prompts."""
    return [str(cli), '--print', '--no-session-persistence', '--model', model, '--effort', effort,
            '--permission-mode', 'bypassPermissions', '--output-format', 'stream-json', '--verbose']


def events(stdout, *, partial=False):
    """Decode stream-json output; only a killed (partial) run may end with a truncated line."""
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors='replace')
    stdout = stdout or ''
    if len(stdout) > MAX_STREAM:
        raise RuntimeError('Claude event output exceeds safety bound')
    lines = stdout.splitlines()
    decoded = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if partial and index == len(lines) - 1 and not stdout.endswith('\n'):
                break
            raise
        if not isinstance(event, dict):
            raise ValueError('Invalid Claude output')
        decoded.append(event)
    return decoded


def structured_run(stdout, *, partial=False):
    """Return (phase, structured output or None, error text, result facts) for a restricted run.

    The phase is 'startup' before the session starts, 'inference' after it starts and 'completed' once it
    reports a result. The result facts are the last result event's static subtype and API status for failure
    diagnostics, both None without a result event. Raises RuntimeError when the session exposed or used any
    tool besides StructuredOutput.
    """
    phase, output, errors = 'startup', None, []
    facts = {'result_subtype': None, 'api_error_status': None}
    for event in events(stdout, partial=partial):
        kind = event.get('type')
        if kind == 'system' and event.get('subtype') == 'init':
            phase = 'inference'
            if set(event.get('tools') or ()) - {STRUCTURED_TOOL} or event.get('mcp_servers'):
                raise RuntimeError('Claude session exposed tools')
        elif kind == 'assistant':
            message = event.get('message')
            content = message.get('content') if isinstance(message, dict) else None
            if not isinstance(content, list):
                raise ValueError('Invalid Claude output')
            if any(isinstance(block, dict) and block.get('type') in ('tool_use', 'server_tool_use')
                   and block.get('name') != STRUCTURED_TOOL for block in content):
                raise RuntimeError('Capture model attempted tool execution')
        elif kind == 'rate_limit_event' and (event.get('rate_limit_info') or {}).get('status') == 'rejected':
            errors.append('rate limit rejected')
        elif kind == 'result':
            phase = 'completed'
            facts = result_facts(event)
            if event.get('is_error') or event.get('subtype') != 'success':
                status = event.get('api_error_status')
                errors.append(' '.join(str(part) for part in (status, event.get('subtype'), event.get('result'))
                                       if part))
            elif isinstance(event.get('structured_output'), dict):
                output = event['structured_output']
        error = event.get('error')
        if isinstance(error, dict):
            error = error.get('message')
        if isinstance(error, str) and error:
            errors.append(error)
    return phase, output, '\n'.join(errors), facts


def final_result(stdout):
    """The final reply of a completed review run: its last result event, wherever later lines fall.

    A full-access review's stream can exceed MAX_STREAM, so lines are decoded from the end, one at a time.
    """
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors='replace')
    for line in reversed((stdout or '').splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get('type') == 'result':
            if not event.get('is_error') and event.get('subtype') == 'success' and isinstance(event.get('result'), str):
                return event['result']
            break
    raise RuntimeError('Claude review did not complete successfully')


def result_facts(result):
    """Static facts about a result event for failure diagnostics; never model or CLI text."""
    subtype, status = result.get('subtype'), result.get('api_error_status')
    if subtype is None:
        label = 'missing'
    elif isinstance(subtype, str) and re.fullmatch(r'[a-z0-9_]{1,40}', subtype):
        label = subtype
    else:
        label = 'other'
    return {'result_subtype': label,
            'api_error_status': status if isinstance(status, int) and not isinstance(status, bool) else None}


def failure_class(text):
    for label, pattern in FAILURE_PATTERNS:
        if re.search(pattern, text or '', re.I):
            return label
    return 'CLI or output'
