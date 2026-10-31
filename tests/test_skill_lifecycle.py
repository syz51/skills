"""Real local lifecycle integration for every maintained skill.

Only model responses and the dependency-provisioning executable are fixtures;
scanning, note validation/writes, receipts, Git transport and releases are real.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import capture
import evidence
import read_guidance
import sync
import public_export


class SkillLifecycleTests(unittest.TestCase):
    def fixture(self, root, skill, read_mode='helper'):
        sessions = root / 'sessions'
        sessions.mkdir()
        config = {'sessions_root': str(sessions),
                  'archived_sessions_root': str(root / 'archive'),
                  'claude_projects_root': str(root / 'claude'),
                  'feedback_dir': str(root / 'local-feedback'),
                  'install_root': str(root / 'installed'), 'host_id': 'client-host'}
        folder = root / 'source/skills' / skill
        folder.mkdir(parents=True)
        entry = folder / 'SKILL.md'
        entry.write_text('---\nname: ' + skill + '\ndescription: Fixture\n---\n# Fixture\n')
        content = entry.read_bytes()
        envelope = read_guidance.read(entry)
        def record(kind, payload):
            return json.dumps({'type': kind, 'payload': payload,
                               'timestamp': '2026-10-10T01:00:00Z'}) + '\n'
        transcript = record('session_meta', {'id': 'session-' + skill, 'source': 'desktop'})
        transcript += record('event_msg', {'type': 'task_started', 'turn_id': 'work'})
        transcript += record('event_msg', {'type': 'user_message', 'message':
                             'Implement the requested adjustment. Keep the result scoped; a reported check was mistaken for proof.'})
        if read_mode == 'helper':
            tool, arguments = 'exec_command', {'cmd': 'uv run --script scripts/read_guidance.py ' + str(entry)}
            result = {'exit_code': 0, 'output': json.dumps(envelope)}
        else:
            tool = 'mcp__node_repl__js'
            arguments = {'code': "var fs = await import('node:fs/promises'); nodeRepl.write(await fs.readFile(" + json.dumps(str(entry)) + ", 'utf8'));"}
            result = {'content': [{'type': 'text', 'text': content.decode()}], 'isError': False}
        transcript += record('response_item', {'type': 'function_call', 'call_id': 'read',
            'name': tool, 'arguments': json.dumps(arguments)})
        transcript += record('response_item', {'type': 'function_call_output', 'call_id': 'read', 'output': json.dumps(result)})
        transcript += record('response_item', {'type': 'message', 'role': 'assistant',
            'content': [{'text': 'The requested correction was applied. Execution was not independently observed.'}]})
        transcript += record('event_msg', {'type': 'task_complete', 'turn_id': 'work'})
        source = sessions / 'rollout.jsonl'
        source.write_text(transcript)
        lesson = {'skill': skill, 'title': 'Qualify reported evidence',
                  'task': 'Apply a requested adjustment.',
                  'observation': 'The user corrected an evidence claim.',
                  'evidence': 'A correction is present; execution remains unverified.',
                  'cause': 'An attribution boundary needed clarification.',
                  'proposal': 'Distinguish observations from completion reports.'}
        return config, source, lesson, hashlib.sha256(content).hexdigest()

    def capture_and_check(self, root, skill, read_mode='helper'):
        config, source, lesson, loaded_sha = self.fixture(root, skill, read_mode)
        with patch.object(capture, 'model_response', return_value={'notes': [lesson]}) as model:
            result = capture.capture(config, root / 'capture-state')
            self.assertEqual((result['notes'], result['errors']), (1, 0))
            self.assertEqual(capture.capture(config, root / 'capture-state')['notes'], 0)
            model.assert_called_once()
        note = next(Path(config['feedback_dir']).glob(skill + '/notes/*.md'))
        original = note.read_bytes()
        turns = capture.completed_turns([source], capture.timestamp(capture.SINCE))
        self.assertEqual(turns[0]['verified_skills'], [skill])
        self.assertEqual(turns[0]['loaded_reads'][0]['sha256'], loaded_sha)
        with patch.object(evidence, 'model_response', return_value={
                'status': 'supported', 'summary': 'The user correction is faithfully attributed; execution is not verified.'}) as checker:
            result = evidence.verify(config, root / 'evidence-state')
            self.assertEqual((result['checked'], result['errors'], result['supported']), (1, 0, 1))
            self.assertEqual(evidence.verify(config, root / 'evidence-state')['checked'], 0)
            checker.assert_called_once()
        receipt = next(Path(config['feedback_dir']).glob(skill + '/verifications/*.json'))
        value = evidence.validate_receipt(json.loads(receipt.read_text()))
        self.assertEqual(value['skill'], skill)
        self.assertEqual(value['noteSha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(value['sourceSha256'], turns[0]['source_sha256'])
        self.assertEqual(value['loadedReads'][0]['sha256'], loaded_sha)
        self.assertEqual(note.read_bytes(), original)
        self.assertIn('runtime outcomes not replayed', value['scope'])
        return config, note, receipt, original

    def test_actual_helper_and_node_formats_bind_receipts_for_every_skill(self):
        for skill in read_guidance.SKILLS:
            for mode in ('helper', 'node'):
                with self.subTest(skill=skill, mode=mode), tempfile.TemporaryDirectory() as temporary:
                    self.capture_and_check(Path(temporary), skill, mode)

    def test_each_skill_flows_from_observed_read_to_reviewed_deployment(self):
        for skill in read_guidance.SKILLS:
            with self.subTest(skill=skill), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config, note, receipt, original = self.capture_and_check(root, skill)
                remote, repo = root / 'remote.git', root / 'repo'
                subprocess.run(['git', 'init', '--bare', '--initial-branch=main', str(remote)], check=True, capture_output=True)
                subprocess.run(['git', 'clone', str(remote), str(repo)], check=True, capture_output=True)
                for place in (remote, repo):
                    for option in ('gc.autoDetach', 'maintenance.autoDetach'):
                        sync.git(place, 'config', option, 'false')
                sync.git(repo, 'config', 'user.name', 'Fixture')
                sync.git(repo, 'config', 'user.email', 'fixture@example.invalid')
                for registered in read_guidance.SKILLS:
                    folder = repo / 'skills' / registered
                    (folder / 'references/nested').mkdir(parents=True)
                    (folder / 'SKILL.md').write_text('---\nname: ' + registered + '\ndescription: Fixture\n---\n# Fixture\n[Details](references/nested/guide.md)\n')
                    (folder / 'references/nested/guide.md').write_text('Original guidance.\n')
                def commit(message):
                    sync.git(repo, 'add', '.')
                    sync.git(repo, 'commit', '-m', message)
                commit('Initial skills')
                sync.git(repo, 'push', 'origin', 'main')
                validator = root / 'validator.py'
                validator.write_text('from pathlib import Path\nimport sys\nassert (Path(sys.argv[1])/"SKILL.md").is_file()\n')
                # Exercise the real validator process without package/network setup.
                binary = root / 'bin'
                binary.mkdir()
                uv = binary / 'uv'
                uv.write_text('#!' + sys.executable + '\nimport subprocess,sys\na=sys.argv[1:]\ni=a.index("--script")+1\nraise SystemExit(subprocess.run([a[a.index("--python")+1],*a[i:]]).returncode)\n')
                uv.chmod(0o755)
                args = SimpleNamespace(repo=repo, host_id='client-host',
                    feedback_dir=Path(config['feedback_dir']), install_root=root / 'installed',
                    validator=validator, bootstrap=True)
                with patch.dict(os.environ, {'PATH': str(binary) + os.pathsep + os.environ.get('PATH', '')}):
                    self.assertTrue(sync.publish(args))
                    self.assertTrue(sync.deploy(args))
                    previous = (args.install_root / 'current').resolve()
                    args.bootstrap = False
                    self.assertTrue(sync.upload(args))
                    self.assertTrue(sync.collect(args))
                    imported = next((repo / 'feedback' / skill / 'notes/client-host').glob('*.md'))
                    self.assertEqual(imported.read_bytes(), original)
                    imported_receipt = next((repo / 'feedback' / skill / 'verifications/client-host').glob('*.json'))
                    self.assertEqual(imported_receipt.read_bytes(), receipt.read_bytes())
                    self.assertEqual(evidence.assessment_inventory(repo / 'feedback')['latest'][0]['status'], 'supported')
                    entry_before = (repo / 'skills' / skill / 'SKILL.md').read_bytes()
                    reference = repo / 'skills' / skill / 'references/nested/guide.md'
                    reference.write_text('Keep observed and reported outcomes distinct.\n')
                    commit('Revise nested guidance')
                    with self.assertRaisesRegex(sync.SyncError, 'review record'):
                        sync.publish(args)
                    self.assertEqual((args.install_root / 'current').resolve(), previous)
                    review = repo / 'feedback' / skill / 'reviews/lifecycle.md'
                    review.parent.mkdir(parents=True)
                    review.write_text('Reviewed synthetic correction and exact note/receipt hashes.\n')
                    commit('Review the guidance change')
                    self.assertTrue(sync.publish(args))
                    self.assertTrue(sync.deploy(args))
                    current = args.install_root / 'current'
                    self.assertEqual((current / 'skills' / skill / 'references/nested/guide.md').read_bytes(), reference.read_bytes())
                    self.assertEqual((current / 'skills' / skill / 'SKILL.md').read_bytes(), entry_before)
                    self.assertEqual((previous / 'skills' / skill / 'references/nested/guide.md').read_text(), 'Original guidance.\n')
                    self.assertEqual((args.feedback_dir / skill / 'reviews/lifecycle.md').read_bytes(), review.read_bytes())
                    config = {
                        'schema_version': 1, 'source_repo': str(repo),
                        'target_url': 'https://github.com/example/skills.git',
                        'state_dir': str(root / 'public-state'),
                        'author_name': 'Fixture', 'author_email': 'fixture@example.invalid',
                        'files': [{'source': 'skills/' + skill + '/' + name, 'target': 'skills/' + skill + '/' + name}
                                  for name in ('SKILL.md', 'references/nested/guide.md')],
                    }
                    public_export.configuration(config)
                    published = public_export.snapshot(config, sync.git(repo, 'rev-parse', 'HEAD').decode().strip())
                    self.assertEqual(published['skills/' + skill + '/SKILL.md'], entry_before)
                    self.assertEqual(published['skills/' + skill + '/references/nested/guide.md'], reference.read_bytes())
                    self.assertTrue(all(path.startswith('skills/' + skill + '/') for path in published))
                    private = {**config, 'files': [{'source': str(imported.relative_to(repo)), 'target': 'private.md'}]}
                    with self.assertRaises(public_export.ExportError):
                        public_export.configuration(private)

    def test_each_skill_rejects_receipt_rebound_to_changed_note(self):
        for skill in read_guidance.SKILLS:
            with self.subTest(skill=skill), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config, note, receipt, original = self.capture_and_check(root, skill)
                changed = original + b'- Proposal: A later unrelated change.\n'
                note.write_bytes(changed)
                with self.assertRaises(ValueError):
                    evidence.validate_receipt(json.loads(receipt.read_text()), hashlib.sha256(changed).hexdigest(), 'client-host', skill)
                with self.assertRaisesRegex(ValueError, 'no originating note'):
                    evidence.assessment_inventory(Path(config['feedback_dir']))

    def test_each_skill_missing_source_preserves_historical_receipt_without_replay(self):
        for skill in read_guidance.SKILLS:
            with self.subTest(skill=skill), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config, note, receipt, original = self.capture_and_check(root, skill)
                retained = receipt.read_bytes()
                next(Path(config['sessions_root']).glob('*.jsonl')).unlink()
                with patch.object(evidence, 'model_response') as checker:
                    result = evidence.verify(config, root / 'evidence-state', recheck=True)
                    checker.assert_not_called()
                self.assertEqual((result['unavailable'], result['checked'], result['errors']), (1, 0, 0))
                self.assertEqual(receipt.read_bytes(), retained)
                self.assertEqual(note.read_bytes(), original)
