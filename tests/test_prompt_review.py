import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import prompt_review as review

SOURCE = Path(__file__).resolve().parents[1]
NOTE = {'skill': 'frontend-ui', 'title': 'Preserve observable uncertainty', 'task': 'Improve reusable guidance',
        'observation': 'A concrete correction exposed missing accessibility guidance',
        'evidence': 'The supplied correction supports a narrow instruction update',
        'cause': 'The original guidance omitted an interaction requirement',
        'proposal': 'Document the interaction requirement and retain verification limits'}


class PromptReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.remote = self.root / 'remote.git'
        subprocess.run(['git', 'init', '--bare', str(self.remote)], check=True, capture_output=True)
        for args in (('init', '-b', 'main'), ('config', 'user.name', 'Fixture'),
                     ('config', 'user.email', 'fixture@example.test')):
            review.git(self.repo, *args)
        shutil.copytree(SOURCE / 'prompts', self.repo / 'prompts')
        shutil.copytree(SOURCE / 'prompt-evals', self.repo / 'prompt-evals')
        shutil.copy(SOURCE / 'maintenance-prompt.md', self.repo)
        (self.repo / 'release.json').write_text('{"version": "unchanged"}\n')
        review.git(self.repo, 'add', '.')
        review.git(self.repo, 'commit', '-m', 'fixture')
        review.git(self.repo, 'remote', 'add', 'origin', str(self.remote))
        review.git(self.repo, 'push', '-u', 'origin', 'main')
        self.base = review.git(self.repo, 'rev-parse', 'HEAD')
        self.config = {'role': 'maintainer', 'host_id': 'maintainer-host', 'repo_path': str(self.repo)}
        self.due = next('2026-11-' + str(n).zfill(2) for n in range(1, 29)
                        if review.target_role(review.due_key('2026-11-' + str(n).zfill(2))) == 'extract')
        self.calls = []
        self.baseline_pass = False
        self.approval = True
        self.proposal_change = {}

    def responder(self, config, state, prompt, data, schema, *, deadline):
        self.assertGreater(deadline, time.monotonic())
        self.calls.append((prompt, json.loads(data), deadline))
        value = json.loads(data)
        if schema is review.PROPOSAL_SCHEMA:
            return {'action': 'candidate', 'text': value['current_text'] + '\nREFINED\n',
                    'base_hash': value['base_hash'], 'role': 'extract', 'new_case': None, **self.proposal_change}
        if schema is review.GRADE_SCHEMA:
            return {'grades': [{'item_id': item['item_id'], 'passed': True} for item in value['items']]}
        if schema is review.REVIEW_SCHEMA:
            return {'approved': self.approval, 'artifact_hash': value['artifact_hash']}
        source = value.get('turn', value)
        is_correction = 'keyboard focus' in json.dumps(source)
        if is_correction:
            return {'notes': [NOTE]}
        if not self.baseline_pass and 'REFINED' not in prompt and 'button spacing' in json.dumps(source):
            return {'notes': [NOTE]}
        return {'notes': []}

    def run_review(self):
        with patch.object(review.capture, 'model_response', side_effect=self.responder):
            return review.run(self.config, self.state, due=self.due)

    def test_promotion_publishes_only_validated_artifacts_and_cleans_main(self):
        result = self.run_review()
        self.assertEqual(result['outcome'], 'promoted')
        self.assertEqual(review.git(self.repo, 'rev-parse', 'HEAD'), result['commit'])
        changed = review.git(self.repo, 'diff', '--name-only', self.base, 'HEAD').splitlines()
        self.assertEqual(len(changed), 2)
        self.assertIn('prompts/extract.md', changed)
        self.assertTrue(any(p.startswith('prompt-reviews/') for p in changed))
        self.assertEqual(review.git(self.repo, 'status', '--porcelain'), '')
        self.assertEqual(len({call[2] for call in self.calls}), 1)
        self.assertEqual(len([c for c in self.calls if c[0] == review.REVIEW_PROMPT]), 2)
        self.assertEqual(self.run_review()['outcome'], 'already_reviewed')

    def test_all_pass_noop_records_measurement_without_proposer(self):
        self.baseline_pass = True
        result = self.run_review()
        self.assertEqual(result['outcome'], 'no_change')
        self.assertTrue(all(all(v) for v in result['baseline'].values()))
        self.assertEqual(len(self.calls), 7)
        self.assertEqual(review.git(self.repo, 'rev-parse', 'HEAD'), self.base)
        self.assertFalse((self.repo / 'prompt-reviews').exists())

    def test_wrong_base_role_and_unapproved_candidate_rejected(self):
        for changes in ({'base_hash': 'stale'}, {'role': 'maintain'}, {'text': '\x00bad'}):
            with self.subTest(changes=changes):
                self.proposal_change = changes
                result = self.run_review()
                self.assertEqual(result['outcome'], 'no_change')
                (self.state / 'prompt-review-state.json').unlink()
        self.proposal_change = {}
        self.approval = False
        self.assertEqual(self.run_review()['outcome'], 'no_change')
        self.assertEqual(review.git(self.repo, 'rev-parse', 'HEAD'), self.base)

    def test_gate_rejects_regression_inconsistency_and_critical_failure(self):
        case = {'role': 'extract', 'input': '{}', 'expected': 'empty', 'rubric': 'synthetic', 'critical': True}
        key = review.digest(case)
        self.assertTrue(review.improves({key: [False, False]}, {key: [True, True]}, [case]))
        self.assertFalse(review.improves({key: [False, True]}, {key: [True, True]}, [case]))
        self.assertFalse(review.improves({key: [False, False]}, {key: [True, False]}, [case]))
        self.assertFalse(review.improves({key: [True, False]}, {key: [False, True]}, [case]))
        self.assertFalse(review.improves({key: [True, True]}, {key: [True, True]}, [case]))

    def test_extraction_and_correction_respect_each_registered_skill(self):
        templates = review.load_cases(self.repo)
        for role in ('extract', 'correct'):
            for skill in review.capture.SKILLS:
                with self.subTest(role=role, skill=skill):
                    case = dict(next(c for c in templates if c['role'] == role and c['expected'] == 'note'))
                    source = json.loads(case['input'])
                    if role == 'extract':
                        source['verified_skills'] = [skill]
                    else:
                        source['skill'] = skill
                        source['note'] = dict(NOTE, skill=skill)
                    case['input'] = json.dumps(source)
                    review.validate_case(case)
                    path = self.repo / 'prompt-evals/cases' / (review.digest(case) + '.json')
                    path.write_bytes(review.encoded(case))
                    self.assertIn(case, review.load_cases(self.repo))
                    path.unlink()
                    output = {'notes': [dict(NOTE, skill=skill)]}
                    self.assertEqual(review.validate_output(case, output), output)
                    self.assertTrue(review.output_passes(case, output))
                    data = review.task_input(case)
                    self.assertEqual(data['turn']['verified_skills'] if role == 'extract' else data['skill'],
                                     [skill] if role == 'extract' else skill)
                    other = next(value for value in review.capture.SKILLS if value != skill)
                    wrong = {'notes': [dict(NOTE, skill=other)]}
                    with self.assertRaises(ValueError):
                        review.validate_output(case, wrong)
                    self.assertFalse(review.output_passes(case, wrong))

    def test_invalid_evaluation_scope_fails_closed(self):
        template = next(c for c in review.load_cases(self.repo) if c['role'] == 'extract')
        for scope in ('typescript', ['unknown'], ['typescript', 'typescript'], [None]):
            source = json.loads(template['input'])
            source['verified_skills'] = scope
            case = dict(template, input=json.dumps(source), expected='empty')
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                review.validate_case(case)
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                review.validate_output(case, {'notes': []})
            self.assertFalse(review.output_passes(case, {'notes': []}))

    def test_reader_source_drift_stops_prompt_review(self):
        original = Path.read_bytes
        for module in (review.read_guidance, review.capture.activation_trace):
            path = Path(module.__file__)
            def changed(value):
                data = original(value)
                return data + b'\n# changed parser\n' if value == path else data
            with self.subTest(module=module.__name__), patch.object(Path, 'read_bytes', changed), self.assertRaises(review.SourceChanged):
                review.assert_sources_current()

    def test_cases_are_immutable_hash_addressed_and_private_cases_rejected(self):
        cases = review.load_cases(self.repo)
        coverage = {'extract': {'empty', 'note'}, 'correct': {'empty', 'note'},
                    'verify': {'supported', 'partial', 'unsupported'},
                    'maintain': {'no_change', 'defer', 'change'}}
        for role, outcomes in coverage.items():
            self.assertEqual({case['expected'] for case in cases if case['role'] == role}, outcomes)
        case = dict(cases[0], input='https://private.example.test', rubric='invented')
        with self.assertRaises(ValueError):
            review.validate_case(case)
        path = next((self.repo / 'prompt-evals/cases').glob('*.json'))
        case = json.loads(path.read_text())
        case['expected'] = 'unsupported'
        path.write_text(json.dumps(case))
        with self.assertRaises(ValueError):
            review.load_cases(self.repo)

    def test_dirty_checkout_and_wrong_host_fail_without_settling_due(self):
        (self.repo / 'unrelated').write_text('preserve')
        with self.assertRaisesRegex(RuntimeError, 'due remains pending'):
            self.run_review()
        self.assertFalse((self.state / 'prompt-review-state.json').exists())
        (self.repo / 'unrelated').unlink()
        self.config['host_id'] = 'client-host'
        with self.assertRaises(RuntimeError):
            self.run_review()

    def test_shared_deadline_expiration_keeps_due_pending(self):
        def expired(*args, **kwargs):
            raise review.capture.CaptureBudgetExpired('private model output must not be logged')
        with patch.object(review.capture, 'model_response', side_effect=expired):
            with self.assertRaisesRegex(RuntimeError, 'due remains pending'):
                review.run(self.config, self.state, due=self.due)
        self.assertNotIn('private model', (self.state / 'prompt-review-status.json').read_text())
        self.assertFalse((self.state / 'prompt-review-state.json').exists())

    def test_retry_after_push_before_status_recognizes_record(self):
        original = review.save
        def interrupted(path, value):
            if path.name == 'prompt-review-state.json':
                raise OSError('interrupted')
            original(path, value)
        with patch.object(review, 'save', side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.run_review()
        self.calls.clear()
        result = self.run_review()
        self.assertEqual(result['outcome'], 'already_reviewed')
        self.assertTrue(result['commit'])
        self.assertEqual(self.calls, [])

    def advance_remote(self, filename='scripts/unrelated.py'):
        competitor = self.root / 'later-competitor'
        subprocess.run(['git', 'clone', '-b', 'main', str(self.remote), str(competitor)], check=True, capture_output=True)
        review.git(competitor, 'config', 'user.name', 'Fixture')
        review.git(competitor, 'config', 'user.email', 'fixture@example.test')
        path = competitor / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('source changed outside prompt review\n')
        review.git(competitor, 'add', '.')
        review.git(competitor, 'commit', '-m', 'unrelated remote source change')
        review.git(competitor, 'push', 'origin', 'main')
        return review.git(competitor, 'rev-parse', 'HEAD')

    def test_imported_source_mismatch_fails_before_fetch_or_model(self):
        original = review.git
        def no_fetch(root, *args, **kwargs):
            self.assertNotEqual(args[0], 'fetch')
            return original(root, *args, **kwargs)
        for module in (review, review.capture, review.prompt_runtime):
            with self.subTest(module=module.__name__):
                with patch.object(module, 'SOURCE_SHA256', '0' * 64), patch.object(review, 'git', side_effect=no_fetch):
                    with self.assertRaisesRegex(RuntimeError, 'fresh process is required'):
                        self.run_review()
                self.assertEqual(self.calls, [])
                self.assertFalse((self.state / 'prompt-review-state.json').exists())

    def test_remote_code_advance_is_rejected_without_advancing_local_main(self):
        remote = self.advance_remote()
        with self.assertRaises(RuntimeError):
            self.run_review()
        self.assertEqual(review.git(self.repo, 'rev-parse', 'HEAD'), self.base)
        self.assertEqual(review.git(self.repo, 'rev-parse', 'origin/main'), remote)
        self.assertFalse((self.repo / 'scripts/unrelated.py').exists())
        self.assertEqual(self.calls, [])
        self.assertFalse((self.state / 'prompt-review-state.json').exists())

    def test_retry_after_push_before_local_fast_forward_proves_single_review(self):
        original = review.git
        def interrupted(root, *args, **kwargs):
            if args and args[0] == 'merge':
                raise RuntimeError('interrupted before local fast-forward')
            return original(root, *args, **kwargs)
        with patch.object(review, 'git', side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.run_review()
        self.assertEqual(review.git(self.repo, 'rev-parse', 'HEAD'), self.base)
        pushed = review.git(self.repo, 'rev-parse', 'origin/main')
        self.assertNotEqual(pushed, self.base)
        self.calls.clear()
        result = self.run_review()
        self.assertEqual(result['outcome'], 'already_reviewed')
        self.assertEqual(result['commit'], pushed)
        self.assertEqual(review.git(self.repo, 'rev-parse', 'HEAD'), pushed)
        self.assertEqual(self.calls, [])
        record = json.loads(next((self.repo / 'prompt-reviews').glob('*.json')).read_text())
        self.assertEqual(record['evaluator_hash'], review.SOURCE_SHA256)
        self.assertEqual(record['runtime_policy_code_hash'], review.prompt_runtime.SOURCE_SHA256)
        self.assertEqual(record['capture_adapter_code_hash'], review.capture.SOURCE_SHA256)
        self.assertEqual(record['guidance_reader_code_hash'], review.read_guidance.SOURCE_SHA256)
        self.assertEqual(record['activation_trace_code_hash'], review.capture.activation_trace.SOURCE_SHA256)

    def test_recovery_rejects_content_addressed_tampered_proof_and_extra_scope(self):
        original = review.git
        def interrupted(root, *args, **kwargs):
            if args and args[0] == 'merge':
                raise RuntimeError('interrupt')
            return original(root, *args, **kwargs)
        with patch.object(review, 'git', side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.run_review()
        pushed = review.git(self.repo, 'rev-parse', 'origin/main')
        for index, fault in enumerate(('base_hash', 'approval_hashes', 'gain', 'scope')):
            with self.subTest(fault=fault):
                competitor = self.root / ('tampered-' + str(index))
                subprocess.run(['git', 'clone', '-b', 'main', str(self.remote), str(competitor)], check=True, capture_output=True)
                review.git(competitor, 'config', 'user.name', 'Fixture')
                review.git(competitor, 'config', 'user.email', 'fixture@example.test')
                path = next((competitor / 'prompt-reviews').glob('*.json'))
                audit = json.loads(path.read_text())
                path.unlink()
                if fault == 'base_hash':
                    audit['base_hash'] = '0' * 64
                elif fault == 'approval_hashes':
                    audit['approval_hashes'] = ['0' * 64]
                elif fault == 'gain':
                    audit['candidate'] = audit['baseline']
                else:
                    (competitor / 'unrelated').write_text('out of scope')
                (path.parent / (review.digest(audit) + '.json')).write_bytes(review.encoded(audit))
                review.git(competitor, 'add', '-A')
                review.git(competitor, 'commit', '--amend', '--no-edit')
                bad = review.git(competitor, 'rev-parse', 'HEAD')
                review.git(competitor, 'push', 'origin', 'HEAD:refs/heads/tampered-' + str(index))
                review.git(self.remote, 'update-ref', 'refs/heads/main', bad)
                self.calls.clear()
                with self.assertRaises(RuntimeError):
                    self.run_review()
                self.assertEqual(review.git(self.repo, 'rev-parse', 'HEAD'), self.base)
                self.assertEqual(self.calls, [])
                review.git(self.remote, 'update-ref', 'refs/heads/main', pushed)
        self.assertEqual(self.run_review()['outcome'], 'already_reviewed')

    def test_after_own_push_only_exact_own_commit_is_fast_forwarded(self):
        original = review.git
        did_push = False
        did_advance = False
        remote_commit = None
        def racing(root, *args, **kwargs):
            nonlocal did_push, did_advance, remote_commit
            if args and args[0] == 'fetch' and did_push and not did_advance:
                did_advance = True
                remote_commit = self.advance_remote()
            result = original(root, *args, **kwargs)
            if args and args[0] == 'push' and args[-1] == 'HEAD:refs/heads/main':
                did_push = True
            return result
        with patch.object(review, 'git', side_effect=racing):
            result = self.run_review()
        self.assertEqual(result['outcome'], 'promoted')
        self.assertEqual(review.git(self.repo, 'rev-parse', 'HEAD'), result['commit'])
        self.assertNotEqual(result['commit'], remote_commit)
        self.assertFalse((self.repo / 'scripts/unrelated.py').exists())
        self.assertEqual(review.git(self.repo, 'status', '--porcelain'), '')

    def test_verification_summary_source_quotation_is_rejected(self):
        case = next(c for c in review.load_cases(self.repo) if c['role'] == 'verify')
        source = json.loads(case['input'])
        quoted = source['messages'][-1]['text']
        with self.assertRaises(ValueError):
            review.validate_output(case, {'status': case['expected'], 'summary': quoted})

    def test_push_race_leaves_local_main_clean_and_pending(self):
        original = review.git
        def racing(root, *args, **kwargs):
            if args and args[0] == 'push':
                competitor = self.root / 'competitor'
                subprocess.run(['git', 'clone', '-b', 'main', str(self.remote), str(competitor)], check=True, capture_output=True)
                original(competitor, 'config', 'user.name', 'Fixture')
                original(competitor, 'config', 'user.email', 'fixture@example.test')
                (competitor / 'competing').write_text('concurrent remote change')
                original(competitor, 'add', 'competing')
                original(competitor, 'commit', '-m', 'concurrent publication')
                original(competitor, 'push', 'origin', 'main')
            return original(root, *args, **kwargs)
        with patch.object(review, 'git', side_effect=racing):
            with self.assertRaises(RuntimeError):
                self.run_review()
        self.assertEqual(review.git(self.repo, 'rev-parse', 'HEAD'), self.base)
        self.assertEqual(review.git(self.repo, 'status', '--porcelain'), '')
        self.assertFalse((self.state / 'prompt-review-state.json').exists())

    def test_missing_malformed_outputs_cannot_be_measured_as_gain(self):
        with patch.object(review.capture, 'model_response', return_value=None):
            with self.assertRaises(RuntimeError):
                review.run(self.config, self.state, due=self.due)
        self.assertEqual(review.git(self.repo, 'rev-parse', 'HEAD'), self.base)
        self.assertFalse((self.state / 'prompt-review-state.json').exists())

    def test_semantically_unrelated_valid_notes_and_bad_grade_ids_fail_closed(self):
        original = self.responder
        def semantic(config, state, prompt, data, schema, *, deadline):
            value = json.loads(data)
            if schema is review.GRADE_SCHEMA:
                return {'grades': [{'item_id': item['item_id'], 'passed': False} for item in value['items']]}
            return original(config, state, prompt, data, schema, deadline=deadline)
        with patch.object(review.capture, 'model_response', side_effect=semantic):
            result = review.run(self.config, self.state, due=self.due)
        self.assertEqual(result['outcome'], 'no_change')
        self.assertTrue(all(not any(v) for v in result['candidate'].values()))
        (self.state / 'prompt-review-state.json').unlink()
        def wrong_ids(config, state, prompt, data, schema, *, deadline):
            if schema is review.GRADE_SCHEMA:
                return {'grades': [{'item_id': 'wrong', 'passed': True} for _ in json.loads(data)['items']]}
            return original(config, state, prompt, data, schema, deadline=deadline)
        with patch.object(review.capture, 'model_response', side_effect=wrong_ids):
            with self.assertRaises(RuntimeError):
                review.run(self.config, self.state, due=self.due)
        self.assertEqual(review.git(self.repo, 'rev-parse', 'HEAD'), self.base)

    def test_task_envelopes_match_runtime_and_hide_labels(self):
        for case in review.load_cases(self.repo):
            data = review.task_input(case)
            self.assertNotIn('expected', data)
            self.assertNotIn('rubric', data)
            if case['role'] == 'extract':
                self.assertEqual(set(data), {'turn', 'existing_notes'})
                self.assertIn('source_index', data['turn']['messages'][0])
            elif case['role'] in ('verify', 'correct'):
                self.assertIn('original_turn', data)
                self.assertIn('messages', data['original_turn'])
                self.assertIsInstance(data['note'], str)
                if case['role'] == 'correct':
                    self.assertIn('assessment', data)
        self.assertTrue(any(c['expected'] == 'partial' for c in review.load_cases(self.repo)))

    def test_symlink_artifact_directories_rejected(self):
        shutil.rmtree(self.repo / 'prompt-evals/cases')
        (self.repo / 'prompt-evals/cases').symlink_to(SOURCE / 'prompt-evals/cases')
        with self.assertRaises(ValueError):
            review.load_cases(self.repo)
        (self.repo / 'prompt-reviews').symlink_to(self.state)
        with self.assertRaises(ValueError):
            review.published_record(self.repo, self.due, 'extract')

    def test_due_is_tokyo_monday(self):
        self.assertEqual(review.due_key('2026-10-25T16:00:00+00:00'), '2026-10-26')
        self.assertEqual(review.due_key('2026-10-28'), '2026-10-26')

    def test_regression_growth_cannot_exceed_evaluation_budget(self):
        case = next(c for c in review.load_cases(self.repo) if c['role'] == 'extract')
        count = sum(c['role'] == 'extract' for c in review.load_cases(self.repo))
        for index in range(review.MAX_CASES_PER_ROLE - count + 1):
            addition = {**case, 'rubric': case['rubric'] + ' Additional contrast ' + str(index)}
            path = self.repo / 'prompt-evals/cases' / (review.digest(addition) + '.json')
            path.write_bytes(review.encoded(addition))
        with self.assertRaisesRegex(ValueError, 'bounded case budget'):
            review.load_cases(self.repo)


if __name__ == '__main__':
    unittest.main()
