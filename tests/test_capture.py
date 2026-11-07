from datetime import datetime, timedelta, timezone
import importlib.util
import hashlib
import json
import io
import os
from pathlib import Path
import tempfile
import sys
import time
import unittest
import warnings
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import read_guidance
spec = importlib.util.spec_from_file_location('capture', Path(__file__).parents[1] / 'scripts/capture.py')
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)

INIT = {'type': 'system', 'subtype': 'init', 'tools': ['StructuredOutput'], 'mcp_servers': []}
HELP = 'Usage: claude [options]\n' + ''.join('  ' + flag + ' <value>\n' for flag in capture.claude_cli.RESTRICTED_FLAGS)


def stream(*events):
    return ''.join(json.dumps(event) + '\n' for event in events)


def answer(output):
    """Stream-json of a restricted run that answered through StructuredOutput."""
    return stream(INIT, {'type': 'assistant', 'message': {'content': [
        {'type': 'tool_use', 'name': 'StructuredOutput', 'input': output}]}},
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': output})


def completed(output):
    return capture.subprocess.CompletedProcess([], 0, answer(output), '')


def tool_use(name):
    return {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': name, 'input': {}}]}}


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.archive = self.root / 'archive'
        self.feedback = self.root / 'feedback'
        self.state = self.root / 'state'
        self.config = {'sessions_root': str(self.sessions), 'archived_sessions_root': str(self.archive),
                       'feedback_dir': str(self.feedback),
                       'install_root': str(self.root / 'install'), 'claude_path': '/bin/false',
                       'claude_projects_root': str(self.root / 'claude-projects')}
        self.lesson = {'skill': 'frontend-ui', **{field: 'Verified useful check' for field in capture.FIELDS}}
        verified = patch.object(capture, 'verify_cli')
        verified.start()
        self.addCleanup(verified.stop)

    def record(self, kind, payload, date='2026-10-10T01:00:00Z'):
        return json.dumps({'type': kind, 'payload': payload, 'timestamp': date}) + '\n'

    def transcript(self, turn='turn1', user='Build a small page', load=True, complete=True):
        text = self.record('event_msg', {'type': 'task_started', 'turn_id': turn})
        text += self.record('event_msg', {'type': 'user_message', 'message': user})
        if load:
            text += self.record('response_item', {'type': 'function_call', 'call_id': 'read', 'name': 'exec_command', 'arguments': 'cat /skills/frontend-ui/SKILL.md'})
            text += self.record('response_item', {'type': 'function_call_output', 'call_id': 'read', 'output': 'Exit code: 0\n# Frontend UI\n' + 'skill policy content ' * 20})
        text += self.record('response_item', {'type': 'message', 'role': 'assistant', 'content': [{'text': 'Built page and checked layout.'}]})
        if complete:
            text += self.record('event_msg', {'type': 'task_complete', 'turn_id': turn})
        return text

    def rollout(self, text=None, source='desktop', filename='rollout.jsonl'):
        path = self.sessions / filename
        path.write_text(self.record('session_meta', {'id': 'session1', 'source': source}) + (text or self.transcript()))
        return path

    def scan(self):
        return capture.completed_turns(self.sessions.glob('*.jsonl'), capture.timestamp(capture.SINCE))

    def test_actual_reads_multiple_turns_rollover_partial_tail(self):
        self.rollout(self.transcript() + self.transcript('turn2', load=False) + '{"partial":')
        self.rollout(self.transcript(), filename='rollover.jsonl')
        self.assertEqual([t['turn'] for t in self.scan()], ['turn1', 'turn2'])
        self.assertEqual(self.scan()[1]['verified_skills'], ['frontend-ui'])
        self.assertEqual(self.scan()[0]['loaded_hash'], 'unknown')

    def test_archived_turn_is_captured_before_its_first_hourly_scan(self):
        source = self.rollout()
        self.archive.mkdir()
        source.rename(self.archive / source.name)
        with patch.object(capture, 'extract', return_value=[]) as model:
            result = capture.capture(self.config, self.state)
        self.assertEqual((result['eligible'], result['processed'], result['pending'], result['errors']), (1, 1, 0, 0))
        model.assert_called_once()

    def test_archived_deferred_turn_retries_and_recovers(self):
        source = self.rollout()
        now = datetime(2026, 10, 10, 2, tzinfo=timezone.utc)
        with patch.object(capture, 'extract', side_effect=RuntimeError('extraction failed')):
            failed = capture.capture(self.config, self.state, now=now)
        self.assertEqual((failed['pending'], failed['errors']), (1, 1))
        self.archive.mkdir()
        source.rename(self.archive / source.name)
        with patch.object(capture, 'extract', return_value=[]) as model:
            recovered = capture.capture(self.config, self.state, now=now + timedelta(hours=2))
        self.assertEqual((recovered['processed'], recovered['pending'], recovered['errors']), (1, 0, 0))
        model.assert_called_once()
        self.assertEqual(json.loads((self.state / 'capture-state.json').read_text())['deferred'], {})

    def test_active_and_archived_copies_use_one_fullest_turn(self):
        source = self.rollout()
        self.archive.mkdir()
        archived = self.archive / source.name
        archived.write_text(source.read_text())
        source.write_text(source.read_text().replace('Built page and checked layout.', 'Built page.'))
        with patch.object(capture, 'extract', return_value=[]) as model:
            result = capture.capture(self.config, self.state)
        self.assertEqual((result['eligible'], result['processed']), (1, 1))
        model.assert_called_once()
        self.assertIn('Built page and checked layout.', [message['text'] for message in model.call_args.args[2]['messages']])

    def test_missing_deferred_source_remains_failed_and_recovers_when_restored(self):
        source = self.rollout()
        original = source.read_text()
        now = datetime(2026, 10, 10, 2, tzinfo=timezone.utc)
        with patch.object(capture, 'extract', side_effect=RuntimeError('private source failure')):
            capture.capture(self.config, self.state, now=now)
        state_path = self.state / 'capture-state.json'
        retained = state_path.read_bytes()
        source.unlink()
        with patch.object(capture, 'extract') as model:
            result = capture.capture(self.config, self.state, now=now + timedelta(hours=2))
        model.assert_not_called()
        self.assertEqual((result['eligible'], result['pending'], result['errors'], result['unavailable_sources']), (0, 1, 1, 1))
        self.assertEqual(result['oldest_pending_at'], '2026-10-10T01:00:00Z')
        self.assertEqual(state_path.read_bytes(), retained)
        self.assertNotIn('private source failure', json.dumps(result))
        source.write_text(original)
        with patch.object(capture, 'extract', return_value=[]):
            result = capture.capture(self.config, self.state, now=now + timedelta(hours=3))
        self.assertEqual((result['processed'], result['pending'], result['errors'], result['unavailable_sources']), (1, 0, 0, 0))

    def test_legacy_missing_pending_and_deferred_ids_stay_visible_once_each(self):
        self.state.mkdir()
        state = {'processed': {}, 'pending': {'shared': {}, 'staged': {}},
                 'deferred': {'shared': {'reason': 'extractor timeout'}}}
        state_path = self.state / 'capture-state.json'
        state_path.write_text(json.dumps(state))
        with patch.object(capture, 'extract') as model:
            result = capture.capture(self.config, self.state)
        model.assert_not_called()
        self.assertEqual((result['pending'], result['errors'], result['unavailable_sources']), (2, 2, 2))
        self.assertIsNone(result['oldest_pending_at'])
        self.assertEqual(json.loads(state_path.read_text()), state)

    def node_turn(self, calls):
        text = self.record('event_msg', {'type': 'task_started', 'turn_id': 'node'})
        text += self.record('event_msg', {'type': 'user_message', 'message': 'Implement the typed parser'})
        for index, (code, output, failed) in enumerate(calls):
            text += self.record('response_item', {'type': 'function_call', 'call_id': str(index),
                'name': 'mcp__node_repl__js', 'arguments': json.dumps({'code': code})})
            text += self.record('response_item', {'type': 'function_call_output', 'call_id': str(index),
                'output': json.dumps({'content': [{'type': 'text', 'text': output}], 'isError': failed})})
        text += self.record('event_msg', {'type': 'task_complete', 'turn_id': 'node'})
        self.rollout(text)
        return self.scan()

    def test_actual_node_reads_preserve_complete_bound_and_partial_provenance(self):
        content = '---\nname: typescript\n---\n# TypeScript\n' + 'Use meaningful type contracts.\n' * 4
        prelude = "var fs = await import('node:fs/promises'); "
        read = "await fs.readFile('/skills/typescript/SKILL.md', 'utf8')"
        for calls, observed, coverage in (
            ([(prelude + 'nodeRepl.write(' + read + ');', content, False)], content, 'complete'),
            ([(prelude + 'var policy = ' + read + ';', '', False),
              ('nodeRepl.write(policy);', content, False)], content, 'complete'),
            ([(prelude + 'nodeRepl.write((' + read + ').slice(0, 40));', content[:40], False)], content[:40], 'partial')):
            with self.subTest(calls=calls):
                [turn] = self.node_turn(calls)
                self.assertEqual(turn['verified_skills'], ['typescript'])
                self.assertEqual(turn['loaded_reads'], [{'path': 'typescript/SKILL.md',
                    'sha256': hashlib.sha256(observed.encode()).hexdigest(), 'coverage': coverage, 'read_turn': 'node'}])

    def test_node_spoofs_failures_and_unknown_transforms_do_not_verify_use(self):
        for code, failed in (
            ("nodeRepl.write('# TypeScript'); // fs.readFile('/skills/typescript/SKILL.md','utf8')", False),
            ("var fs = await import('node:fs/promises'); nodeRepl.write(await fs.readFile('/skills/typescript/SKILL.md','utf8'));", True),
            ("var fs = await import('node:fs/promises'); nodeRepl.write((await fs.readFile('/skills/typescript/SKILL.md','utf8')).toUpperCase());", False)):
            with self.subTest(code=code):
                self.assertEqual(self.node_turn([(code, '# TypeScript\n', failed)]), [])

    def test_node_bindings_survive_turns_but_not_repl_reset_or_other_sessions(self):
        code = "var fs = await import('node:fs/promises'); var policy = await fs.readFile('/skills/typescript/SKILL.md','utf8');"
        for reset in (False, True):
            with self.subTest(reset=reset):
                self.assertEqual(self.node_turn([(code, '', False)]), [])
                text = self.record('event_msg', {'type': 'task_started', 'turn_id': 'later'})
                text += self.record('event_msg', {'type': 'user_message', 'message': 'Implement the parser correction'})
                if reset:
                    text += self.record('response_item', {'type': 'function_call', 'call_id': 'reset', 'name': 'mcp__node_repl__reset', 'arguments': '{}'})
                    text += self.record('response_item', {'type': 'function_call_output', 'call_id': 'reset', 'output': '{"isError":false}'})
                text += self.record('response_item', {'type': 'function_call', 'call_id': 'later-read', 'name': 'mcp__node_repl__js', 'arguments': json.dumps({'code': 'nodeRepl.write(policy);'})})
                text += self.record('response_item', {'type': 'function_call_output', 'call_id': 'later-read', 'output': json.dumps({'content': [{'type': 'text', 'text': '# TypeScript\n'}], 'isError': False})})
                text += self.record('event_msg', {'type': 'task_complete', 'turn_id': 'later'})
                with (self.sessions / 'rollout.jsonl').open('a') as stream:
                    stream.write(text)
                turns = self.scan()
                self.assertEqual([turn['turn'] for turn in turns], [] if reset else ['later'])
        self.assertEqual(self.node_turn([('nodeRepl.write(policy);', '# TypeScript\n', False)]), [])

    def test_printed_or_ambiguous_skill_heading_is_not_verified_use(self):
        for command, status in (("printf '%s\\n' '# TypeScript'; # cat /skills/typescript/SKILL.md", 0),
                                ('echo "cat /skills/typescript/SKILL.md"', 0),
                                ('grep TypeScript /skills/typescript/SKILL.md', 0),
                                ('cat /skills/typescript/SKILL.md; echo extra', 0),
                                ('cat /skills/typescript/SKILL.md', 1)):
            with self.subTest(command=command):
                text = self.record('event_msg', {'type': 'task_started', 'turn_id': 'spoof'})
                text += self.record('event_msg', {'type': 'user_message', 'message': 'Implement the parser'})
                text += self.record('response_item', {'type': 'function_call', 'call_id': 'r', 'name': 'exec_command', 'arguments': json.dumps({'cmd': command})})
                text += self.record('response_item', {'type': 'function_call_output', 'call_id': 'r', 'output': json.dumps({'exit_code': status, 'output': '# TypeScript\n'})})
                text += self.record('event_msg', {'type': 'task_complete', 'turn_id': 'spoof'})
                self.rollout(text)
                self.assertEqual(self.scan(), [])

    def test_every_maintained_skill_is_recognized_by_name_or_heading(self):
        for skill, heading in (('find-skills', '# Find Skills'), ('content-research-writer', '# Content Research Writer'), ('tauri', '# Tauri'), ('typescript', '# TypeScript')):
            for output in ('---\nname: ' + skill + '\n---\n', heading + '\n'):
                with self.subTest(skill=skill, output=output):
                    text = self.transcript().replace('/skills/frontend-ui/SKILL.md', '/skills/' + skill + '/SKILL.md')
                    self.rollout(text.replace(json.dumps('# Frontend UI\n')[1:-1], json.dumps(output)[1:-1]))
                    self.assertEqual(self.scan()[0]['verified_skills'], [skill])
        self.assertEqual(set(capture.HEADINGS), set(capture.SKILLS))

    def test_look_alike_skill_names_are_not_maintained_skill_reads(self):
        text = self.transcript().replace('/skills/frontend-ui/SKILL.md', '/skills/xfind-skills/SKILL.md')
        self.rollout(text.replace(json.dumps('# Frontend UI\n')[1:-1], json.dumps('---\nname: find-skills\n---\n')[1:-1]))
        self.assertEqual(self.scan(), [])
        result = json.dumps({'exit_code': 0, 'output': '# Find Skills\n'})
        self.assertEqual(capture.loaded_reads('cat /skills/my-find-skills/SKILL.md', result, 'turn'), [])
        self.assertEqual(len(capture.loaded_reads('cat /skills/find-skills/SKILL.md', result, 'turn')), 1)

    def test_evidence_scan_includes_readonly_confirmations_without_changing_capture(self):
        self.rollout(self.transcript(user='Read-only confirmation: I checked the result.', load=False))
        self.assertEqual(self.scan(), [])
        turns = capture.completed_turns(self.sessions.glob('*.jsonl'), capture.timestamp(capture.SINCE), eligible_only=False)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]['verified_skills'], [])
        self.rollout(self.transcript(complete=False), filename='active.jsonl')
        self.rollout(self.transcript(), source={'subagent': {'thread_spawn': {}}}, filename='child.jsonl')
        self.assertEqual(len(capture.completed_turns(self.sessions.glob('*.jsonl'), capture.timestamp(capture.SINCE), eligible_only=False)), 1)

    def test_ineligible_sources_and_turns(self):
        for text, source in [(self.transcript(load=False), 'desktop'),
                             (self.transcript(complete=False), 'desktop'),
                             (self.transcript(user='Review only; do not edit'), 'desktop'),
                             (self.transcript(user='Run scripts/capture.py'), 'desktop'),
                             (self.transcript(user='Run a synthetic exercise'), 'desktop'),
                             (self.transcript(), {'subagent': {'thread_spawn': {}}})]:
            with self.subTest(text=text[:40], source=source):
                self.rollout(text, source)
                self.assertEqual(self.scan(), [])

    def test_failed_skill_read_does_not_count(self):
        text = self.transcript().replace('Exit code: 0', 'Exit code: 1')
        self.rollout(text)
        self.assertEqual(self.scan(), [])

    def test_supporting_code_and_asset_shell_reads_have_exact_fingerprints(self):
        for relative in ('scripts/check.ts', 'assets/templates/example.html', 'agents/openai.yaml'):
            with self.subTest(relative=relative):
                content = 'Historical supporting text\n'
                result = json.dumps({'exit_code': 0, 'output': content})
                self.assertEqual(capture.loaded_reads('cat /skills/typescript/' + relative, result, 'turn'),
                                 [{'path': 'typescript/' + relative, 'sha256': hashlib.sha256(content.encode()).hexdigest(),
                                   'coverage': 'complete', 'read_turn': 'turn'}])
        result = json.dumps({'exit_code': 0, 'output': 'data\x00binary'})
        self.assertEqual(capture.loaded_reads('cat /skills/typescript/assets/data.bin', result, 'turn'), [])

    def test_typescript_package_or_project_reads_are_not_skill_evidence(self):
        result = json.dumps({'exit_code': 0, 'output': '# TypeScript\n'})
        for path in ('/project/node_modules/typescript/lib/typescript.js',
                     '/project/node_modules/typescript/package.json',
                     '/project/typescript/customer-notes.md', '/project/typescript/SKILL.md'):
            with self.subTest(path=path):
                self.assertEqual(capture.loaded_reads('cat ' + path, result, 'turn'), [])
                text = self.transcript().replace('/skills/frontend-ui/SKILL.md', path)
                self.rollout(text.replace(json.dumps('# Frontend UI\n')[1:-1], json.dumps('# TypeScript\n')[1:-1]))
                self.assertEqual(self.scan(), [])

    def test_relative_typescript_entrypoint_cat_and_helper_are_eligible(self):
        content = '---\nname: typescript\n---\n# TypeScript\n'
        digest = hashlib.sha256(content.encode()).hexdigest()
        envelope = {'protocol': read_guidance.PROTOCOL, 'path': 'typescript/SKILL.md', 'start': 0,
                    'next_start': None, 'coverage': 'complete', 'sha256': digest, 'content': content}
        for command, output in (
                ('cat skills/typescript/SKILL.md', content),
                ('uv run --script scripts/read_guidance.py skills/typescript/SKILL.md', json.dumps(envelope))):
            for arguments in (command, json.dumps({'cmd': command})):
                with self.subTest(command=command, arguments=arguments):
                    text = self.record('event_msg', {'type': 'task_started', 'turn_id': 'relative'})
                    text += self.record('event_msg', {'type': 'user_message', 'message': 'Implement the typed parser'})
                    text += self.record('response_item', {'type': 'function_call', 'call_id': 'r', 'name': 'exec_command', 'arguments': arguments})
                    text += self.record('response_item', {'type': 'function_call_output', 'call_id': 'r', 'output': json.dumps({'exit_code': 0, 'output': output})})
                    text += self.record('event_msg', {'type': 'task_complete', 'turn_id': 'relative'})
                    self.rollout(text)
                    [turn] = self.scan()
                    self.assertEqual(turn['verified_skills'], ['typescript'])
                    self.assertEqual(turn['loaded_reads'], [{'path': 'typescript/SKILL.md', 'sha256': digest,
                                                            'coverage': 'complete', 'read_turn': 'relative'}])

    def test_hashes_observed_skill_and_reference_reads_across_turns(self):
        skill = '---\nname: frontend-ui\n---\n# Frontend and UI\nActual historical policy.\n'
        reference = '# Historical reference\nKeep this observed text.\n'
        text = self.record('event_msg', {'type': 'task_started', 'turn_id': 'first'})
        for index, (name, content) in enumerate([('SKILL.md', skill), ('references/polish.md', reference)]):
            raw = 'const r = await tools.exec_command({cmd:' + json.dumps('cat /old/skills/frontend-ui/' + name) + '});text(r);'
            output = str([{'type': 'input_text', 'text': json.dumps({'status': 'fulfilled', 'value': {'exit_code': 0, 'output': content}})}])
            text += self.record('response_item', {'type': 'custom_tool_call', 'call_id': str(index), 'name': 'exec', 'input': raw})
            text += self.record('response_item', {'type': 'custom_tool_call_output', 'call_id': str(index), 'output': output})
        text += self.record('event_msg', {'type': 'task_complete', 'turn_id': 'first'})
        self.rollout(text + text.replace('"first"', '"second"') + self.transcript('third', load=False))
        reads = self.scan()[2]['loaded_reads']
        self.assertEqual([r['path'] for r in reads], ['frontend-ui/SKILL.md', 'frontend-ui/references/polish.md'])
        self.assertEqual([r['sha256'] for r in reads], [hashlib.sha256(s.encode()).hexdigest() for s in (skill, reference)])
        self.assertEqual({r['read_turn'] for r in reads}, {'first'})
        self.assertEqual({r['coverage'] for r in reads}, {'complete'})
        self.assertRegex(self.scan()[0]['source_sha256'], r'^[a-f0-9]{64}$')

    def test_partial_failed_mixed_and_truncated_reads_are_not_full_file_hashes(self):
        result = {'exit_code': 0, 'output': '# Example\nVisible excerpt\n'}
        read = capture.loaded_reads('sed -n \'1,20p\' /skills/frontend-ui/references/polish.md', json.dumps(result), 'turn')
        self.assertEqual(read[0]['coverage'], 'partial')
        for command, value in [
            ('cat /skills/frontend-ui/SKILL.md; echo other', result),
            ('cat /skills/frontend-ui/SKILL.md /skills/frontend-ui/references/polish.md', result),
            ('cat /skills/frontend-ui/SKILL.md', {**result, 'exit_code': 1}),
            ('cat /skills/frontend-ui/SKILL.md', {**result, 'output': 'Warning: truncated output\n# Partial'}),
        ]:
            with self.subTest(command=command, result=value):
                self.assertEqual(capture.loaded_reads(command, json.dumps(value), 'turn'), [])

    def test_parallel_reads_require_proven_index_binding_not_print_order(self):
        first, second = '# First\n', '# Second\n'
        raw = 'const rs=await Promise.allSettled([tools.exec_command({cmd:"cat /skills/frontend-ui/SKILL.md"}),tools.exec_command({cmd:"cat /skills/frontend-ui/references/polish.md"})]);for(let i=0;i<rs.length;i++)text({i,...rs[i]});'
        output = [{'text': json.dumps({'i': i, 'status': 'fulfilled', 'value': {'exit_code': 0, 'output': content}})} for i, content in [(1, second), (0, first)]]
        reads = capture.loaded_reads(raw, str(output), 'turn')
        self.assertEqual([r['sha256'] for r in reads], [hashlib.sha256(s.encode()).hexdigest() for s in (first, second)])
        self.assertEqual(len(capture.loaded_reads(raw.replace('for(let i=0;i<rs.length;i++)text({i,...rs[i]});', 'rs.forEach((x,i)=>text({i,...x}));'), str(output), 'turn')), 2)
        self.assertEqual(capture.loaded_reads(raw.replace('for(let', 'rs.reverse();for(let'), str(output), 'turn'), [])
        self.assertEqual(capture.loaded_reads(raw, str([{'text': json.dumps({'value': {'exit_code': 0, 'output': first}})}] * 2), 'turn'), [])

    def test_single_read_fingerprints_require_an_unchanged_result_wrapper(self):
        command = 'cat /skills/frontend-ui/SKILL.md'
        call = 'tools.exec_command({cmd:' + json.dumps(command) + ',max_output_tokens:8000})'
        content = '# Frontend and UI\nActual historical policy.\n'
        result = json.dumps({'exit_code': 0, 'output': content})
        for raw in (command, json.dumps({'cmd': command}), 'text(await ' + call + ');',
                    'const r=await ' + call + ';text(r);', 'let result=await ' + call + ';text(result);'):
            with self.subTest(raw=raw):
                reads = capture.loaded_reads(raw, result, 'turn')
                self.assertEqual(len(reads), 1)
                self.assertEqual(reads[0]['coverage'], 'complete')
                self.assertEqual(reads[0]['sha256'], hashlib.sha256(content.encode()).hexdigest())
        for mutation in ('r.output=r.output.slice(0,20);', 'r.output="fabricated";',
                         'r={...r,output:r.output.slice(0,20)};', 'r=transform(r);'):
            with self.subTest(mutation=mutation):
                raw = 'const r=await ' + call + ';' + mutation + 'text(r);'
                self.assertEqual(capture.loaded_reads(raw, result, 'turn'), [])

    def test_rewritten_helper_envelopes_are_not_independently_trusted(self):
        path = self.root / 'skills/frontend-ui/SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text('# Frontend and UI\nHistorical guidance.\n')
        envelope = read_guidance.read(path)
        command = 'uv run --no-project python scripts/read_guidance.py ' + str(path)
        call = 'tools.exec_command({cmd:' + json.dumps(command) + '})'
        result = json.dumps({'exit_code': 0, 'output': json.dumps(envelope)})
        self.assertEqual(len(capture.loaded_reads('text(await ' + call + ');', result, 'turn')), 1)
        rewritten = 'const r=await ' + call + ';r.output=JSON.stringify(forged);text(r);'
        self.assertEqual(capture.loaded_reads(rewritten, result, 'turn'), [])
        fabricated = 'const unused={cmd:' + json.dumps(command) + '};text(forged);'
        self.assertEqual(capture.loaded_reads(fabricated, result, 'turn'), [])

    def test_read_wrappers_reject_command_overrides_and_noncall_batch_items(self):
        command = json.dumps('cat /skills/frontend-ui/SKILL.md')
        result = json.dumps({'exit_code': 0, 'output': 'fabricated'})
        options = (
            '{cmd:' + command + ',"cmd":"printf fabricated"}',
            '{cmd:' + command + ',["cmd"]:"printf fabricated"}',
            '{cmd:' + command + ',"\\u0063md":"printf fabricated"}',
            '{cmd:' + command + ',...{cmd:"printf fabricated"}}',
        )
        for arguments in options:
            for raw in ('text(await tools.exec_command(' + arguments + '));',
                        'const rs=await Promise.all([tools.exec_command(' + arguments + ')]);rs.forEach(text);'):
                with self.subTest(raw=raw):
                    self.assertEqual(capture.loaded_reads(raw, result, 'turn'), [])
        body = '(tools.exec_command({cmd:' + command + '}),{exit_code:0,output:"fabricated"})'
        raw = 'const rs=await Promise.all([' + body + ']);rs.forEach(text);'
        self.assertEqual(capture.loaded_reads(raw, result, 'turn'), [])

    def test_shell_control_syntax_cannot_forge_literal_read_provenance(self):
        commands = (
            'cat /dev/null;#/skills/frontend-ui/SKILL.md',
            "cat /dev/null;{echo,'# Frontend UI'};#/skills/frontend-ui/SKILL.md",
            'cat /dev/null\n#/skills/frontend-ui/SKILL.md',
            'cat #/skills/frontend-ui/SKILL.md',
        )
        for command in commands:
            actual = capture.subprocess.run(['/bin/bash', '-c', command], stdin=capture.subprocess.DEVNULL,
                                            capture_output=True, text=True, check=True)
            result = json.dumps({'exit_code': actual.returncode, 'output': actual.stdout})
            for raw in (command, json.dumps({'cmd': command}),
                        'text(await tools.exec_command({cmd:' + json.dumps(command) + ',shell:"/bin/bash"}));'):
                with self.subTest(raw=raw):
                    self.assertEqual(capture.loaded_reads(raw, result, 'turn'), [])
        for path in ('/old/*/skills/frontend-ui/SKILL.md', '/old/{a,b}/skills/frontend-ui/SKILL.md',
                     '$HOME/skills/frontend-ui/SKILL.md', '$(printf /old)/skills/frontend-ui/SKILL.md'):
            with self.subTest(path=path):
                self.assertEqual(capture.loaded_reads('cat ' + path, json.dumps({'exit_code': 0, 'output': '# Frontend UI'}), 'turn'), [])
        plain = capture.loaded_reads("cat '/old/My Project/skills/frontend-ui/SKILL.md'",
                                     json.dumps({'exit_code': 0, 'output': '# Frontend UI'}), 'turn')
        self.assertEqual(plain[0]['coverage'], 'complete')
        script = self.root / 'skills/frontend-ui/SKILL.md'
        script.parent.mkdir(parents=True)
        script.write_text('# Frontend UI\n')
        command = 'sed -n 1,20p -f' + str(script)
        # Option-shaped arguments are not literal filenames, regardless of sed dialect.
        self.assertEqual(capture.loaded_reads(command, json.dumps({'exit_code': 0, 'output': '# Frontend UI'}), 'turn'), [])
        lookalike = script.with_name('SKILL.md#other')
        lookalike.write_text('# Frontend UI\nDifferent file.\n')
        command = 'cat ' + str(lookalike)
        actual = capture.subprocess.run(['/bin/bash', '-c', command], stdin=capture.subprocess.DEVNULL,
                                        capture_output=True, text=True, check=True)
        self.assertEqual(capture.loaded_reads(command, json.dumps({'exit_code': 0, 'output': actual.stdout}), 'turn'), [])

    def test_reader_content_reaches_automatic_note_without_raw_guidance(self):
        path = self.root / 'skills/frontend-ui/SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text('---\nname: frontend-ui\n---\n# Frontend and UI\nHistorical guide.\nOutput may be truncated; no such file is an error example.\n')
        envelope = read_guidance.read(path)
        text = self.transcript(load=False)
        index = text.index(self.record('response_item', {'type': 'message', 'role': 'assistant', 'content': [{'text': 'Built page and checked layout.'}]}))
        text = text[:index] + self.record('response_item', {'type': 'function_call', 'call_id': 'guide', 'name': 'exec_command',
            'arguments': json.dumps({'cmd': 'uv run --no-project python scripts/read_guidance.py ' + str(path)})}) + self.record(
            'response_item', {'type': 'function_call_output', 'call_id': 'guide', 'output': json.dumps({'exit_code': 0, 'output': json.dumps(envelope)})}) + text[index:]
        self.rollout(text)
        self.assertEqual(self.scan()[0]['loaded_reads'][0]['sha256'], envelope['sha256'])
        with patch.object(capture, 'extract', return_value=[self.lesson]):
            self.assertEqual(capture.capture(self.config, self.state)['notes'], 1)
        note = next(self.feedback.rglob('*.md')).read_text()
        self.assertIn(envelope['sha256'], note)
        self.assertIn('"coverage": "complete"', note)
        self.assertNotIn('Historical guide.', note)

    def test_capture_dedupe_and_provenance(self):
        self.rollout()
        with patch.object(capture, 'extract', return_value=[self.lesson]) as model:
            self.assertEqual(capture.capture(self.config, self.state)['notes'], 1)
            self.assertEqual(capture.capture(self.config, self.state)['notes'], 0)
            model.assert_called_once()
        note = next(self.feedback.rglob('*.md'))
        self.assertIn('loaded unknown', note.read_text())
        self.assertIn('deployed-at-capture unavailable', note.read_text())
        (self.state / 'capture-state.json').unlink()
        with patch.object(capture, 'extract') as model:
            self.assertEqual(capture.capture(self.config, self.state)['notes'], 0)
            model.assert_not_called()

    def test_model_failure_does_not_skip_cursor(self):
        self.rollout()
        with patch.object(capture, 'extract', side_effect=RuntimeError('model unavailable')):
            self.assertEqual(capture.capture(self.config, self.state)['errors'], 1)
        self.assertEqual(json.loads((self.state / 'capture-state.json').read_text())['processed'], {})
        with patch.object(capture, 'extract', return_value=[]) as model:
            self.assertEqual(capture.capture(self.config, self.state, now=datetime.now(timezone.utc) + timedelta(hours=2))['processed'], 1)
            model.assert_called_once()
        with patch.object(capture, 'extract') as model:
            capture.capture(self.config, self.state)
            model.assert_not_called()

    def test_python_literal_tool_values_with_invalid_escapes_parse_without_warnings(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            results = capture.read_results("{'exit_code': 0, 'output': 'a\\|b'}")
        self.assertEqual(results, [{'exit_code': 0, 'output': 'a\\|b'}])
        self.assertEqual([str(item.message) for item in caught], [])

    def test_javascript_command_literals_decode_with_javascript_semantics(self):
        call = 'text(await tools.exec_command({cmd:%s}));'
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            for literal, expected in [('"cat skills\\/rust\\/SKILL.md"', ['cat skills/rust/SKILL.md']),
                                      ('"printf \\"a\\tb\\""', ['printf "a\tb"']), ("'it\\'s'", ["it's"]),
                                      # Escapes pair from the left: an escaped backslash, then a literal bar.
                                      ("'grep a\\\\|b x'", ['grep a\\|b x']),
                                      # An escape with no meaning of its own is that character, as in JavaScript.
                                      ("'grep a\\|b x'", ['grep a|b x']), ('"grep a\\|b x"', ['grep a|b x']),
                                      ('"it\\\'s \\x41\\u{42}\\v"', ["it's AB\v"]), ("'a\\\nb'", ['ab']),
                                      # Literals invalid in strict-mode JavaScript leave the command unknown.
                                      ("'\\1'", []), ("'\\x4'", []), ("'\\ud83d'", []), ("'a\nb'", [])]:
                with self.subTest(literal=literal):
                    self.assertEqual(capture.literal_commands(call % literal), expected)
        self.assertEqual([str(item.message) for item in caught], [])

    def test_escaped_slash_skill_read_is_recognized_and_capture_emits_no_warnings(self):
        skill = '---\nname: rust\n---\n# Rust\nObserved guidance.\n'
        read = 'text(await tools.exec_command({cmd:"cat skills\\/rust\\/SKILL.md"}));'
        envelope = str([{'type': 'input_text', 'text': json.dumps({'exit_code': 0, 'output': skill})}])
        # A command that is no skill read and a Python-literal envelope with an escape Python warns about.
        unknown = "text(await tools.exec_command({cmd:'cat skills/rust/references/a\\|b.md'}));"
        text = self.record('event_msg', {'type': 'task_started', 'turn_id': 'escaped'})
        text += self.record('event_msg', {'type': 'user_message', 'message': 'Fix the borrow error'})
        for call, raw, output in (('read', read, envelope), ('unknown', unknown, "{'exit_code': 0, 'output': 'a\\|b'}")):
            text += self.record('response_item', {'type': 'custom_tool_call', 'call_id': call, 'name': 'exec', 'input': raw})
            text += self.record('response_item', {'type': 'custom_tool_call_output', 'call_id': call, 'output': output})
        text += self.record('response_item', {'type': 'message', 'role': 'assistant', 'content': [{'text': 'Fixed it.'}]})
        text += self.record('event_msg', {'type': 'task_complete', 'turn_id': 'escaped'})
        self.rollout(text)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            [turn] = self.scan()
            with patch.object(capture, 'run_extractor', return_value=completed({'notes': []})):
                self.assertEqual(capture.capture(self.config, self.state)['processed'], 1)
        self.assertEqual(turn['verified_skills'], ['rust'])
        self.assertEqual(turn['loaded_reads'], [{'path': 'rust/SKILL.md', 'sha256': hashlib.sha256(skill.encode()).hexdigest(),
                                                'coverage': 'complete', 'read_turn': 'escaped'}])
        self.assertEqual([str(item.message) for item in caught], [])

    def test_maintenance_turn_with_an_absolute_pipeline_path_is_excluded(self):
        # Turn messages stay raw for local filters; redacted, this path would be a placeholder.
        path = '/Us' + 'ers/alice/.local/share/codex-skills/repository/scripts/capture.py'
        self.rollout(self.transcript(user='Fix ' + path + ' retries'))
        self.assertEqual(self.scan(), [])
        self.assertEqual(len(capture.completed_turns(self.sessions.glob('*.jsonl'), capture.timestamp(capture.SINCE),
                                                     eligible_only=False)), 1)

    def test_null_codex_message_text_does_not_abort_capture(self):
        text = self.transcript().replace('"message": "Build a small page"', '"message": null', 1)
        text = text.replace('"content": [{"text": "Built page and checked layout."}]', '"content": [{"text": null}]', 1)
        text = text.replace(self.record('event_msg', {'type': 'task_complete', 'turn_id': 'turn1'}),
                            self.record('response_item', {'type': 'message', 'role': 'assistant', 'content': None})
                            + self.record('event_msg', {'type': 'task_complete', 'turn_id': 'turn1'}), 1)
        self.rollout(text + self.transcript('turn2'))
        first, second = self.scan()
        self.assertEqual([m['text'] for m in first['messages'] if m['role'] != 'tool'], ['', '', ''])
        self.assertEqual(second['turn'], 'turn2')
        with patch.object(capture, 'run_extractor', return_value=completed({'notes': []})):
            self.assertEqual(capture.capture(self.config, self.state)['processed'], 2)

    def test_compact_bound_applies_to_redacted_text_so_no_secret_fragment_leaks(self):
        # Truncating first would leave a fragment the patterns no longer recognize.
        for private, fragment in (('ghp_' + 'Q' * 30, 'Q'), ('alice@example.com', 'alice')):
            with self.subTest(fragment=fragment):
                text = 'x' * 1990 + ' ' + private
                self.assertIn(fragment, text[:2000])
                context = capture.message_context([{'role': 'user', 'text': text}], compact=True)
                [message] = context['messages']
                self.assertNotIn(fragment, message['text'])
                self.assertEqual(len(message['text']), 2000)
                self.assertEqual(message['original_chars'], len(capture.redact(text)))
                self.assertTrue(context['truncated'])
        [message] = capture.message_context([{'role': 'user', 'text': 'Use token=abc123'}], compact=True)['messages']
        self.assertEqual((message['text'], message['original_chars']), ('Use [redacted credential]', 25))

    def test_redaction_covers_credential_forms(self):
        key = '-----BEGIN ' + 'OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----'
        for text, expected in [
                ('password="hunter2"', '[redacted credential]'),
                ("API_KEY='abc123'", '[redacted credential]'),
                ('{"api_key": "abc123", "token": "xyz"}', '{[redacted credential], [redacted credential]}'),
                ('AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY', '[redacted credential]'),
                ('GITHUB_TOKEN=abc DB_PASSWORD=x client_secret: y OPENAI_API_KEY=z',
                 '[redacted credential] [redacted credential] [redacted credential] [redacted credential]'),
                ('token=abc123 and Bearer abc.def and password: hunter2', '[redacted credential] and [redacted credential] and [redacted credential]'),
                ('github_pat_11ABCDEFG0_abcdefghijklmnop', '[redacted credential]'),
                ('AKIA' + 'IOSFODNN7EXAMPLE', '[redacted credential]'),
                ('sk-proj-abcdefgh12345678 sk-abcdefgh12345', '[redacted credential] [redacted credential]'),
                ('ghp_abcdefgh12345 gho_abcdefgh123 ghu_abcdefgh123 ghs_abcdefgh123 ghr_abcdefgh123',
                 ' '.join(['[redacted credential]'] * 5)),
                ('curl -d "{\\"api_key\\": \\"abc123\\", \\"token\\": \\"x y\\"}" now',
                 'curl -d "{[redacted credential], [redacted credential]}" now'),
                ('githubTokenValue: abc123 apiKeyId=x private_key: k passwd=p',
                 '[redacted credential] [redacted credential] [redacted credential] [redacted credential]'),
                ('key:\n' + key + '\ndone', 'key:\n[redacted credential]\ndone'),
                ('cut ' + key[:40], 'cut [redacted credential]'),
                ('-----BEGIN PGP ' + 'PRIVATE KEY BLOCK-----\nlQOYBF\n-----END PGP PRIVATE KEY BLOCK-----\nafter',
                 '[redacted credential]\nafter'),
                # Ordinary words that contain or resemble credential names are kept.
                ('max_tokens: 100, tokenizer: bpe, secretary: Ann, FAKE_TOKEN placeholder, passwords: 3, the cupbearer of wine',
                 'max_tokens: 100, tokenizer: bpe, secretary: Ann, FAKE_TOKEN placeholder, passwords: 3, the cupbearer of wine')]:
            with self.subTest(text=text[:30]):
                self.assertEqual(capture.redact(text), expected)

    def test_redaction_stays_linear_on_long_words_and_repeated_credential_names(self):
        # Each case took tens of seconds with a backtracking name pattern.
        for text in ('token_' * 20000, '-'.join(['api-key'] * 20000), 'a' * 120000, 'a-' * 60000, '"token' * 20000):
            with self.subTest(text=text[:12]):
                started = time.monotonic()
                capture.redact(text)
                self.assertLess(time.monotonic() - started, 2)

    def test_redaction_replaces_urls_and_git_remotes_before_emails(self):
        for text, expected in [
                ('https://alice:pw@github.com/acme/secret-repo', '[redacted URL]'),
                ('git@github.com:acme/secret-repo.git', '[redacted URL]'),
                ('ssh://git@github.com/acme/secret-repo.git and file:///tmp/x', '[redacted URL] and [redacted URL]'),
                ('https://example.com/?token=abc&x=1 next', '[redacted URL] next'),
                ('mail a@example.com: thanks', 'mail [redacted email]: thanks'),
                # Pattern-based limit: a local path is replaced only up to its first space.
                ('open /Us' + 'ers/alice/My Project/app.tsx', 'open [local path] Project/app.tsx')]:
            with self.subTest(text=text[:30]):
                self.assertEqual(capture.redact(text), expected)

    def test_schema(self):
        turn = {'verified_skills': ['frontend-ui']}
        self.assertEqual(capture.validate_notes({'notes': [self.lesson]}, turn), [self.lesson])
        for bad in [{'notes': [{**self.lesson, 'skill': 'other'}]}, {'notes': [{**self.lesson, 'evidence': 'two\nlines'}]},
                    {'notes': [{**self.lesson, 'cause': ' '}]}, {'notes': [{**self.lesson, 'title': 'x' * 1001}]},
                    {'notes': [self.lesson] * 4}, {'notes': [], 'extra': True}]:
            with self.assertRaises(ValueError):
                capture.validate_notes(bad, turn)

    def test_notes_may_reuse_source_wording_names_and_identifiers(self):
        run = 'the layout check failed after the header moved'
        source = ('Claude Code built the Aster Labs page on 2026-11-04 at https://example.com/docs for a@example.com. '
                  '"The private draft must stay unpublished until launch." ' + run + '. 未公開研究資料には秘密顧客情報が含まれています')
        turn = {'verified_skills': ['frontend-ui'], 'messages': [{'role': 'assistant', 'text': source}]}
        for field, text in [('task', 'Claude Code built the Aster Labs page'), ('evidence', 'Checked on 2026-11-04'),
                            ('evidence', 'Read https://example.com/docs'), ('evidence', 'Mailed a@example.com'),
                            ('task', 'Edited /Us' + 'ers/alice/project/app.tsx'), ('proposal', 'Call +1 (555) 010-0199 at 12 Main Street'),
                            ('observation', '"The private draft must stay unpublished until launch."'),
                            ('cause', '未公開研究資料には秘密顧客情報が含まれています'), ('observation', 'Saw that ' + run)]:
            note = {**self.lesson, field: text}
            self.assertEqual(capture.validate_notes({'notes': [note]}, turn), [note])

    def test_notes_with_credentials_pass_validation(self):
        turn = {'verified_skills': ['frontend-ui']}
        for credential in ('api_key=abc123', 'Bearer abc.def', 'password: hunter2', 'sk-abcdefgh12345', 'ghp_abcdefgh12345'):
            note = {**self.lesson, 'evidence': 'Used ' + credential}
            self.assertEqual(capture.validate_notes({'notes': [note]}, turn), [note])

    def test_summary_uses_note_field_rules(self):
        for summary in ('Quoted "source text" at https://example.com and token=abc123', 'x' * 1000):
            capture.validate_summary(summary)
            capture.validate_summary(summary, 'rust')
        for summary in ('', ' ', 'First\nsecond', 'x' * 1001, None):
            with self.subTest(summary=summary), self.assertRaises(ValueError):
                capture.validate_summary(summary)

    def test_capture_timestamp_and_turn_provenance(self):
        self.rollout()
        now = datetime(2026, 10, 13, 1, 2, 3, tzinfo=timezone.utc)
        with patch.object(capture, 'extract', return_value=[self.lesson]):
            capture.capture(self.config, self.state, now=now)
        path = next(self.feedback.rglob('*.md'))
        self.assertIn('- Recorded: 2026-10-13T01:02:03+00:00', path.read_text())
        self.assertIn('- Completed: 2026-10-10T01:00:00Z', path.read_text())
        self.assertIn('- Turn: turn1', path.read_text())
        self.assertTrue(path.name.startswith('2026-10-13T010203-0000'))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_capture_uses_migrated_home_without_allowing_feedback_symlinks(self):
        home = self.root / 'home'
        canonical = home / '.config/codex'
        (canonical / 'sessions').mkdir(parents=True)
        (home / '.codex').symlink_to(canonical)
        (canonical / 'sessions/source.jsonl').write_bytes(self.rollout().read_bytes())
        config = {key: value for key, value in self.config.items() if key not in ('sessions_root', 'feedback_dir')}
        with patch.object(Path, 'home', return_value=home), patch.dict(os.environ, {'CODEX_HOME': '', 'CLAUDE_CONFIG_DIR': ''}), patch.object(capture, 'extract', return_value=[self.lesson]):
            self.assertEqual(capture.capture(config, self.state)['notes'], 1)
            self.assertEqual(len(list((canonical / 'skill-feedback').rglob('*.md'))), 1)
            external = self.root / 'external'
            external.mkdir()
            folder = canonical / 'skill-feedback/content-research-writer'
            folder.mkdir()
            (folder / 'notes').symlink_to(external)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                capture.capture(config, self.state)
            self.assertEqual(list(external.iterdir()), [])

    def test_codex_home_honors_explicit_location(self):
        canonical = self.root / 'custom-home'
        canonical.mkdir()
        alias = self.root / 'custom-alias'
        alias.symlink_to(canonical)
        with patch.dict(os.environ, {'CODEX_HOME': str(alias)}):
            self.assertEqual(capture.codex_home(), canonical.resolve())

    def test_atomic_immutable_and_symlink_defense(self):
        path = self.feedback / 'note.md'
        capture.atomic(path, 'one', exclusive=True)
        capture.atomic(path, 'one', exclusive=True)
        with self.assertRaises(ValueError):
            capture.atomic(path, 'two', exclusive=True)
        self.assertEqual(path.read_text(), 'one')
        link = self.root / 'link'
        link.symlink_to(self.feedback, target_is_directory=True)
        with self.assertRaises(ValueError):
            capture.atomic(link / 'danger.md', 'bad')
        self.assertEqual(list(self.feedback.glob('.capture-*')), [])

    def test_actual_custom_tool_and_response_user_format(self):
        text = self.transcript().replace('function_call_output', 'custom_tool_call_output').replace('function_call', 'custom_tool_call')
        old = self.record('event_msg', {'type': 'user_message', 'message': 'Build a small page'})
        new = self.record('response_item', {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Build a small page'}]})
        self.rollout(text.replace(old, new))
        turn = self.scan()[0]
        self.assertEqual(turn['messages'][0]['text'], 'Build a small page')
        self.assertEqual(turn['verified_skills'], ['frontend-ui'])
        self.assertEqual(turn['calls'], [{'name': 'exec_command'}])

    def test_partial_note_transaction_retries_without_model(self):
        self.rollout()
        second = {**self.lesson, 'title': 'Second observed check'}
        original = capture.atomic
        def fail_second(path, data, exclusive=False):
            if exclusive and '# Second observed check' in data:
                raise OSError('disk interrupted')
            return original(path, data, exclusive)
        with patch.object(capture, 'extract', return_value=[self.lesson, second]), patch.object(capture, 'atomic', side_effect=fail_second):
            with self.assertRaises(OSError):
                capture.capture(self.config, self.state)
        self.assertEqual(len(list(self.feedback.rglob('*.md'))), 1)
        with patch.object(capture, 'extract') as model:
            self.assertEqual(capture.capture(self.config, self.state, now=datetime.now(timezone.utc) + timedelta(days=1))['notes'], 1)
            model.assert_not_called()
        self.assertEqual(len(list(self.feedback.rglob('*.md'))), 2)

    def test_oversized_inline_images_preserve_turn_and_raw_source_identity(self):
        output = [{'type': 'input_text', 'text': 'Image preview'},
                  *[{'type': 'input_image', 'image_url': 'data:image/png;base64,' + '/AAA' * 3000}
                    for _ in range(2)]]
        image = self.record('response_item', {'type': 'custom_tool_call_output', 'call_id': 'preview', 'output': output})
        # JSON permits escaped slashes as well as literal slashes in image data.
        image = image.replace('/', r'\/')
        lines = self.transcript().splitlines(keepends=True)
        lines.insert(-2, image)
        text = ''.join(lines)
        path = self.rollout(text)
        with patch.object(capture, 'MAX_RECORD_BYTES', len(text.encode()) + 1):
            original = self.scan()
        errors = []
        with patch.object(capture, 'MAX_RECORD_BYTES', 2048):
            result = capture.completed_turns([path], capture.timestamp(capture.SINCE), errors)
            evidence_result = capture.completed_turns([path], capture.timestamp(capture.SINCE), errors, eligible_only=False)
        self.assertEqual(errors, [])
        self.assertEqual(result, original)
        self.assertEqual(evidence_result, original)
        self.assertEqual(result[0]['verified_skills'], ['frontend-ui'])
        self.assertEqual(result[0]['source_sha256'], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(result[0]['evidence_id'], hashlib.sha256(b'session1:turn1').hexdigest())

    def test_base64_reduction_preserves_json_and_ordinary_records(self):
        data = '/AAA' * 1500
        value = {'data': data, 'url': 'data:application/pdf;base64,' + data,
                 'text': 'A quoted payload: "' + data,
                 'quoted': 'A quoted payload: "' + data + '"; trailing prose.',
                 'prose': 'Large prose ' * 500}
        line = json.dumps(value).encode()
        self.assertIs(capture.parse_bytes(line), line)
        with patch.object(capture, 'MAX_RECORD_BYTES', 512):
            for candidate in (line, line.replace(b'/', b'\\/')):
                with self.subTest(escaped=candidate != line):
                    reduced = json.loads(capture.parse_bytes(candidate))
                    self.assertEqual(reduced, {'data': '[inline base64 omitted]',
                                              'url': '[inline base64 omitted]',
                                              'text': 'A quoted payload: "[inline base64 omitted]',
                                              'quoted': value['quoted'],
                                              'prose': value['prose']})
            prose = json.dumps(value['prose']).encode()
            self.assertEqual(capture.parse_bytes(prose), prose)
            small = b'{"data": "AAAA"}'
            self.assertIs(capture.parse_bytes(small), small)

    def test_oversized_nonimage_and_residual_text_keep_rollout_diagnostic(self):
        for output in ('Large prose ' * 500, [{'type': 'input_image', 'image_url': 'data:image/png;base64,' + 'A' * 5000},
                                    {'type': 'input_text', 'text': 'Large prose ' * 500}]):
            with self.subTest(output=str(output)[:80]):
                lines = self.transcript().splitlines(keepends=True)
                lines.insert(-2, self.record('response_item', {'type': 'custom_tool_call_output', 'output': output}))
                path = self.rollout(''.join(lines))
                errors = []
                with patch.object(capture, 'MAX_RECORD_BYTES', 2048):
                    self.assertEqual(capture.completed_turns([path], capture.timestamp(capture.SINCE), errors), [])
                self.assertEqual([error['reason'] for error in errors], ['oversized rollout record'])

    def test_bad_record_does_not_hide_other_completed_turns(self):
        self.rollout(self.transcript() + 'invalid complete JSON\n' + self.transcript('turn2'))
        errors = []
        self.assertEqual(len(capture.completed_turns(self.sessions.glob('*.jsonl'), capture.timestamp(capture.SINCE), errors)), 2)
        self.assertEqual(errors, [{'file': 'rollout.jsonl', 'line': 8, 'reason': 'invalid rollout record'}])

    def test_failure_does_not_starve_later_turn(self):
        self.rollout(self.transcript() + self.transcript('turn2', load=False))
        def model(config, root, turn, existing, *, deadline=None, wait_for_rate_limit=True):
            if turn['turn'] == 'turn1':
                raise RuntimeError('unavailable')
            return []
        with patch.object(capture, 'extract', side_effect=model):
            summary = capture.capture(self.config, self.state)
        self.assertEqual(summary['processed'], 1)
        self.assertEqual(summary['pending'], 1)
        self.assertEqual(summary['errors'], 1)

    def test_extraction_puts_shared_notes_first_and_marks_them_as_the_cache_prefix(self):
        self.state.mkdir()
        existing = [{'skill': 'frontend-ui', 'text': 'Shared lesson', 'order': 1}]
        turn = {'verified_skills': ['frontend-ui'], 'messages': [], 'calls': []}
        seen = []
        with patch.object(capture, 'model_response', side_effect=lambda *args, **kwargs: (seen.append((args, kwargs)), {'notes': []})[1]):
            self.assertEqual(capture.extract(self.config, self.state, turn, existing), [])
            self.assertEqual(capture.extract(self.config, self.state, turn, []), [])
        (args, kwargs), (empty_args, empty_kwargs) = seen
        data, prefix = args[3], kwargs['cache_prefix']
        self.assertEqual(list(json.loads(data)), ['existing_notes', 'turn'])
        self.assertEqual(json.loads(data[:prefix].rstrip(', ') + '}'), {'existing_notes': existing})
        self.assertTrue(data[prefix:].startswith('"turn": '))
        # Without existing notes there is no shared prefix to cache.
        self.assertIsNone(empty_kwargs['cache_prefix'])
        self.assertEqual(json.loads(empty_args[3])['existing_notes'], [])

    def test_dedupe_context_uses_recent_notes_for_relevant_skill(self):
        self.state.mkdir()
        existing = [{'skill': 'frontend-ui', 'text': 'Relevant newest lesson', 'order': 99}] + [
            {'skill': 'content-research-writer', 'text': 'Unrelated writer lesson', 'order': index} for index in range(20)]
        turn = {'verified_skills': ['frontend-ui'], 'messages': [], 'calls': []}
        def run(command, **kwargs):
            self.assertIn('Relevant newest lesson', kwargs['input'])
            self.assertNotIn('Unrelated writer lesson', kwargs['input'])
            return completed({'notes': []})
        with patch.object(capture, 'run_extractor', side_effect=run):
            self.assertEqual(capture.extract(self.config, self.state, turn, existing), [])

    def test_original_message_tails_and_chronology_reach_extractor(self):
        text = 'x' * 8500 + ' Material correction at the end'
        transcript = self.transcript(user=text)
        transcript = transcript.replace(self.record('event_msg', {'type': 'task_complete', 'turn_id': 'turn1'}), '')
        transcript += self.record('response_item', {'type': 'message', 'role': 'user', 'content': [{'text': text}]})
        transcript += self.record('response_item', {'type': 'message', 'role': 'assistant', 'content': [{'text': 'Final reply'}]})
        transcript += self.record('event_msg', {'type': 'task_complete', 'turn_id': 'turn1'})
        self.rollout(transcript)
        turn = self.scan()[0]
        self.assertEqual([len(m['text']) for m in turn['messages'] if m['role'] == 'user'], [len(text), len(text)])
        with patch.object(capture, 'model_response', return_value={'notes': []}) as model:
            capture.extract(self.config, self.state, turn, [])
        compact = json.loads(model.call_args.args[3])['turn']
        self.assertEqual([m['role'] for m in compact['messages']], ['user', 'assistant', 'user', 'assistant'])
        self.assertTrue(all(m['text'].endswith('Material correction at the end') for m in compact['messages'] if m['role'] == 'user'))
        self.assertFalse(compact['truncated'])

    def test_bounded_message_context_keeps_order_and_identifies_every_omission(self):
        messages = [{'role': role, 'text': str(i) + 'x' * 3000} for i in range(9) for role in ('assistant', 'user')]
        result = capture.message_context(messages, compact=True)
        indices = [m['source_index'] for m in result['messages']]
        self.assertEqual(indices, sorted(indices))
        self.assertEqual(set(indices) | set(result['omitted_message_indices']), set(range(len(messages))))
        self.assertTrue(result['truncated'])
        self.assertTrue(all(m['original_chars'] == 3001 and len(m['text']) == 2000 for m in result['messages']))

    def test_large_multi_skill_turn_fallback_keeps_balanced_context(self):
        self.state.mkdir()
        existing = [{'skill': skill, 'text': skill + ' context ' + 'x' * 1000, 'order': index}
                    for skill in capture.SKILLS for index in range(4)]
        turn = {'verified_skills': list(capture.SKILLS), 'calls': [], 'messages': [
            *[{'role': 'user', 'text': 'u' * 3000} for _ in range(6)],
            *[{'role': 'assistant', 'text': 'a' * 6000} for _ in range(2)]]}
        def run(command, **kwargs):
            data = json.loads(kwargs['input'][len(capture.PROMPT) + 1:])
            self.assertEqual({note['skill'] for note in data['existing_notes']}, set(capture.SKILLS))
            self.assertEqual(len(data['existing_notes']), 2 * len(capture.SKILLS))
            self.assertTrue(data['turn']['truncated'])
            return completed({'notes': []})
        with patch.object(capture, 'MAX_INPUT', 20000 + 2000 * len(capture.SKILLS)), patch.object(capture, 'run_extractor', side_effect=run):
            self.assertEqual(capture.extract(self.config, self.state, turn, existing), [])

    def test_multi_skill_context_does_not_drop_the_first_enrolled_skill(self):
        turn = {'verified_skills': list(capture.SKILLS), 'calls': [], 'messages': []}
        existing = [{'skill': skill, 'text': 'Relevant history', 'order': index}
                    for skill in capture.SKILLS for index in range(4)]
        with patch.object(capture, 'model_response', return_value={'notes': []}) as model:
            self.assertEqual(capture.extract(self.config, self.state, turn, existing), [])
        notes = json.loads(model.call_args.args[3])['existing_notes']
        self.assertEqual({note['skill'] for note in notes}, set(capture.SKILLS))
        self.assertTrue(all(sum(n['skill'] == skill for n in notes) >= 2 for skill in capture.SKILLS))

    def test_non_tool_events_do_not_imply_tool_execution(self):
        self.state.mkdir()
        turn = {'verified_skills': ['frontend-ui'], 'messages': [], 'calls': []}
        thinking = {'type': 'assistant', 'error': None, 'message': {'content': [
            {'type': 'thinking', 'thinking': 'private'}, {'type': 'text', 'text': 'private'}]}}
        limit = {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed'}}
        events = answer({'notes': []}).splitlines(keepends=True)
        result = capture.subprocess.CompletedProcess([], 0, events[0] + stream(thinking, limit) + ''.join(events[1:]), '')
        with patch.object(capture, 'run_extractor', return_value=result):
            self.assertEqual(capture.extract(self.config, self.state, turn, []), [])
        result = capture.subprocess.CompletedProcess([], 0, stream(INIT, tool_use('Read')), '')
        with patch.object(capture, 'run_extractor', return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, 'attempted tool execution'):
                capture.extract(self.config, self.state, turn, [])
        run.assert_called_once()

    def test_static_failure_diagnostics_never_echo_source_text(self):
        cases = [(ValueError('Invalid capture note field'), 'model schema rejected'),
                 (ValueError('Invalid capture note scope'), 'model scope rejected'),
                 (RuntimeError('Capture model attempted tool execution'), 'model tool event rejected'),
                 (capture.subprocess.TimeoutExpired('private command', 240), 'extractor timeout'),
                 (ValueError('confidential customer text'), 'model or schema extraction failure')]
        for error, expected in cases:
            self.assertEqual(capture.failure_reason(error), expected)
        self.rollout()
        with patch.object(capture, 'extract', side_effect=ValueError('confidential customer text')):
            capture.capture(self.config, self.state)
        state = (self.state / 'capture-state.json').read_text()
        self.assertNotIn('confidential customer text', state)
        self.assertIn('model or schema extraction failure', state)

    def test_legacy_privacy_deferral_is_retried_without_a_hint(self):
        self.rollout()
        with patch.object(capture, 'extract', side_effect=ValueError('Invalid capture output')):
            capture.capture(self.config, self.state)
        path = self.state / 'capture-state.json'
        state = json.loads(path.read_text())
        [entry] = state['deferred'].values()
        entry['reason'] = 'privacy source quotation rejected'
        entry['retry_at'] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        path.write_text(json.dumps(state))
        with patch.object(capture, 'extract', return_value=[self.lesson]) as model:
            result = capture.capture(self.config, self.state)
        self.assertEqual((result['processed'], result['notes'], result['errors']), (1, 1, 0))
        self.assertNotIn('previous_capture_rejection', model.call_args.args[2])
        self.assertEqual(json.loads(path.read_text())['deferred'], {})

    def test_recovery_clears_pending_and_deferred_transactions(self):
        self.rollout()
        with patch.object(capture, 'extract', return_value=[self.lesson]):
            capture.capture(self.config, self.state)
        path = self.state / 'capture-state.json'
        state = json.loads(path.read_text())
        evidence = next(iter(state['processed']))
        state['processed'] = {}
        state['pending'][evidence] = {'lessons': [self.lesson]}
        state['deferred'][evidence] = {'reason': 'obsolete'}
        path.write_text(json.dumps(state))
        with patch.object(capture, 'extract') as model:
            self.assertEqual(capture.capture(self.config, self.state)['pending'], 0)
            model.assert_not_called()
        recovered = json.loads(path.read_text())
        self.assertIn(evidence, recovered['processed'])
        self.assertEqual(recovered['pending'], {})
        self.assertEqual(recovered['deferred'], {})

    def test_scanner_diagnostics_are_bounded_static_and_local_only(self):
        self.rollout(self.transcript() + 'private invalid payload\n' * 15)
        with patch.object(capture, 'extract', return_value=[]):
            summary = capture.capture(self.config, self.state)
        self.assertEqual(summary['scanner_error_count'], 15)
        self.assertEqual(summary['errors'], 15)
        self.assertEqual(len(summary['scanner_diagnostics']), 10)
        locator = summary['scanner_diagnostics'][0]
        self.assertEqual(locator, {'file': 'rollout.jsonl', 'line': 8, 'reason': 'invalid rollout record'})
        self.assertNotIn('private invalid payload', json.dumps(summary))
        self.assertNotIn(str(self.sessions), json.dumps(summary))

    def test_extractor_receives_redacted_transcript_text(self):
        user = ('Build a small page; mail a@example.com, open https://example.com/x, read /Us' + 'ers/alice/notes.md, '
                'use token=abc123 and Bearer abc.def and sk-abcdefgh12345')
        context = 'Also check /home/alice/app with ghp_abcdefgh12345 and password: hunter2'
        reply = 'Built page at https://example.com/x and mailed a@example.com.'
        text = self.record('event_msg', {'type': 'task_started', 'turn_id': 'turn1'})
        text += self.record('event_msg', {'type': 'user_message', 'message': user})
        text += self.record('response_item', {'type': 'message', 'role': 'user', 'content': [{'text': context}]})
        text += self.record('response_item', {'type': 'function_call', 'call_id': 'read', 'name': 'exec_command', 'arguments': 'cat /skills/frontend-ui/SKILL.md'})
        text += self.record('response_item', {'type': 'function_call_output', 'call_id': 'read', 'output': 'Exit code: 0\n# Frontend UI\n' + 'skill policy content ' * 20})
        text += self.record('response_item', {'type': 'message', 'role': 'assistant', 'content': [{'text': reply}]})
        text += self.record('event_msg', {'type': 'task_complete', 'turn_id': 'turn1'})
        self.rollout(text)
        inputs = []
        def run(command, **kwargs):
            inputs.append(json.loads(kwargs['input'][len(capture.PROMPT) + 1:]))
            return completed({'notes': []})
        with patch.object(capture, 'run_extractor', side_effect=run):
            self.assertEqual(capture.capture(self.config, self.state)['processed'], 1)
        [data] = inputs
        self.assertEqual([m['text'] for m in data['turn']['messages']], [
            'Build a small page; mail [redacted email], open [redacted URL] read [local path] '
            'use [redacted credential] and [redacted credential] and [redacted credential]',
            'Also check [local path] with [redacted credential] and [redacted credential]',
            'Built page at [redacted URL] and mailed [redacted email].'])
        for private in ('a@example.com', 'example.com/x', 'alice', 'abc123', 'abc.def', 'abcdefgh12345', 'hunter2'):
            self.assertNotIn(private, json.dumps(data))

    def test_extractor_receives_redacted_existing_note_context(self):
        self.rollout()
        notes = self.feedback / 'frontend-ui/notes'
        notes.mkdir(parents=True)
        (notes / 'existing.md').write_text('# Earlier lesson\n- Evidence: Read /Us' + 'ers/alice/app.tsx via '
                                           'https://example.com/x for a@example.com with token=abc123\n')
        inputs = []
        def run(command, **kwargs):
            inputs.append(json.loads(kwargs['input'][len(capture.PROMPT) + 1:]))
            return completed({'notes': []})
        with patch.object(capture, 'run_extractor', side_effect=run):
            self.assertEqual(capture.capture(self.config, self.state)['processed'], 1)
        [data] = inputs
        self.assertEqual([note['text'] for note in data['existing_notes']], [
            '# Earlier lesson\n- Evidence: Read [local path] via [redacted URL] for [redacted email] with [redacted credential]\n'])

    def test_retry_after_rejection_carries_no_rejection_hint(self):
        self.rollout()
        attempts = []
        rejected = {**self.lesson, 'evidence': 'two\nlines'}
        def run(command, **kwargs):
            data = json.loads(kwargs['input'][len(capture.PROMPT) + 1:])
            attempts.append(data['turn'])
            note = rejected if len(attempts) == 1 else self.lesson
            return completed({'notes': [note]})
        now = datetime.now(timezone.utc)
        with patch.object(capture, 'run_extractor', side_effect=run):
            first = capture.capture(self.config, self.state, now=now)
            self.assertEqual((first['errors'], first['processed']), (1, 0))
            deferred = next(iter(json.loads((self.state / 'capture-state.json').read_text())['deferred'].values()))
            self.assertEqual(deferred['reason'], 'model schema rejected')
            second = capture.capture(self.config, self.state, now=now + timedelta(hours=2))
        self.assertEqual((second['errors'], second['processed'], second['notes']), (0, 1, 1))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertNotIn('previous_capture_rejection', attempts[1])

    def test_manual_retry_bypasses_backoff_and_retains_note_validation(self):
        self.rollout()
        now = datetime.now(timezone.utc)
        rejected = {**self.lesson, 'evidence': 'two\nlines'}
        attempts = []

        def run(command, **kwargs):
            data = json.loads(kwargs['input'][len(capture.PROMPT) + 1:])
            attempts.append(data['turn'])
            return completed({'notes': [rejected]})

        with patch.object(capture, 'run_extractor', side_effect=run):
            first = capture.capture(self.config, self.state, now=now)
            self.assertEqual((first['errors'], first['pending']), (1, 1))
            skipped = capture.capture(self.config, self.state, now=now)
            self.assertEqual((skipped['errors'], skipped['pending']), (1, 1))
            self.assertEqual(len(attempts), 1)
            retried = capture.capture(self.config, self.state, now=now, retry_now=True)
        self.assertEqual(len(attempts), 2)
        self.assertNotIn('previous_capture_rejection', attempts[1])
        self.assertEqual((retried['processed'], retried['notes'], retried['pending'], retried['errors']),
                         (0, 0, 1, 1))
        state = json.loads((self.state / 'capture-state.json').read_text())
        deferred = next(iter(state['deferred'].values()))
        self.assertEqual(deferred['attempts'], 2)
        self.assertEqual(deferred['reason'], 'model schema rejected')
        self.assertEqual(datetime.fromisoformat(deferred['retry_at']), now + timedelta(hours=2))
        with patch.object(capture, 'extract', return_value=[self.lesson]):
            recovered = capture.capture(self.config, self.state, now=now, retry_now=True)
        self.assertEqual((recovered['processed'], recovered['notes'], recovered['pending'], recovered['errors']),
                         (1, 1, 0, 0))
        self.assertEqual(json.loads((self.state / 'capture-state.json').read_text())['deferred'], {})

    def test_arbitrary_failure_text_is_never_added_to_retry_input(self):
        self.rollout()
        now = datetime.now(timezone.utc)
        with patch.object(capture, 'extract', side_effect=RuntimeError('confidential arbitrary exception')):
            capture.capture(self.config, self.state, now=now)
        def run(command, **kwargs):
            self.assertNotIn('confidential arbitrary exception', kwargs['input'])
            data = json.loads(kwargs['input'][len(capture.PROMPT) + 1:])
            self.assertNotIn('previous_capture_rejection', data['turn'])
            return completed({'notes': []})
        with patch.object(capture, 'run_extractor', side_effect=run):
            self.assertEqual(capture.capture(self.config, self.state, now=now + timedelta(hours=2))['processed'], 1)

    def test_cli_version_metadata_accepts_future_versions_and_hides_private_output(self):
        class Result:
            returncode = 0
            stdout = '2.1.0 (Claude Code)\n'
        def run(command, **kwargs):
            self.assertEqual(command, ['/bin/false', '--version'])
            self.assertEqual(kwargs['timeout'], 10)
            self.assertNotIn('CLAUDECODE', kwargs['env'])
            return Result()
        with patch.dict(capture.os.environ, {'CLAUDECODE': '1'}), patch.object(capture.subprocess, 'run', side_effect=run):
            for version in ('2.1.0', '2.2.0-beta.1', '3.0.0+build.2'):
                Result.stdout = version + ' (Claude Code)\n'
                self.assertEqual(capture.cli_version(self.config), version + ' (Claude Code)')
            for output in ('2.1.0 (Claude Code) private output', '', '2.1.0 (Claude Code)\nprivate output',
                           'codex-cli 0.160.0', 'x' * 257):
                Result.stdout = output
                self.assertEqual(capture.cli_version(self.config), 'unknown')
            Result.stdout = '2.1.0 (Claude Code)'
            Result.returncode = 1
            self.assertEqual(capture.cli_version(self.config), 'unknown')
        for error in (capture.subprocess.TimeoutExpired('secret command', 10), OSError('private path'),
                      UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid output')):
            with patch.object(capture.subprocess, 'run', side_effect=error):
                self.assertEqual(capture.cli_version(self.config), 'unknown')

    def test_future_or_unknown_version_does_not_block_capture(self):
        self.state.mkdir()
        config = self.state / 'config.json'
        config.write_text(json.dumps({**self.config, 'host_id': 'client-host', 'claude_path': sys.executable}))
        for version in ('9.0.0 (Claude Code)', 'unknown'):
            self.rollout(self.transcript(turn=version))
            with patch.object(sys, 'argv', ['capture.py', '--config', str(config)]), \
                    patch.object(capture, 'cli_version', return_value=version), \
                    patch.object(capture, 'extract', return_value=[]) as model, patch('builtins.print'):
                self.assertEqual(capture.main(), 0)
            model.assert_called_once()
            status = json.loads((self.state / 'capture-status.json').read_text())
            self.assertTrue(status['success'])
            self.assertEqual(status['cli_version'], version)
            self.assertEqual(status['counts']['processed'], 1)

    def test_status_names_a_standing_subscription_fallback(self):
        self.state.mkdir()
        key = self.state / 'key'
        key.write_text('sk-ant-secret\n')
        key.chmod(0o600)
        config = self.state / 'config.json'
        config.write_text(json.dumps({**self.config, 'host_id': 'client-host', 'claude_path': sys.executable,
                                      'api_key_file': str(key)}))
        counts = {'errors': 0, 'rate_limit': None}
        for outcome, code in ((counts, 0), (RuntimeError('Capture model attempted tool execution'), 1)):
            record = capture.record_api_fallback({'api_key_file': str(key)}, self.state, 'authentication',
                                                 datetime.now(timezone.utc))
            with self.subTest(code=code), patch.object(sys, 'argv', ['capture.py', '--config', str(config)]), \
                    patch.object(capture, 'cli_version', return_value='unknown'), \
                    patch.object(capture, 'capture', side_effect=[outcome]), patch('builtins.print') as log:
                self.assertEqual(capture.main(), code)
            status = json.loads((self.state / 'capture-status.json').read_text())
            self.assertEqual(status['api_fallback'], record)
            self.assertTrue(status['warning'].startswith(
                'Running on subscription fallback: the Claude API is unavailable (authentication)'))
            if not code:
                self.assertEqual(json.loads(log.call_args.args[0])['warning'], status['warning'])
        capture.api_fallback_path(self.state).unlink()
        with patch.object(sys, 'argv', ['capture.py', '--config', str(config)]), \
                patch.object(capture, 'cli_version', return_value='unknown'), \
                patch.object(capture, 'capture', return_value=counts), patch('builtins.print'):
            self.assertEqual(capture.main(), 0)
        status = json.loads((self.state / 'capture-status.json').read_text())
        self.assertNotIn('api_fallback', status)
        self.assertNotIn('warning', status)

    def test_host_gate_requires_the_claude_executable(self):
        self.state.mkdir()
        config = self.state / 'config.json'
        config.write_text(json.dumps({**self.config, 'host_id': 'client-host', 'codex_path': sys.executable,
                                      'claude_path': str(self.root / 'missing-claude')}))
        with patch.object(sys, 'argv', ['capture.py', '--config', str(config)]), \
                patch.object(capture, 'capture') as run, patch('builtins.print') as log:
            self.assertEqual(capture.main(), 1)
        run.assert_not_called()
        self.assertIn('invalid host configuration', json.loads(log.call_args.args[0])['error'])

    def test_failed_extraction_prints_safe_error_in_worker_log(self):
        self.state.mkdir()
        config = self.state / 'config.json'
        config.write_text(json.dumps({**self.config, 'host_id': 'client-host', 'claude_path': sys.executable}))
        class Result:
            returncode = 0
            stdout = '2.1.0 (Claude Code) private output'
        with patch.object(sys, 'argv', ['capture.py', '--config', str(config)]), \
                patch.object(capture.subprocess, 'run', return_value=Result()), \
                patch.object(capture, 'capture', side_effect=RuntimeError('Capture model attempted tool execution')), \
                patch('builtins.print') as log:
            self.assertEqual(capture.main(), 1)
        summary = json.loads(log.call_args.args[0])
        self.assertFalse(summary['success'])
        self.assertIn('model tool event rejected', summary['error'])
        self.assertNotIn('private output', summary['error'])
        status = json.loads((self.state / 'capture-status.json').read_text())
        self.assertEqual(status['cli_version'], 'unknown')
        self.assertEqual(summary['error'], status['error'])

    def test_exposed_or_used_tools_are_rejected_without_retry(self):
        self.state.mkdir()
        turn = {'verified_skills': ['frontend-ui'], 'messages': [], 'calls': []}
        for events, message in (((dict(INIT, tools=['StructuredOutput', 'Bash']),), 'exposed tools'),
                                ((dict(INIT, mcp_servers=[{'name': 'private'}]),), 'exposed tools'),
                                ((INIT, tool_use('Bash')), 'attempted tool execution')):
            result = capture.subprocess.CompletedProcess([], 0, stream(*events) + answer({'notes': []}), '')
            with self.subTest(message=message), patch.object(capture, 'run_extractor', return_value=result) as run:
                with self.assertRaisesRegex(RuntimeError, message) as caught:
                    capture.extract(self.config, self.state, turn, [])
                run.assert_called_once()
            self.assertIn(capture.failure_reason(caught.exception),
                          ('CLI isolation verification exposed tools', 'model tool event rejected'))

    def test_failed_cli_verification_keeps_evidence_without_sending_private_prompt(self):
        self.rollout(self.transcript(user='Build a page with confidential project evidence'))
        with patch.object(capture, 'verify_cli', side_effect=RuntimeError('Capture CLI required control is unavailable')), \
                patch.object(capture, 'run_extractor') as cli:
            result = capture.capture(self.config, self.state)
        cli.assert_not_called()
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(result['errors'], 1)
        state = json.loads((self.state / 'capture-state.json').read_text())
        self.assertIn('CLI safety control unavailable', [value['reason'] for value in state['deferred'].values()])
        self.assertFalse(self.feedback.exists() and list(self.feedback.rglob('*.md')))

    def test_transient_timeout_retries_once_without_changing_model_or_controls(self):
        self.state.mkdir()
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            self.assertEqual(command[command.index('--model') + 1], capture.claude_cli.RESTRICTED_MODEL)
            self.assertEqual(command[command.index('--effort') + 1], 'high')
            self.assertLessEqual(kwargs['timeout'], 240)
            if len(calls) == 1:
                raise capture.subprocess.TimeoutExpired('private command', 240,
                    output=stream(INIT).encode(), stderr=b'private source text')
            return completed({'notes': []})
        with patch.object(capture, 'run_extractor', side_effect=run):
            self.assertEqual(capture.model_response(self.config, self.state, 'private prompt', '{}', capture.SCHEMA), {'notes': []})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])

    def test_repeated_timeout_raises_extractor_timeout(self):
        self.state.mkdir()
        error = capture.subprocess.TimeoutExpired('private command', 240, output=stream(INIT) + '{"type":"assis')
        with patch.object(capture, 'run_extractor', side_effect=error) as run, \
                patch.object(capture.sys, 'stderr', io.StringIO()):
            with self.assertRaises(capture.ExtractorFailure) as caught:
                capture.model_response(self.config, self.state, 'private prompt', '{}', capture.SCHEMA)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(caught.exception.reason, 'extractor timeout')
        self.assertEqual(capture.failure_reason(caught.exception), 'extractor timeout')
        self.assertEqual((caught.exception.diagnostics['phase'], caught.exception.diagnostics['attempts']), ('inference', 2))

    def test_recovered_timeout_logs_only_safe_metadata_to_stderr(self):
        self.state.mkdir()
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                raise capture.subprocess.TimeoutExpired('private command', 240,
                    output=stream(INIT).encode(), stderr=b'private source secret')
            return completed({'notes': []})
        with patch.object(capture, 'run_extractor', side_effect=run), \
                patch.object(capture.sys, 'stderr', io.StringIO()) as stderr, \
                patch.object(capture.sys, 'stdout', io.StringIO()) as stdout:
            result = capture.model_response(self.config, self.state, 'private prompt', '{}', capture.SCHEMA)
        self.assertEqual(result, {'notes': []})
        self.assertEqual(stdout.getvalue(), '')
        metadata = json.loads(stderr.getvalue())
        self.assertEqual(set(metadata), {'event', 'elapsed_seconds', 'phase', 'failure_class', 'attempts',
                                         'exit_code', 'result_subtype', 'api_error_status', 'rate_limit_type',
                                         'rate_limit_resets_at'})
        self.assertEqual((metadata['exit_code'], metadata['result_subtype'], metadata['api_error_status']), (None, None, None))
        self.assertEqual((metadata['event'], metadata['failure_class'], metadata['phase'], metadata['attempts']),
                         ('extractor_attempt_failed', 'timeout', 'inference', 1))
        self.assertNotIn('private', stderr.getvalue())
        self.assertNotIn('secret', stderr.getvalue())

    def rejected(self, resets=None):
        """A run the account's usage limit rejected, optionally reporting the window's reset time."""
        events = [INIT]
        if resets is not None:
            events.append({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected', 'rateLimitType': 'five_hour',
                                                                           'resetsAt': resets}})
        events.append({'type': 'result', 'subtype': 'success', 'is_error': True, 'api_error_status': 429,
                       'result': "You've hit your 5-hour limit"})
        return capture.subprocess.CompletedProcess([], 1, stream(*events), '')

    def test_usage_limit_defers_model_calls_to_its_reset_without_failing_capture(self):
        self.rollout(self.transcript() + self.transcript('turn2'))
        reset = int(time.time()) + 7200
        with patch.object(capture, 'run_extractor', return_value=self.rejected(reset)) as run, \
                patch.object(capture.sys, 'stderr', io.StringIO()):
            first = capture.capture(self.config, self.state)
            second = capture.capture(self.config, self.state)
        # One rejected call; the second turn and the next cycle wait for the reset instead of calling again.
        self.assertEqual(run.call_count, 1)
        for summary in (first, second):
            self.assertEqual((summary['processed'], summary['pending'], summary['errors'], summary['rate_limited']),
                             (0, 2, 0, 2))
        limit = json.loads((self.state / 'rate-limit.json').read_text())
        self.assertEqual(limit['window'], 'five_hour')
        self.assertEqual(capture.timestamp(limit['retry_at']),
                         datetime.fromtimestamp(reset, timezone.utc) + timedelta(minutes=1))
        self.assertEqual(first['rate_limit'], {**limit, 'deferred': 2, 'overdue': False})
        # Both turns are retained as deferred work, with no failure backoff of their own.
        deferred = json.loads((self.state / 'capture-state.json').read_text())['deferred']
        self.assertEqual(sorted(entry['reason'] for entry in deferred.values()), ['extractor rate limit failure'] * 2)
        self.assertEqual({(entry['attempts'], entry['retry_at'], entry['rate_limited']) for entry in deferred.values()},
                         {(0, limit['retry_at'], True)})
        # Once the reset has passed, the calls resume and a success clears the record.
        (self.state / 'rate-limit.json').write_text(json.dumps({**limit, 'retry_at': datetime.now(timezone.utc).isoformat()}))
        with patch.object(capture, 'run_extractor', return_value=completed({'notes': []})) as run:
            third = capture.capture(self.config, self.state)
        self.assertEqual((run.call_count, third['processed'], third['pending'], third['errors'], third['rate_limited']),
                         (2, 2, 0, 0, 0))
        self.assertIsNone(third['rate_limit'])
        self.assertFalse((self.state / 'rate-limit.json').exists())
        self.assertEqual(json.loads((self.state / 'capture-state.json').read_text())['deferred'], {})

    def test_usage_limit_lets_saved_transactions_finish_and_retry_now_makes_one_call(self):
        self.rollout(self.transcript() + self.transcript('turn2'))
        with patch.object(capture, 'run_extractor', return_value=self.rejected(int(time.time()) + 7200)), \
                patch.object(capture.sys, 'stderr', io.StringIO()):
            capture.capture(self.config, self.state)
        # A transaction extracted before the limit needs no model call and still completes.
        state = json.loads((self.state / 'capture-state.json').read_text())
        [first, second] = sorted(state['deferred'])
        state['pending'][first] = {'lessons': [self.lesson], 'deployed': 'unavailable', 'machine': 'host',
                                   'recorded_at': datetime.now(timezone.utc).isoformat()}
        (self.state / 'capture-state.json').write_text(json.dumps(state))
        with patch.object(capture, 'run_extractor') as run:
            summary = capture.capture(self.config, self.state)
        run.assert_not_called()
        self.assertEqual((summary['processed'], summary['notes'], summary['rate_limited'], summary['errors']), (1, 1, 1, 0))
        # --retry-now makes the call despite the record; a success clears it.
        with patch.object(capture, 'run_extractor', return_value=completed({'notes': []})) as run:
            summary = capture.capture(self.config, self.state, retry_now=True)
        self.assertEqual((run.call_count, summary['processed'], summary['rate_limited']), (1, 1, 0))
        self.assertFalse((self.state / 'rate-limit.json').exists())

    def test_rate_limit_text_without_a_structured_signal_is_an_ordinary_failure(self):
        self.rollout()
        # A stack trace line number or a module name can match the text classifier; it must not defer silently.
        crash = capture.subprocess.CompletedProcess([], 1, stream(INIT), 'TypeError at cli.js:429:12')
        with patch.object(capture, 'run_extractor', return_value=crash), patch.object(capture.sys, 'stderr', io.StringIO()):
            summary = capture.capture(self.config, self.state)
        self.assertEqual((summary['errors'], summary['rate_limited']), (1, 0))
        self.assertFalse((self.state / 'rate-limit.json').exists())
        [entry] = json.loads((self.state / 'capture-state.json').read_text())['deferred'].values()
        self.assertEqual((entry['reason'], entry['attempts'], 'rate_limited' in entry), ('extractor rate limit failure', 1, False))

    def test_a_rejection_after_the_cycle_deadline_is_still_recorded(self):
        self.state.mkdir()
        clock = [0.0]
        def rejected(*args, **kwargs):
            clock[0] = 10 ** 6
            return self.rejected(int(time.time()) + 7200)
        with patch.object(capture.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(capture, 'run_extractor', side_effect=rejected), patch.object(capture.sys, 'stderr', io.StringIO()):
            with self.assertRaises(capture.CaptureBudgetExpired):
                capture.model_response(self.config, self.state, 'prompt', '{}', capture.SCHEMA, deadline=1000)
        self.assertEqual(capture.read_rate_limit(self.state)['window'], 'five_hour')

    def test_usage_limit_status_warns_then_fails_once_the_limit_outlasts_the_grace_period(self):
        self.rollout()
        before = datetime.now(timezone.utc)
        config = self.state / 'config.json'
        self.state.mkdir()
        config.write_text(json.dumps({**self.config, 'host_id': 'client-host', 'claude_path': sys.executable}))
        def main():
            with patch.object(sys, 'argv', ['capture.py', '--config', str(config)]), \
                    patch.object(capture, 'cli_version', return_value='unknown'), patch('builtins.print'), \
                    patch.object(capture.sys, 'stderr', io.StringIO()):
                return capture.main(), json.loads((self.state / 'capture-status.json').read_text())
        # Without a reported reset, the next hourly cycle retries.
        with patch.object(capture, 'run_extractor', return_value=self.rejected()):
            code, status = main()
        limit = json.loads((self.state / 'rate-limit.json').read_text())
        self.assertIsNone(limit['window'])
        self.assertLessEqual(before + capture.RATE_LIMIT_RETRY, capture.timestamp(limit['retry_at']))
        self.assertLessEqual(capture.timestamp(limit['retry_at']), datetime.now(timezone.utc) + capture.RATE_LIMIT_RETRY)
        self.assertEqual((code, status['success'], status['counts']['errors']), (0, True, 0))
        self.assertEqual(status['warning'], 'Claude usage limit reached; 1 turns wait for its reset at ' + limit['retry_at'] + '.')
        self.assertNotIn('error', status)
        # A limit that has blocked work for more than a day fails capture, so it is never silent.
        (self.state / 'rate-limit.json').write_text(json.dumps({**limit, 'since': (before - timedelta(hours=25)).isoformat()}))
        with patch.object(capture, 'run_extractor') as run:
            code, status = main()
        run.assert_not_called()
        self.assertEqual((code, status['success'], status['counts']['errors'], status['counts']['rate_limited']), (1, False, 1, 1))
        self.assertTrue(status['counts']['rate_limit']['overdue'])
        self.assertEqual(status['error'], 'Capture has deferred evidence; source remains pending. '
                                          'The Claude usage limit has blocked 1 turns for more than 24 hours.')
        self.assertNotIn('warning', status)

    def test_a_rejection_soon_after_the_expected_reset_continues_the_same_limit(self):
        self.state.mkdir()
        now = datetime.now(timezone.utc)
        reset = now + timedelta(hours=3)
        first = capture.record_rate_limit(self.state, {'rate_limit_resets_at': int(reset.timestamp()),
                                                       'rate_limit_type': 'five_hour'}, now)
        self.assertEqual((first['since'], first['window']), (now.isoformat(), 'five_hour'))
        # A rejection reporting no reset never shortens a known wait, and keeps the window it continues.
        later = capture.record_rate_limit(self.state, {'rate_limit_resets_at': None}, now + timedelta(hours=1))
        self.assertEqual(later, first)
        after = capture.record_rate_limit(self.state, {'rate_limit_resets_at': None}, now + timedelta(hours=4))
        self.assertEqual((after['since'], after['retry_at']),
                         (now.isoformat(), (now + timedelta(hours=4) + capture.RATE_LIMIT_RETRY).isoformat()))
        # A rejection hours after the recorded reset is a new limit with its own grace period.
        fresh = capture.record_rate_limit(self.state, {'rate_limit_resets_at': None}, now + timedelta(hours=20))
        self.assertEqual((fresh['since'], fresh['window']), ((now + timedelta(hours=20)).isoformat(), None))
        # A reset outside the plausible range falls back to the next cycle.
        far = capture.record_rate_limit(self.state, {'rate_limit_resets_at': int((now + timedelta(days=30)).timestamp()),
                                                     'rate_limit_type': 'seven_day'}, now + timedelta(days=5))
        self.assertEqual((capture.timestamp(far['retry_at']), far['window']),
                         (now + timedelta(days=5) + capture.RATE_LIMIT_RETRY, 'seven_day'))
        for invalid in ('{"since": "x"}', json.dumps({'since': 1, 'retry_at': now.isoformat(), 'window': None}),
                        json.dumps({'since': 'soon', 'retry_at': now.isoformat(), 'window': None})):
            (self.state / 'rate-limit.json').write_text(invalid)
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'Invalid rate limit state'):
                capture.read_rate_limit(self.state)
        self.assertEqual(capture.failure_reason(ValueError('Invalid rate limit state')), 'invalid rate limit state')

    def test_transient_cli_failure_retries_but_authentication_and_limits_do_not(self):
        self.state.mkdir()
        for stderr, expected, attempts in (
                ('API Error: Connection error. connection reset; private source', 'connection', 2),
                ('Invalid API key · Please run /login; private source', 'authentication', 1),
                ('Claude usage limit reached; private source', 'rate limit', 1),
                ('private unexpected failure', 'CLI or output', 1)):
            with self.subTest(category=expected), patch.object(capture, 'run_extractor', return_value=
                    capture.subprocess.CompletedProcess([], 1, stream(INIT), stderr)) as run, \
                    patch.object(capture.sys, 'stderr', io.StringIO()):
                with self.assertRaises(capture.ExtractorFailure) as caught:
                    capture.model_response(self.config, self.state, 'private prompt', '{}', capture.SCHEMA)
            self.assertEqual(run.call_count, attempts)
            self.assertEqual(caught.exception.diagnostics['failure_class'], expected)
            self.assertEqual(caught.exception.diagnostics['phase'], 'inference')
            self.assertNotIn('private', str(caught.exception))
            self.assertNotIn('private', json.dumps(caught.exception.diagnostics))

    def test_error_results_classify_without_logging_private_error_text(self):
        self.state.mkdir()
        for result, expected, attempts in (
                ({'api_error_status': None, 'result': 'API Error: Connection error. private source text'}, 'connection', 2),
                ({'api_error_status': 529, 'result': 'Overloaded; private source text'}, 'connection', 2),
                ({'api_error_status': 401, 'result': 'authentication_error; private source text'}, 'authentication', 1),
                ({'api_error_status': 429, 'result': 'private source text'}, 'rate limit', 1)):
            event = {'type': 'result', 'subtype': 'success', 'is_error': True, **result}
            with self.subTest(category=expected), patch.object(capture, 'run_extractor', return_value=
                    capture.subprocess.CompletedProcess([], 1, stream(INIT, event), '')) as run, \
                    patch.object(capture.sys, 'stderr', io.StringIO()) as log:
                with self.assertRaises(capture.ExtractorFailure) as caught:
                    capture.model_response(self.config, self.state, 'private prompt', '{}', capture.SCHEMA)
            self.assertEqual(run.call_count, attempts)
            self.assertEqual(caught.exception.diagnostics['failure_class'], expected)
            self.assertEqual(caught.exception.diagnostics['phase'], 'completed')
            self.assertEqual((caught.exception.diagnostics['exit_code'], caught.exception.diagnostics['result_subtype'],
                              caught.exception.diagnostics['api_error_status']), (1, 'success', result['api_error_status']))
            self.assertNotIn('private source text', str(caught.exception) + log.getvalue())
            self.assertNotIn('private source text', json.dumps(caught.exception.diagnostics))

    def test_success_requires_completed_zero_exit_dict_within_output_bound(self):
        self.state.mkdir()
        large = {'notes': [{'skill': 'frontend-ui', 'title': 'x' * capture.MAX_OUTPUT}]}
        for result in (capture.subprocess.CompletedProcess([], 0, answer(large), ''),
                       capture.subprocess.CompletedProcess([], 1, answer({'notes': []}), ''),
                       capture.subprocess.CompletedProcess([], 0, stream(INIT), ''),
                       capture.subprocess.CompletedProcess([], 0, stream(INIT, {'type': 'result', 'subtype': 'success',
                           'is_error': False, 'result': '{"notes":[]}'}), '')):
            with self.subTest(result=result.stdout[-80:]), patch.object(capture, 'run_extractor', return_value=result) as run, \
                    patch.object(capture.sys, 'stderr', io.StringIO()):
                with self.assertRaises(capture.ExtractorFailure) as caught:
                    capture.model_response(self.config, self.state, 'private prompt', '{}', capture.SCHEMA)
            run.assert_called_once()
            self.assertEqual(caught.exception.reason, 'extractor CLI or output failure')
        self.assertNotIn('x' * 100, json.dumps(caught.exception.diagnostics))

    def test_timeout_with_tool_event_is_rejected_without_retry(self):
        self.state.mkdir()
        error = capture.subprocess.TimeoutExpired('private command', 240, output=stream(INIT, tool_use('Bash')).encode())
        with patch.object(capture, 'run_extractor', side_effect=error) as run:
            with self.assertRaisesRegex(RuntimeError, 'attempted tool'):
                capture.model_response(self.config, self.state, 'private prompt', '{}', capture.SCHEMA)
        run.assert_called_once()

    def test_deadline_caps_request_and_prevents_retry_or_new_cli(self):
        self.state.mkdir()
        clock = [0]
        def run(command, **kwargs):
            self.assertEqual(kwargs['timeout'], 3)
            clock[0] = 3
            raise capture.subprocess.TimeoutExpired('private command', 3, output=stream(INIT).encode())
        with patch.object(capture.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(capture, 'run_extractor', side_effect=run) as model:
            with self.assertRaises(capture.CaptureBudgetExpired):
                capture.model_response(self.config, self.state, 'private prompt', '{}', capture.SCHEMA, deadline=3)
            model.assert_called_once()
            with patch.object(capture, 'verify_cli') as verify:
                with self.assertRaises(capture.CaptureBudgetExpired):
                    capture.model_response(self.config, self.state, 'private prompt', '{}', capture.SCHEMA, deadline=3)
                verify.assert_not_called()
        with patch.object(capture.subprocess, 'run') as cli, patch.object(capture.time, 'monotonic', return_value=3):
            with self.assertRaises(capture.CaptureBudgetExpired):
                capture.cli_text('/private/cli', '--help', deadline=3)
            cli.assert_not_called()

    def test_helper_timeouts_distinguish_cycle_deadline_from_cli_failure(self):
        clock = [0]
        def timeout(command, **kwargs):
            clock[0] += kwargs['timeout']
            raise capture.subprocess.TimeoutExpired('private command', kwargs['timeout'])
        with patch.object(capture.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(capture.subprocess, 'run', side_effect=timeout):
            with self.assertRaises(capture.CaptureBudgetExpired):
                capture.cli_text('/private/cli', '--help', deadline=1)
            clock[0] = 0
            with self.assertRaises(capture.subprocess.TimeoutExpired):
                capture.cli_text('/private/cli', '--help', deadline=100)

    def test_capture_budget_leaves_evidence_without_failure_backoff(self):
        self.rollout()
        with patch.object(capture, 'extract', side_effect=capture.CaptureBudgetExpired):
            counts = capture.capture(self.config, self.state)
        self.assertEqual((counts['processed'], counts['pending'], counts['errors']), (0, 1, 0))
        path = self.state / 'capture-state.json'
        self.assertTrue(not path.exists() or not json.loads(path.read_text()).get('deferred'))

    def test_crashed_cli_failure_keeps_static_diagnostics(self):
        self.state.mkdir()
        crashed = capture.subprocess.CompletedProcess([], -9, stream(INIT) + '{"type": "assis', 'private stderr')
        with patch.object(capture, 'run_extractor', return_value=crashed) as run, \
                patch.object(capture.sys, 'stderr', io.StringIO()) as log:
            with self.assertRaises(capture.ExtractorFailure) as caught:
                capture.model_response(self.config, self.state, 'private prompt', '{}', capture.SCHEMA)
        self.assertEqual(run.call_count, 1)
        diagnostics = caught.exception.diagnostics
        self.assertEqual((diagnostics['phase'], diagnostics['failure_class'], diagnostics['exit_code'],
                          diagnostics['result_subtype'], diagnostics['api_error_status']),
                         ('inference', 'CLI or output', -9, None, None))
        self.assertEqual(json.loads(log.getvalue())['exit_code'], -9)
        self.assertNotIn('private', log.getvalue())

    def test_timeout_keeps_a_finished_answer_and_the_cli_exit_status(self):
        self.state.mkdir()
        finished = capture.subprocess.TimeoutExpired('cli', 240, output=answer({'notes': []}).encode(), stderr=b'')
        with patch.object(capture, 'run_extractor', side_effect=finished) as run:
            self.assertEqual(capture.model_response(self.config, self.state, 'p', '{}', capture.SCHEMA), {'notes': []})
        self.assertEqual(run.call_count, 1)
        hung = capture.subprocess.TimeoutExpired('cli', 240, output=stream(INIT).encode(), stderr=b'')
        hung.returncode = 3
        with patch.object(capture, 'run_extractor', side_effect=[hung, hung]), \
                patch.object(capture.sys, 'stderr', io.StringIO()):
            with self.assertRaises(capture.ExtractorFailure) as caught:
                capture.model_response(self.config, self.state, 'p', '{}', capture.SCHEMA)
        self.assertEqual((caught.exception.diagnostics['failure_class'], caught.exception.diagnostics['exit_code'],
                          caught.exception.diagnostics['attempts']), ('timeout', 3, 2))

    def test_failed_capture_retains_only_safe_extractor_diagnostics(self):
        self.rollout()
        failed = capture.ExtractorFailure('extractor connection failure',
            {'elapsed_seconds': 1.25, 'phase': 'startup', 'failure_class': 'connection', 'attempts': 2,
             'exit_code': 1, 'result_subtype': None, 'api_error_status': None})
        with patch.object(capture, 'extract', side_effect=failed):
            counts = capture.capture(self.config, self.state)
        state = json.loads((self.state / 'capture-state.json').read_text())
        pending = next(iter(state['deferred'].values()))
        self.assertEqual(pending['diagnostics'], failed.diagnostics)
        self.assertEqual(counts['errors'], 1)

    def test_timeout_keeps_the_status_of_a_cli_that_left_a_child_running(self):
        program = 'import os,sys,time; pid=os.fork(); time.sleep(20) if pid == 0 else sys.exit(3)'
        with self.assertRaises(capture.subprocess.TimeoutExpired) as caught:
            capture.run_extractor([sys.executable, '-c', program], env=dict(os.environ), input='', timeout=0.5)
        self.assertEqual(caught.exception.returncode, 3)

    def test_timeout_kills_subprocess_group_and_closes_descendant_pipes(self):
        pidfile = self.root / 'child.pid'
        program = ('import os,time,pathlib; child=os.fork(); '
                   'pathlib.Path(' + repr(str(pidfile)) + ').write_text(str(child)) if child else None; '
                   'print("private output",flush=True); time.sleep(20)')
        began = time.monotonic()
        with self.assertRaises(capture.subprocess.TimeoutExpired) as caught:
            capture.run_extractor([sys.executable, '-c', program], env=dict(os.environ), input='', timeout=0.5)
        self.assertLess(time.monotonic() - began, 3)
        self.assertNotIn('private output', str(caught.exception))
        self.assertTrue(pidfile.is_file())
        state = capture.subprocess.run(['ps', '-o', 'stat=', '-p', pidfile.read_text()], capture_output=True, text=True)
        self.assertTrue(not state.stdout.strip() or state.stdout.lstrip().startswith('Z'))


FAKE_CLI = """
import json, os, sys
from pathlib import Path
with Path(os.environ['FAKE_CLAUDE_RECORD']).open('a') as record:
    stdin = '' if sys.argv[1:] == ['--help'] else sys.stdin.read()
    record.write(json.dumps({'argv': sys.argv[1:], 'stdin': stdin, 'cwd': os.getcwd(), 'env': dict(os.environ)}) + '\\n')
if sys.argv[1:] == ['--help']:
    print(os.environ['FAKE_CLAUDE_HELP'])
else:
    sys.stdout.write(os.environ['FAKE_CLAUDE_STREAM'])
"""


class ClaudeCliCaptureTests(unittest.TestCase):
    """Drive model_response through a fake Claude executable."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.cli = self.root / 'claude'
        self.cli.write_text('#!' + sys.executable + '\n' + FAKE_CLI)
        self.cli.chmod(0o755)
        self.record = self.root / 'record.jsonl'
        self.config = {'claude_path': str(self.cli)}
        capture.VERIFIED_CLIS.clear()
        self.addCleanup(capture.VERIFIED_CLIS.clear)
        environ = patch.dict(os.environ, {'FAKE_CLAUDE_RECORD': str(self.record), 'FAKE_CLAUDE_HELP': HELP,
                                          'FAKE_CLAUDE_STREAM': answer({'notes': []}), 'CLAUDECODE': '1',
                                          'CLAUDE_CODE_SESSION_ID': 'parent-session'})
        environ.start()
        self.addCleanup(environ.stop)

    def calls(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()] if self.record.exists() else []

    def test_isolated_command_receives_schema_and_prompt_on_stdin(self):
        for _ in range(2):
            result = capture.model_response(self.config, self.state, 'private prompt', '{"data": 1}', capture.SCHEMA)
            self.assertEqual(result, {'notes': []})
        help_call, *model_calls = self.calls()
        self.assertEqual(help_call['argv'], ['--help'])
        self.assertEqual(help_call['env']['LC_ALL'], 'C')
        self.assertEqual(len(model_calls), 2, 'help verification is cached per CLI path')
        call = model_calls[0]
        argv = call['argv']
        self.assertEqual(argv, capture.claude_cli.restricted_command(self.cli, capture.SCHEMA)[1:])
        self.assertEqual(argv[argv.index('--setting-sources') + 1], '')
        self.assertEqual(argv[argv.index('--tools') + 1], '')
        for flag in ('--print', '--strict-mcp-config', '--disable-slash-commands', '--no-session-persistence'):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index('--output-format') + 1], 'stream-json')
        self.assertEqual(json.loads(argv[argv.index('--json-schema') + 1]), capture.SCHEMA)
        self.assertEqual(call['stdin'], 'private prompt\n{"data": 1}')
        self.assertNotIn('private prompt', json.dumps(argv))
        self.assertTrue(Path(call['cwd']).name.startswith('.capture-model-'))
        self.assertEqual(Path(call['cwd']).parent.resolve(), self.state.resolve())
        self.assertFalse(Path(call['cwd']).exists())
        self.assertNotIn('CLAUDECODE', call['env'])
        self.assertNotIn('CLAUDE_CODE_SESSION_ID', call['env'])
        self.assertEqual(call['env']['CLAUDE_CODE_DISABLE_AUTO_MEMORY'], '1')
        self.assertEqual(sorted(path.name for path in self.state.iterdir()), [])
        self.assertFalse((self.state / 'capture-cli.json').exists())

    def test_missing_required_flag_fails_closed_before_any_prompt(self):
        missing = HELP.replace('  --strict-mcp-config <value>\n', '  --strict-mcp-config-extra\n')
        with patch.dict(os.environ, {'FAKE_CLAUDE_HELP': missing}):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'required control') as caught:
                    capture.model_response(self.config, self.state, 'private prompt', '{}', capture.SCHEMA)
        self.assertEqual([call['argv'] for call in self.calls()], [['--help'], ['--help']])
        self.assertEqual(capture.failure_reason(caught.exception), 'CLI safety control unavailable')

    def test_exposed_tools_from_cli_fail_without_retry(self):
        exposed = stream(dict(INIT, tools=['StructuredOutput', 'Read'])) + answer({'notes': []})
        with patch.dict(os.environ, {'FAKE_CLAUDE_STREAM': exposed}):
            with self.assertRaisesRegex(RuntimeError, 'exposed tools'):
                capture.model_response(self.config, self.state, 'private prompt', '{}', capture.SCHEMA)
        self.assertEqual([call['argv'][0] for call in self.calls()], ['--help', '--print'])


if __name__ == '__main__':
    unittest.main()
