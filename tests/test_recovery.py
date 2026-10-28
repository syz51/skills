import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import capture
import evidence


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.feedback = self.root / 'feedback'
        self.config = {'host_id': 'client-host', 'sessions_root': str(self.sessions),
            'install_root': str(self.root / 'install'),
            'archived_sessions_root': str(self.root / 'archive'), 'feedback_dir': str(self.feedback),
            'claude_projects_root': str(self.root / 'claude-projects')}
        self.path = self.feedback / 'frontend-ui/notes/20261008T091722Z-manual-deadbeef.md'
        self.path.parent.mkdir(parents=True)
        self.note = ('# Keep reports qualified\n- Recorded: 2026-10-08T09:17:22+00:00\n'
            '- Skill version: ' + 'f' * 64 + '\n- Observation: A document correction was reported.\n'
            '- Evidence: Runtime effectiveness remains unverified.\n')
        self.path.write_text(self.note)
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.target = '/old/.codex/skill-feedback/frontend-ui/notes/' + self.path.name
        self.notes = {self.digest: {'skill': 'frontend-ui', 'name': self.path.name, 'text': self.note}}
        template = self.note.replace('2026-10-08T09:17:22+00:00', '{recorded}').replace('f' * 64, '{version}')
        self.body = ("from pathlib import Path\nroot=Path('/old/.codex')\nstore=root/'skill-feedback/frontend-ui'\n"
            "note_path=store/'notes'/f'{stamp}-manual-{suffix}.md'\nnote=f" + repr(template) +
            "\nfor path,body in [(note_path,note)]:\n with path.open('x',encoding='utf-8') as file:\n  file.write(body)\n")
        self.result = {'status': 'supported', 'summary': 'The note preserves the reported correction and unverified effectiveness.'}

    def command(self, body=None):
        return "uv run --no-project python - <<'PY'\n" + (body or self.body) + '\nPY'

    def raw(self, body=None):
        return 'text(await tools.exec_command({cmd:' + json.dumps(self.command(body)) + '}));'

    def output(self, target=None, exit_code=0):
        return json.dumps({'exit_code': exit_code, 'output': json.dumps({'note': target or self.target}) + '\n'})

    def record(self, kind, payload, date='2026-10-08T09:17:22Z'):
        return json.dumps({'type': kind, 'payload': payload, 'timestamp': date}) + '\n'

    def rollout(self, filename='original.jsonl', thread='original-chat', complete=True, source='desktop', body=None, output=None):
        text = self.record('session_meta', {'id': thread, 'source': source})
        text += self.record('event_msg', {'type': 'task_started', 'turn_id': 'original-turn'})
        text += self.record('event_msg', {'type': 'user_message', 'message': 'Review a guide and report what changed.'})
        text += self.record('response_item', {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'create', 'input': self.raw(body)})
        text += self.record('response_item', {'type': 'custom_tool_call_output', 'call_id': 'create',
                                             'output': self.output() if output is None else output})
        text += self.record('response_item', {'type': 'message', 'role': 'assistant', 'content': [
            {'text': 'A document correction was reported. Runtime effectiveness remains unverified.'}]})
        if complete:
            text += self.record('event_msg', {'type': 'task_complete', 'turn_id': 'original-turn'}, '2026-10-08T09:18:00Z')
        path = self.sessions / filename
        path.write_text(text)
        return path

    def atomic_body(self):
        self.path.unlink()
        self.path = self.path.with_name('20261008T091722Z-test-mac.local-e4969f52-6a08-4bd1-bf14-37668e1379ff.md')
        self.note = self.note.replace('- Skill version:', '- Machine: test-mac.local\n- Thread: unavailable\n- Turn: unavailable\n- Skill version:')
        self.path.write_text(self.note)
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.target = '/old/.codex/skill-feedback/frontend-ui/notes/' + self.path.name
        self.notes = {self.digest: {'skill': 'frontend-ui', 'name': self.path.name, 'text': self.note}}
        template = self.note.replace('2026-10-08T09:17:22+00:00', '{now.isoformat()}').replace('test-mac.local', '{socket.gethostname()}')
        return ("import datetime,os,socket,tempfile,uuid\nfrom pathlib import Path\n"
            "now=datetime.datetime.now(datetime.timezone.utc)\n"
            "store=Path.home()/'.codex/skill-feedback/frontend-ui/notes'\n"
            "body=f" + repr(template) + "\nfd,temp=tempfile.mkstemp(prefix='.tmp-',dir=store)\n"
            "try:\n with os.fdopen(fd,'w') as stream: stream.write(body)\n"
            " while True:\n"
            "  final=store/(now.strftime('%Y%m%dT%H%M%SZ')+'-'+socket.gethostname().replace('/','-')+'-'+str(uuid.uuid4())+'.md')\n"
            "  try: os.link(temp,final);break\n"
            "  except FileExistsError: continue\n"
            "finally: os.unlink(temp)\nprint('Saved local feedback')\n")

    def test_atomic_writer_recovers_unknown_identity_without_replaying_saved_code(self):
        body = self.atomic_body()
        marker = self.root / 'saved-code-was-executed'
        body = 'from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n' + body
        # The real writer prints no destination, only a successful completion message.
        output = json.dumps({'exit_code': 0, 'output': 'Saved local feedback\n'})
        self.assertEqual(capture.creation_matches(self.raw(body), output, self.notes, '2026-10-08T09:17:22Z'), [self.digest])
        self.rollout(body=body, output=output)
        original = self.path.read_bytes()
        with patch.object(evidence, 'model_response', return_value=self.result) as model:
            first = evidence.verify(self.config, self.root)
            second = evidence.verify(self.config, self.root)
        self.assertEqual((first['recovered'], first['checked'], first['unavailable']), (1, 1, 0))
        self.assertEqual((second['checked'], second['recovered']), (0, 0))
        model.assert_called_once()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(marker.exists())
        receipt = evidence.assessment_inventory(self.feedback)['latest'][0]
        self.assertEqual((receipt['thread'], receipt['turn']), ('original-chat', 'original-turn'))

    def test_atomic_writer_rejects_unmatched_text_filename_and_publish_steps(self):
        body = self.atomic_body()
        variants = [body.replace('stream.write(body)', "stream.write('different note')"),
            body.replace("+'-'+str(uuid.uuid4())", "+'-wrong-'+str(uuid.uuid4())"),
            body.replace('os.link(temp,final)', 'os.link(other,final)'),
            body.replace('os.link(temp,final)', 'os.replace(temp,final)'),
            body.replace('os.unlink(temp)', 'os.unlink(other)'),
            body.replace('FileExistsError', 'Exception'),
            body.replace("os.fdopen(fd,'w')", "os.fdopen(other,'w')"),
            body.replace('stream.write(body)', 'stream.write(untrusted_prose)'),
            body.replace('try:\n with os.fdopen', 'if False:\n pass\ntry:\n with os.fdopen')]
        for variant in variants:
            with self.subTest(writer=variant):
                self.assertEqual(capture.creation_matches(self.raw(variant), self.output(), self.notes, '2026-10-08T09:17:22Z'), [])
        for output, date in ((self.output(exit_code=1), '2026-10-08T09:17:22Z'),
                (self.output(), '2026-10-18T09:17:22Z'),
                (json.dumps({'exit_code': 0, 'output': 'Warning: truncated output'}), '2026-10-08T09:17:22Z')):
            self.assertEqual(capture.creation_matches(self.raw(body), output, self.notes, date), [])

    def test_atomic_writer_conflicting_and_multiple_origins_stay_unavailable(self):
        body = self.atomic_body()
        self.rollout(body=body)
        self.rollout('different.jsonl', thread='different-chat', body=body)
        with patch.object(evidence, 'model_response') as model:
            counts = evidence.verify(self.config, self.root)
        self.assertEqual((counts['ambiguous'], counts['unavailable'], counts['checked']), (1, 1, 0))
        model.assert_not_called()
        self.path.write_text(self.note.replace('Thread: unavailable', 'Thread: conflicting-chat'))
        with patch.object(evidence, 'model_response') as model:
            self.assertEqual(evidence.verify(self.config, self.root)['unavailable'], 1)
        model.assert_not_called()

    def test_hourly_recovery_restored_older_source_and_unchanged_repeat(self):
        original = self.path.read_bytes()
        with patch.object(evidence, 'model_response', return_value=self.result) as model:
            missing = evidence.verify(self.config, self.root)
            self.assertEqual((missing['unavailable'], missing['recovered']), (1, 0))
            model.assert_not_called()
            self.rollout()
            first = evidence.verify(self.config, self.root)
            second = evidence.verify(self.config, self.root)
            self.assertEqual((first['checked'], first['recovered'], first['unavailable']), (1, 1, 0))
            self.assertEqual((second['checked'], second['recovered']), (0, 0))
            model.assert_called_once()
        self.assertEqual(self.path.read_bytes(), original)
        value = evidence.assessment_inventory(self.feedback)['latest'][0]
        self.assertEqual((value['thread'], value['turn']), ('original-chat', 'original-turn'))
        self.assertEqual(list(self.path.parent.glob('*.md')), [self.path])

    def test_literal_write_and_legacy_output_envelopes(self):
        raw = json.dumps({'cmd': self.command('from pathlib import Path\nPath(' + repr(self.target) + ').write_text(' + repr(self.note) + ')')})
        for output in (self.output(), [{'type': 'input_text', 'text': self.output()}],
                [{'type': 'text', 'text': json.dumps({'status': 'fulfilled', 'value': json.loads(self.output())})}]):
            self.assertEqual(capture.creation_matches(raw, output, self.notes, '2026-10-08T09:17:22Z'), [self.digest])

    def test_different_origins_are_ambiguous_but_rollover_copies_are_not(self):
        self.rollout()
        self.rollout('rollover.jsonl')
        with patch.object(evidence, 'model_response', return_value=self.result):
            self.assertEqual(evidence.verify(self.config, self.root)['recovered'], 1)
        for path in (self.feedback / 'frontend-ui/verifications').glob('*.json'):
            path.unlink()
        self.rollout('unrelated.jsonl', thread='different-chat')
        with patch.object(evidence, 'model_response') as model:
            result = evidence.verify(self.config, self.root)
            self.assertEqual((result['ambiguous'], result['unavailable'], result['checked']), (1, 1, 0))
            model.assert_not_called()

    def test_readback_print_function_and_conditional_are_not_creation(self):
        bodies = ["print(" + repr(self.note) + ')',
            'def save():\n Path(' + repr(self.target) + ').write_text(' + repr(self.note) + ')',
            'if False:\n Path(' + repr(self.target) + ').write_text(' + repr(self.note) + ')',
            'from pathlib import Path\nnote=f"{untrusted_prose}"\nPath(' + repr(self.target) + ').write_text(note)']
        for body in bodies:
            self.assertEqual(capture.creation_matches(self.raw(body), self.output(), self.notes, '2026-10-08T09:17:22Z'), [])
        self.assertEqual(capture.creation_matches(json.dumps({'cmd': 'cat ' + self.target}), self.output(), self.notes, '2026-10-08T09:17:22Z'), [])

    def test_wrong_filename_time_failure_truncation_and_changed_text_are_rejected(self):
        for output, date in ((self.output(target=self.target.replace('deadbeef', 'different')), '2026-10-08T09:17:22Z'),
                (self.output(exit_code=1), '2026-10-08T09:17:22Z'), (self.output(), '2026-10-16T09:17:22Z'),
                (json.dumps({'exit_code': 0, 'output': 'Warning: truncated output\n' + json.dumps({'note': self.target})}), '2026-10-08T09:17:22Z')):
            self.assertEqual(capture.creation_matches(self.raw(), output, self.notes, date), [])
        changed = {self.digest: {**self.notes[self.digest], 'text': self.note.replace('A document correction', 'A runtime improvement')}}
        self.assertEqual(capture.creation_matches(self.raw(), self.output(), changed, '2026-10-08T09:17:22Z'), [])

    def test_incomplete_subagent_and_explicit_identity_conflicts_stay_unknown(self):
        for kwargs in ({'complete': False}, {'source': {'subagent': {'thread_spawn': {}}}}):
            self.rollout(**kwargs)
            with patch.object(evidence, 'model_response') as model:
                self.assertEqual(evidence.verify(self.config, self.root)['unavailable'], 1)
                model.assert_not_called()
        self.rollout()
        self.path.write_text(self.note + '- Thread: conflicting-chat\n')
        with patch.object(evidence, 'model_response') as model:
            self.assertEqual(evidence.verify(self.config, self.root)['unavailable'], 1)
            model.assert_not_called()

    def test_existing_evidence_id_recovers_missing_identity_without_creation(self):
        self.rollout(body='print("No note creation in this turn")')
        identity = hashlib.sha256(b'original-chat:original-turn').hexdigest()
        self.path.write_text(self.note + '- Capture evidence: ' + identity + '\n')
        with patch.object(evidence, 'model_response', return_value=self.result):
            result = evidence.verify(self.config, self.root)
        self.assertEqual((result['checked'], result['recovered'], result['unavailable']), (1, 1, 0))

    def test_no_saved_script_is_executed(self):
        marker = self.root / 'historical-side-effect'
        self.rollout(body='Path(' + repr(str(marker)) + ').touch()\n' + self.body)
        with patch.object(evidence, 'model_response', return_value=self.result):
            self.assertEqual(evidence.verify(self.config, self.root)['recovered'], 1)
        self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
