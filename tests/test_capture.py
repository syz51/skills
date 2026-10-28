from datetime import datetime, timedelta, timezone
import importlib.util
import http.client
import hashlib
import json
import io
import os
from pathlib import Path
import tempfile
import sys
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import read_guidance
spec = importlib.util.spec_from_file_location('capture', Path(__file__).parents[1] / 'scripts/capture.py')
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)

PROFILE = {'command': 'exec', 'standalone': '--no-daemon', 'strict': '--strict-config',
    'ignore_config': '--ignore-user-config', 'ignore_rules': '--ignore-rules', 'ephemeral': '--ephemeral',
    'cd': '-C', 'model': '-m', 'config': '-c', 'sandbox': '--sandbox', 'schema': '--output-schema',
    'output': '-o', 'json': '--json', 'skip_git': '--skip-git-repo-check',
    'overrides': list(capture.EXTRACTOR_CONFIG) + ['features.' + name + '=false' for name in sorted(capture.TOOL_FEATURES)]}
ROOT_HELP = '''Commands:
  exec      Run Codex non-interactively [aliases: e]
  features  Inspect feature flags
Options:
      --no-daemon
          Run without the shared background server
'''
EXEC_HELP = '''Options:
      --strict-config
          Error out when config.toml contains fields that are not recognized
      --ignore-user-config
          Do not load user config.toml
      --ignore-rules
          Do not load user or project execpolicy .rules files
      --ephemeral
          Run without persisting session files
  -C, --cd <DIR>
          Tell the agent to use the specified directory as its working root
  -m, --model <MODEL>
          Model the agent should use
  -c, --config <key=value>
          Override a configuration value
  -s, --sandbox <MODE>
          Select the sandbox policy for model-generated commands
          [possible values: read-only, workspace-write, danger-full-access]
      --output-schema <FILE>
          Path to a JSON Schema describing the final response
  -o, --output-last-message <FILE>
          Specifies file where the last message should be written
      --json
          Print events to stdout as JSONL
      --skip-git-repo-check
          Allow running Codex outside a Git repository
'''


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
                       'install_root': str(self.root / 'install'), 'codex_path': '/bin/false',
                       'claude_projects_root': str(self.root / 'claude-projects')}
        self.lesson = {'skill': 'frontend-ui', **{field: 'Verified useful check' for field in capture.FIELDS}}
        profile = patch.object(capture, 'compatible_profile', return_value=PROFILE)
        profile.start()
        self.addCleanup(profile.stop)

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

    def test_every_maintained_skill_is_recognized_by_name_or_heading(self):
        for skill, heading in (('find-skills', '# Find Skills'), ('content-research-writer', '# Content Research Writer'), ('tauri', '# Tauri')):
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

    def test_schema_and_redaction(self):
        turn = {'verified_skills': ['frontend-ui']}
        self.assertEqual(capture.validate_notes({'notes': [self.lesson]}, turn), [self.lesson])
        for bad in [{'notes': [{**self.lesson, 'skill': 'other'}]}, {'notes': [{**self.lesson, 'evidence': 'token=secret_value'}]}, {'notes': [], 'extra': True}]:
            with self.assertRaises(ValueError):
                capture.validate_notes(bad, turn)
        self.assertNotIn('abc@example.com', capture.redact('abc@example.com token=private https://private.example/secret'))

    def test_private_short_and_cjk_source_quotations(self):
        for source in ('Secret acquisition target has confidential projected revenue', '未公開研究資料には秘密顧客情報が含まれています'):
            turn = {'verified_skills': ['frontend-ui'], 'messages': [{'text': source}]}
            with self.assertRaises(ValueError):
                capture.validate_notes({'notes': [{**self.lesson, 'evidence': source}]}, turn)

    def test_short_private_sentences_names_and_generic_technical_feedback(self):
        for source in ('Aster Labs acquired Cygnus secretly.', 'Project Zephyr launches next Friday.',
                       '张明患有重病。', '保密项目曙光即将发布。'):
            turn = {'verified_skills': ['frontend-ui'], 'messages': [{'text': source}]}
            with self.assertRaises(ValueError):
                capture.validate_notes({'notes': [{**self.lesson, 'evidence': source}]}, turn)
        for source, name in [('Build the Aster Labs launch page', 'Aster Labs'),
                             ('Edit the private draft for customer Rowan Hale', 'Rowan Hale')]:
            turn = {'verified_skills': ['frontend-ui'], 'messages': [{'text': source}]}
            with self.assertRaises(ValueError):
                capture.validate_notes({'notes': [{**self.lesson, 'task': name}]}, turn)
        turn = {'verified_skills': ['frontend-ui'], 'messages': [{'text': 'Build a Vue and Nuxt page with JSON Schema validation'}]}
        note = {**self.lesson, 'task': 'Checked Vue and Nuxt integration', 'evidence': 'Verified JSON Schema validation'}
        self.assertEqual(capture.validate_notes({'notes': [note]}, turn), [note])

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
        def model(config, root, turn, existing, *, deadline=None):
            if turn['turn'] == 'turn1':
                raise RuntimeError('unavailable')
            return []
        with patch.object(capture, 'extract', side_effect=model):
            summary = capture.capture(self.config, self.state)
        self.assertEqual(summary['processed'], 1)
        self.assertEqual(summary['pending'], 1)
        self.assertEqual(summary['errors'], 1)

    def test_dedupe_context_uses_recent_notes_for_relevant_skill(self):
        self.state.mkdir()
        existing = [{'skill': 'frontend-ui', 'text': 'Relevant newest lesson', 'order': 99}] + [
            {'skill': 'content-research-writer', 'text': 'Unrelated writer lesson', 'order': index} for index in range(20)]
        turn = {'verified_skills': ['frontend-ui'], 'messages': [], 'calls': []}
        def run(command, **kwargs):
            self.assertIn('Relevant newest lesson', kwargs['input'])
            self.assertNotIn('Unrelated writer lesson', kwargs['input'])
            Path(command[command.index('-o') + 1]).write_text(json.dumps({'notes': []}))
            class Result:
                returncode = 0
                stdout = ''
            return Result()
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
            Path(command[command.index('-o') + 1]).write_text(json.dumps({'notes': []}))
            class Result:
                returncode = 0
                stdout = ''
            return Result()
        with patch.object(capture, 'MAX_INPUT', 20000 + 2000 * len(capture.SKILLS)), patch.object(capture, 'run_extractor', side_effect=run):
            self.assertEqual(capture.extract(self.config, self.state, turn, existing), [])

    def test_nonfatal_error_metadata_does_not_imply_tool_execution(self):
        self.state.mkdir()
        turn = {'verified_skills': ['frontend-ui'], 'messages': [], 'calls': []}
        error = json.dumps({'type': 'item.completed', 'item': {'type': 'error', 'message': capture.STARTUP_DIAGNOSTIC}})
        started = json.dumps({'type': 'turn.started'})
        class Result:
            returncode = 0
            stdout = error + '\n' + started
        def run(command, **kwargs):
            Path(command[command.index('-o') + 1]).write_text(json.dumps({'notes': []}))
            return Result()
        with patch.object(capture, 'run_extractor', side_effect=run):
            self.assertEqual(capture.extract(self.config, self.state, turn, []), [])
            Result.stdout = started + '\n' + error
            self.assertEqual(capture.extract(self.config, self.state, turn, []), [])
            Result.stdout = error.replace('Code Mode is unavailable', 'Other failure')
            self.assertEqual(capture.extract(self.config, self.state, turn, []), [])
            Result.stdout = json.dumps({'type': 'tool_call', 'item': {'type': 'error', 'message': 'failure'}})
            with self.assertRaises(RuntimeError):
                capture.extract(self.config, self.state, turn, [])

    def test_static_failure_diagnostics_never_echo_source_text(self):
        cases = [(ValueError('Capture output includes a source identifying name'), 'privacy source name rejected'),
                 (ValueError('Capture output quotes a private source sentence'), 'privacy source quotation rejected'),
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

    def test_privacy_retry_informs_model_with_static_reason_only(self):
        self.rollout()
        attempts = []
        rejected = {**self.lesson, 'evidence': 'Built page and checked layout.'}
        def run(command, **kwargs):
            data = json.loads(kwargs['input'][len(capture.PROMPT) + 1:])
            attempts.append(data['turn'])
            note = rejected if len(attempts) == 1 else self.lesson
            Path(command[command.index('-o') + 1]).write_text(json.dumps({'notes': [note]}))
            class Result:
                returncode = 0
                stdout = ''
            return Result()
        now = datetime.now(timezone.utc)
        with patch.object(capture, 'run_extractor', side_effect=run):
            first = capture.capture(self.config, self.state, now=now)
            self.assertEqual(first['errors'], 1)
            self.assertEqual(first['processed'], 0)
            self.assertEqual(len(attempts), 1)
            second = capture.capture(self.config, self.state, now=now + timedelta(hours=2))
        self.assertEqual(second['errors'], 0)
        self.assertEqual(second['processed'], 1)
        self.assertEqual(second['notes'], 1)
        self.assertEqual(len(attempts), 2)
        self.assertNotIn('previous_capture_rejection', attempts[0])
        self.assertEqual(attempts[1]['previous_capture_rejection'], 'privacy source quotation rejected')
        self.assertNotIn('previous_rejected_output', attempts[1])

    def test_arbitrary_failure_text_is_never_added_to_retry_input(self):
        self.rollout()
        now = datetime.now(timezone.utc)
        with patch.object(capture, 'extract', side_effect=RuntimeError('confidential arbitrary exception')):
            capture.capture(self.config, self.state, now=now)
        def run(command, **kwargs):
            self.assertNotIn('confidential arbitrary exception', kwargs['input'])
            data = json.loads(kwargs['input'][len(capture.PROMPT) + 1:])
            self.assertNotIn('previous_capture_rejection', data['turn'])
            Path(command[command.index('-o') + 1]).write_text(json.dumps({'notes': []}))
            class Result:
                returncode = 0
                stdout = ''
            return Result()
        with patch.object(capture, 'run_extractor', side_effect=run):
            self.assertEqual(capture.capture(self.config, self.state, now=now + timedelta(hours=2))['processed'], 1)

    def test_cli_version_metadata_accepts_future_versions_and_hides_private_output(self):
        class Result:
            returncode = 0
            stdout = 'codex-cli 0.160.0\n'
        def run(command, **kwargs):
            self.assertEqual(command, ['/bin/false', '--version'])
            self.assertEqual(kwargs['timeout'], 10)
            self.assertNotIn('CODEX_TEST_UNTRUSTED', kwargs['env'])
            return Result()
        with patch.dict(capture.os.environ, {'CODEX_TEST_UNTRUSTED': 'remove'}), patch.object(capture.subprocess, 'run', side_effect=run):
            for version in ('0.160.0', '0.162.0-alpha.17.2', '0.162.1', '0.999.0', '1.0.0-beta.1+build.2'):
                Result.stdout = 'codex-cli ' + version + '\n'
                self.assertEqual(capture.cli_version(self.config), 'codex-cli ' + version)
            for output in ('codex-cli 0.999.0 private output', '', 'codex-cli 1.0.0\nprivate output', 'x' * 257):
                Result.stdout = output
                self.assertEqual(capture.cli_version(self.config), 'unknown')
            Result.stdout = 'codex-cli 0.999.0'
            Result.returncode = 1
            self.assertEqual(capture.cli_version(self.config), 'unknown')
        for error in (capture.subprocess.TimeoutExpired('secret command', 10), OSError('private path'),
                      UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid output')):
            with patch.object(capture.subprocess, 'run', side_effect=error):
                self.assertEqual(capture.cli_version(self.config), 'unknown')

    def test_future_or_unknown_version_does_not_block_capture(self):
        self.state.mkdir()
        config = self.state / 'config.json'
        config.write_text(json.dumps({**self.config, 'host_id': 'client-host', 'codex_path': sys.executable}))
        for version in ('codex-cli 0.999.0', 'unknown'):
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

    def test_failed_extraction_prints_safe_error_in_worker_log(self):
        self.state.mkdir()
        config = self.state / 'config.json'
        config.write_text(json.dumps({**self.config, 'host_id': 'client-host', 'codex_path': sys.executable}))
        class Result:
            returncode = 0
            stdout = 'codex-cli 0.999.0 private output'
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

    def test_strict_model_command_and_tool_rejection(self):
        self.state.mkdir()
        turn = {'verified_skills': ['frontend-ui'], 'messages': [], 'calls': []}
        def run(command, **kwargs):
            self.assertIn('--ignore-user-config', command)
            self.assertIn('--strict-config', command)
            self.assertIn('--no-daemon', command)
            self.assertIn('features.code_mode=false', command)
            self.assertIn('features.multi_agent_v2.non_code_mode_only=false', command)
            self.assertIn('features.code_mode.excluded_tool_namespaces=["functions","clock","collaboration"]', command)
            self.assertNotIn('CODEX_TEST_UNTRUSTED', kwargs['env'])
            self.assertIn('features.shell_tool=false', command)
            self.assertEqual(command[-1], '-')
            Path(command[command.index('-o') + 1]).write_text(json.dumps({'notes': []}))
            class Result:
                returncode = 0
                stdout = '{"type":"item.completed","item":{"type":"command_execution"}}'
            return Result()
        with patch.dict(capture.os.environ, {'CODEX_TEST_UNTRUSTED': 'remove'}), patch.object(capture, 'run_extractor', side_effect=run):
            with self.assertRaises(RuntimeError):
                capture.extract(self.config, self.state, turn, [])

    def test_failed_compatibility_check_keeps_evidence_without_sending_private_prompt(self):
        self.rollout(self.transcript(user='Build a page with confidential project evidence'))
        with patch.object(capture, 'compatible_profile', side_effect=RuntimeError('Capture CLI isolation check exposed tools')), \
                patch.object(capture.subprocess, 'run') as cli:
            result = capture.capture(self.config, self.state)
        cli.assert_not_called()
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(result['errors'], 1)
        state = json.loads((self.state / 'capture-state.json').read_text())
        self.assertIn('CLI isolation verification exposed tools', [value['reason'] for value in state['deferred'].values()])
        self.assertFalse(self.feedback.exists() and list(self.feedback.rglob('*.md')))

    def test_transient_timeout_retries_once_without_changing_model_or_controls(self):
        self.state.mkdir()
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            self.assertIn('model_reasoning_effort="xhigh"', command)
            self.assertEqual(command[command.index('-m') + 1], 'gpt-6.1-sol')
            self.assertLessEqual(kwargs['timeout'], 240)
            output = Path(command[command.index('-o') + 1])
            if len(calls) == 1:
                output.write_text('{"notes":["stale private output"]}')
                raise capture.subprocess.TimeoutExpired('private command', 240,
                    output=b'{"type":"turn.started"}\n', stderr=b'private source text')
            self.assertFalse(output.exists())
            output.write_text('{"notes":[]}')
            return capture.subprocess.CompletedProcess(command, 0, '{"type":"turn.completed"}\n', '')
        with patch.object(capture, 'run_extractor', side_effect=run):
            self.assertEqual(capture.model_response(self.config, self.state, 'private prompt', '{}', capture.SCHEMA), {'notes': []})
        self.assertEqual(len(calls), 2)

    def test_recovered_timeout_logs_only_safe_metadata_to_stderr(self):
        self.state.mkdir()
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                raise capture.subprocess.TimeoutExpired('private command', 240,
                    output=b'{"type":"turn.started"}\n', stderr=b'private source secret')
            Path(command[command.index('-o') + 1]).write_text('{"notes":[]}')
            return capture.subprocess.CompletedProcess(command, 0, '', '')
        with patch.object(capture, 'run_extractor', side_effect=run), \
                patch.object(capture.sys, 'stderr', io.StringIO()) as stderr, \
                patch.object(capture.sys, 'stdout', io.StringIO()) as stdout:
            result = capture.model_response(self.config, self.state, 'private prompt', '{}', capture.SCHEMA)
        self.assertEqual(result, {'notes': []})
        self.assertEqual(stdout.getvalue(), '')
        metadata = json.loads(stderr.getvalue())
        self.assertEqual(set(metadata), {'event', 'elapsed_seconds', 'phase', 'failure_class', 'attempts'})
        self.assertEqual((metadata['event'], metadata['failure_class'], metadata['phase'], metadata['attempts']),
                         ('extractor_attempt_failed', 'timeout', 'inference', 1))
        self.assertNotIn('private', stderr.getvalue())
        self.assertNotIn('secret', stderr.getvalue())

    def test_transient_cli_failure_retries_but_authentication_and_limits_do_not(self):
        self.state.mkdir()
        for stderr, expected, attempts in (
                ('stream disconnected: connection reset; private source', 'connection', 2),
                ('HTTP 401: authentication failed; private source', 'authentication', 1),
                ('HTTP 429: usage limit; private source', 'rate limit', 1),
                ('private unexpected failure', 'CLI or output', 1)):
            with self.subTest(category=expected), patch.object(capture, 'run_extractor', return_value=
                    capture.subprocess.CompletedProcess([], 1, '{"type":"turn.started"}\n', stderr)) as run:
                with self.assertRaises(capture.ExtractorFailure) as caught:
                    capture.model_response(self.config, self.state, 'private prompt', '{}', capture.SCHEMA)
            self.assertEqual(run.call_count, attempts)
            self.assertEqual(caught.exception.diagnostics['failure_class'], expected)
            self.assertEqual(caught.exception.diagnostics['phase'], 'inference')
            self.assertNotIn('private', str(caught.exception))
            self.assertNotIn('private', json.dumps(caught.exception.diagnostics))

    def test_connection_error_item_retries_without_logging_private_error_text(self):
        self.state.mkdir()
        stream = '\n'.join((json.dumps({'type': 'turn.started'}), json.dumps({
            'type': 'item.completed', 'item': {'type': 'error',
                'message': 'stream disconnected: connection reset; private source text'}})))
        with patch.object(capture, 'run_extractor', return_value=
                capture.subprocess.CompletedProcess([], 1, stream, '')) as run:
            with self.assertRaises(capture.ExtractorFailure) as caught:
                capture.model_response(self.config, self.state, 'private prompt', '{}', capture.SCHEMA)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(caught.exception.diagnostics['failure_class'], 'connection')
        self.assertNotIn('private source text', str(caught.exception))
        self.assertNotIn('private source text', json.dumps(caught.exception.diagnostics))

    def test_timeout_with_tool_event_is_rejected_without_retry(self):
        self.state.mkdir()
        error = capture.subprocess.TimeoutExpired('private command', 240,
            output=b'{"type":"item.started","item":{"type":"command_execution"}}\n')
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
            raise capture.subprocess.TimeoutExpired('private command', 3, output=b'{"type":"turn.started"}\n')
        with patch.object(capture.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(capture, 'run_extractor', side_effect=run) as model:
            with self.assertRaises(capture.CaptureBudgetExpired):
                capture.model_response(self.config, self.state, 'private prompt', '{}', capture.SCHEMA, deadline=3)
            model.assert_called_once()
            with patch.object(capture, 'compatible_profile') as profile:
                with self.assertRaises(capture.CaptureBudgetExpired):
                    capture.model_response(self.config, self.state, 'private prompt', '{}', capture.SCHEMA, deadline=3)
                profile.assert_not_called()
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

    def test_failed_capture_retains_only_safe_extractor_diagnostics(self):
        self.rollout()
        failed = capture.ExtractorFailure('extractor connection failure',
            {'elapsed_seconds': 1.25, 'phase': 'startup', 'failure_class': 'connection', 'attempts': 2})
        with patch.object(capture, 'extract', side_effect=failed):
            counts = capture.capture(self.config, self.state)
        state = json.loads((self.state / 'capture-state.json').read_text())
        pending = next(iter(state['deferred'].values()))
        self.assertEqual(pending['diagnostics'], failed.diagnostics)
        self.assertEqual(counts['errors'], 1)

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


class CaptureCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cli = self.root / 'codex'
        self.cli.write_text('fake binary generation one')
        capture.verified_profile.cache_clear()
        self.addCleanup(capture.verified_profile.cache_clear)

    def test_discovery_adapts_renamed_commands_flags_aliases_and_removed_features(self):
        root = ROOT_HELP.replace('exec ', 'run ').replace('features ', 'flags ')
        help_text = EXEC_HELP.replace('--ignore-user-config', '--ignore-config').replace('--json', '--experimental-json')
        help_text = help_text.replace('-o, --output-last-message', '--output-last-message')
        help_text = help_text[:help_text.index('      --skip-git-repo-check')]
        replies = {('--help',): root, ('run', '--help'): help_text,
                   ('flags', '--help'): 'Commands:\n  show  List known features with their stage and effective state',
                   ('flags', 'show'): 'shell_tool  stable  true\ncomputer_use  removed  false\ncode_mode_host  stable  true\n'}
        with patch.object(capture, 'cli_text', side_effect=lambda cli, *args, **kwargs: replies[args]):
            profile = capture.discover_profile(str(self.cli))
        self.assertEqual(profile['command'], 'run')
        self.assertEqual(profile['ignore_config'], '--ignore-config')
        self.assertEqual(profile['json'], '--experimental-json')
        self.assertEqual(profile['output'], '--output-last-message')
        self.assertIsNone(profile['skip_git'])
        self.assertIn('features.shell_tool=false', profile['overrides'])
        self.assertNotIn('features.computer_use=false', profile['overrides'])
        with patch.object(capture.subprocess, 'run') as git:
            command = capture.extractor_command(str(self.cli), profile, self.root, self.root / 'schema', self.root / 'output')
        git.assert_called_once()
        self.assertEqual(command[:3], [str(self.cli), '--no-daemon', 'run'])
        self.assertIn('--ignore-config', command)
        self.assertIn('read-only', command)
        self.assertIn('--experimental-json', command)
        self.assertNotIn('--skip-git-repo-check', command)

    def test_discovery_never_drops_missing_safety_control(self):
        missing = EXEC_HELP.replace('--ignore-user-config', '--unrelated').replace('Do not load user config.toml', 'Unrelated option')
        with patch.object(capture, 'cli_text', side_effect=[ROOT_HELP, missing]):
            with self.assertRaisesRegex(RuntimeError, 'required control'):
                capture.discover_profile(str(self.cli))

    def audit_run(self, tools=None, retired=None):
        def run(command, **kwargs):
            self.assertEqual(kwargs['input'], 'Compatibility check only. Return {"notes":[]}.')
            self.assertIn('--ignore-user-config', command)
            self.assertIn('read-only', command)
            if retired and any(value.split('=', 1)[0] == retired for value in command):
                return capture.subprocess.CompletedProcess(command, 1, '', 'unknown configuration field `' + retired + '` in -c/--config override')
            url = next(value.split('=', 1)[1].strip('"') for value in command if value.startswith('model_providers.capture_audit.base_url='))
            endpoint = urlsplit(url)
            connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=5)
            try:
                connection.request('POST', endpoint.path + '/responses', body=json.dumps(
                    {'model': 'gpt-6.1-sol', 'input': [{'role': 'user', 'content': kwargs['input']}], 'tools': tools or []}))
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
            finally:
                connection.close()
            return capture.subprocess.CompletedProcess(command, 1, '', 'Compatibility check complete')
        return run

    def test_audit_removes_retired_configuration_only_before_evidence(self):
        profile = {**PROFILE, 'overrides': list(PROFILE['overrides'])}
        with patch.object(capture.subprocess, 'run', side_effect=self.audit_run(retired='tools.update_plan.enabled')) as cli:
            capture.audit_profile(str(self.cli), profile, self.root)
        self.assertEqual(cli.call_count, 2)
        self.assertNotIn('tools.update_plan.enabled=false', profile['overrides'])
        self.assertIn('approval_policy="never"', profile['overrides'])

    def test_audit_rejects_exposed_tools_and_does_not_cache_profile(self):
        profile = {**PROFILE, 'overrides': list(PROFILE['overrides'])}
        with patch.object(capture, 'discover_profile', return_value=profile), \
                patch.object(capture.subprocess, 'run', side_effect=self.audit_run(tools=[{'type': 'function', 'name': 'shell'}])):
            with self.assertRaisesRegex(RuntimeError, 'exposed tools'):
                capture.compatible_profile(str(self.cli), self.root)
        self.assertFalse((self.root / 'capture-cli.json').exists())

    def test_audit_never_removes_required_configuration(self):
        profile = {**PROFILE, 'overrides': list(PROFILE['overrides'])}
        with patch.object(capture.subprocess, 'run', side_effect=self.audit_run(retired='approval_policy')) as cli:
            with self.assertRaisesRegex(RuntimeError, 'isolation check failed'):
                capture.audit_profile(str(self.cli), profile, self.root)
        cli.assert_called_once()
        self.assertIn('approval_policy="never"', profile['overrides'])

    def test_verified_profile_reuses_audit_and_rechecks_replaced_binary_or_bad_cache(self):
        def discover(cli, **kwargs):
            return {**PROFILE, 'overrides': list(PROFILE['overrides'])}
        def audit(cli, profile, root, **kwargs):
            profile['overrides'].remove('tools.update_plan.enabled=false')
        with patch.object(capture, 'discover_profile', side_effect=discover), patch.object(capture, 'audit_profile', side_effect=audit) as check:
            first = capture.compatible_profile(str(self.cli), self.root)
            capture.verified_profile.cache_clear()
            self.assertEqual(capture.compatible_profile(str(self.cli), self.root), first)
            self.assertEqual(check.call_count, 1)
            self.cli.write_text('fake binary generation two')
            capture.compatible_profile(str(self.cli), self.root)
            self.assertEqual(check.call_count, 2)
            (self.root / 'capture-cli.json').write_text('invalid partial JSON')
            capture.verified_profile.cache_clear()
            capture.compatible_profile(str(self.cli), self.root)
            self.assertEqual(check.call_count, 3)


if __name__ == '__main__':
    unittest.main()
