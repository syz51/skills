#!/usr/bin/env python3
"""One structured Claude API call, run in its own process by capture.model_response.

The request arrives as JSON on stdin and one JSON result leaves on stdout. The call has no tools by
construction: the fixed prompt is the system prompt and the untrusted data is the only user message.
Failures report static categories and numbers only, never model, prompt or error text.
Runs through `uv run --with anthropic==SDK_VERSION`, so the pipeline's own scripts stay stdlib-only.
"""
from datetime import datetime
import email.utils
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time

SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SDK_VERSION = '1.17.0'
# Dependencies of the pinned SDK resolve as of this date, so a later release cannot enter the key's process.
RESOLVED_AS_OF = '2026-10-21T00:00:00Z'
MAX_TOKENS = 16000
CATEGORIES = ('rate limit', 'authentication', 'billing', 'invalid request', 'timeout', 'connection', 'refusal',
              'output limit', 'invalid output')
# Structured outputs reject these constraints, so they are restated in the field's description for the model;
# the pipeline still validates every response itself.
UNSUPPORTED = {'minLength': 'At least {} characters.', 'maxLength': 'At most {} characters.',
               'minItems': 'At least {} items.', 'maxItems': 'At most {} items.', 'minimum': 'At least {}.',
               'maximum': 'At most {}.', 'multipleOf': 'A multiple of {}.'}


