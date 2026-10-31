from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import capture
import evidence
import read_guidance

SESSION = '11111111-2222-4333-8444-555555555555'
GUIDANCE = '# Find Skills\n\nUse the leaderboard first.'
LATER = datetime(2026, 10, 20, tzinfo=timezone.utc)
SKILL_FILE = '---\nname: find-skills\n---\n# Find Skills\n'


class ClaudeTranscript:
    """Synthetic records shaped like saved Claude Code transcripts."""

    def __init__(self):
        self.records, self.count = [], 0

    def add(self, kind, content=None, **extra):
        self.count += 1
        record = {'type': kind, 'sessionId': SESSION, 'uuid': 'u' + str(self.count), 'isSidechain': False,
                  'timestamp': '2026-10-16T01:00:%02dZ' % self.count, **extra}
        if kind in ('user', 'assistant'):
            record['message'] = {'role': kind, 'content': content}
            for key in ('stop', 'model'):
                if key in record:
                    record['message']['stop_reason' if key == 'stop' else key] = record.pop(key)
        self.records.append(record)
        return self

    def prompt(self, text, prompt='prompt-1', **extra):
        return self.add('user', text, promptId=prompt, **extra)

    def reply(self, text, stop='end_turn', **extra):
        return self.add('assistant', [{'type': 'text', 'text': text}], stop=stop, **extra)

    def tool(self, name, arguments, output, prompt='prompt-1', result=None, error=False, use='toolu_1'):
        self.add('assistant', [{'type': 'tool_use', 'id': use, 'name': name, 'input': arguments}], stop='tool_use')
        item = {'type': 'tool_result', 'tool_use_id': use, 'content': output}
        if error:
            item['is_error'] = True
        return self.add('user', [item], promptId=prompt, **({'toolUseResult': result} if result is not None else {}))

    def skill(self, name='find-skills', prompt='prompt-1', arguments='changelog', use='toolu_skill'):
        self.tool('Skill', {'skill': name, 'args': arguments}, 'Launching skill: ' + name, prompt, {'success': True}, use=use)
        text = 'Base directory for this skill: /home/user/.claude/skills/' + name + '\n\n' + GUIDANCE + '\n\n\nARGUMENTS: ' + arguments
        return self.add('user', [{'type': 'text', 'text': text}], promptId=prompt, isMeta=True, sourceToolUseID=use)

    def write(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(record) + '\n' for record in self.records))
        # Keep the file's quiet-period clock aligned with the synthetic records,
        # rather than the wall clock on whichever day this suite runs.
        modified = capture.timestamp(self.records[-1]['timestamp']).timestamp()
        os.utime(path, (modified, modified))
        return path


def turns(path, now=LATER, **options):
    return capture.claude_turns([path], capture.timestamp(capture.SINCE), now=now, **options)


class ClaudeCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.projects = self.root / 'claude-projects'
        self.serial = 0

    def path(self, name=None):
        self.serial += 1
        return self.projects / 'project' / (name or 'session-%d.jsonl' % self.serial)

    def scan(self, transcript, **options):
        return turns(transcript.write(self.path()), **options)

    def test_skill_tool_load_is_a_verified_turn_with_an_excerpt_hash(self):
        transcript = ClaudeTranscript().prompt('<system-reminder>harness text</system-reminder>Find a changelog skill')
        [turn] = self.scan(transcript.skill().reply('Found two candidates.'))
        self.assertEqual((turn['thread'], turn['turn'], turn['verified_skills']), (SESSION, 'prompt-1', ['find-skills']))
        self.assertEqual(turn['loaded_reads'], [{'path': 'find-skills/SKILL.md', 'coverage': 'partial', 'read_turn': 'prompt-1',
                                                 'sha256': hashlib.sha256(GUIDANCE.encode()).hexdigest()}])
        self.assertEqual([(m['role'], m['text']) for m in turn['messages'] if m['role'] != 'tool'],
                         [('user', 'Find a changelog skill'), ('assistant', 'Found two candidates.')])
        self.assertNotIn('leaderboard', json.dumps(turn['messages']))
        self.assertNotIn('filter', turn)
        self.assertEqual(turn['evidence_id'], hashlib.sha256((SESSION + ':prompt-1').encode()).hexdigest())

    def test_slash_command_load_is_recognized(self):
        transcript = ClaudeTranscript().prompt('<command-message>find-skills</command-message>\n<command-name>/find-skills</command-name>\n<command-args>changelog</command-args>')
        transcript.add('user', [{'type': 'text', 'text': 'Base directory for this skill: /x/find-skills\n\n' + GUIDANCE}], promptId='prompt-1', isMeta=True)
        [turn] = self.scan(transcript.reply('Searched.'))
        self.assertEqual(turn['verified_skills'], ['find-skills'])
        self.assertEqual(turn['messages'][0], {'role': 'user', 'text': '/find-skills changelog'})

    def test_look_alike_or_unlinked_injections_do_not_count(self):
        transcript = ClaudeTranscript().prompt('Find a skill')
        transcript.add('user', [{'type': 'text', 'text': 'Base directory for this skill: /x/xfind-skills\n\n' + GUIDANCE}], promptId='prompt-1', isMeta=True)
        transcript.tool('Bash', {'command': 'echo hi'}, 'hi', use='toolu_bash')
        transcript.add('user', [{'type': 'text', 'text': 'Base directory for this skill: /x/find-skills\n\n' + GUIDANCE}],
                       promptId='prompt-1', isMeta=True, sourceToolUseID='toolu_bash')
        self.assertEqual(self.scan(transcript.reply('Done.')), [])

    def test_loaded_skill_stays_in_effect_for_later_turns(self):
        transcript = ClaudeTranscript().prompt('Find a skill').skill().reply('Use the first one.')
        transcript.prompt('No, that one is unmaintained; check stars first.', prompt='prompt-2').reply('Checked stars.')
        first, second = self.scan(transcript)
        self.assertEqual(second['turn'], 'prompt-2')
        self.assertEqual(second['verified_skills'], ['find-skills'])
        self.assertEqual([read['read_turn'] for read in second['loaded_reads']], ['prompt-1'])
        self.assertEqual(second['messages'][0]['text'], 'No, that one is unmaintained; check stars first.')

    def test_completion_waits_for_a_real_final_reply_and_a_quiet_file(self):
        path = ClaudeTranscript().prompt('First').skill().reply('One.').prompt('Second', prompt='prompt-2').reply('Two.').write(self.path())
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        self.assertEqual([t['turn'] for t in turns(path, now=modified + timedelta(minutes=1))], ['prompt-1'])
        self.assertEqual([t['turn'] for t in turns(path, now=modified + capture.CLAUDE_QUIET)], ['prompt-1', 'prompt-2'])
        # An interrupt keeps the prompt ID and ends with a synthetic reply; the resumed prompt is a new turn.
        interrupted = ClaudeTranscript().prompt('Find').skill().add('assistant', [{'type': 'text', 'text': 'Partial'}], stop='tool_use')
        interrupted.prompt('[Request interrupted by user]').reply('No response requested.', stop='stop_sequence', model='<synthetic>')
        self.assertEqual(self.scan(interrupted), [])
        interrupted.prompt('Continue where you left off.', prompt='prompt-2').reply('Finished.')
        self.assertEqual([t['turn'] for t in self.scan(interrupted)], ['prompt-2'])
        limited = ClaudeTranscript().prompt('Find').skill().reply('You have hit your limit', stop='stop_sequence', isApiErrorMessage=True)
        self.assertEqual(self.scan(limited), [])
        failed = ClaudeTranscript().prompt('Find').skill().reply('Error', model='<synthetic>')
        self.assertEqual(self.scan(failed), [])

    def test_notification_turns_inherit_the_latest_prompt_scope(self):
        def transcript(text):
            records = ClaudeTranscript().prompt(text).skill().reply('Started a background check.')
            records.prompt('<task-notification>\n<task-id>x</task-id>\n</task-notification>', prompt='prompt-2',
                           turnOrigin='task_notification').reply('The background check finished.')
            return records
        self.assertEqual(self.scan(transcript('Review only: find a changelog skill')), [])
        found = self.scan(transcript('Find a changelog skill'))
        self.assertEqual([t['turn'] for t in found], ['prompt-1', 'prompt-2'])
        self.assertEqual([m['role'] for m in found[1]['messages']], ['assistant'])

    def test_oversized_images_preserve_claude_turn_and_raw_source_identity(self):
        # Put data first to exercise field-order independence, including nested tool results.
        image = {'source': {'data': '/AAA' * 3000, 'media_type': 'image/png', 'type': 'base64'}, 'type': 'image'}
        transcript = ClaudeTranscript().prompt('Find a skill').skill()
        transcript.add('user', [{'type': 'tool_result', 'tool_use_id': 'preview',
                                 'content': [{'type': 'text', 'text': 'Image preview'}, image, image]}], promptId='prompt-1')
        path = transcript.reply('Found a maintained skill.').write(self.path())
        with patch.object(capture, 'MAX_RECORD_BYTES', path.stat().st_size + 1):
            original = turns(path)
        errors = []
        with patch.object(capture, 'MAX_RECORD_BYTES', 2048):
            result = turns(path, errors=errors)
            evidence_result = turns(path, errors=errors, eligible_only=False)
        self.assertEqual(errors, [])
        self.assertEqual(result, original)
        self.assertEqual(evidence_result, original)
        self.assertEqual(result[0]['verified_skills'], ['find-skills'])
        self.assertEqual(result[0]['source_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(result[0]['evidence_id'], hashlib.sha256((SESSION + ':prompt-1').encode()).hexdigest())

    def test_oversized_nonimage_transcript_keeps_diagnostic(self):
        transcript = ClaudeTranscript().prompt('Find a skill').skill()
        transcript.add('user', [{'type': 'text', 'text': 'Large prose ' * 500}], promptId='prompt-1')
        path = transcript.reply('Done.').write(self.path())
        errors = []
        with patch.object(capture, 'MAX_RECORD_BYTES', 2048):
            self.assertEqual(turns(path, errors=errors), [])
        self.assertEqual([error['reason'] for error in errors], ['oversized transcript record'])

    def test_records_outside_the_prompt_turn_do_not_change_its_source_hash(self):
        transcript = ClaudeTranscript().prompt('Find').skill().reply('Done.')
        path = self.path()
        before = turns(transcript.write(path))[0]['source_sha256']
        transcript.add('user', '<local-command-caveat>Caveat</local-command-caveat>', promptId='prompt-x', isMeta=True)
        transcript.add('user', 'Summary of the earlier conversation', promptId='prompt-y', isCompactSummary=True)
        transcript.add('attachment', attachment={'type': 'total_tokens_reminder'})
        transcript.add('attachment', attachment={'type': 'queued_command', 'commandMode': 'task-notification',
                                                 'prompt': '<task-notification>x</task-notification>'})
        self.assertEqual(turns(transcript.write(path))[0]['source_sha256'], before)

    def test_input_queued_during_a_turn_is_part_of_it(self):
        transcript = ClaudeTranscript().prompt('Find a skill').skill()
        transcript.add('attachment', attachment={'type': 'queued_command', 'commandMode': 'prompt', 'prompt': 'Only official sources, please'})
        [turn] = self.scan(transcript.reply('Filtered to official sources.'))
        self.assertIn({'role': 'user', 'text': 'Only official sources, please'}, turn['messages'])
        readonly = ClaudeTranscript().prompt('Find a skill').skill()
        readonly.add('attachment', attachment={'type': 'queued_command', 'commandMode': 'prompt', 'prompt': 'Actually, review only'})
        self.assertEqual(self.scan(readonly.reply('Stopped.')), [])

    def test_subagents_sidechains_forks_and_maintenance_turns_are_excluded(self):
        subagent = ClaudeTranscript().prompt('Find').skill().reply('Done.').write(self.projects / 'p/s/subagents/agent-1.jsonl')
        self.assertEqual(turns(subagent), [])
        for extra in ({'isSidechain': True}, {'forkedFrom': {'sessionId': 'other', 'messageUuid': 'm'}}):
            with self.subTest(extra=extra):
                self.assertEqual(self.scan(ClaudeTranscript().prompt('Find', **extra).skill().reply('Done.')), [])
        for text in ('Review only; find a skill', 'Update scripts/capture.py'):
            with self.subTest(text=text):
                self.assertEqual(self.scan(ClaudeTranscript().prompt(text).skill().reply('Done.')), [])
        evidence_view = self.scan(ClaudeTranscript().prompt('Review only').skill().reply('Done.'), eligible_only=False)
        self.assertEqual(len(evidence_view), 1)
        self.assertNotIn('filter', evidence_view[0])

    def test_read_tool_records_complete_or_partial_file_hashes(self):
        for lines, coverage in ((4, 'complete'), (2, 'partial')):
            with self.subTest(coverage=coverage):
                result = {'type': 'text', 'file': {'filePath': '/x/.agents/skills/find-skills/SKILL.md', 'content': SKILL_FILE,
                                                   'startLine': 1, 'numLines': lines, 'totalLines': 4}}
                [turn] = self.scan(ClaudeTranscript().prompt('Find a skill').tool('Read', {'file_path': '/x'}, '1\t---', result=result).reply('Done.'))
                self.assertEqual(turn['verified_skills'], ['find-skills'])
                self.assertEqual(turn['loaded_reads'], [{'path': 'find-skills/SKILL.md', 'coverage': coverage, 'read_turn': 'prompt-1',
                                                         'sha256': hashlib.sha256(SKILL_FILE.encode()).hexdigest()}])
        failed = ClaudeTranscript().prompt('Find').tool('Read', {}, 'missing', result={'file': {'filePath': '/x/find-skills/SKILL.md', 'content': SKILL_FILE}}, error=True)
        self.assertEqual(self.scan(failed.reply('Done.')), [])

    def test_printed_or_ambiguous_bash_heading_is_not_verified_skill_use(self):
        for command, failed in (("printf '%s\\n' '# TypeScript'; # cat /skills/typescript/SKILL.md", False),
                                ('echo "cat /skills/typescript/SKILL.md"', False),
                                ('grep TypeScript /skills/typescript/SKILL.md', False),
                                ('cat /skills/typescript/SKILL.md; echo extra', False),
                                ('cat /skills/typescript/SKILL.md', True)):
            with self.subTest(command=command):
                transcript = ClaudeTranscript().prompt('Implement parser').tool('Bash', {'command': command}, '# TypeScript\n', error=failed).reply('Done.')
                self.assertEqual(self.scan(transcript), [])

    def test_shell_reads_load_skills_but_only_reader_envelopes_are_hashed(self):
        [turn] = self.scan(ClaudeTranscript().prompt('Find').tool('Bash', {'command': 'cat /x/skills/find-skills/SKILL.md'}, SKILL_FILE.rstrip('\n')).reply('Done.'))
        self.assertEqual((turn['verified_skills'], turn['loaded_reads']), (['find-skills'], []))

    def test_typescript_native_read_tracks_all_text_supporting_parts(self):
        root = '---\nname: typescript\n---\n# TypeScript\n'
        transcript = ClaudeTranscript().prompt('Implement the typed parser')
        paths = [('SKILL.md', root), ('references/skill-map.md', '# Map\n'),
                 ('references/tooling-and-public-types.md', '# Public types\n'),
                 ('scripts/check.ts', 'export const check = true;\n'),
                 ('assets/templates/example.html', '<main>Example</main>\n')]
        for index, (relative, content) in enumerate(paths):
            path = '/x/.codex/skills/typescript/' + relative
            result = {'file': {'filePath': path, 'content': content, 'startLine': 1, 'numLines': 1, 'totalLines': 1}}
            transcript.tool('Read', {'file_path': path}, content, result=result, use='read-' + str(index))
        [turn] = self.scan(transcript.reply('Implemented and checked.'))
        self.assertEqual(turn['verified_skills'], ['typescript'])
        self.assertEqual([item['path'] for item in turn['loaded_reads']], ['typescript/' + relative for relative, _ in paths])
        self.assertEqual([item['sha256'] for item in turn['loaded_reads']],
                         [hashlib.sha256(content.encode()).hexdigest() for _, content in paths])

    def test_typescript_package_and_project_native_reads_are_not_skill_evidence(self):
        for path in ('/project/node_modules/typescript/lib/typescript.js',
                     '/project/node_modules/typescript/package.json',
                     '/project/typescript/customer-notes.md', '/project/typescript/SKILL.md'):
            with self.subTest(path=path):
                transcript = ClaudeTranscript().prompt('Implement the typed parser').skill()
                content = '# TypeScript\n'
                result = {'file': {'filePath': path, 'content': content, 'startLine': 1, 'numLines': 1, 'totalLines': 1}}
                transcript.tool('Read', {'file_path': path}, content, result=result, use='not-a-skill')
                [turn] = self.scan(transcript.reply('Implemented.'))
                self.assertEqual(turn['verified_skills'], ['find-skills'])
                self.assertEqual([r['path'] for r in turn['loaded_reads']], ['find-skills/SKILL.md'])

    def test_relative_typescript_bash_cat_and_helper_are_eligible(self):
        content = '---\nname: typescript\n---\n# TypeScript\n'
        digest = hashlib.sha256(content.encode()).hexdigest()
        envelope = {'protocol': read_guidance.PROTOCOL, 'path': 'typescript/SKILL.md', 'start': 0,
                    'next_start': None, 'coverage': 'complete', 'sha256': digest, 'content': content}
        for command, output, hashed in (
                ('cat skills/typescript/SKILL.md', content, False),
                ('uv run --script scripts/read_guidance.py skills/typescript/SKILL.md', json.dumps(envelope), True)):
            with self.subTest(command=command):
                transcript = ClaudeTranscript().prompt('Implement the typed parser').tool('Bash', {'command': command}, output)
                [turn] = self.scan(transcript.reply('Implemented.'))
                self.assertEqual(turn['verified_skills'], ['typescript'])
                self.assertEqual(turn['loaded_reads'], [{'path': 'typescript/SKILL.md', 'sha256': digest,
                                                       'coverage': 'complete', 'read_turn': 'prompt-1'}] if hashed else [])

    def test_shell_reader_envelopes_require_complete_unwrapped_results(self):
        failed = ClaudeTranscript().prompt('Find').tool('Bash', {'command': 'cat /x/skills/find-skills/SKILL.md'}, 'No such file', error=True)
        self.assertEqual(self.scan(failed.reply('Done.')), [])
        guide = self.root / 'installed/skills/find-skills/SKILL.md'
        guide.parent.mkdir(parents=True)
        guide.write_text(SKILL_FILE)
        command = 'uv run --script scripts/read_guidance.py ' + str(guide)
        output = json.dumps(read_guidance.read(guide))
        [turn] = self.scan(ClaudeTranscript().prompt('Find').tool('Bash', {'command': command}, output).reply('Done.'))
        self.assertEqual(turn['loaded_reads'], [{'path': 'find-skills/SKILL.md', 'coverage': 'complete', 'read_turn': 'prompt-1',
                                                 'sha256': hashlib.sha256(SKILL_FILE.encode()).hexdigest()}])
        wrapped = '<persisted-output>\nOutput too large. Preview:\n' + output[:40] + '\n</persisted-output>'
        [turn] = self.scan(ClaudeTranscript().prompt('Find').skill().tool('Bash', {'command': command}, wrapped, use='b').reply('Done.'))
        self.assertEqual([read['coverage'] for read in turn['loaded_reads']], ['partial'])

    def test_malformed_records_are_contained_to_their_turn(self):
        transcript = ClaudeTranscript().prompt('First').skill().reply('One.')
        transcript.prompt('Second', prompt='prompt-2').add('assistant', [{'type': 'text', 'text': None}], stop='end_turn')
        transcript.records[-1]['timestamp'] = 5
        transcript.prompt('Third', prompt='prompt-3').reply('Three.')
        path = transcript.write(self.path())
        with path.open('a') as stream:
            stream.write('not json\n{"partial":')
        errors = []
        self.assertEqual([t['turn'] for t in turns(path, errors=errors)], ['prompt-1'])
        self.assertEqual([e['reason'] for e in errors], ['invalid transcript timestamp', 'invalid transcript record'])

    def test_capture_writes_notes_from_claude_and_codex_turns(self):
        ClaudeTranscript().prompt('Find a changelog skill').skill().reply('Found one.').write(self.projects / 'p/s.jsonl')
        sessions, feedback = self.root / 'sessions', self.root / 'feedback'
        sessions.mkdir()
        codex = [{'type': 'session_meta', 'payload': {'id': 'codex-session', 'source': 'desktop'}},
                 {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'codex-turn'}},
                 {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Build a page'}},
                 {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'r', 'name': 'exec_command', 'arguments': 'cat /s/skills/frontend-ui/SKILL.md'}},
                 {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'r', 'output': 'Exit code: 0\n# Frontend UI\n'}},
                 {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'codex-turn'}, 'timestamp': '2026-10-16T02:00:00Z'}]
        (sessions / 'rollout.jsonl').write_text(''.join(json.dumps(record) + '\n' for record in codex))
        config = {'sessions_root': str(sessions), 'archived_sessions_root': str(self.root / 'archive'),
                  'feedback_dir': str(feedback), 'install_root': str(self.root / 'install'),
                  'codex_path': '/bin/false', 'claude_projects_root': str(self.projects)}
        def lessons(config, state_root, turn, existing, *, deadline=None):
            return [{'skill': turn['verified_skills'][0], **{field: 'Verified useful check' for field in capture.FIELDS}}]
        with patch.object(capture, 'extract', side_effect=lessons) as extract:
            summary = capture.capture(config, self.root / 'state', now=LATER)
        self.assertEqual((summary['eligible'], summary['notes'], summary['errors']), (2, 2, 0))
        self.assertEqual([call[0][2]['thread'] for call in extract.call_args_list], [SESSION, 'codex-session'])
        note = next((feedback / 'find-skills/notes').glob('*.md')).read_text()
        self.assertIn('- Thread: ' + SESSION + '\n- Turn: prompt-1', note)
        self.assertIn('"path": "find-skills/SKILL.md"', note)
        self.assertTrue(next((feedback / 'frontend-ui/notes').glob('*.md')).is_file())
        with patch.object(capture, 'extract') as extract:
            self.assertEqual(capture.capture(config, self.root / 'state', now=LATER)['eligible'], 0)
        extract.assert_not_called()

    def recovery(self, write):
        feedback = self.root / 'feedback'
        text = '# Prefer leaderboard\n- Recorded: 2026-10-16T01:00:05Z\n- Thread: ' + SESSION + '\n- Observation: Reported.\n'
        path = feedback / 'find-skills/notes/20261016T010005Z-host-abc.md'
        path.parent.mkdir(parents=True)
        path.write_text(text)
        transcript = write(ClaudeTranscript().prompt('Find a skill').skill(), path, text).reply('Saved a note.')
        source = transcript.write(self.path())
        settled = (datetime.now(timezone.utc) - capture.CLAUDE_QUIET - timedelta(minutes=1)).timestamp()
        os.utime(source, (settled, settled))
        config = {'host_id': 'client-host', 'feedback_dir': str(feedback), 'sessions_root': str(self.root / 'sessions'),
                  'install_root': str(self.root / 'install'),
                  'archived_sessions_root': str(self.root / 'archive'), 'claude_projects_root': str(self.projects)}
        assessment = {'status': 'supported', 'summary': 'Attribution is supported; outcomes remain reported.'}
        with patch.object(evidence, 'model_response', return_value=assessment):
            counts = evidence.verify(config, self.root)
        receipts = list((feedback / 'find-skills/verifications').glob('*.json'))
        return counts, json.loads(receipts[0].read_text()) if receipts else None

    def test_source_check_recovers_a_claude_note_written_through_a_temporary_file(self):
        def write(transcript, path, text):
            result = {'type': 'create', 'filePath': str(path.parent / '.tmp-note'), 'content': text}
            return transcript.tool('Write', {'file_path': 'x'}, 'File created', result=result, use='w')
        counts, receipt = self.recovery(write)
        self.assertEqual((counts['checked'], counts['recovered']), (1, 1))
        self.assertEqual((receipt['thread'], receipt['turn'], receipt['skill']), (SESSION, 'prompt-1', 'find-skills'))
        self.assertEqual(receipt['loadedReads'][0]['path'], 'find-skills/SKILL.md')

    def test_source_check_recovers_a_claude_note_written_by_a_python_heredoc(self):
        def write(transcript, path, text):
            command = "python3 - <<'PY'\nfrom pathlib import Path\nPath(" + repr(str(path)) + ").write_text(" + repr(text) + ")\nPY"
            return transcript.tool('Bash', {'command': command}, '', use='h')
        counts, receipt = self.recovery(write)
        self.assertEqual((counts['checked'], counts['recovered']), (1, 1))
        self.assertEqual(receipt['turn'], 'prompt-1')

    def test_unrelated_writes_do_not_recover_a_note(self):
        def write(transcript, path, text):
            result = {'type': 'create', 'filePath': '/elsewhere/notes/' + path.name, 'content': text}
            return transcript.tool('Write', {'file_path': 'x'}, 'File created', result=result, use='w')
        counts, receipt = self.recovery(write)
        self.assertEqual((counts['checked'], counts['unavailable'], receipt), (0, 1, None))


if __name__ == '__main__':
    unittest.main()
