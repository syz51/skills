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

    def responder(self, config, state, prompt, data, schema, *, deadline, role=None, cache_prefix=None):
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
        # Model both supported guidance corrections; a new positive corpus case
        # must not become an unrelated candidate regression in this test double.
        is_correction = any(text in json.dumps(source) for text in
                            ('keyboard focus', 'accessible names for icon-only controls'))
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
        visible = [c for c in review.load_cases(self.repo) if c['role'] == 'extract']
        self.assertEqual(len(self.calls), 2 * len(visible) + 1)  # Two runs per case and one independent grade.
        self.assertEqual(review.git(self.repo, 'rev-parse', 'HEAD'), self.base)
        self.assertFalse((self.repo / 'prompt-reviews').exists())

    def test_holdouts_are_withheld_until_candidate_is_frozen_and_recorded(self):
        result = self.run_review()
        self.assertEqual(result['outcome'], 'promoted')
        holdouts = review.load_cases(self.repo, holdouts=True)
        proposal = next(data for _, data, _ in self.calls if 'current_text' in data)
        oracle = next(data for prompt, data, _ in self.calls if prompt == review.REVIEW_PROMPT)
        for case in holdouts:
            self.assertNotIn(case, proposal['cases'])
            self.assertNotIn(json.dumps(case, sort_keys=True), json.dumps(oracle, sort_keys=True))
            source = json.loads(case['input'])
            texts = [m['text'] for m in source.get('messages', [])] or [source.get('scenario', '')]
            for text in texts:
                self.assertNotIn(text, json.dumps(proposal))
                self.assertNotIn(text, json.dumps(oracle))
        self.assertNotIn('holdout_baseline', proposal)
        self.assertNotIn('holdout_baseline', oracle['artifact'])
        record = json.loads(next((self.repo / 'prompt-reviews').glob('*.json')).read_text())
        self.assertEqual(record['holdout_dataset_hash'], review.digest(holdouts))
        self.assertTrue(review.holdout_passes(record['holdout_baseline'], record['holdout_candidate'],
                                             [c for c in holdouts if c['role'] == 'extract']))

    def test_training_gain_cannot_hide_heldout_failure(self):
        original = review.evaluate
        def evaluate(config, state, root, role, text, cases, deadline):
            values = original(config, state, root, role, text, cases, deadline)
            if cases == review.load_cases(root, holdouts=True) and 'REFINED' in text:
                values[next(iter(values))] = [False, False]
            return values
        with patch.object(review, 'evaluate', side_effect=evaluate):
            result = self.run_review()
        self.assertEqual(result['reason'], 'held_out_regression')
        self.assertEqual(review.git(self.repo, 'rev-parse', 'HEAD'), self.base)
        self.assertEqual(len([c for c in self.calls if c[0] == review.REVIEW_PROMPT]), 1)
        events = review.attempt_events(self.state)
        terminal = next(v for v in events.values() if v['kind'] == 'terminal')
        self.assertEqual(terminal['reason'], 'held_out_regression')
        self.assertEqual(terminal['holdout_candidate'], result['holdout_candidate'])

    def manual_rejection(self):
        text = (self.repo / 'prompts/extract.md').read_text() + '\nREFINED\n'
        visible = [c for c in review.load_cases(self.repo) if c['role'] == 'extract']
        hidden = [c for c in review.load_cases(self.repo, holdouts=True) if c['role'] == 'extract']
        identity = review.register_rejected_candidate(self.repo, self.state, 'extract', text,
            'held_out_regression', baseline={review.digest(c): [False, False] for c in visible},
            candidate_results={review.digest(c): [True, True] for c in visible},
            holdout_baseline={review.digest(c): [True, True] for c in hidden},
            holdout_candidate={review.digest(c): [True, False] for c in hidden})
        return identity, visible, hidden

    def test_prior_rejection_feedback_excludes_every_heldout_field(self):
        identity, visible, hidden = self.manual_rejection()
        self.run_review()
        proposal = next(data for _, data, _ in self.calls if 'current_text' in data)
        feedback = proposal['prior_attempts']
        self.assertEqual(len(feedback['visible_lessons']), 1)
        lesson = feedback['visible_lessons'][0]
        self.assertIn('REFINED', lesson['candidate_text'])
        self.assertEqual(set(lesson['baseline']), {review.digest(c) for c in visible})
        serialized = json.dumps(feedback)
        for case in hidden:
            self.assertNotIn(review.digest(case), serialized)
            self.assertNotIn(case['rubric'], serialized)
            for message in json.loads(case['input']).get('messages', []):
                self.assertNotIn(message['text'], serialized)
        self.assertNotIn('holdout', serialized)
        self.assertNotIn('held_out_regression', serialized)
        self.assertNotIn('reason', serialized)
        self.assertEqual(len(review.attempt_events(self.state)), 2)

    def test_exact_rejected_candidate_skips_oracle_and_candidate_evaluation(self):
        self.manual_rejection()
        with patch.object(review, 'evaluate', wraps=review.evaluate) as evaluate:
            result = self.run_review()
        self.assertEqual(result['reason'], 'previously_rejected_candidate')
        self.assertEqual(evaluate.call_count, 1)  # Current visible baseline only.
        self.assertFalse(any(prompt == review.REVIEW_PROMPT for prompt, _, _ in self.calls))
        self.assertEqual(review.git(self.repo, 'rev-parse', 'HEAD'), self.base)

    def test_rehashed_terminal_cannot_forward_heldout_measurements_as_visible(self):
        _, _, hidden = self.manual_rejection()
        events = review.attempt_events(self.state)
        identity, terminal = next((h, v) for h, v in events.items() if v['kind'] == 'terminal')
        terminal['baseline'] = {review.digest(c): [True, False] for c in hidden}
        (self.state/'prompt-attempts'/(identity+'.json')).unlink()
        (self.state/'prompt-attempts'/(review.digest(terminal)+'.json')).write_bytes(review.encoded(terminal))
        with self.assertRaisesRegex(ValueError, 'measurements'):
            review.prior_attempt_feedback(self.repo, self.state, 'extract')

    def test_rehashed_case_inventories_cannot_relabel_heldouts_as_visible(self):
        identity, _, hidden = self.manual_rejection()
        events = review.attempt_events(self.state); pending = events[identity]
        terminal_id, terminal = next((h, v) for h, v in events.items() if v['kind'] == 'terminal')
        pending['visible_ids'] = [review.digest(c) for c in hidden]
        pending['holdout_ids'] = ['a'*64, 'b'*64]
        terminal['attempt'] = review.digest(pending)
        terminal['baseline'] = terminal['candidate'] = {h: [True, False] for h in pending['visible_ids']}
        terminal['holdout_baseline'] = terminal['holdout_candidate'] = None
        for old in (identity, terminal_id): (self.state/'prompt-attempts'/(old+'.json')).unlink()
        for value in (pending, terminal):
            (self.state/'prompt-attempts'/(review.digest(value)+'.json')).write_bytes(review.encoded(value))
        with self.assertRaisesRegex(ValueError, 'visible inventory mismatch'):
            review.prior_attempt_feedback(self.repo, self.state, 'extract')

    def test_cases_may_name_urls_emails_and_paths_but_stay_non_executable(self):
        template = next(c for c in review.load_cases(self.repo) if c['role'] == 'extract')
        identifiers = ' See https://example.com/a, a@example.com and /home/alice/notes; token=abc123.'
        review.validate_case(dict(template, rubric=template['rubric'] + identifiers))
        for unsafe in (' ```sh', ' $(id)', ' eval(x)', ' C:\\tmp', ' /etc/hosts'):
            with self.subTest(unsafe=unsafe), self.assertRaisesRegex(ValueError, 'Unsafe synthetic case'):
                review.validate_case(dict(template, rubric=template['rubric'] + unsafe))

    def test_attempt_candidates_may_name_urls_emails_and_paths_but_not_nul(self):
        base = (self.repo/'prompts/extract.md').read_text()
        candidate = base + '\nCompare https://example.com/a, a@example.com and /home/alice/notes.\n'
        identity = review.freeze_attempt(self.repo, self.state, 'extract', candidate)
        self.assertEqual(review.attempt_events(self.state)[identity]['candidate'], candidate)
        for unsafe in (base + '\x00', 'x' * 24001):
            with self.subTest(size=len(unsafe)), self.assertRaisesRegex(ValueError, 'Unsafe prompt attempt candidate'):
                review.freeze_attempt(self.repo, self.state, 'extract', unsafe)

    def test_interrupted_atomic_attempt_write_leaves_no_partial_event(self):
        candidate = (self.repo/'prompts/extract.md').read_text()+'\nClarify correction attribution.\n'
        with patch.object(review.capture.os, 'link', side_effect=OSError('interrupted before publication')):
            with self.assertRaises(OSError): review.freeze_attempt(self.repo, self.state, 'extract', candidate)
        self.assertEqual(review.attempt_events(self.state), {})
        self.assertEqual(list((self.state/'prompt-attempts').iterdir()), [])

    def test_model_decision_and_request_are_retained_locally(self):
        with patch.object(review.capture, 'model_response', return_value={'notes': []}):
            result = review.model(self.config, self.state, 'Synthetic check', {'case': 'abstract'}, review.capture.SCHEMA, time.monotonic()+30)
        self.assertEqual(result, {'notes': []})
        directory = self.state/'prompt-evaluation-calls'
        request = next(directory.glob('*.request.json')); response = next(directory.glob('*.response.json'))
        self.assertEqual(request.name.split('.')[0], response.name.split('.')[0])
        self.assertEqual(json.loads(response.read_text())['response'], result)
        self.assertEqual(json.loads(request.read_text())['evaluator_hash'], review.SOURCE_SHA256)
        self.assertEqual(json.loads(request.read_text())['backend'], 'cli')
        # The whole case input is the cache prefix, so its repeat run reads it from the cache.
        with patch.object(review.capture, 'model_response', return_value={'notes': []}) as response:
            review.model(self.config, self.state, 'Synthetic check 3', {'case': 'x'}, review.capture.SCHEMA, time.monotonic()+30)
        self.assertEqual(response.call_args.kwargs['cache_prefix'], len(response.call_args.args[3]))
        self.assertEqual(request.stat().st_mode & 0o777, 0o600)
        with patch.object(review.capture, 'model_response', return_value={'notes': []}):
            review.model({**self.config, 'api_key_file': '/unused'}, self.state, 'Synthetic check 2', {}, review.capture.SCHEMA,
                         time.monotonic()+30)
        backends = sorted(json.loads(path.read_text())['backend'] for path in directory.glob('*.request.json'))
        self.assertEqual(backends, ['api', 'cli', 'cli'])

    def test_model_failure_preserves_request_without_exception_secrets(self):
        with patch.object(review.capture, 'model_response', side_effect=RuntimeError('private credentials in diagnostic')):
            with self.assertRaises(RuntimeError):
                review.model(self.config, self.state, 'Synthetic check', {}, review.capture.SCHEMA, time.monotonic()+30)
        directory = self.state/'prompt-evaluation-calls'
        failure = json.loads(next(directory.glob('*.failure.json')).read_text())
        self.assertEqual(failure['failure_class'], 'RuntimeError')
        self.assertNotIn('credentials', json.dumps(failure))
        self.assertEqual(len(list(directory.glob('*.request.json'))), 1)
        self.assertFalse(list(directory.glob('*.response.json')))

    def test_manual_case_budget_failure_cannot_poison_ledger(self):
        visible = [c for c in review.load_cases(self.repo) if c['role'] == 'extract']
        case = dict(next(c for c in visible if c['expected'] == 'empty'))
        for index in range(review.MAX_CASES_PER_ROLE - len(visible)):
            extra = dict(case)
            data = json.loads(case['input']); data['messages'] = [{'role': 'user', 'text': 'Adjust ordinary spacing '+str(index)+'.'}]
            extra['input'] = json.dumps(data)
            (self.repo/'prompt-evals/cases'/(review.digest(extra)+'.json')).write_bytes(review.encoded(extra))
        data = json.loads(case['input']); data['messages'] = [{'role': 'user', 'text': 'Adjust ordinary text spacing.'}]
        case['input'] = json.dumps(data)
        candidate = (self.repo/'prompts/extract.md').read_text()+'\nClarify bounded attribution.\n'
        with self.assertRaisesRegex(ValueError, 'case inventory'):
            review.register_rejected_candidate(self.repo, self.state, 'extract', candidate, 'oracle_rejected', new_case=case)
        self.assertEqual(review.attempt_events(self.state), {})

    def test_changed_evaluation_context_can_reconsider_same_candidate(self):
        self.manual_rejection()
        with patch.object(review, 'MODEL', 'updated-fixed-evaluator'):
            result = self.run_review()
        self.assertEqual(result['outcome'], 'promoted')
        self.assertEqual(len(review.attempt_events(self.state)), 4)

    def test_historical_evaluator_identity_is_preserved_without_new_context_rejection(self):
        candidate = (self.repo / 'prompts/extract.md').read_text() + '\nREFINED\n'
        visible = [c for c in review.load_cases(self.repo) if c['role'] == 'extract']
        observed = 'a' * 64
        identity = review.register_rejected_candidate(self.repo, self.state, 'extract', candidate,
            'no_consistent_gain', baseline={review.digest(c): [False, False] for c in visible},
            candidate_results={review.digest(c): [True, False] for c in visible},
            observed_evaluator_hash=observed)
        self.assertEqual(review.attempt_events(self.state)[identity]['context']['evaluator_hash'], observed)
        feedback = review.prior_attempt_feedback(self.repo, self.state, 'extract')
        self.assertEqual(feedback['visible_lessons'][0]['observed_evaluator_hash'], observed)
        self.assertEqual(feedback['rejected_hashes'], [])
        self.assertFalse(review.previously_rejected(self.repo, self.state, 'extract', candidate))

    def test_oracle_rejection_is_immutable_local_evidence(self):
        self.approval = False
        self.run_review()
        events = review.attempt_events(self.state)
        terminal = next(v for v in events.values() if v['kind'] == 'terminal')
        self.assertEqual(terminal['reason'], 'oracle_rejected')
        self.assertIsNone(terminal['candidate'])
        path = self.state / 'prompt-attempts' / (review.digest(terminal) + '.json')
        path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            review.attempt_events(self.state)

    def test_interrupted_candidate_is_frozen_before_evaluation_and_retryable(self):
        evaluate = review.evaluate
        def interrupt(config, state, root, role, text, cases, deadline):
            if 'REFINED' in text:
                raise TimeoutError('evaluation interrupted')
            return evaluate(config, state, root, role, text, cases, deadline)
        with patch.object(review, 'evaluate', side_effect=interrupt):
            with self.assertRaises(RuntimeError):
                self.run_review()
        events = review.attempt_events(self.state)
        self.assertEqual(len(events), 1)
        pending = next(iter(events.values()))
        self.assertEqual(pending['kind'], 'pending')
        self.assertIn('REFINED', pending['candidate'])
        self.assertFalse(review.previously_rejected(self.repo, self.state, 'extract', pending['candidate']))
        self.assertEqual(self.run_review()['outcome'], 'promoted')

    def test_attempt_ledger_and_feedback_budgets_are_enforced(self):
        for index in range(review.MAX_PRIOR_LESSONS + 2):
            text = (self.repo / 'prompts/extract.md').read_text() + '\nRefinement ' + str(index)
            review.register_rejected_candidate(self.repo, self.state, 'extract', text, 'oracle_rejected')
        feedback = review.prior_attempt_feedback(self.repo, self.state, 'extract')
        self.assertEqual(len(feedback['visible_lessons']), review.MAX_PRIOR_LESSONS)
        with patch.object(review, 'MAX_ATTEMPT_EVENTS', len(review.attempt_events(self.state))):
            with self.assertRaisesRegex(ValueError, 'explicit archival'):
                review.register_rejected_candidate(self.repo, self.state, 'extract', text + ' next', 'oracle_rejected')

    def test_heldout_gate_requires_complete_consistent_critical_results(self):
        cases = [c for c in review.load_cases(self.repo, holdouts=True) if c['role'] == 'extract']
        good = {review.digest(c): [True, True] for c in cases}
        self.assertTrue(review.holdout_passes(good, good, cases))
        bad = {**good, next(iter(good)): [False, True]}
        self.assertFalse(review.holdout_passes(good, bad, cases))
        self.assertFalse(review.holdout_passes(bad, bad, cases))
        self.assertFalse(review.holdout_passes({}, {}, cases))
        self.assertFalse(review.holdout_passes(good, {**good, 'extra': [True, True]}, cases))

    def test_missing_overlapping_or_tampered_holdouts_fail_before_model(self):
        directory = self.repo / 'prompt-evals/holdouts'
        paths = {p.name: p.read_bytes() for p in directory.glob('*.json')}
        for fault in ('missing', 'overlap', 'tampered'):
            with self.subTest(fault=fault):
                for p in directory.glob('*.json'):
                    p.unlink()
                for name, data in paths.items():
                    (directory / name).write_bytes(data)
                if fault == 'missing':
                    for p in directory.glob('*.json'):
                        if json.loads(p.read_text())['role'] == 'extract':
                            p.unlink()
                elif fault == 'overlap':
                    p = next(directory.glob('*.json'))
                    role = json.loads(p.read_text())['role']
                    for q in directory.glob('*.json'):
                        if json.loads(q.read_text())['role'] == role:
                            q.unlink()
                    case = next(c for c in review.load_cases(self.repo) if c['role'] == role)
                    (directory / (review.digest(case) + '.json')).write_bytes(review.encoded(case))
                else:
                    p = next(directory.glob('*.json'))
                    case = json.loads(p.read_text())
                    case['rubric'] += ' Additional requirement.'
                    p.write_bytes(review.encoded(case))
                with patch.object(review, 'model') as model:
                    with self.assertRaises(ValueError):
                        review.review(self.config, self.state, self.repo, self.base, 'extract', review.due_key(self.due), time.monotonic() + 60)
                    model.assert_not_called()

    def test_new_training_case_cannot_copy_a_holdout(self):
        self.proposal_change = {'new_case': next(c for c in review.load_cases(self.repo, holdouts=True) if c['role'] == 'extract')}
        result = self.run_review()
        self.assertEqual(result['reason'], 'rejected_candidate')
        self.assertFalse(any(prompt == review.REVIEW_PROMPT for prompt, _, _ in self.calls))

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
        for module in (review.read_guidance, review.capture.activation_trace, review.claude_cli):
            path = Path(module.__file__)
            def changed(value):
                data = original(value)
                return data + b'\n# changed parser\n' if value == path else data
            with self.subTest(module=module.__name__), patch.object(Path, 'read_bytes', changed), self.assertRaises(review.SourceChanged):
                review.assert_sources_current()

    def test_cases_are_immutable_hash_addressed_and_invalid_cases_rejected(self):
        cases = review.load_cases(self.repo)
        coverage = {'extract': {'empty', 'note'}, 'correct': {'empty', 'note'},
                    'verify': {'supported', 'partial', 'unsupported'},
                    'maintain': {'no_change', 'defer', 'change'}}
        for role, outcomes in coverage.items():
            self.assertEqual({case['expected'] for case in cases if case['role'] == role}, outcomes)
        case = dict(cases[0], input='not a JSON object', rubric='invented')
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
        for module in (review, review.capture, review.claude_cli, review.prompt_runtime):
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
        for index, fault in enumerate(('base_hash', 'approval_hashes', 'gain', 'holdout_hash', 'holdout_results', 'scope')):
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
                elif fault == 'holdout_hash':
                    audit['holdout_dataset_hash'] = '0' * 64
                elif fault == 'holdout_results':
                    audit['holdout_candidate'][next(iter(audit['holdout_candidate']))] = [False, False]
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

    def test_verification_summary_allows_source_wording_and_identifiers(self):
        case = next(c for c in review.load_cases(self.repo) if c['role'] == 'verify')
        quoted = json.loads(case['input'])['messages'][-1]['text']
        for summary in (quoted, 'See https://example.com/a and /home/alice/x, token=abc123'):
            output = {'status': case['expected'], 'summary': summary}
            self.assertEqual(review.validate_output(case, output), output)
            self.assertTrue(review.output_passes(case, output))
        for summary in ('First\nsecond', ''):
            output = {'status': case['expected'], 'summary': summary}
            with self.subTest(summary=summary):
                with self.assertRaises(ValueError):
                    review.validate_output(case, output)
                self.assertFalse(review.output_passes(case, output))

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
        def semantic(config, state, prompt, data, schema, *, deadline, role=None, cache_prefix=None):
            value = json.loads(data)
            if schema is review.GRADE_SCHEMA:
                return {'grades': [{'item_id': item['item_id'], 'passed': False} for item in value['items']]}
            return original(config, state, prompt, data, schema, deadline=deadline, role=role)
        with patch.object(review.capture, 'model_response', side_effect=semantic):
            result = review.run(self.config, self.state, due=self.due)
        self.assertEqual(result['outcome'], 'no_change')
        self.assertTrue(all(not any(v) for v in result['candidate'].values()))
        (self.state / 'prompt-review-state.json').unlink()
        def wrong_ids(config, state, prompt, data, schema, *, deadline, role=None, cache_prefix=None):
            if schema is review.GRADE_SCHEMA:
                return {'grades': [{'item_id': 'wrong', 'passed': True} for _ in json.loads(data)['items']]}
            return original(config, state, prompt, data, schema, deadline=deadline, role=role)
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

    def test_task_envelopes_redact_messages_notes_and_note_context_like_production(self):
        private = 'Read /Us' + 'ers/alice/app.tsx via https://example.com/x with token=abc123'
        redacted = 'Read [local path] via [redacted URL] with [redacted credential]'
        templates = review.load_cases(self.repo)
        for role in ('extract', 'verify', 'correct'):
            with self.subTest(role=role):
                case = dict(next(c for c in templates if c['role'] == role))
                source = json.loads(case['input'])
                source['messages'] = [{'role': 'user', 'text': private}]
                if role == 'extract':
                    source['existing_notes'] = [{'skill': 'frontend-ui', 'text': private}]
                else:
                    source['note'] = dict(source['note'], evidence=private)
                data = review.task_input(dict(case, input=json.dumps(source)))
                if role == 'extract':
                    self.assertEqual(data['existing_notes'], [{'skill': 'frontend-ui', 'text': redacted}])
                    self.assertEqual(data['turn']['messages'][0]['text'], redacted)
                else:
                    self.assertIn('- Evidence: ' + redacted, data['note'])
                    self.assertEqual(data['original_turn']['messages'][0]['text'], redacted)
                self.assertNotIn('abc123', json.dumps(data))

    def test_symlink_artifact_directories_rejected(self):
        shutil.rmtree(self.repo / 'prompt-evals/cases')
        (self.repo / 'prompt-evals/cases').symlink_to(SOURCE / 'prompt-evals/cases')
        with self.assertRaises(ValueError):
            review.load_cases(self.repo)
        (self.repo / 'prompt-reviews').symlink_to(self.state)
        with self.assertRaises(ValueError):
            review.published_record(self.repo, self.due, 'extract')

    def test_due_is_tokyo_monday_deadline_and_explicit_date(self):
        self.assertEqual(review.due_key('2026-11-09T09:00:00+09:00'), '2026-11-09')
        self.assertEqual(review.due_key('2026-11-09T00:00:00Z'), '2026-11-09')
        self.assertEqual(review.due_key('2026-11-08T23:59:59Z'), '2026-11-02')
        self.assertEqual(review.due_key('2026-11-09T08:59:59+09:00'), '2026-11-02')
        self.assertEqual(review.due_key('2026-11-15T12:00:00+09:00'), '2026-11-09')
        self.assertEqual(review.due_key('2026-10-28'), '2026-10-26')
        with self.assertRaises(ValueError):
            review.due_key('2026-11-03T09:00:00')
        with self.assertRaises(ValueError):
            review.due_key('2026-02-30')

    def test_rotation_keeps_past_weekly_targets_and_resumes_after_daily_period(self):
        roles = review.ROLES
        self.assertEqual(review.target_role('2026-10-26'), roles[42 % len(roles)])
        self.assertEqual(review.target_role('2026-11-02'), roles[43 % len(roles)])
        # The daily period reviewed indexes 43 to 46 (2026-11-02 to 2026-11-05); Mondays from
        # 2026-11-09 continue with 47, so no role repeats before every other role has had a turn.
        mondays = ['2026-11-09', '2026-11-16', '2026-11-23', '2026-11-30']
        self.assertEqual([review.target_role(day) for day in mondays], [roles[i % len(roles)] for i in range(47, 51)])
        self.assertEqual(review.target_role(mondays[0]), review.target_role('2026-11-02'))
        self.assertEqual(review.target_role('2026-12-07'), review.target_role(mondays[0]))

    def test_saved_completion_suppresses_this_week_but_not_next_monday(self):
        saved = {'completed_due': '2026-11-09', 'result': {'outcome': 'no_change'}}
        path = self.state / 'prompt-review-state.json'
        path.write_text(json.dumps(saved))
        with patch.object(review, 'review', return_value={'outcome': 'no_change'}) as work:
            result = review.run(self.config, self.state, due='2026-11-09T09:00:00+09:00')
            self.assertEqual(result['outcome'], 'already_reviewed')
            work.assert_not_called()
            review.run(self.config, self.state, due='2026-11-16T09:00:00+09:00')
            work.assert_called_once()
            self.assertEqual(work.call_args.args[4], review.target_role('2026-11-16'))
            self.assertEqual(json.loads(path.read_text())['completed_due'], '2026-11-16')
            review.run(self.config, self.state, due='2026-11-18T23:00:00+09:00')
            work.assert_called_once()

    def test_earlier_due_than_completion_is_already_reviewed_and_keeps_marker(self):
        saved = {'completed_due': '2026-11-05', 'result': {'outcome': 'no_change'}}
        path = self.state / 'prompt-review-state.json'
        path.write_text(json.dumps(saved))
        with patch.object(review, 'review', return_value={'outcome': 'no_change'}) as work:
            result = review.run(self.config, self.state, due='2026-11-06T12:00:00+09:00')
            self.assertEqual(result['outcome'], 'already_reviewed')
            work.assert_not_called()
            self.assertEqual(json.loads(path.read_text()), saved)

    def test_daily_period_completion_does_not_suppress_next_monday(self):
        saved = {'completed_due': '2026-11-05', 'result': {'outcome': 'no_change'}}
        (self.state / 'prompt-review-state.json').write_text(json.dumps(saved))
        with patch.object(review, 'review', return_value={'outcome': 'no_change'}) as work:
            review.run(self.config, self.state, due='2026-11-09T09:00:00+09:00')
            work.assert_called_once()
            self.assertEqual(work.call_args.args[4], review.target_role('2026-11-09'))


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
