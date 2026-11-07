from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import capture
import claude_api
import claude_cli


class APIStatusError(Exception):
    def __init__(self, message, status_code, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = SimpleNamespace(headers=headers or {})


class RateLimitError(APIStatusError):
    pass


class AuthenticationError(APIStatusError):
    pass


class PermissionDeniedError(APIStatusError):
    pass


class BadRequestError(APIStatusError):
    pass


class APIConnectionError(Exception):
    pass


class APITimeoutError(APIConnectionError):
    pass


# The SDK's exception surface, so the helper's mapping is tested offline without the package.
SDK = SimpleNamespace(**{cls.__name__: cls for cls in (APIStatusError, RateLimitError, AuthenticationError,
                                                        PermissionDeniedError, BadRequestError, APIConnectionError,
                                                        APITimeoutError)})
REQUEST = {'model': 'claude-sonnet-5-5', 'effort': 'high', 'max_tokens': 16000, 'timeout': 200,
           'prompt': 'Fixed prompt', 'data': '{"private": "source"}',
           'schema': {'type': 'object', 'additionalProperties': False, 'required': ['notes'],
                      'properties': {'notes': {'type': 'array', 'maxItems': 3,
                                               'items': {'type': 'string', 'maxLength': 1000, 'minLength': 1}}}}}


def response(text, stop='end_turn'):
    return SimpleNamespace(model='claude-sonnet-5-5', stop_reason=stop, content=[
        SimpleNamespace(type='thinking'), SimpleNamespace(type='text', text=text)],
        usage=SimpleNamespace(input_tokens=1200, output_tokens=300, cache_read_input_tokens=None))


class Client:
    def __init__(self, outcome):
        self.outcome, self.calls, self.timeouts = outcome, [], []
        self.messages = SimpleNamespace(create=self.create)

    def with_options(self, timeout):
        self.timeouts.append(timeout)
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class ClaudeApiTests(unittest.TestCase):
    def test_schema_restates_constraints_structured_outputs_rejects_as_descriptions(self):
        self.assertEqual(claude_api.api_schema(REQUEST['schema']), {
            'type': 'object', 'additionalProperties': False, 'required': ['notes'],
            'properties': {'notes': {'type': 'array', 'description': 'At most 3 items.',
                                     'items': {'type': 'string', 'description': 'At least 1 characters. At most 1000 characters.'}}}})
        self.assertEqual(claude_api.api_schema({'type': 'string', 'description': 'Summary.', 'maxLength': 10}),
                         {'type': 'string', 'description': 'Summary. At most 10 characters.'})

    def test_call_sends_fixed_prompt_as_system_and_data_as_the_only_message(self):
        client = Client(response('{"notes": []}'))
        result = claude_api.call(REQUEST, client, SDK)
        self.assertEqual(result, {'model': 'claude-sonnet-5-5', 'stop_reason': 'end_turn', 'type': 'result',
                                  'structured_output': {'notes': []},
                                  'usage': {'input_tokens': 1200, 'output_tokens': 300, 'cache_read_input_tokens': 0,
                                            'cache_creation_input_tokens': 0}})
        [sent] = client.calls
        # The fixed prompt carries a cache breakpoint; data without a cache prefix carries none.
        self.assertEqual((sent['model'], sent['system'], sent['messages']),
                         ('claude-sonnet-5-5', [{'type': 'text', 'text': 'Fixed prompt', 'cache_control': {'type': 'ephemeral'}}],
                          [{'role': 'user', 'content': [{'type': 'text', 'text': '{"private": "source"}'}]}]))
        self.assertEqual(sent['output_config']['effort'], 'high')
        self.assertEqual(sent['output_config']['format']['schema'], claude_api.api_schema(REQUEST['schema']))
        self.assertNotIn('tools', sent)

    def test_cache_prefix_splits_the_data_without_changing_its_text(self):
        data = '{"existing_notes": ["a"], "turn": {"x": 1}}'
        prefix = len('{"existing_notes": ["a"], ')
        blocks = claude_api.user_content({**REQUEST, 'data': data, 'cache_prefix': prefix})
        self.assertEqual(''.join(block['text'] for block in blocks), data)
        self.assertEqual([block.get('cache_control') for block in blocks], [{'type': 'ephemeral'}, None])
        self.assertEqual(blocks[0]['text'], '{"existing_notes": ["a"], ')
        # A prefix covering the whole data caches all of it, as for a repeated evaluation case.
        self.assertEqual(claude_api.user_content({**REQUEST, 'data': data, 'cache_prefix': len(data)}),
                         [{'type': 'text', 'text': data, 'cache_control': {'type': 'ephemeral'}}])
        self.assertEqual(claude_api.user_content({**REQUEST, 'data': data, 'cache_prefix': None}), [{'type': 'text', 'text': data}])
        self.assertEqual(json.loads(claude_api.request('m', 'high', 'p', data, {}, 60, prefix))['cache_prefix'], prefix)
        # The corrective follow-up resends the first message unchanged, so it reads the same cache entry.
        client = Client(None)
        replies = iter([response('{"notes": ["' + 'x' * 1001 + '"]}'), response('{"notes": ["short"]}')])
        client.messages.create = lambda **kwargs: (client.calls.append(kwargs), next(replies))[1]
        claude_api.call({**REQUEST, 'data': data, 'cache_prefix': prefix}, client, SDK)
        self.assertEqual(client.calls[1]['messages'][0], client.calls[0]['messages'][0])
        self.assertEqual(client.calls[1]['system'], client.calls[0]['system'])

    def test_failures_report_static_categories_and_never_error_text(self):
        now = datetime.now(timezone.utc).timestamp()
        reset = datetime.fromtimestamp(int(now) + 3600, timezone.utc).isoformat()
        for outcome, expected in (
                (RateLimitError('private 429 text', 429, {'retry-after': '120', 'anthropic-ratelimit-tokens-reset': reset}),
                 {'category': 'rate limit', 'status': 429, 'rate_limit_resets_at': int(now) + 3600}),
                (AuthenticationError('private key text', 401), {'category': 'authentication', 'status': 401}),
                (PermissionDeniedError('private', 403), {'category': 'authentication', 'status': 403}),
                (BadRequestError('Your credit balance is too low; private', 400), {'category': 'billing', 'status': 400}),
                (BadRequestError('private schema text', 400), {'category': 'invalid request', 'status': 400}),
                (APIStatusError('private billing_error', 402), {'category': 'billing', 'status': 402}),
                (APIStatusError('private overloaded', 529), {'category': 'connection', 'status': 529}),
                (APITimeoutError('private'), {'category': 'timeout', 'status': None}),
                (APIConnectionError('private'), {'category': 'connection', 'status': None})):
            with self.subTest(expected=expected):
                result = claude_api.call(REQUEST, Client(outcome), SDK)
                self.assertEqual(result, {'type': 'error', **expected})
                self.assertNotIn('private', json.dumps(result))
        for content, stop, category in (('{"notes": []}', 'refusal', 'refusal'), ('{"notes": [', 'max_tokens', 'output limit'),
                                        ('not json', 'end_turn', 'invalid output')):
            with self.subTest(category=category):
                result = claude_api.call(REQUEST, Client(response(content, stop)), SDK)
                self.assertEqual((result['type'], result['category'], result['stop_reason']), ('error', category, stop))

    def test_an_answer_breaking_a_stripped_limit_gets_one_append_only_correction(self):
        long_answer, short_answer = response('{"notes": ["' + 'x' * 1001 + '"]}'), response('{"notes": ["short"]}')
        client = Client(None)
        replies = iter([long_answer, short_answer])
        client.messages.create = lambda **kwargs: (client.calls.append(kwargs), next(replies))[1]
        result = claude_api.call(REQUEST, client, SDK)
        self.assertEqual((result['type'], result['structured_output']), ('result', {'notes': ['short']}))
        self.assertEqual(result['usage']['input_tokens'], 2400)
        first, second = client.calls
        self.assertEqual(len(first['messages']), 1)
        # The first reply goes back unchanged, then a note naming only the path and the limit.
        self.assertIs(second['messages'][1]['content'], long_answer.content)
        self.assertEqual(second['messages'][2]['content'], 'That JSON breaks these limits: $.notes[0] must be at most '
                         '1000 characters. Return the complete JSON again within them, changing nothing else.')
        self.assertEqual(claude_api.violations(REQUEST['schema'], {'notes': ['a', 'b', 'c', '']}),
                         ['$.notes must have at most 3 items', '$.notes[3] must be at least 1 characters'])
        # A second answer that still breaks a limit is returned; the pipeline's validation rejects it.
        client = Client(long_answer)
        self.assertEqual(claude_api.call(REQUEST, client, SDK)['type'], 'result')
        self.assertEqual(len(client.calls), 2)

    def test_reset_time_takes_the_latest_announced_reset(self):
        now = 1_800_000_000.0
        self.assertEqual(claude_api.reset_time({'retry-after': '30'}, now), 1_800_000_030)
        self.assertEqual(claude_api.reset_time({'retry-after': 'Thu, 15 Jan 2027 08:00:00 GMT'}, now),
                         int(datetime(2027, 1, 15, 8, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(claude_api.reset_time({'retry-after': '30', 'anthropic-ratelimit-requests-reset':
                                                '2027-01-15T09:00:00Z'}, now),
                         int(datetime(2027, 1, 15, 9, tzinfo=timezone.utc).timestamp()))
        self.assertIsNone(claude_api.reset_time({'retry-after': 'soon'}, now))
        self.assertIsNone(claude_api.reset_time({'retry-after': 'inf'}, now))
        self.assertEqual(claude_api.reset_time({'retry-after': '0.2'}, now), 1_800_000_001)

    def test_limit_checks_follow_the_schema_branch_and_field_names_are_not_keywords(self):
        case = {'type': 'object', 'properties': {'input': {'type': 'string', 'maxLength': 3}}}
        schema = {'type': 'object', 'properties': {'new_case': {'anyOf': [case, {'type': 'null'}]},
                                                   'maximum': {'type': 'string', 'maxLength': 2}}}
        self.assertEqual(claude_api.violations(schema, {'new_case': {'input': 'long'}, 'maximum': 'abc'}),
                         ['$.new_case.input must be at most 3 characters', '$.maximum must be at most 2 characters'])
        self.assertEqual(claude_api.violations(schema, {'new_case': None, 'maximum': 'ab'}), [])
        self.assertEqual(claude_api.api_schema(schema)['properties']['maximum'],
                         {'type': 'string', 'description': 'At most 2 characters.'})

    def test_no_correction_starts_without_time_for_it(self):
        clock = [0.0]
        client = Client(response('{"notes": ["' + 'x' * 1001 + '"]}'))
        def create(**kwargs):
            client.calls.append(kwargs)
            clock[0] += REQUEST['timeout'] - 20
            return client.outcome
        client.messages.create = create
        with patch.object(claude_api.time, 'monotonic', side_effect=lambda: clock[0]):
            result = claude_api.call(REQUEST, client, SDK)
        self.assertEqual((len(client.calls), result['type'], client.timeouts), (1, 'result', [REQUEST['timeout']]))

    def test_outcome_keeps_only_validated_static_facts(self):
        phase, output, category, facts, usage = claude_api.outcome(json.dumps(
            {'type': 'result', 'structured_output': {'notes': []}, 'stop_reason': 'end_turn',
             'usage': {'input_tokens': 5, 'output_tokens': 2, 'note': 'x'}}) + '\n')
        self.assertEqual((phase, output, category, usage), ('completed', {'notes': []}, None, {'input_tokens': 5, 'output_tokens': 2}))
        self.assertEqual(facts['result_subtype'], 'end_turn')
        phase, output, category, facts, _ = claude_api.outcome(json.dumps(
            {'type': 'error', 'category': 'rate limit', 'status': 429, 'rate_limit_resets_at': 1_800_000_000}))
        self.assertEqual((phase, output, category, facts['api_error_status'], facts['rate_limit_resets_at']),
                         ('completed', None, 'rate limit', 429, 1_800_000_000))
        self.assertEqual(claude_api.outcome(json.dumps({'type': 'error', 'category': 'private text'}))[2], 'CLI or output')
        for broken in ('', 'Traceback (most recent call last)', '[1]'):
            self.assertEqual(claude_api.outcome(broken)[:3], ('startup', None, 'CLI or output'))

    def test_environment_names_the_key_file_and_passes_no_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / 'key'
            key.write_text('sk-ant-secret\n')
            key.chmod(0o600)
            uv = Path(folder) / 'uv'
            uv.write_text('')
            with patch.dict(claude_api.os.environ, {'ANTHROPIC_API_KEY': 'parent', 'UV_CACHE_DIR': '/cache', 'HOME': folder}):
                env = claude_api.environment({'api_key_file': str(key)})
            self.assertEqual(env['CODEX_SKILLS_API_KEY_FILE'], str(key.resolve()))
            self.assertEqual((env['UV_CACHE_DIR'], env['HOME']), ('/cache', folder))
            self.assertNotIn('ANTHROPIC_API_KEY', env)
            self.assertNotIn('sk-ant-secret', json.dumps(env))
            command = claude_api.command({'uv_path': str(uv)})
            self.assertEqual(command[:2], [str(uv), 'run'])
            self.assertEqual(command[command.index('--with') + 1], 'anthropic==' + claude_api.SDK_VERSION)
            self.assertEqual(command[-3:], ['python', '-I', str(Path(claude_api.__file__).resolve())])
            with self.assertRaisesRegex(ValueError, 'key file'):
                claude_api.environment({'api_key_file': str(Path(folder) / 'absent')})
            self.assertIn('--exclude-newer', command)
            key.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'readable only by its owner'):
                claude_api.environment({'api_key_file': str(key)})
            key.chmod(0o600)
            with patch.dict(claude_api.os.environ, {'HTTPS_PROXY': 'http://proxy:8080', 'SSL_CERT_FILE': '/ca.pem'}):
                env = claude_api.environment({'api_key_file': str(key)})
            self.assertEqual((env['HTTPS_PROXY'], env['SSL_CERT_FILE']), ('http://proxy:8080', '/ca.pem'))
            self.assertTrue(Path(env['CODEX_SKILLS_API_KEY_FILE']).is_absolute())
            with self.assertRaisesRegex(ValueError, 'uv'):
                claude_api.command({'uv_path': str(Path(folder) / 'absent')})


class ApiBackendTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        (self.state / 'key').write_text('sk-ant-secret\n')
        (self.state / 'key').chmod(0o600)
        (self.state / 'uv').write_text('')
        self.config = {'api_key_file': str(self.state / 'key'), 'uv_path': str(self.state / 'uv')}

    def completed(self, value, code=0):
        return capture.subprocess.CompletedProcess([], code, json.dumps(value) + '\n', '')

    def test_api_backend_runs_the_role_model_and_logs_only_usage(self):
        answer = {'type': 'result', 'structured_output': {'status': 'supported', 'summary': 'ok'}, 'stop_reason': 'end_turn',
                  'usage': {'input_tokens': 900, 'output_tokens': 120}}
        with patch.dict(claude_cli.ROLE_MODELS, {'verify': 'claude-sonnet-5-5'}), \
                patch.object(capture, 'run_extractor', return_value=self.completed(answer)) as run, \
                patch.object(capture, 'verify_cli') as verified, patch.object(capture.sys, 'stderr', io.StringIO()) as log:
            output = capture.model_response(self.config, self.state, 'Fixed prompt', '{"source": 1}', {'type': 'object'},
                                            role='verify')
        self.assertEqual(output, {'status': 'supported', 'summary': 'ok'})
        verified.assert_not_called()
        sent = json.loads(run.call_args.kwargs['input'])
        self.assertEqual((sent['model'], sent['prompt'], sent['data'], sent['effort']),
                         ('claude-sonnet-5-5', 'Fixed prompt', '{"source": 1}', claude_cli.EFFORT))
        self.assertLess(sent['timeout'], capture.MODEL_TIMEOUT)
        self.assertNotIn('sk-ant-secret', json.dumps(run.call_args.kwargs['env']))
        self.assertEqual(json.loads(log.getvalue()), {'event': 'model_call', 'role': 'verify', 'model': 'claude-sonnet-5-5',
                                                      'usage': {'input_tokens': 900, 'output_tokens': 120}})

    def test_api_rate_limit_defers_to_the_announced_reset_and_connections_retry_once(self):
        reset = int(datetime.now(timezone.utc).timestamp()) + 3600
        limited = self.completed({'type': 'error', 'category': 'rate limit', 'status': 429, 'rate_limit_resets_at': reset}, 1)
        with patch.object(capture, 'run_extractor', return_value=limited) as run, patch.object(capture.sys, 'stderr', io.StringIO()):
            with self.assertRaises(capture.RateLimited) as caught:
                capture.model_response(self.config, self.state, 'p', '{}', {'type': 'object'}, role='extract')
        self.assertEqual(run.call_count, 1)
        self.assertEqual(capture.timestamp(caught.exception.limit['retry_at']),
                         datetime.fromtimestamp(reset, timezone.utc) + capture.timedelta(minutes=1))
        self.assertFalse(capture.rate_limit_path(self.state).exists())
        capture.rate_limit_path(self.state, api=True).unlink()
        # A subscription limit recorded by the CLI backend never blocks the separately billed API.
        capture.record_rate_limit(self.state, {'rate_limit_resets_at': reset}, datetime.now(timezone.utc))
        answer = self.completed({'type': 'result', 'structured_output': {'ok': True}})
        with patch.object(capture, 'run_extractor', return_value=answer) as run, patch.object(capture.sys, 'stderr', io.StringIO()):
            self.assertEqual(capture.model_response(self.config, self.state, 'p', '{}', {'type': 'object'}), {'ok': True})
        self.assertTrue(capture.rate_limit_path(self.state).exists())
        self.assertEqual(capture.failure_reason(ValueError('Claude API key file is unavailable')), 'API key file unavailable')
        dropped = self.completed({'type': 'error', 'category': 'connection', 'status': 529}, 1)
        with patch.object(capture, 'run_extractor', return_value=dropped) as run, patch.object(capture.sys, 'stderr', io.StringIO()):
            with self.assertRaises(capture.ExtractorFailure) as caught:
                capture.model_response(self.config, self.state, 'p', '{}', {'type': 'object'})
        self.assertEqual((run.call_count, caught.exception.reason), (2, 'extractor connection failure'))
        # Billing and authentication fall back to the subscription (ApiFallbackTests); other failures stand.
        for category, reason in (('refusal', 'extractor refusal failure'), ('invalid request', 'extractor invalid request failure')):
            failed = self.completed({'type': 'error', 'category': category, 'status': None}, 1)
            with self.subTest(category=category), patch.object(capture, 'run_extractor', return_value=failed), \
                    patch.object(capture.sys, 'stderr', io.StringIO()):
                with self.assertRaises(capture.ExtractorFailure) as caught:
                    capture.model_response(self.config, self.state, 'p', '{}', {'type': 'object'})
                self.assertEqual(caught.exception.reason, reason)
                self.assertNotIsInstance(caught.exception, capture.RateLimited)
                self.assertFalse(capture.api_fallback_path(self.state).exists())


def cli_answer(output):
    """A restricted CLI run that answered through StructuredOutput."""
    events = [{'type': 'system', 'subtype': 'init', 'tools': ['StructuredOutput'], 'mcp_servers': []},
              {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'StructuredOutput', 'input': output}]}},
              {'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': output}]
    return capture.subprocess.CompletedProcess([], 0, ''.join(json.dumps(event) + '\n' for event in events), '')


