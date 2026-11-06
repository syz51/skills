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
        for category, reason in (('billing', 'extractor billing failure'), ('refusal', 'extractor refusal failure')):
            failed = self.completed({'type': 'error', 'category': category, 'status': None}, 1)
            with self.subTest(category=category), patch.object(capture, 'run_extractor', return_value=failed), \
                    patch.object(capture.sys, 'stderr', io.StringIO()):
                with self.assertRaises(capture.ExtractorFailure) as caught:
                    capture.model_response(self.config, self.state, 'p', '{}', {'type': 'object'})
                self.assertEqual(caught.exception.reason, reason)
                self.assertNotIsInstance(caught.exception, capture.RateLimited)


if __name__ == '__main__':
    unittest.main()