def api_schema(value):
    if isinstance(value, list):
        return [api_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    # Under 'properties' the keys are field names, never keywords.
    result = {key: {name: api_schema(field) for name, field in item.items()} if key == 'properties' and isinstance(item, dict)
              else api_schema(item) for key, item in value.items() if key not in UNSUPPORTED}
    limits = ' '.join(text.format(value[key]) for key, text in UNSUPPORTED.items() if key in value)
    if limits:
        result['description'] = ' '.join(filter(None, (value.get('description'), limits)))
    return result


def reset_time(headers, now):
    """The latest reset the response announces: retry-after, or an anthropic-ratelimit-*-reset header."""
    candidates = []
    retry = headers.get('retry-after')
    if retry is not None:
        try:
            seconds = float(retry)
            if math.isfinite(seconds):
                candidates.append(now + seconds)
        except ValueError:
            try:
                candidates.append(email.utils.parsedate_to_datetime(retry).timestamp())
            except (TypeError, ValueError):
                pass
    for name, value in headers.items():
        if name.lower().startswith('anthropic-ratelimit-') and name.lower().endswith('-reset'):
            try:
                candidates.append(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp())
            except ValueError:
                pass
    candidates = [value for value in candidates if math.isfinite(value) and 0 < value < 10 ** 10]
    return math.ceil(max(candidates)) if candidates else None


def violations(schema, value, path='$'):
    """The stripped length and count limits a structured answer breaks, as static paths and limits."""
    found = []
    if isinstance(value, str) and isinstance(schema.get('maxLength'), int) and len(value) > schema['maxLength']:
        found.append(f"{path} must be at most {schema['maxLength']} characters")
    if isinstance(value, str) and isinstance(schema.get('minLength'), int) and len(value) < schema['minLength']:
        found.append(f"{path} must be at least {schema['minLength']} characters")
    if isinstance(value, list):
        if isinstance(schema.get('maxItems'), int) and len(value) > schema['maxItems']:
            found.append(f"{path} must have at most {schema['maxItems']} items")
        if isinstance(schema.get('items'), dict):
            for index, item in enumerate(value):
                found += violations(schema['items'], item, f'{path}[{index}]')
    if isinstance(value, dict) and isinstance(schema.get('properties'), dict):
        for key, item in value.items():
            if isinstance(schema['properties'].get(key), dict):
                found += violations(schema['properties'][key], item, f'{path}.{key}')
    for branch in schema.get('anyOf', []):
        # Check the branch the value's shape selects; other branches do not apply.
        if isinstance(branch, dict) and (isinstance(value, dict) and 'properties' in branch
                                         or isinstance(value, list) and branch.get('type') == 'array'
                                         or isinstance(value, str) and branch.get('type') == 'string'):
            found += violations(branch, value, path)
    return found


CACHE = {'type': 'ephemeral'}


def user_content(request):
    """The data as text blocks: up to `cache_prefix` characters end on a cache breakpoint, so calls sharing that
    prefix (the same notes, or a repeated evaluation case) read it from the cache. The text is unchanged."""
    data, prefix = request['data'], request.get('cache_prefix')
    if not prefix:
        return [{'type': 'text', 'text': data}]
    if prefix >= len(data):
        return [{'type': 'text', 'text': data, 'cache_control': CACHE}]
    return [{'type': 'text', 'text': data[:prefix], 'cache_control': CACHE}, {'type': 'text', 'text': data[prefix:]}]


def call(request, client, anthropic):
    """Return the result object for one request; `anthropic` is the SDK module, `client` its client.

    An answer that breaks a limit structured outputs cannot enforce gets one corrective follow-up naming only
    the field paths and limits. The reply is sent back unchanged, so the conversation stays append-only.
    """
    end = time.monotonic() + request['timeout']
    # The fixed prompt is the same for every call of a role, so it is cached; a breakpoint costs nothing when the
    # prefix is below the model's minimum, and a cache write pays for itself on the second call within five minutes.
    system = [{'type': 'text', 'text': request['prompt'], 'cache_control': CACHE}]
    messages = [{'role': 'user', 'content': user_content(request)}]
    usage = dict.fromkeys(('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'), 0)
    for attempt in range(2):
        try:
            response = client.with_options(timeout=max(1.0, end - time.monotonic())).messages.create(
                model=request['model'], max_tokens=request['max_tokens'], system=system, messages=list(messages),
                output_config={'effort': request['effort'],
                               'format': {'type': 'json_schema', 'schema': api_schema(request['schema'])}})
        except anthropic.RateLimitError as error:
            return {'type': 'error', 'category': 'rate limit', 'status': 429,
                    'rate_limit_resets_at': reset_time(error.response.headers, time.time())}
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as error:
            return {'type': 'error', 'category': 'authentication', 'status': error.status_code}
        except anthropic.BadRequestError as error:
            # An exhausted credit balance is a billing problem, not a malformed request.
            billing = 'credit balance' in str(error).lower()
            return {'type': 'error', 'category': 'billing' if billing else 'invalid request', 'status': error.status_code}
        except anthropic.APITimeoutError:
            return {'type': 'error', 'category': 'timeout', 'status': None}
        except anthropic.APIConnectionError:
            return {'type': 'error', 'category': 'connection', 'status': None}
        except anthropic.APIStatusError as error:
            # 402 is an exhausted or unpaid Console balance; 5xx and 529 overloaded are transient.
            category = 'billing' if error.status_code == 402 else 'connection' if error.status_code >= 500 else 'invalid request'
            return {'type': 'error', 'category': category, 'status': error.status_code}
        for key in usage:
            usage[key] += getattr(response.usage, key, None) or 0
        result = {'model': response.model, 'stop_reason': response.stop_reason, 'usage': dict(usage)}
        if response.stop_reason != 'end_turn':
            return {**result, 'type': 'error', 'status': None,
                    'category': 'refusal' if response.stop_reason == 'refusal' else 'output limit'}
        text = next((block.text for block in response.content if block.type == 'text'), None)
        try:
            output = json.loads(text)
        except (TypeError, ValueError):
            return {**result, 'type': 'error', 'category': 'invalid output', 'status': None}
        broken = violations(request['schema'], output)
        # A correction needs time of its own; otherwise the caller's validation decides.
        if not broken or end - time.monotonic() < 30:
            return {**result, 'type': 'result', 'structured_output': output}
        messages += [{'role': 'assistant', 'content': response.content},
                     {'role': 'user', 'content': 'That JSON breaks these limits: ' + '; '.join(broken)
                      + '. Return the complete JSON again within them, changing nothing else.'}]
    # A second answer that still breaks a limit fails the caller's validation as before.
    return {**result, 'type': 'result', 'structured_output': output}


def command(config):
    """The helper's command: the pinned SDK in an environment uv manages, never the pipeline's interpreter."""
    uv = config.get('uv_path') or shutil.which('uv')
    if not uv or not Path(uv).expanduser().is_file():
        raise ValueError('Claude API helper requires uv')
    return [str(Path(uv).expanduser()), 'run', '--quiet', '--no-project', '--python', sys.executable,
            '--exclude-newer', RESOLVED_AS_OF, '--with', 'anthropic==' + SDK_VERSION, 'python', '-I',
            str(Path(__file__).resolve())]


def environment(config):
    """Only what uv and the helper need; the key is read from its file, never placed in the environment."""
    key = Path(config['api_key_file']).expanduser()
    if key.is_symlink() or not key.is_file():
        raise ValueError('Claude API key file is unavailable')
    if key.stat().st_mode & 0o077:
        raise ValueError('Claude API key file must be readable only by its owner')
    # Proxy and certificate settings reach uv and the SDK as they reach the CLI backend.
    passed = ('HOME', 'TMPDIR', 'XDG_CACHE_HOME', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE')
    env = {name: value for name, value in os.environ.items()
           if name.startswith('UV_') or name in passed or name.lower() in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')}
    return {**env, 'PATH': os.pathsep.join(['/usr/bin', '/bin']), 'CODEX_SKILLS_API_KEY_FILE': str(key.resolve())}


def request(model, effort, prompt, data, schema, timeout, cache_prefix=None):
    # The HTTP timeout ends before the process deadline, so a slow call still reports its own category.
    return json.dumps({'model': model, 'effort': effort, 'max_tokens': MAX_TOKENS, 'timeout': max(1.0, timeout - 5),
                       'prompt': prompt, 'data': data, 'schema': schema, 'cache_prefix': cache_prefix})


def outcome(stdout):
    """(phase, structured output or None, failure category or None, static facts, token usage or None)."""
    facts = {'result_subtype': None, 'api_error_status': None, 'rate_limit_type': None, 'rate_limit_resets_at': None}
    try:
        value = json.loads((stdout or '').strip().splitlines()[-1])
        if not isinstance(value, dict) or value.get('type') not in ('result', 'error'):
            raise ValueError('Invalid helper result')
    except (ValueError, IndexError):
        return 'startup', None, 'CLI or output', facts, None
    status, resets = value.get('status'), value.get('rate_limit_resets_at')
    facts['api_error_status'] = status if type(status) is int else None
    facts['rate_limit_resets_at'] = resets if type(resets) is int and 0 < resets < 10 ** 10 else None
    if value.get('stop_reason') in ('end_turn', 'max_tokens', 'refusal', 'pause_turn', 'stop_sequence', 'tool_use'):
        facts['result_subtype'] = value['stop_reason']
    usage = value.get('usage')
    usage = {key: number for key, number in usage.items() if type(number) is int} if isinstance(usage, dict) else None
    if value['type'] == 'result':
        return 'completed', value.get('structured_output'), None, facts, usage
    return 'completed', None, value.get('category') if value.get('category') in CATEGORIES else 'CLI or output', facts, usage


def main():
    request = json.loads(sys.stdin.read())
    key = Path(os.environ['CODEX_SKILLS_API_KEY_FILE']).read_text().strip()
    import anthropic  # Provided by `uv run --with`; the pipeline's own modules never import it.
    client = anthropic.Anthropic(api_key=key, timeout=request['timeout'], max_retries=0)
    result = call(request, client, anthropic)
    print(json.dumps(result))
    return 0 if result['type'] == 'result' else 1


if __name__ == '__main__':
    raise SystemExit(main())