class ApiFallbackTests(unittest.TestCase):
    """An exhausted API balance or rejected key falls back to the CLI's subscription and stays there."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.key = self.state / 'key'
        self.key.write_text('sk-ant-secret\n')
        self.key.chmod(0o600)
        (self.state / 'uv').write_text('')
        self.config = {'api_key_file': str(self.key), 'uv_path': str(self.state / 'uv'),
                       'claude_path': str(self.state / 'claude')}
        self.calls = []
        self.api_answer = {'type': 'error', 'category': 'billing', 'status': 400}
        patcher = patch.object(capture, 'verify_cli')
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_extractor(self, command, **kwargs):
        api = command[0] == str(self.state / 'uv')
        self.calls.append('api' if api else 'cli')
        if api:
            code = 1 if self.api_answer.get('type') == 'error' else 0
            return capture.subprocess.CompletedProcess([], code, json.dumps(self.api_answer) + '\n', '')
        self.assertNotIn('sk-ant-secret', json.dumps(kwargs['env']))
        return cli_answer({'from': 'cli'})

    def call(self):
        with patch.object(capture, 'run_extractor', side_effect=self.run_extractor), \
                patch.object(capture.sys, 'stderr', io.StringIO()) as log:
            output = capture.model_response(self.config, self.state, 'p', '{}', {'type': 'object'}, role='verify')
        return output, [json.loads(line) for line in log.getvalue().splitlines()]

    def test_billing_falls_back_and_sticks_until_the_key_changes(self):
        started = datetime.now(timezone.utc)
        output, log = self.call()
        self.assertEqual((output, self.calls), ({'from': 'cli'}, ['api', 'cli']))
        record = capture.read_api_fallback(self.state)
        stat = self.key.lstat()
        self.assertEqual((record['reason'], record['key']), ('billing', f'{stat.st_ino}-{stat.st_size}-{stat.st_mtime_ns}'))
        since = capture.timestamp(record['since'])
        self.assertLessEqual(abs((since - started).total_seconds()), 2)
        self.assertEqual(capture.timestamp(record['retry_at']) - since, capture.API_RETRY)
        self.assertIn({'event': 'api_fallback', 'reason': 'billing', 'retry_at': record['retry_at']}, log)
        self.assertEqual(capture.model_backend(self.config, self.state), 'cli')

        # While the record stands, calls go straight to the subscription.
        self.call()
        self.assertEqual(self.calls, ['api', 'cli', 'cli'])

        # A changed key file retries the API at once; a repeat failure keeps the first `since`.
        self.key.write_text('sk-ant-other-secret\n')
        self.call()
        self.assertEqual(self.calls, ['api', 'cli', 'cli', 'api', 'cli'])
        self.assertEqual(capture.read_api_fallback(self.state)['since'], record['since'])

    def test_the_daily_retry_or_an_unreadable_record_tries_the_api_and_success_clears_it(self):
        self.call()
        record = capture.read_api_fallback(self.state)
        record['retry_at'] = (datetime.now(timezone.utc) - capture.timedelta(minutes=1)).isoformat(timespec='seconds')
        capture.api_fallback_path(self.state).write_text(json.dumps(record))
        self.call()
        self.assertEqual(self.calls, ['api', 'cli', 'api', 'cli'])
        capture.api_fallback_path(self.state).write_text('not json')
        self.assertIsNone(capture.read_api_fallback(self.state))
        self.api_answer = {'type': 'result', 'structured_output': {'from': 'api'}}
        output, _ = self.call()
        self.assertEqual((output, self.calls[-1]), ({'from': 'api'}, 'api'))
        self.assertFalse(capture.api_fallback_path(self.state).exists())
        self.assertEqual(capture.model_backend(self.config, self.state), 'api')

    def test_authentication_falls_back_but_rate_limits_and_other_failures_do_not(self):
        self.api_answer = {'type': 'error', 'category': 'authentication', 'status': 401}
        self.assertEqual(self.call()[0], {'from': 'cli'})
        self.assertEqual(capture.read_api_fallback(self.state)['reason'], 'authentication')
        capture.api_fallback_path(self.state).unlink()
        reset = int(datetime.now(timezone.utc).timestamp()) + 3600
        for answer, error in (({'type': 'error', 'category': 'rate limit', 'status': 429, 'rate_limit_resets_at': reset},
                               capture.RateLimited),
                              ({'type': 'error', 'category': 'refusal', 'status': None}, capture.ExtractorFailure)):
            self.api_answer, self.calls = answer, []
            with self.subTest(answer=answer['category']), self.assertRaises(error):
                self.call()
            self.assertEqual(self.calls, ['api'])
            self.assertFalse(capture.api_fallback_path(self.state).exists())
            capture.rate_limit_path(self.state, api=True).unlink(missing_ok=True)

    def test_the_fallback_honors_the_subscription_usage_limit(self):
        self.call()
        reset = int(datetime.now(timezone.utc).timestamp()) + 5 * 3600
        capture.record_rate_limit(self.state, {'rate_limit_resets_at': reset}, datetime.now(timezone.utc))
        self.calls = []
        with self.assertRaises(capture.RateLimited):
            self.call()
        self.assertEqual(self.calls, [])
        # A requested immediate retry still runs, on the subscription.
        with patch.object(capture, 'run_extractor', side_effect=self.run_extractor), \
                patch.object(capture.sys, 'stderr', io.StringIO()):
            capture.model_response(self.config, self.state, 'p', '{}', {'type': 'object'}, wait_for_rate_limit=False)
        self.assertEqual(self.calls, ['cli'])

    def test_status_names_a_standing_fallback(self):
        status = {'success': True, 'warning': 'Claude usage limit reached.'}
        capture.report_api_fallback(status, self.config, self.state)
        self.assertEqual(status, {'success': True, 'warning': 'Claude usage limit reached.'})
        self.call()
        capture.report_api_fallback(status, self.config, self.state)
        self.assertEqual(status['api_fallback'], capture.read_api_fallback(self.state))
        self.assertTrue(status['warning'].startswith('Running on subscription fallback: the Claude API is unavailable (billing)'))
        self.assertTrue(status['warning'].endswith('Claude usage limit reached.'))
        # Without a key file a leftover record is not reported.
        plain = {'success': True}
        capture.report_api_fallback(plain, {}, self.state)
        self.assertEqual(plain, {'success': True})

    def test_a_record_no_longer_in_effect_is_not_reported(self):
        self.call()
        self.key.write_text('sk-ant-replaced\n')
        status = {'success': True}
        capture.report_api_fallback(status, self.config, self.state)
        self.assertEqual((status, capture.model_backend(self.config, self.state)), ({'success': True}, 'api'))
        record = capture.read_api_fallback(self.state)
        stat = self.key.lstat()
        capture.api_fallback_path(self.state).write_text(json.dumps(dict(
            record, key=f'{stat.st_ino}-{stat.st_size}-{stat.st_mtime_ns}',
            retry_at=(datetime.now(timezone.utc) - capture.timedelta(seconds=1)).isoformat(timespec='seconds'))))
        capture.report_api_fallback(status, self.config, self.state)
        self.assertEqual(status, {'success': True})

    def test_api_success_keeps_another_key_files_record(self):
        other = {**self.config, 'api_key_file': str(self.state / 'other-key')}
        (self.state / 'other-key').write_text('sk-ant-other\n')
        foreign = capture.record_api_fallback(other, self.state, 'billing', datetime.now(timezone.utc))
        self.api_answer = {'type': 'result', 'structured_output': {'from': 'api'}}
        self.call()
        self.assertEqual(capture.read_api_fallback(self.state), foreign)

    def test_the_fallback_drops_an_inherited_api_key(self):
        envs = []
        def run(command, **kwargs):
            envs.append(kwargs['env'])
            return self.run_extractor(command, **kwargs)
        with patch.dict(capture.os.environ, {'ANTHROPIC_API_KEY': 'sk-ant-inherited'}), \
                patch.object(capture, 'run_extractor', side_effect=run), patch.object(capture.sys, 'stderr', io.StringIO()):
            capture.model_response(self.config, self.state, 'p', '{}', {'type': 'object'})
        self.assertEqual(self.calls, ['api', 'cli'])
        self.assertNotIn('ANTHROPIC_API_KEY', envs[1])
        # Without a key file the CLI backend keeps the caller's credentials, as before.
        envs.clear()
        with patch.dict(capture.os.environ, {'ANTHROPIC_API_KEY': 'sk-ant-inherited'}), \
                patch.object(capture, 'run_extractor', side_effect=run), patch.object(capture.sys, 'stderr', io.StringIO()):
            capture.model_response({'claude_path': self.config['claude_path']}, self.state, 'p', '{}', {'type': 'object'})
        self.assertEqual(envs[0]['ANTHROPIC_API_KEY'], 'sk-ant-inherited')

    def test_a_pinned_backend_never_moves(self):
        def pinned(backend):
            with patch.object(capture, 'run_extractor', side_effect=self.run_extractor), \
                    patch.object(capture.sys, 'stderr', io.StringIO()):
                return capture.model_response(self.config, self.state, 'p', '{}', {'type': 'object'}, backend=backend)
        # A billing failure is still recorded for everyone else, but the pinned call fails instead of moving.
        with self.assertRaises(capture.ExtractorFailure) as caught:
            pinned('api')
        self.assertEqual((caught.exception.reason, self.calls), ('extractor backend changed', ['api']))
        self.assertEqual(capture.read_api_fallback(self.state)['reason'], 'billing')
        with self.assertRaises(capture.ExtractorFailure):
            pinned('api')
        self.assertEqual(self.calls, ['api'])
        # A run pinned to the CLI stays there after the API recovers.
        capture.api_fallback_path(self.state).unlink()
        self.assertEqual(pinned('cli'), {'from': 'cli'})
        self.assertEqual(self.calls, ['api', 'cli'])

    def test_record_matches_the_rust_publisher_format(self):
        now = datetime(2026, 11, 6, 3, 4, 5, 678000, tzinfo=timezone.utc)
        record = capture.record_api_fallback(self.config, self.state, 'billing', now)
        self.assertEqual(json.loads(capture.api_fallback_path(self.state).read_text()), record)
        self.assertEqual((record['since'], record['retry_at']), ('2026-11-06T03:04:05+00:00', '2026-11-07T03:04:05+00:00'))
        # The publisher writes the same shape; Python honors it.
        rust = dict(record, since='2026-11-06T03:04:05+00:00',
                    retry_at=(datetime.now(timezone.utc) + capture.timedelta(hours=1)).isoformat(timespec='seconds'))
        capture.api_fallback_path(self.state).write_text(json.dumps(rust, indent=2) + '\n')
        self.assertEqual(capture.active_api_fallback(self.config, self.state), rust)
        for broken in ({**rust, 'reason': 'refusal'}, {**rust, 'extra': 1}, {**rust, 'since': 'yesterday'}, {**rust, 'since': 5},
                       {**rust, 'retry_at': None}, {**rust, 'key': 7}, [rust]):
            capture.api_fallback_path(self.state).write_text(json.dumps(broken))
            self.assertIsNone(capture.read_api_fallback(self.state), broken)


if __name__ == '__main__':
    unittest.main()
