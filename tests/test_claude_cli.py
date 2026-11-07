import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import claude_cli as cli  # noqa: E402

SCHEMA = {'type': 'object', 'required': ['notes'], 'properties': {'notes': {'type': 'array'}}}


def stream(*events):
    return ''.join(json.dumps(event) + '\n' for event in events)


INIT = {'type': 'system', 'subtype': 'init', 'tools': ['StructuredOutput'], 'mcp_servers': [], 'model': 'm'}
ANSWER = {'type': 'assistant', 'error': None, 'message': {'content': [
    {'type': 'tool_use', 'name': 'StructuredOutput', 'input': {'notes': []}}]}}
RESULT = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': '{"notes":[]}',
          'structured_output': {'notes': []}}


class ClaudeCliTests(unittest.TestCase):
    def test_restricted_command_isolates_the_session(self):
        command = cli.restricted_command('/bin/claude', SCHEMA)
        self.assertEqual(command[:2], ['/bin/claude', '--print'])
        for flag, value in (('--setting-sources', ''), ('--tools', ''), ('--model', cli.RESTRICTED_MODEL),
                            ('--effort', cli.EFFORT), ('--output-format', 'stream-json')):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertEqual(json.loads(command[command.index('--json-schema') + 1]), SCHEMA)
        for flag in ('--strict-mcp-config', '--disable-slash-commands', '--no-session-persistence', '--verbose'):
            self.assertIn(flag, command)
        self.assertNotIn('--permission-mode', command)

    def test_review_command_keeps_user_configuration_without_prompts(self):
        command = cli.review_command('/bin/claude', '/p/sandbox.json')
        self.assertEqual(command[command.index('--permission-mode') + 1], 'bypassPermissions')
        self.assertEqual(command[command.index('--settings') + 1], '/p/sandbox.json')
        self.assertEqual(command[command.index('--model') + 1], cli.REVIEW_MODEL)
        self.assertNotIn('--setting-sources', command)

    def test_every_required_flag_is_in_the_commands_it_guards(self):
        restricted = cli.restricted_command('c', SCHEMA)
        review = cli.review_command('c', 's')
        self.assertEqual([flag for flag in cli.RESTRICTED_FLAGS if flag not in restricted], [])
        self.assertEqual([flag for flag in cli.REVIEW_FLAGS if flag not in review], [])

    def test_missing_flags_matches_whole_flags_only(self):
        help_text = '  --print  --model <m>  --tools-extra\n  --output-format <f>'
        self.assertEqual(cli.missing_flags(help_text, ('--print', '--tools', '--output-format')), ['--tools'])

    def test_child_env_detaches_from_parent_sessions(self):
        env = cli.child_env({'HOME': '/h', 'CLAUDECODE': '1', 'CLAUDE_CODE_SESSION_ID': 's', 'CLAUDE_EFFORT': 'max',
                             'CLAUDE_AGENT_SDK_VERSION': '1', 'CLAUDE_CODE_MESSAGING_SOCKET': '/s',
                             'CLAUDE_CODE_ENTRYPOINT': 'cli', 'CLAUDE_CODE_EFFORT_LEVEL': 'max', 'CLAUDE_PID': '1',
                             'CLAUDE_CONFIG_DIR': '/c', 'ANTHROPIC_API_KEY': 'k', 'CLAUDE_CODE_OAUTH_TOKEN': 't',
                             'CLAUDE_CODE_USE_BEDROCK': '1'})
        self.assertEqual(env, {'HOME': '/h', 'CLAUDE_CONFIG_DIR': '/c', 'ANTHROPIC_API_KEY': 'k',
                               'CLAUDE_CODE_OAUTH_TOKEN': 't', 'CLAUDE_CODE_USE_BEDROCK': '1',
                               'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1',
                               'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'CLAUDE_CODE_PROMPT_CACHE_TTL': '5m'})

    def test_cli_path_defaults_to_native_install(self):
        self.assertEqual(cli.cli_path({}), cli.DEFAULT_CLI)
        self.assertEqual(cli.cli_path({'claude_path': '/opt/claude'}), Path('/opt/claude'))

    def test_structured_run_returns_validated_output(self):
        self.assertEqual(cli.structured_run(stream(INIT, ANSWER, {'type': 'rate_limit_event'}, RESULT)),
                         ('completed', {'notes': []}, '', {'result_subtype': 'success', 'api_error_status': None,
                                                           'rate_limit_type': None, 'rate_limit_resets_at': None}))

    def test_structured_run_rejects_exposed_or_used_tools(self):
        exposed = {**INIT, 'tools': ['StructuredOutput', 'Bash']}
        with self.assertRaisesRegex(RuntimeError, 'exposed tools'):
            cli.structured_run(stream(exposed, RESULT))
        with self.assertRaisesRegex(RuntimeError, 'exposed tools'):
            cli.structured_run(stream({**INIT, 'mcp_servers': [{'name': 'x'}]}, RESULT))
        used = {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Read', 'input': {}}]}}
        with self.assertRaisesRegex(RuntimeError, 'attempted tool execution'):
            cli.structured_run(stream(INIT, used, RESULT))

    def test_structured_run_reports_errors_and_phase(self):
        failed = {'type': 'result', 'subtype': 'success', 'is_error': True, 'api_error_status': 429,
                  'result': 'API Error: rate limited'}
        phase, output, errors, facts = cli.structured_run(stream(INIT, failed))
        self.assertEqual(facts, {'result_subtype': 'success', 'api_error_status': 429,
                                 'rate_limit_type': None, 'rate_limit_resets_at': None})
        self.assertEqual((phase, output), ('completed', None))
        self.assertEqual(cli.failure_class(errors), 'rate limit')
        self.assertEqual(cli.structured_run(stream(INIT))[0], 'inference')
        self.assertEqual(cli.structured_run('')[0], 'startup')

    def test_partial_output_tolerates_only_a_truncated_last_line(self):
        truncated = stream(INIT) + '{"type": "assist'
        self.assertEqual(cli.structured_run(truncated, partial=True)[0], 'inference')
        with self.assertRaises(json.JSONDecodeError):
            cli.structured_run(truncated)

    def test_oversized_output_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, 'safety bound'):
            cli.events('x' * (cli.MAX_STREAM + 1))

    def test_final_result_requires_success(self):
        self.assertEqual(cli.final_result(stream(INIT, {**RESULT, 'result': 'report'})), 'report')
        with self.assertRaises(RuntimeError):
            cli.final_result(stream(INIT, {**RESULT, 'subtype': 'error_max_turns'}))
        with self.assertRaises(RuntimeError):
            cli.final_result(stream(INIT))

    def test_final_result_finds_the_result_before_trailing_lines(self):
        trailing = stream(INIT, {**RESULT, 'result': 'report'}, {'type': 'system', 'subtype': 'hook_response'})
        self.assertEqual(cli.final_result(trailing + 'warning: not json\n'), 'report')
        self.assertEqual(cli.final_result(('x' * (cli.MAX_STREAM + 1)) + '\n' + stream({**RESULT, 'result': 'big'})), 'big')

    def test_structured_run_rejects_malformed_assistant_messages(self):
        for message in ('text', None, {'content': 'text'}):
            with self.assertRaisesRegex(ValueError, 'Invalid Claude output'):
                cli.structured_run(stream(INIT, {'type': 'assistant', 'message': message}, RESULT))

    def test_result_facts_keep_only_static_values(self):
        failed = {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'api_error_status': 500,
                  'result': 'private text'}
        self.assertEqual(cli.result_facts(failed), {'result_subtype': 'error_during_execution', 'api_error_status': 500})
        self.assertEqual(cli.result_facts({'type': 'result', 'is_error': True}), {'result_subtype': 'missing', 'api_error_status': None})
        odd = {'type': 'result', 'subtype': 'Private Text!', 'api_error_status': 'private'}
        self.assertEqual(cli.result_facts(odd), {'result_subtype': 'other', 'api_error_status': None})
        self.assertEqual(cli.structured_run(stream(INIT))[3], {'result_subtype': None, 'api_error_status': None,
                                                               'rate_limit_type': None, 'rate_limit_resets_at': None})

    def test_rejecting_usage_limit_reports_its_window_and_reset_but_no_text(self):
        allowed = {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed', 'rateLimitType': 'seven_day',
                                                                   'resetsAt': 1793000000}}
        rejected = {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected', 'rateLimitType': 'five_hour',
                                                                    'resetsAt': 1793853000}}
        failed = {'type': 'result', 'subtype': 'success', 'is_error': True, 'api_error_status': 429,
                  'result': "You've hit your 5-hour limit · resets 1:30pm (Asia/Tokyo)"}
        phase, output, errors, facts = cli.structured_run(stream(INIT, allowed, rejected, failed))
        self.assertEqual(facts, {'result_subtype': 'success', 'api_error_status': 429,
                                 'rate_limit_type': 'five_hour', 'rate_limit_resets_at': 1793853000})
        self.assertEqual(cli.failure_class(errors), 'rate limit')
        # Only rejections count, and only static values are kept.
        self.assertEqual(cli.structured_run(stream(INIT, allowed, RESULT))[3]['rate_limit_type'], None)
        odd = {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected', 'rateLimitType': 'Private Text',
                                                               'resetsAt': 'soon'}}
        self.assertEqual({key: value for key, value in cli.structured_run(stream(INIT, odd))[3].items() if 'rate' in key},
                         {'rate_limit_type': 'other', 'rate_limit_resets_at': None})

    def test_failure_classes(self):
        for text, label in (('401 authentication_failed', 'authentication'), ('Not logged in · Please run /login', 'authentication'),
                            ('usage limit reached', 'rate limit'), ("You've hit your 5-hour limit", 'rate limit'),
                            ("You've hit your weekly limit", 'rate limit'), ('529 overloaded_error', 'connection'),
                            ('Request timed out', 'connection'), ('weird', 'CLI or output'), ('', 'CLI or output')):
            self.assertEqual(cli.failure_class(text), label, text)


if __name__ == '__main__':
    unittest.main()
