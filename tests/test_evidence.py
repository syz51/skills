from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import evidence


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.feedback = self.root / 'feedback'
        self.config = {'feedback_dir': str(self.feedback), 'sessions_root': str(self.root / 'sessions'),
                       'install_root': str(self.root / 'install'),
                       'archived_sessions_root': str(self.root / 'archive'), 'host_id': 'client-host',
                       'claude_projects_root': str(self.root / 'claude-projects')}
        self.path = self.feedback / 'frontend-ui/notes/note.md'
        self.path.parent.mkdir(parents=True)
        self.path.write_text('# Status correction\n- Thread: session1\n- Turn: turn1\n- Capture evidence: ' + 'a' * 64 +
                             '\n- Observation: Observed: the user requested a stable status position.\n- Evidence: The fix was reported.\n')
        self.turn = {'thread': 'session1', 'turn': 'turn1', 'evidence_id': 'a' * 64,
            'completed_at': '2026-10-14T01:00:00Z', 'source_sha256': 'b' * 64, 'loaded_reads': [],
            'messages': [{'role': 'user', 'text': 'Keep the status position steady.'},
                         {'role': 'assistant', 'text': 'Updated the layout. Checks passed.'}]}
        self.correction = {'skill': 'frontend-ui', 'title': 'Keep reported checks qualified',
            'task': 'Review a reported interface change.', 'observation': 'A layout revision was reported.',
            'evidence': 'The completion describes checks without independent runtime evidence.',
            'cause': 'The installed result remains unknown.', 'proposal': 'Distinguish reported checks from observed behavior.'}

    def test_automatic_correction_is_source_checked_and_not_repeated(self):
        original = self.path.read_bytes()
        responses = [{'status': 'partial', 'summary': 'The observation overstates the reported result.'},
                     {'notes': [self.correction]},
                     {'status': 'supported', 'summary': 'The revised attribution preserves uncertainty.'}]
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(evidence, 'model_response', side_effect=responses) as model:
            first = evidence.verify(self.config, self.root)
            second = evidence.verify(self.config, self.root)
        self.assertEqual(first['corrected'], 1)
        self.assertEqual(second['checked'], 0)
        self.assertEqual(model.call_count, 3)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list((self.feedback / 'frontend-ui/notes').glob('*.md')), [self.path])
        report = evidence.assessment_inventory(self.feedback)
        value = report['latest'][0]
        self.assertEqual((report['corrected'], report['unresolved']), (1, 0))
        self.assertEqual(value['status'], 'partial')
        self.assertEqual(value['correction']['note'], self.correction)
        checked = json.loads(model.call_args_list[2].args[3])
        self.assertNotIn('assessment', checked)
        self.assertEqual(checked['correction_of'], original.decode())
        self.assertIn(self.correction['observation'], checked['note'])

    def test_original_turn_assessment_is_immutable_and_not_repeated(self):
        result = {'status': 'supported', 'summary': 'The correction is present; execution success is only reported.'}
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]) as scanner, patch.object(evidence, 'model_response', return_value=result) as model:
            first = evidence.verify(self.config, self.root)
            second = evidence.verify(self.config, self.root)
        self.assertEqual(first['checked'], 1)
        self.assertEqual(second['checked'], 0)
        model.assert_called_once()
        scanner.assert_called_once()
        request = json.loads(model.call_args.args[3])
        self.assertEqual([{k: m[k] for k in ('role', 'text')} for m in request['original_turn']['messages']], self.turn['messages'])
        self.assertFalse(request['original_turn']['truncated'])
        receipt = next(self.feedback.rglob('*.json'))
        value = evidence.validate_receipt(json.loads(receipt.read_text()))
        self.assertEqual(value['noteSha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(value['sourceSha256'], self.turn['source_sha256'])
        self.assertIn('runtime outcomes not replayed', value['scope'])
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)

    def test_note_text_and_receipt_hash_use_one_atomic_byte_snapshot(self):
        original = self.path.read_bytes()
        changed = original + b'- Proposal: Keep the reported outcome qualified.\n'
        original_sha = hashlib.sha256(original).hexdigest()
        changed_sha = hashlib.sha256(changed).hexdigest()
        read_text, read_bytes = Path.read_text, Path.read_bytes
        replaced = False
        def replace_after_read(path, value):
            nonlocal replaced
            if path == self.path and not replaced:
                replacement = self.path.with_suffix('.replacement')
                replacement.write_bytes(changed)
                os.replace(replacement, self.path)
                replaced = True
            return value
        def text_snapshot(path, *args, **kwargs):
            return replace_after_read(path, read_text(path, *args, **kwargs))
        def byte_snapshot(path, *args, **kwargs):
            return replace_after_read(path, read_bytes(path, *args, **kwargs))
        result = {'status': 'supported', 'summary': 'The correction is present; execution remains reported.'}
        with patch.object(Path, 'read_text', text_snapshot), patch.object(Path, 'read_bytes', byte_snapshot), \
                patch.object(evidence, 'completed_turns', return_value=[self.turn]), \
                patch.object(evidence, 'model_response', return_value=result) as model:
            self.assertEqual(evidence.verify(self.config, self.root)['checked'], 1)
            first = list((self.feedback / 'frontend-ui/verifications').glob('*.json'))
            self.assertEqual(len(first), 1)
            self.assertEqual(json.loads(read_text(first[0]))['noteSha256'], original_sha)
            self.assertEqual(json.loads(model.call_args.args[3])['note'], original.decode())
            self.assertEqual(evidence.verify(self.config, self.root)['checked'], 1)
        self.assertTrue(replaced)
        self.assertEqual(model.call_count, 2)
        self.assertEqual({json.loads(read_text(path))['noteSha256'] for path in
                          (self.feedback / 'frontend-ui/verifications').glob('*.json')},
                         {original_sha, changed_sha})

    def test_note_snapshot_preserves_raw_hash_and_universal_newlines(self):
        original = self.path.read_bytes()
        raw = original.replace(b'\n', b'\r\n')
        self.path.write_bytes(raw)
        result = {'status': 'supported', 'summary': 'The correction remains attributed to the source.'}
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), \
                patch.object(evidence, 'model_response', return_value=result) as model:
            self.assertEqual(evidence.verify(self.config, self.root)['checked'], 1)
        self.assertEqual(json.loads(model.call_args.args[3])['note'], original.decode())
        receipt = next((self.feedback / 'frontend-ui/verifications').glob('*.json'))
        self.assertEqual(json.loads(receipt.read_text())['noteSha256'], hashlib.sha256(raw).hexdigest())

    def test_full_chronological_messages_and_explicit_bounded_fallback(self):
        messages = [{'role': 'assistant', 'text': 'First reply'},
                    {'role': 'user', 'text': 'x' * 3100 + ' Essential correction'},
                    {'role': 'assistant', 'text': 'Second reply'}]
        turn = {**self.turn, 'messages': messages}
        result = {'status': 'supported', 'summary': 'The assessment preserves attribution and uncertainty.'}
        with patch.object(evidence, 'model_response', return_value=result) as model:
            full = evidence.verify_note(self.config, self.root, 'Abstract note', turn, 'frontend-ui')
            request = json.loads(model.call_args.args[3])['original_turn']
            self.assertEqual([m['role'] for m in request['messages']], ['assistant', 'user', 'assistant'])
            self.assertTrue(request['messages'][1]['text'].endswith('Essential correction'))
            self.assertFalse(full['inputTruncated'])
            with patch.object(evidence, 'MAX_INPUT', 3000):
                bounded = evidence.verify_note(self.config, self.root, 'Abstract note', turn, 'frontend-ui')
            request = json.loads(model.call_args.args[3])['original_turn']
            self.assertTrue(bounded['inputTruncated'])
            self.assertGreater(request['messages'][1]['original_chars'], len(request['messages'][1]['text']))
            self.assertNotEqual(full['inputSha256'], bounded['inputSha256'])

    def test_source_check_preserves_long_turn_without_expanding_capture_bound(self):
        import capture
        turn = {**self.turn, 'messages': [
            {'role': 'assistant', 'text': 'Progress report. ' * 4000},
            {'role': 'user', 'text': 'Important final correction.'}]}
        result = {'status': 'supported', 'summary': 'Attribution is supported; execution remains unverified.'}
        with patch.object(evidence, 'model_response', return_value=result) as model:
            value = evidence.verify_note(self.config, self.root, 'Abstract note', turn, 'frontend-ui')
        data = model.call_args.args[3]
        self.assertGreater(len(data), capture.MAX_INPUT)
        self.assertLessEqual(len(data), evidence.MAX_INPUT)
        self.assertEqual(capture.MAX_INPUT, 48000)
        self.assertFalse(value['inputTruncated'])
        self.assertEqual(json.loads(data)['original_turn']['messages'][0]['text'], turn['messages'][0]['text'])

    def test_report_only_notes_are_checked_and_bounded_backlog_drains(self):
        self.path.write_text(self.path.read_text().replace('Observed: the user requested a stable status position.', 'Reported technique with no keyword trigger.'))
        for index in range(10):
            self.path.with_name('note' + str(index) + '.md').write_text(self.path.read_text() + '- Task: distinct lesson ' + str(index) + '\n')
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(evidence, 'model_response',
                return_value={'status': 'supported', 'summary': 'Reported attribution is supported; execution is unverified.'}) as model:
            first = evidence.verify(self.config, self.root)
            second = evidence.verify(self.config, self.root)
        self.assertEqual((first['checked'], first['pending']), (10, 1))
        self.assertEqual((second['checked'], second['current'], second['pending']), (1, 11, 0))
        self.assertEqual(model.call_count, 11)

    def test_legacy_partial_is_preserved_and_superseded_in_normal_report(self):
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        directory = self.feedback / 'frontend-ui/verifications'
        directory.mkdir()
        legacy = {'schema_version': 1, 'noteSha256': digest, 'sourceMachine': 'client-host', 'skill': 'frontend-ui',
                  'thread': 'session1', 'turn': 'turn1', 'sourceSha256': 'b' * 64,
                  'completed_at': self.turn['completed_at'], 'checked_at': '2026-10-14T02:00:00Z', 'loadedReads': [],
                  'scope': 'original-turn messages; artifacts and runtime outcomes not replayed',
                  'status': 'partial', 'summary': 'The original excerpt lacked a material correction.'}
        original = directory / (digest + '.json')
        original.write_text(json.dumps(legacy))
        before = original.read_bytes()
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(evidence, 'model_response',
                return_value={'status': 'supported', 'summary': 'The complete original messages support the attribution.'}):
            self.assertEqual(evidence.verify(self.config, self.root)['checked'], 1)
        self.assertEqual(original.read_bytes(), before)
        report = evidence.assessment_inventory(self.feedback)
        self.assertEqual((report['assessments'], len(report['latest'])), (2, 1))
        current = report['latest'][0]
        self.assertEqual(current['status'], 'supported')
        self.assertEqual(current['supersedes'], hashlib.sha256(before).hexdigest())
        self.assertFalse(current['inputTruncated'])
        new = directory / Path(current['receipt']).name
        foreign_directory = directory / 'maintainer-host'
        foreign_directory.mkdir()
        foreign = foreign_directory / new.name
        foreign.write_bytes(new.read_bytes())
        with self.assertRaisesRegex(ValueError, 'identity'):
            evidence.assessment_inventory(self.feedback)
        foreign.unlink()
        with self.assertRaisesRegex(ValueError, 'content'):
            evidence.parse_receipt(new.name, new.read_bytes() + b' ')
        original.unlink()
        with self.assertRaisesRegex(ValueError, 'predecessor'):
            evidence.assessment_inventory(self.feedback)

    def test_reader_dependency_fingerprint_drift_and_receipt_invalidation(self):
        baseline = evidence.checker_fingerprint(self.config)
        original_path = Path(evidence.read_guidance.__file__)
        changed = original_path.read_text().replace('MAX_BYTES = 6000', 'MAX_BYTES = 6001')
        alternate_path = self.root / 'read_guidance.py'
        alternate_path.write_text(changed)
        spec = importlib.util.spec_from_file_location('changed_reader', alternate_path)
        alternate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(alternate)
        self.assertEqual(alternate.MAX_BYTES, 6001)
        with patch.object(evidence, 'read_guidance', alternate):
            updated = evidence.checker_fingerprint(self.config)
        self.assertNotEqual(updated, baseline)
        real_read = Path.read_bytes
        def drift(path):
            return changed.encode() if path == original_path else real_read(path)
        with patch.object(Path, 'read_bytes', drift):
            with self.assertRaisesRegex(ValueError, 'source changed'):
                evidence.checker_fingerprint(self.config)
        responses = [{'status': 'partial', 'summary': 'One attribution remains unresolved.'}, {'notes': []},
                     {'status': 'supported', 'summary': 'The user correction is faithfully attributed.'}]
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(evidence, 'model_response',
                side_effect=responses) as model:
            self.assertEqual(evidence.verify(self.config, self.root)['checked'], 1)
            previous = {p: p.read_bytes() for p in self.feedback.rglob('*.json')}
            with patch.object(evidence, 'read_guidance', alternate):
                self.assertEqual(evidence.verify(self.config, self.root)['checked'], 1)
                self.assertEqual(evidence.verify(self.config, self.root)['checked'], 0)
            self.assertEqual(model.call_count, 3)
            for path, data in previous.items():
                self.assertEqual(path.read_bytes(), data)

    def test_explicit_recheck_appends_without_repeating_on_normal_cycles(self):
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(evidence, 'model_response',
                side_effect=[{'status': 'partial', 'summary': 'One claim remains unverified.'}, {'notes': []}]):
            evidence.verify(self.config, self.root)
        before = {p.name: p.read_bytes() for p in self.feedback.rglob('*.json')}
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(evidence, 'model_response',
                return_value={'status': 'supported', 'summary': 'A later assessment resolves the attribution.'}) as model:
            self.assertEqual(evidence.verify(self.config, self.root, digest, recheck=True)['checked'], 1)
            self.assertEqual(evidence.verify(self.config, self.root)['checked'], 0)
            model.assert_called_once()
        for name, data in before.items():
            self.assertEqual(next(self.feedback.rglob(name)).read_bytes(), data)
        self.assertEqual(evidence.assessment_inventory(self.feedback)['latest'][0]['status'], 'supported')

    def test_missing_source_stays_unavailable_without_invented_receipt(self):
        with patch.object(evidence, 'completed_turns', return_value=[]), patch.object(evidence, 'model_response') as model:
            result = evidence.verify(self.config, self.root)
        self.assertEqual(result['unavailable'], 1)
        self.assertEqual(list(self.feedback.rglob('*.json')), [])
        model.assert_not_called()

    def manual_receipt(self):
        self.path.write_text('# Historical document review\n- Observation: The correction was reported; outcomes remain unverified.\n')
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        value = {'schema_version': 4, 'noteSha256': digest, 'sourceMachine': 'client-host', 'skill': 'frontend-ui',
            'thread': self.turn['thread'], 'turn': self.turn['turn'], 'sourceSha256': self.turn['source_sha256'],
            'completed_at': self.turn['completed_at'], 'checked_at': '2026-10-16T01:00:00Z', 'loadedReads': [],
            'scope': evidence.ORIGINAL_SCOPE, 'checkerSha256': 'c' * 64, 'inputSha256': 'd' * 64,
            'inputTruncated': False, 'supersedes': None, 'correction': None, 'evidenceSha256': 'e' * 64,
            'followUps': [], 'status': 'supported', 'summary': 'The historical report is faithfully attributed.'}
        data = (json.dumps(value) + '\n').encode()
        path = self.feedback / 'frontend-ui/verifications' / (digest + '--' + hashlib.sha256(data).hexdigest() + '.json')
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        return digest, path

    def test_checker_fingerprint_tracks_exact_selected_prompts(self):
        runtime = evidence.prompt_runtime
        source = self.root / 'prompt-source'
        runtime.copy_bundle(runtime.DEFAULT_ROOT, source)
        config = {**self.config, 'repo_path': str(source), 'install_root': str(self.root / 'managed')}
        original = evidence.checker_fingerprint(config)
        current = self.root / 'managed/current'
        runtime.copy_bundle(source, current)
        (current / 'manifest.json').write_text(json.dumps({'promptsSha256': runtime.bundle_hash(current)}))
        self.assertEqual(evidence.checker_fingerprint(config), original)
        path = source / runtime.PROMPT_PATHS['verify']
        path.write_text(path.read_text() + ' Source-only refinement.\n')
        self.assertEqual(evidence.checker_fingerprint(config), original)
        path = current / runtime.PROMPT_PATHS['verify']
        path.write_text(path.read_text() + ' Approved verifier refinement.\n')
        (current / 'manifest.json').write_text(json.dumps({'promptsSha256': runtime.bundle_hash(current)}))
        self.assertNotEqual(evidence.checker_fingerprint(config), original)

    def test_recovered_manual_receipt_links_source_before_capture_cutoff(self):
        self.turn['completed_at'] = '2026-10-08T09:00:00Z'
        digest, receipt = self.manual_receipt()
        original = self.path.read_bytes(), receipt.read_bytes()
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]) as scanner, patch.object(evidence, 'model_response',
                return_value={'status': 'supported', 'summary': 'The original report supports this historical attribution.'}):
            counts = evidence.verify(self.config, self.root, digest, recheck=True)
        self.assertEqual((counts['checked'], counts['unavailable']), (1, 0))
        self.assertEqual(scanner.call_args.args[1], evidence.timestamp(self.turn['completed_at']))
        self.assertEqual((self.path.read_bytes(), receipt.read_bytes()), original)

    def test_duplicate_manual_note_uses_settled_source_host_assessment(self):
        digest, receipt = self.manual_receipt()
        canonical = self.root / 'repository/feedback/frontend-ui'
        (canonical / 'notes/client-host').mkdir(parents=True)
        (canonical / 'notes/client-host/note.md').write_bytes(self.path.read_bytes())
        target = canonical / 'verifications/client-host' / receipt.name
        target.parent.mkdir(parents=True)
        receipt.rename(target)
        evidence.prompt_runtime.copy_bundle(evidence.prompt_runtime.DEFAULT_ROOT, self.root / 'repository')
        config = {**self.config, 'host_id': 'maintainer-host', 'repo_path': str(self.root / 'repository')}
        with patch.object(evidence, 'completed_turns', return_value=[]) as scanner, patch.object(evidence, 'model_response') as model:
            counts = evidence.verify(config, self.root)
            self.assertEqual((counts['current'], counts['checked'], counts['unavailable']), (1, 0, 0))
            scanner.assert_not_called()
            model.assert_not_called()
            self.assertEqual(list(self.feedback.rglob('*.json')), [])
            # An explicit source-host recheck must not be replaced by a cached assessment.
            self.assertEqual(evidence.verify(config, self.root, digest, recheck=True)['unavailable'], 1)
            self.path.write_text(self.path.read_text() + '- Task: A different case.\n')
            self.assertEqual(evidence.verify(config, self.root)['unavailable'], 1)

    def test_duplicate_manual_note_does_not_borrow_unresolved_or_invalid_assessment(self):
        _, receipt = self.manual_receipt()
        canonical = self.root / 'repository/feedback/frontend-ui'
        (canonical / 'notes/client-host').mkdir(parents=True)
        (canonical / 'notes/client-host/note.md').write_bytes(self.path.read_bytes())
        value = json.loads(receipt.read_text())
        value.update(status='partial')
        data = (json.dumps(value) + '\n').encode()
        target = canonical / 'verifications/client-host' / (value['noteSha256'] + '--' + hashlib.sha256(data).hexdigest() + '.json')
        target.parent.mkdir(parents=True)
        target.write_bytes(data)
        receipt.unlink()
        evidence.prompt_runtime.copy_bundle(evidence.prompt_runtime.DEFAULT_ROOT, self.root / 'repository')
        config = {**self.config, 'host_id': 'maintainer-host', 'repo_path': str(self.root / 'repository')}
        with patch.object(evidence, 'completed_turns', return_value=[]), patch.object(evidence, 'model_response') as model:
            self.assertEqual(evidence.verify(config, self.root)['unavailable'], 1)
            model.assert_not_called()
        target.write_bytes(data + b' ')
        with self.assertRaisesRegex(ValueError, 'content'):
            evidence.verify(config, self.root)

    def test_source_checks_follow_the_migrated_codex_home(self):
        home = self.root / 'home'
        canonical = home / '.config/codex'
        notes = canonical / 'skill-feedback/frontend-ui/notes'
        notes.mkdir(parents=True)
        (home / '.codex').symlink_to(canonical)
        (notes / self.path.name).write_bytes(self.path.read_bytes())
        for name in ('sessions', 'archived_sessions'):
            (canonical / name).mkdir()
            (canonical / name / 'source.jsonl').write_text('{}\n')
        config = {'host_id': 'client-host'}
        result = {'status': 'supported', 'summary': 'Attribution is supported; execution remains unverified.'}
        with patch.object(Path, 'home', return_value=home), patch.dict(os.environ, {'CODEX_HOME': '', 'CLAUDE_CONFIG_DIR': ''}), patch.object(evidence, 'completed_turns', return_value=[self.turn]) as scanner, patch.object(evidence, 'model_response', return_value=result):
            self.assertEqual(evidence.verify(config, self.root)['checked'], 1)
        self.assertEqual(set(scanner.call_args.args[0]), {canonical.resolve() / name / 'source.jsonl' for name in ('sessions', 'archived_sessions')})
        self.assertEqual(len(list((canonical / 'skill-feedback/frontend-ui/verifications').glob('*.json'))), 1)

    def test_unsupported_feedback_is_preserved_and_private_summary_is_rejected(self):
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(evidence, 'model_response',
                side_effect=[{'status': 'unsupported', 'summary': 'The stated correction does not match the original request.'}, {'notes': []}]):
            evidence.verify(self.config, self.root)
        value = json.loads(next(self.feedback.rglob('*.json')).read_text())
        self.assertEqual(value['status'], 'unsupported')
        with patch.object(evidence, 'model_response', return_value={'status': 'supported', 'summary': 'https://private.example/source'}):
            with self.assertRaises(ValueError):
                evidence.verify_note(self.config, self.root, self.path.read_text(), self.turn, 'frontend-ui')

    def test_unverified_correction_is_not_published_or_retried_hourly(self):
        responses = [{'status': 'unsupported', 'summary': 'The observation misstates the reported result.'},
                     {'notes': [self.correction]},
                     {'status': 'partial', 'summary': 'The candidate still infers an unsupported cause.'}]
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(evidence, 'model_response', side_effect=responses) as model:
            first = evidence.verify(self.config, self.root)
            second = evidence.verify(self.config, self.root)
        self.assertEqual((first['errors'], first['corrected'], first['unresolved']), (0, 0, 1))
        self.assertEqual(second['checked'], 0)
        self.assertEqual(model.call_count, 3)
        receipt = next(self.feedback.rglob('*.json'))
        self.assertIsNone(json.loads(receipt.read_text())['correction'])
        self.assertNotIn(self.correction['title'], receipt.read_text())

    def test_new_same_chat_confirmation_rechecks_unresolved_note_once(self):
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(evidence, 'model_response', side_effect=[
                {'status': 'partial', 'summary': 'The outcome is not established.'}, {'notes': []}]):
            evidence.verify(self.config, self.root)
        old = {p: p.read_bytes() for p in self.feedback.rglob('*.json')}
        follow = {**self.turn, 'turn': 'confirmation', 'completed_at': '2026-10-14T02:00:00Z',
                  'source_sha256': 'c' * 64, 'messages': [{'role': 'user', 'text': 'I checked the final version and the behavior matches.'}]}
        with patch.object(evidence, 'completed_turns', return_value=[self.turn, follow]), patch.object(evidence, 'model_response',
                return_value={'status': 'supported', 'summary': 'The later confirmation supplies the previously missing report.'}) as model:
            first = evidence.verify(self.config, self.root)
            second = evidence.verify(self.config, self.root)
        self.assertEqual((first['checked'], first['unresolved'], second['checked']), (1, 0, 0))
        model.assert_called_once()
        request = json.loads(model.call_args.args[3])
        self.assertEqual(request['follow_ups'][0]['turn'], 'confirmation')
        for path, data in old.items():
            self.assertEqual(path.read_bytes(), data)

    def test_unrelated_chat_does_not_recheck_unresolved_note(self):
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(evidence, 'model_response', side_effect=[
                {'status': 'partial', 'summary': 'The result is not established.'}, {'notes': []}]):
            evidence.verify(self.config, self.root)
        other = {**self.turn, 'thread': 'different-chat', 'turn': 'later', 'completed_at': '2026-10-15T00:00:00Z'}
        with patch.object(evidence, 'completed_turns', return_value=[self.turn, other]), patch.object(evidence, 'model_response') as model:
            result = evidence.verify(self.config, self.root)
        self.assertEqual(result['checked'], 0)
        model.assert_not_called()

    def test_changed_original_evidence_rechecks_and_unchanged_evidence_does_not(self):
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(evidence, 'model_response', side_effect=[
                {'status': 'partial', 'summary': 'The result is not established.'}, {'notes': []}]):
            evidence.verify(self.config, self.root)
        restored = {**self.turn, 'source_sha256': 'd' * 64, 'messages': [*self.turn['messages'],
                    {'role': 'assistant', 'text': 'Additional saved context qualifies the earlier report.'}]}
        with patch.object(evidence, 'completed_turns', return_value=[restored]), patch.object(evidence, 'model_response', side_effect=[
                {'status': 'partial', 'summary': 'The added context is still insufficient.'}, {'notes': []}]) as model:
            first = evidence.verify(self.config, self.root)
            second = evidence.verify(self.config, self.root)
        self.assertEqual((first['checked'], second['checked']), (1, 0))
        self.assertEqual(model.call_count, 2)

    def test_restored_source_is_checked_after_being_unavailable(self):
        with patch.object(evidence, 'completed_turns', return_value=[]), patch.object(evidence, 'model_response') as model:
            missing = evidence.verify(self.config, self.root)
        model.assert_not_called()
        self.assertEqual(missing['unavailable'], 1)
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(evidence, 'model_response',
                return_value={'status': 'supported', 'summary': 'The restored messages support attribution.'}):
            self.assertEqual(evidence.verify(self.config, self.root)['checked'], 1)

    def test_long_followups_stay_bounded_and_unresolved(self):
        later = [{**self.turn, 'turn': f'later{i}', 'completed_at': f'2026-10-15T{i:02d}:00:00Z',
                  'messages': [{'role': 'user', 'text': 'detail ' * 5000}]} for i in range(10)]
        turn = {**self.turn, 'follow_ups': later}
        with patch.object(evidence, 'MAX_INPUT', 4000):
            data, truncated = evidence.source_input('Abstract note', turn)
        self.assertTrue(truncated)
        self.assertLessEqual(len(data), 4000)
        self.assertGreater(json.loads(data)['omitted_follow_up_turns'], 0)

    def test_followup_private_content_cannot_leak_into_corrections(self):
        turn = {**self.turn, 'follow_ups': [{**self.turn, 'messages': [
            {'role': 'user', 'text': 'Project Confidential Client holds sensitive plans for its unreleased product.'}]}]}
        unsafe = {**self.correction, 'observation': 'Confidential Client needs careful treatment.'}
        with patch.object(evidence, 'model_response', return_value={'notes': [unsafe]}):
            with self.assertRaises(ValueError):
                evidence.correct_note(self.config, self.root, 'Abstract note', turn, 'frontend-ui',
                                      {'status': 'partial', 'summary': 'The result is unverified.'})

    def test_followup_receipt_rejects_foreign_chat_and_invalid_chronology(self):
        follow = {**self.turn, 'turn': 'later', 'source_sha256': 'c' * 64, 'completed_at': '2026-10-15T00:00:00Z'}
        with patch.object(evidence, 'completed_turns', return_value=[self.turn, follow]), patch.object(evidence, 'model_response',
                return_value={'status': 'supported', 'summary': 'The reports are faithfully attributed.'}):
            evidence.verify(self.config, self.root)
        value = json.loads(next(self.feedback.rglob('*.json')).read_text())
        self.assertEqual(value['schema_version'], 4)
        for bad in ({'thread': 'different-chat'}, {'turn': self.turn['turn']}, {'sourceSha256': 'invalid'},
                    {'completed_at': '2026-10-13T00:00:00Z'}):
            with self.assertRaises(ValueError):
                evidence.validate_receipt({**value, 'followUps': [{**value['followUps'][0], **bad}]})

    def test_bounded_source_cannot_generate_a_correction(self):
        turn = {**self.turn, 'messages': [{'role': 'user', 'text': 'x' * 5000}]}
        with patch.object(evidence, 'completed_turns', return_value=[turn]), patch.object(evidence, 'MAX_INPUT', 3000), patch.object(
                evidence, 'model_response', return_value={'status': 'partial', 'summary': 'The input is incomplete.'}) as model:
            result = evidence.verify(self.config, self.root)
        model.assert_called_once()
        self.assertEqual(result['unresolved'], 1)
        self.assertIsNone(json.loads(next(self.feedback.rglob('*.json')).read_text())['correction'])

    def test_old_partial_is_corrected_without_modifying_its_receipt(self):
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(evidence, 'model_response',
                side_effect=[{'status': 'partial', 'summary': 'The result is overstated.'}, {'notes': []}]):
            evidence.verify(self.config, self.root)
        previous = next(self.feedback.rglob('*.json'))
        old = json.loads(previous.read_text())
        old['checkerSha256'] = 'c' * 64
        previous.unlink()
        data = (json.dumps(old) + '\n').encode()
        previous = previous.with_name(old['noteSha256'] + '--' + hashlib.sha256(data).hexdigest() + '.json')
        previous.write_bytes(data)
        responses = [{'status': 'partial', 'summary': 'The result remains overstated.'}, {'notes': [self.correction]},
                     {'status': 'supported', 'summary': 'The correction preserves the reported limit.'}]
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(evidence, 'model_response', side_effect=responses):
            result = evidence.verify(self.config, self.root)
        self.assertEqual(result['corrected'], 1)
        self.assertEqual(previous.read_bytes(), data)
        value = evidence.assessment_inventory(self.feedback)['latest'][0]
        self.assertEqual(value['supersedes'], hashlib.sha256(data).hexdigest())

    def test_correction_failures_preserve_history_and_fail_the_job(self):
        for failure in (RuntimeError('Service unavailable'), {'notes': [{**self.correction, 'evidence': 'https://private.example/text'}]}):
            with self.subTest(failure=failure), patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(
                    evidence, 'model_response', side_effect=[{'status': 'partial', 'summary': 'The result is overstated.'}, failure]):
                result = evidence.verify(self.config, self.root)
            self.assertEqual(result['errors'], 1)
            self.assertEqual(list(self.feedback.rglob('*.json')), [])

    def test_budget_exhaustion_during_correction_keeps_whole_assessment_pending(self):
        partial = {'status': 'partial', 'summary': 'The result remains a reported attribution.'}
        for interrupted_stage in (1, 2):
            responses = [partial, {'notes': [self.correction]}][:interrupted_stage]
            responses.append(evidence.CaptureBudgetExpired('Budget exhausted'))
            with self.subTest(stage=interrupted_stage), patch.object(evidence, 'completed_turns', return_value=[self.turn]), \
                    patch.object(evidence, 'model_response', side_effect=responses):
                first = evidence.verify(self.config, self.root)
            self.assertEqual((first['checked'], first['pending'], first['errors']), (0, 1, 0))
            self.assertEqual(list(self.feedback.rglob('*.json')), [])
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), \
                patch.object(evidence, 'model_response', side_effect=[partial, {'notes': [self.correction]},
                    {'status': 'supported', 'summary': 'The revised note keeps attribution qualified.'}]) as model:
            second = evidence.verify(self.config, self.root)
        self.assertEqual((second['checked'], second['corrected'], second['pending']), (1, 1, 0))
        deadlines = [call.kwargs['deadline'] for call in model.call_args_list]
        self.assertEqual(len(deadlines), 3)
        self.assertTrue(all(deadline == deadlines[0] for deadline in deadlines))

    def test_cycle_deadline_includes_source_scan_and_starts_no_assessment_afterward(self):
        clock = [0]
        def scan(*args, **kwargs):
            clock[0] = evidence.CYCLE_TIMEOUT
            return [self.turn]
        with patch.object(evidence.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(evidence, 'completed_turns', side_effect=scan), patch.object(evidence, 'model_response') as model:
            counts = evidence.verify(self.config, self.root)
        model.assert_not_called()
        self.assertEqual((counts['checked'], counts['pending'], counts['errors']), (0, 1, 0))
        self.assertEqual(list(self.feedback.rglob('*.json')), [])

    def test_extractor_failure_keeps_assessment_pending_and_reports_safe_metadata(self):
        diagnostic = {'elapsed_seconds': 2.5, 'phase': 'startup', 'failure_class': 'authentication', 'attempts': 1}
        error = evidence.ExtractorFailure('extractor authentication failure', diagnostic)
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), \
                patch.object(evidence, 'model_response', side_effect=error):
            counts = evidence.verify(self.config, self.root)
        self.assertEqual((counts['checked'], counts['pending'], counts['errors']), (0, 1, 1))
        self.assertEqual(counts['extractor_diagnostics'], [diagnostic])
        self.assertEqual(list(self.feedback.rglob('*.json')), [])

    def test_legacy_v2_partial_is_corrected_automatically(self):
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(evidence, 'model_response',
                side_effect=[{'status': 'partial', 'summary': 'The result is overstated.'}, {'notes': []}]):
            evidence.verify(self.config, self.root)
        previous = next(self.feedback.rglob('*.json'))
        old = json.loads(previous.read_text())
        old.update(schema_version=2)
        old.pop('correction')
        old.pop('evidenceSha256')
        old.pop('followUps')
        previous.unlink()
        data = (json.dumps(old) + '\n').encode()
        previous = previous.with_name(old['noteSha256'] + '--' + hashlib.sha256(data).hexdigest() + '.json')
        previous.write_bytes(data)
        responses = [{'status': 'partial', 'summary': 'The result remains overstated.'}, {'notes': [self.correction]},
                     {'status': 'supported', 'summary': 'The correction preserves uncertainty.'}]
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(evidence, 'model_response', side_effect=responses):
            result = evidence.verify(self.config, self.root)
        self.assertEqual(result['corrected'], 1)
        self.assertEqual(previous.read_bytes(), data)

    def test_correction_receipt_rejects_wrong_skill_bounded_or_unverified_content(self):
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(evidence, 'model_response', side_effect=[
                {'status': 'partial', 'summary': 'The result is overstated.'}, {'notes': [self.correction]},
                {'status': 'supported', 'summary': 'The correction preserves uncertainty.'}]):
            evidence.verify(self.config, self.root)
        value = json.loads(next(self.feedback.rglob('*.json')).read_text())
        for candidate in [dict(value, inputTruncated=True), dict(value, status='supported'),
                dict(value, correction={**value['correction'], 'status': 'partial'}),
                dict(value, correction={**value['correction'], 'note': {**self.correction, 'skill': 'content-research-writer'}}),
                dict(value, correction={**value['correction'], 'generationInputSha256': 'invalid'})]:
            with self.assertRaises(ValueError):
                evidence.validate_receipt(candidate)

    def test_foreign_or_unsafe_read_provenance_is_rejected(self):
        with patch.object(evidence, 'completed_turns', return_value=[self.turn]), patch.object(evidence, 'model_response',
                return_value={'status': 'supported', 'summary': 'Attribution is supported; execution remains unverified.'}):
            evidence.verify(self.config, self.root)
        value = json.loads(next(self.feedback.rglob('*.json')).read_text())
        for changed in [dict(value, sourceMachine='other'), dict(value, noteSha256='invalid'),
                        dict(value, loadedReads=[{'path': '../private', 'sha256': 'a' * 64, 'coverage': 'complete', 'read_turn': 'turn1'}])]:
            with self.assertRaises(ValueError):
                evidence.validate_receipt(changed)


if __name__ == '__main__':
    unittest.main()
