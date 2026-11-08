from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import investigations as inv


class InvestigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.state = self.root / 'state'; self.state.mkdir()
        for args in (('init', '-b', 'main'), ('config', 'user.name', 'Fixture'), ('config', 'user.email', 'fixture@example.test')):
            inv.git(self.repo, *args)
        (self.repo / 'fixture').write_text('fixture')
        inv.git(self.repo, 'add', '.'); inv.git(self.repo, 'commit', '-m', 'fixture')
        self.config = {'repo_path': str(self.repo)}
        self.binding = {'skill': 'frontend-ui', 'note_sha256': 'a'*64, 'receipt_sha256': 'b'*64, 'source_machine': 'client-host'}
        self.patcher = patch.object(inv, 'bindings', return_value=[]); self.patcher.start(); self.addCleanup(self.patcher.stop)
        self.validation = patch.object(inv, 'validate_events', return_value=0); self.validation.start(); self.addCleanup(self.validation.stop)

    def cycle(self):
        return inv.prepare(self.config, self.state)

    def decision(self, cycle, **changes):
        return {'schema_version': 2, 'issue_id': 'focus', 'skill': 'frontend-ui', 'supersedes': None,
                'evidence': [], 'action': 'investigate', 'state': 'open',
                'summary': 'Investigate conditional focus and retained draft behavior',
                'baseline_commit': inv.cycle_data(cycle)['base_commit'], 'change': None,
                'experiments': [], 'board_task': 'dotfiles-12', **changes}

    def legacy_open_event(self):
        # A schema 1 record from before open work moved to the board, committed as history.
        event = {'schema_version': 1, 'issue_id': 'focus', 'skill': 'frontend-ui', 'supersedes': None, 'evidence': [],
                 'action': 'investigate', 'state': 'blocked', 'baseline_commit': inv.git(self.repo, 'rev-parse', 'HEAD'),
                 'change': None, 'experiments': [], 'cycle_id': 'd' * 64, 'recorded_at': '2026-11-04T00:00:00+00:00',
                 'summary': 'Focus replay waits for an original project revision',
                 'follow_up': {'next_action': 'Replay the focus case from a project revision', 'next_due': '2026-11-09T00:00:00+00:00'}}
        inv.save(self.repo / 'feedback/frontend-ui/investigations' / ('focus--' + inv.digest(event) + '.json'), event)
        inv.git(self.repo, 'add', '.'); inv.git(self.repo, 'commit', '-m', 'legacy investigation')
        return inv.digest(event)

    def fixture(self, code='import sys; sys.exit(1 if sys.argv[-1]=="baseline" else 0)'):
        case = self.repo / 'skills/frontend-ui/evaluations/focus/case.json'; case.parent.mkdir(parents=True)
        (case.parents[2] / 'SKILL.md').write_text('Skill guidance')
        (case.parent / 'check.py').write_text(code)
        case.write_text(json.dumps({'schema_version': 1, 'skill': 'frontend-ui', 'id': 'focus', 'files': ['check.py'],
            'commands': {v: ['uv', 'run', '--no-project', 'python', 'check.py', v] for v in ('baseline', 'candidate', 'contrast')},
            'success_codes': {v: [0] for v in ('baseline', 'candidate', 'contrast')}, 'timeout_seconds': 10,
            'evidence_scope': 'synthetic_behavior'}))
        return case

    def test_summary_and_follow_up_may_name_urls_and_paths_but_stay_single_line(self):
        base = {'schema_version': 1, 'issue_id': 'focus', 'skill': 'frontend-ui', 'supersedes': None, 'evidence': [],
                'action': 'investigate', 'state': 'open', 'baseline_commit': 'c' * 40, 'change': None, 'experiments': [],
                'cycle_id': 'd' * 64, 'recorded_at': '2026-11-04T00:00:00+00:00',
                'summary': 'Compare https://example.com/a with /home/alice/project, token=abc123',
                'follow_up': {'next_action': 'Rerun /tmp/case against https://example.com/a', 'next_due': '2026-11-09T00:00:00+00:00'}}
        inv.validate_event(base)
        for changes in ({'summary': 'two\nlines here'}, {'summary': 'short'},
                        {'follow_up': {'next_action': 'Rerun\nthe case now', 'next_due': '2026-11-09T00:00:00+00:00'}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                inv.validate_event({**base, **changes})

    def test_schema_2_open_issue_cites_a_board_task_instead_of_a_follow_up(self):
        base = {'schema_version': 2, 'issue_id': 'focus', 'skill': 'frontend-ui', 'supersedes': None, 'evidence': [],
                'action': 'investigate', 'state': 'blocked', 'baseline_commit': 'c' * 40, 'change': None, 'experiments': [],
                'cycle_id': 'd' * 64, 'recorded_at': '2026-11-07T00:00:00+00:00',
                'summary': 'Focus replay waits for an original project revision', 'board_task': 'dotfiles-62'}
        inv.validate_event(base)
        runs = ['1' * 64, '2' * 64, '3' * 64]
        inv.validate_event({**base, 'state': 'resolved', 'action': 'keep', 'board_task': None, 'experiments': runs})
        inv.validate_event({**base, 'state': 'resolved', 'action': 'keep', 'experiments': runs})
        rejected = ({'board_task': None}, {'state': 'provisional', 'board_task': None}, {'board_task': 'dotfiles'},
                    {'board_task': 'dotfiles-0'}, {'board_task': 'Dotfiles-4'}, {'board_task': 62},
                    # Only the dotfiles board carries investigation work.
                    {'board_task': 'example-62'}, {'board_task': 'board-feedback-36'}, {'board_task': 'xdotfiles-4'},
                    {'schema_version': 3})
        for changes in rejected:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                inv.validate_event({**base, **changes})
        with self.assertRaisesRegex(ValueError, 'dotfiles board task id'):
            inv.validate_event({**base, 'board_task': 'example-62'})
        with self.assertRaisesRegex(ValueError, 'need board_task'):
            inv.validate_event({k: v for k, v in base.items() if k != 'board_task'})
        follow = {'next_action': 'Replay the focus case later', 'next_due': '2026-11-09T00:00:00+00:00'}
        with self.assertRaisesRegex(ValueError, 'carry no follow_up'):
            inv.validate_event({**base, 'follow_up': follow})
        with self.assertRaisesRegex(ValueError, 'carry no follow_up'):
            inv.validate_event({**{k: v for k, v in base.items() if k != 'board_task'}, 'follow_up': follow})

    def test_resolved_schema_2_event_counts_its_behavior_evidence(self):
        # Run receipts stay host-local, so sync cannot check them, but it can refuse a resolution
        # without the baseline, candidate and contrast runs that the cycle gate would demand.
        base = {'schema_version': 2, 'issue_id': 'focus', 'skill': 'frontend-ui', 'supersedes': None, 'evidence': [],
                'action': 'keep', 'state': 'resolved', 'baseline_commit': 'c' * 40, 'change': None,
                'cycle_id': 'd' * 64, 'recorded_at': '2026-11-07T00:00:00+00:00', 'board_task': 'dotfiles-62',
                'summary': 'Focus replay passes candidate and contrast', 'experiments': ['1' * 64, '2' * 64, '3' * 64]}
        inv.validate_event(base)
        for experiments in ([], ['1' * 64, '2' * 64], ['1' * 64, '1' * 64, '2' * 64]):
            with self.subTest(experiments=experiments), self.assertRaisesRegex(ValueError, 'behavior evidence'):
                inv.validate_event({**base, 'experiments': experiments})
        # Schema 1 history keeps its own rules.
        legacy = {k: v for k, v in base.items() if k != 'board_task'}
        inv.validate_event({**legacy, 'schema_version': 1, 'follow_up': None, 'experiments': []})

    def test_dropped_task_is_a_terminal_disposition_that_claims_no_fix(self):
        base = {'schema_version': 2, 'issue_id': 'focus', 'skill': 'frontend-ui', 'supersedes': None, 'evidence': [],
                'action': 'keep', 'state': 'dropped', 'baseline_commit': 'c' * 40, 'change': None, 'experiments': [],
                'cycle_id': 'd' * 64, 'recorded_at': '2026-11-07T00:00:00+00:00', 'board_task': 'dotfiles-62',
                'summary': 'The owner rejected the Proposal; the provisional wording stays unproven'}
        inv.validate_event(base)
        inv.validate_event({**base, 'action': 'investigate'})
        for changes in ({'board_task': None}, {'action': 'revise'}, {'action': 'revert'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                inv.validate_event({**base, **changes})
        legacy = {**{k: v for k, v in base.items() if k != 'board_task'}, 'schema_version': 1, 'follow_up': None}
        with self.assertRaisesRegex(ValueError, 'cannot be dropped'):
            inv.validate_event(legacy)
        # Recorded through a cycle, it closes the issue's open work without behavior evidence.
        first = self.cycle(); opened = inv.record(self.config, self.state, first, self.decision(first))
        inv.git(self.repo, 'add', '.'); inv.git(self.repo, 'commit', '-m', 'open investigation')
        later = self.cycle()
        dropped = inv.record(self.config, self.state, later, self.decision(
            later, supersedes=inv.digest(opened), action='keep', state='dropped',
            summary='The owner dropped dotfiles-12; the issue stays unresolved'))
        self.assertEqual(dropped['board_task'], 'dotfiles-12')
        summary = inv.validate_cycle(self.config, self.state, later)
        self.assertEqual((summary['complete'], summary['open'], summary['dispositions']), (True, 0, 1))
        inv.git(self.repo, 'add', '.'); inv.git(self.repo, 'commit', '-m', 'drop investigation')
        self.assertEqual(inv.cycle_data(self.cycle())['outstanding'], {})

    def test_resolved_schema_1_head_stays_valid_history(self):
        event = {'schema_version': 1, 'issue_id': 'focus', 'skill': 'frontend-ui', 'supersedes': None, 'evidence': [],
                 'action': 'keep', 'state': 'resolved', 'baseline_commit': inv.git(self.repo, 'rev-parse', 'HEAD'),
                 'change': None, 'experiments': [], 'cycle_id': 'd' * 64, 'recorded_at': '2026-11-04T00:00:00+00:00',
                 'summary': 'Focus replay passed before the board held open work', 'follow_up': None}
        inv.save(self.repo / 'feedback/frontend-ui/investigations' / ('focus--' + inv.digest(event) + '.json'), event)
        inv.git(self.repo, 'add', '.'); inv.git(self.repo, 'commit', '-m', 'legacy resolution')
        _, heads = inv.inventory(self.repo)
        self.assertEqual(list(heads.values()), [event])
        cycle = self.cycle()
        self.assertEqual(inv.cycle_data(cycle)['outstanding'], {})
        self.assertEqual(inv.validate_cycle(self.config, self.state, cycle)['open'], 0)
        # A later schema 2 event may reopen it on a board task.
        reopened = inv.record(self.config, self.state, cycle, self.decision(cycle, supersedes=inv.digest(event)))
        self.assertEqual(list(inv.inventory(self.repo)[1].values()), [reopened])

    def test_schema_1_event_cannot_supersede_schema_2(self):
        head = inv.git(self.repo, 'rev-parse', 'HEAD')
        newer = {'schema_version': 2, 'issue_id': 'focus', 'skill': 'frontend-ui', 'supersedes': None, 'evidence': [],
                 'action': 'investigate', 'state': 'blocked', 'baseline_commit': head, 'change': None, 'experiments': [],
                 'cycle_id': 'd' * 64, 'recorded_at': '2026-11-07T00:00:00+00:00', 'board_task': 'dotfiles-62',
                 'summary': 'Focus replay waits for an original project revision'}
        older = {k: v for k, v in newer.items() if k != 'board_task'}
        older.update(schema_version=1, supersedes=inv.digest(newer), state='resolved', action='keep',
                     recorded_at='2026-11-08T00:00:00+00:00', follow_up=None)
        with self.assertRaisesRegex(ValueError, 'schema 1 investigation event cannot supersede'):
            inv.heads_for({inv.digest(newer): newer, inv.digest(older): older})
        cycle = self.cycle()
        with self.assertRaisesRegex(ValueError, 'schema 2 investigation events only'):
            inv.record(self.config, self.state, cycle, {**self.decision(cycle), 'schema_version': 1})

    def test_open_head_check_follows_board_task_not_the_current_schema_number(self):
        event = {'schema_version': 2, 'issue_id': 'focus', 'skill': 'frontend-ui', 'supersedes': None, 'evidence': [],
                 'action': 'investigate', 'state': 'blocked', 'baseline_commit': 'c' * 40, 'change': None,
                 'experiments': [], 'cycle_id': 'd' * 64, 'recorded_at': '2026-11-07T00:00:00+00:00',
                 'board_task': 'dotfiles-62', 'summary': 'Focus replay waits for an original project revision'}
        with patch.object(inv, 'SCHEMA_VERSION', 3):
            self.assertEqual(list(inv.heads_for({inv.digest(event): event}).values()), [event])

    def test_record_refuses_a_chain_another_writer_extended(self):
        cycle = self.cycle(); base = inv.git(self.repo, 'rev-parse', 'HEAD')
        inv.record(self.config, self.state, cycle, self.decision(cycle, summary='Another writer extended this issue first'))
        inv.git(self.repo, 'add', '.'); inv.git(self.repo, 'commit', '-m', 'their event')
        theirs = inv.git(self.repo, 'rev-parse', 'HEAD')
        inv.git(self.repo, 'reset', '--hard', base)
        inv.git(self.repo, 'update-ref', 'refs/remotes/origin/main', theirs)
        with self.assertRaisesRegex(ValueError, 'another writer extended it'):
            inv.record(self.config, self.state, cycle, self.decision(cycle))
        # A chain recorded before the other writer's event landed is caught by the cycle gate.
        inv.git(self.repo, 'update-ref', 'refs/remotes/origin/main', base)
        inv.record(self.config, self.state, cycle, self.decision(cycle))
        self.assertTrue(inv.validate_cycle(self.config, self.state, cycle)['complete'])
        inv.git(self.repo, 'update-ref', 'refs/remotes/origin/main', theirs)
        with self.assertRaisesRegex(ValueError, 'another writer extended it'):
            inv.validate_cycle(self.config, self.state, cycle)

    def test_command_line_runs_the_documented_cycle(self):
        config = self.state / 'config.json'; config.write_text(json.dumps(self.config))
        def main(*args):
            output = io.StringIO()
            with patch.object(sys, 'argv', ['investigations.py', *args, '--config', str(config)]), redirect_stdout(output):
                self.assertEqual(inv.main(), 0)
            return json.loads(output.getvalue())
        cycle = main('prepare')['cycle']
        self.assertNotIn('due', inv.cycle_data(cycle))
        decision = self.state / 'decision.json'
        decision.write_text(json.dumps({k: v for k, v in self.decision(cycle).items() if k != 'schema_version'}))
        self.assertEqual(main('record', '--cycle', cycle, '--decision', str(decision))['schema_version'], 2)
        result = main('validate', '--cycle', cycle)
        self.assertEqual((result['complete'], result['open'], result['dispositions']), (True, 1, 1))
        self.assertEqual(set(main('report', '--cycle', cycle)), {'cycle_id', 'base_commit', 'required', 'outstanding'})

    def test_legacy_open_issue_must_move_to_a_board_task(self):
        legacy = self.legacy_open_event()
        with self.assertRaisesRegex(ValueError, 'frontend-ui/focus must cite its board task'):
            inv.inventory(self.repo)
        with self.assertRaisesRegex(ValueError, 'must cite its board task'):
            self.cycle()
        # The migration appends a schema 2 event to the chain; the schema 1 record stays as history.
        moved = {'schema_version': 2, 'issue_id': 'focus', 'skill': 'frontend-ui', 'supersedes': legacy, 'evidence': [],
                 'action': 'investigate', 'state': 'blocked', 'baseline_commit': inv.git(self.repo, 'rev-parse', 'HEAD'),
                 'change': None, 'experiments': [], 'cycle_id': 'e' * 64, 'recorded_at': '2026-11-07T00:00:00+00:00',
                 'summary': 'Focus replay waits for an original project revision', 'board_task': 'dotfiles-62'}
        inv.save(self.repo / 'feedback/frontend-ui/investigations' / ('focus--' + inv.digest(moved) + '.json'), moved)
        events, heads = inv.inventory(self.repo)
        self.assertEqual(len(events), 2)
        self.assertEqual([v['board_task'] for v in heads.values()], ['dotfiles-62'])
        inv.git(self.repo, 'add', '.'); inv.git(self.repo, 'commit', '-m', 'move investigation to the board')
        self.assertEqual([v['board_task'] for v in inv.cycle_data(self.cycle())['outstanding'].values()], ['dotfiles-62'])

    def test_cycle_fingerprint_rejects_tampering(self):
        path = self.cycle(); data = inv.read(path); data['required'].append(self.binding); path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'modified'): inv.validate_cycle(self.config, self.state, path)

    def test_new_evidence_requires_disposition(self):
        with patch.object(inv, 'bindings', return_value=[self.binding]):
            cycle = self.cycle()
            with self.assertRaisesRegex(ValueError, 'no investigation'): inv.validate_cycle(self.config, self.state, cycle)
            inv.record(self.config, self.state, cycle, self.decision(cycle, evidence=[{k:v for k,v in self.binding.items() if k!='skill'}]))
            self.assertTrue(inv.validate_cycle(self.config, self.state, cycle)['complete'])

    def test_open_issue_is_cited_by_board_task_not_carried_and_cannot_be_rubberstamped(self):
        first = self.cycle(); event = inv.record(self.config, self.state, first, self.decision(first)); h = inv.digest(event)
        self.assertEqual((event['schema_version'], event['board_task']), (2, 'dotfiles-12'))
        inv.git(self.repo, 'add', '.'); inv.git(self.repo, 'commit', '-m', 'retain investigation')
        later = self.cycle()
        self.assertNotIn('pending', inv.cycle_data(later))
        self.assertEqual({k: v['board_task'] for k, v in inv.cycle_data(later)['outstanding'].items()}, {h: 'dotfiles-12'})
        # The board task carries the issue, so a later cycle need not revisit it.
        self.assertTrue(inv.validate_cycle(self.config, self.state, later)['complete'])
        with self.assertRaisesRegex(ValueError, 'cite its board task'):
            inv.record(self.config, self.state, later, self.decision(later, supersedes=h, state='blocked', board_task=None))
        with self.assertRaisesRegex(ValueError, 'behavior evidence'):
            inv.record(self.config, self.state, later, self.decision(later, supersedes=h, action='keep', state='resolved'))
        inv.record(self.config, self.state, later, self.decision(later, supersedes=h, state='blocked', board_task='dotfiles-13'))
        summary = inv.validate_cycle(self.config, self.state, later)
        self.assertEqual((summary['complete'], summary['open'], summary['dispositions']), (True, 1, 1))

    def test_event_hash_and_chain_tampering_rejected(self):
        cycle = self.cycle(); event = inv.record(self.config, self.state, cycle, self.decision(cycle))
        path = next((self.repo/'feedback/frontend-ui/investigations').glob('*.json'))
        event['summary'] = 'Tampered abstract evidence should fail identity'; path.write_text(json.dumps(event))
        with self.assertRaisesRegex(ValueError, 'identity mismatch'): inv.inventory(self.repo)

    def test_real_failed_run_is_learning_and_frozen_criteria_cannot_change(self):
        cycle = self.cycle(); case = self.fixture()
        baseline = inv.run(self.config, self.state, cycle, case, 'baseline')
        self.assertFalse(baseline['passed']); self.assertEqual(baseline['exit_code'], 1)
        candidate = inv.run(self.config, self.state, cycle, case, 'candidate')
        contrast = inv.run(self.config, self.state, cycle, case, 'contrast')
        self.assertTrue(candidate['passed'] and contrast['passed'])
        (case.parent/'check.py').write_text('print("changed oracle")')
        with self.assertRaisesRegex(ValueError, 'Frozen'): inv.run(self.config, self.state, cycle, case, 'candidate')
        inv.record(self.config, self.state, cycle, self.decision(cycle, experiments=[baseline['run_sha256']], state='provisional'))
        self.assertTrue(inv.validate_cycle(self.config, self.state, cycle)['complete'])

    def test_receipt_identity_and_host_binding_rejected(self):
        with patch.object(inv, 'bindings', return_value=[self.binding]):
            cycle = self.cycle()
            bad = {k:v for k,v in self.binding.items() if k!='skill'}; bad['source_machine']='maintainer-host'
            with self.assertRaisesRegex(ValueError, 'not current'): inv.record(self.config,self.state,cycle,self.decision(cycle,evidence=[bad]))

    def test_project_copy_preserves_dirty_source_and_excludes_credentials(self):
        (self.repo/'dirty.py').write_text('uncommitted user code')
        self.config['investigation_projects']={'fixture':str(self.repo)}
        result=inv.snapshot_project(self.config,'fixture',['dirty.py'],self.root/'copy')
        (self.root/'copy/dirty.py').write_text('changed reproduction')
        self.assertEqual((self.repo/'dirty.py').read_text(),'uncommitted user code')
        self.assertEqual(result['scope'],'project_snapshot')
        with self.assertRaises(ValueError):inv.snapshot_project(self.config,'fixture',['.env'],self.root/'bad')

    def test_authorized_workspace_project_can_be_copied_without_registration(self):
        (self.repo/'dirty.py').write_text('user data')
        self.config['investigation_roots']=[str(self.root)]
        result=inv.snapshot_project(self.config,str(self.repo),['dirty.py'],self.root/'allowed-copy')
        self.assertTrue(result['originals_verified_unchanged']);self.assertTrue(result['dirty'])
        with self.assertRaisesRegex(ValueError,'outside authorized'):
            inv.snapshot_project({**self.config,'investigation_roots':[]},str(self.repo),['dirty.py'],self.root/'denied-copy')
        with self.assertRaisesRegex(ValueError,'original project'):
            inv.snapshot_project(self.config,str(self.repo),['dirty.py'],self.repo/'bad-copy')

    def test_missing_runner_persists_blocked_receipt(self):
        cycle=self.cycle();case=self.fixture()
        with patch.object(inv.shutil,'which',return_value=None):
            result=inv.run(self.config,self.state,cycle,case,'baseline')
        self.assertEqual(result['outcome'],'missing_dependency');self.assertFalse(result['passed'])
        self.assertEqual(inv.local_receipt(self.state,result['run_sha256'],inv.cycle_data(cycle))['exit_code'],127)

    def test_runner_launch_failure_persists_blocked_receipt(self):
        cycle = self.cycle(); case = self.fixture()
        with patch.object(inv.subprocess, 'Popen', side_effect=FileNotFoundError('missing interpreter')):
            result = inv.run(self.config, self.state, cycle, case, 'baseline')
        self.assertEqual(result['outcome'], 'missing_dependency'); self.assertFalse(result['passed'])
        self.assertEqual(inv.local_receipt(self.state, result['run_sha256'], inv.cycle_data(cycle))['exit_code'], 127)

    def test_handwritten_resolved_event_cannot_bypass_behavior_gate(self):
        cycle=self.cycle();event=inv.record(self.config,self.state,cycle,self.decision(cycle))
        path=next((self.repo/'feedback/frontend-ui/investigations').glob('*.json'));path.unlink()
        event['state']='resolved';event['action']='keep';event['board_task']=None
        written=path.with_name('focus--'+inv.digest(event)+'.json');inv.save(written,event)
        # Without runs, sync's own validation already refuses it.
        with self.assertRaisesRegex(ValueError,'behavior evidence'):inv.inventory(self.repo)
        with self.assertRaisesRegex(ValueError,'behavior evidence'):inv.validate_cycle(self.config,self.state,cycle)
        written.unlink();event['experiments']=['1'*64,'2'*64,'3'*64]
        inv.save(path.with_name('focus--'+inv.digest(event)+'.json'),event)
        # Invented run hashes pass that count but not the cycle gate, which reads the receipts.
        inv.inventory(self.repo)
        with self.assertRaisesRegex(ValueError,'evidence file'):inv.validate_cycle(self.config,self.state,cycle)

    def test_minimal_handwritten_run_receipts_are_rejected(self):
        cycle = self.cycle(); frozen = inv.cycle_data(cycle)
        hashes = []
        for variant in ('baseline', 'candidate', 'contrast'):
            fake = {'cycle_id': frozen['cycle_id'], 'runner_sha256': frozen['runner_sha256'],
                    'skill': 'frontend-ui', 'issue_id': 'focus', 'variant': variant, 'passed': True,
                    'plan_sha256': 'a'*64, 'fixture_sha256': 'b'*64, 'evidence_scope': 'synthetic_behavior'}
            h = inv.digest(fake); inv.save(self.state/'investigations/runs'/(h+'.json'), fake); hashes.append(h)
        with self.assertRaisesRegex(ValueError, 'Incomplete execution receipt'):
            inv.record(self.config, self.state, cycle, self.decision(cycle, action='keep', state='resolved', board_task=None, experiments=hashes))

    def test_final_gate_rejects_stale_guidance_and_missing_logs(self):
        cycle = self.cycle(); case = self.fixture()
        runs = [inv.run(self.config, self.state, cycle, case, v)['run_sha256'] for v in ('baseline', 'candidate', 'contrast')]
        decision = self.decision(cycle, action='keep', state='resolved', board_task=None, experiments=runs)
        inv.record(self.config, self.state, cycle, decision)
        self.assertTrue(inv.validate_cycle(self.config, self.state, cycle)['complete'])
        nested = case.parents[2]/'references/new.md'; nested.parent.mkdir(); nested.write_text('untested nested policy')
        with self.assertRaisesRegex(ValueError, 'current guidance'):
            inv.validate_cycle(self.config, self.state, cycle)
        nested.unlink()
        (self.state/'investigations/logs'/runs[1]/'stdout').write_text('changed output')
        with self.assertRaisesRegex(ValueError, 'logs are missing or changed'):
            inv.validate_cycle(self.config, self.state, cycle)

    def test_cannot_record_resolution_after_untested_revision(self):
        cycle = self.cycle(); case = self.fixture()
        runs = [inv.run(self.config, self.state, cycle, case, v)['run_sha256'] for v in ('baseline', 'candidate', 'contrast')]
        (case.parents[2]/'SKILL.md').write_text('untested subsequent guidance')
        with self.assertRaisesRegex(ValueError, 'current guidance'):
            inv.record(self.config, self.state, cycle, self.decision(cycle, action='keep', state='resolved', board_task=None, experiments=runs))

    def test_refresh_cannot_grandfather_failed_draft_events_or_reviews(self):
        first = self.cycle()
        inv.record(self.config, self.state, first, self.decision(first))
        directory = self.repo/'feedback/frontend-ui/reviews'; directory.mkdir(parents=True)
        (directory/'draft.md').write_text(self.binding['note_sha256']+' '+self.binding['receipt_sha256'])
        with patch.object(inv, 'bindings', return_value=[self.binding]):
            fresh = self.cycle()
            self.assertEqual(inv.cycle_data(fresh)['prior_events'], [])
            self.assertEqual(inv.cycle_data(fresh)['required'], [self.binding])
            with self.assertRaisesRegex(ValueError, 'another cycle'):
                inv.validate_cycle(self.config, self.state, fresh)

    def test_forward_revision_and_revert_bind_real_git_bytes(self):
        skill=self.repo/'skills/frontend-ui/SKILL.md';skill.parent.mkdir(parents=True);skill.write_text('stable guidance')
        inv.git(self.repo,'add','.');inv.git(self.repo,'commit','-m','stable')
        cycle=self.cycle();before=inv.digest(skill.read_bytes());skill.write_text('provisional candidate')
        inv.git(self.repo,'add','.');inv.git(self.repo,'commit','-m','candidate');candidate=inv.git(self.repo,'rev-parse','HEAD');after=inv.digest(skill.read_bytes())
        event=inv.record(self.config,self.state,cycle,self.decision(cycle,action='revise',state='provisional',
            change={'candidate_commit':candidate,'reverts_commit':None,'paths':{'skills/frontend-ui/SKILL.md':{'before':before,'after':after}}}))
        inv.git(self.repo,'add','.');inv.git(self.repo,'commit','-m','record provisional')
        second=self.cycle();skill.write_text('stable guidance');inv.git(self.repo,'add','.');inv.git(self.repo,'commit','-m','restore guidance forward')
        reverted=inv.record(self.config,self.state,second,self.decision(second,action='revert',state='provisional',supersedes=inv.digest(event),
            change={'candidate_commit':inv.git(self.repo,'rev-parse','HEAD'),'reverts_commit':candidate,
                    'paths':{'skills/frontend-ui/SKILL.md':{'before':after,'after':before}}}))
        self.assertTrue(inv.validate_cycle(self.config,self.state,second)['complete'])
        self.assertEqual(reverted['action'],'revert')

    def test_mutable_implementation_can_be_fixed_but_oracle_stays_frozen(self):
        cycle=self.cycle();case=self.fixture('import implementation;raise SystemExit(0 if implementation.valid else 1)')
        implementation=case.parent/'implementation.py';implementation.write_text('valid=False')
        data=inv.read(case);data['files'].append('implementation.py');data['mutable_files']=['implementation.py'];case.write_text(json.dumps(data))
        baseline=inv.run(self.config,self.state,cycle,case,'baseline');self.assertFalse(baseline['passed'])
        implementation.write_text('valid=True')
        candidate=inv.run(self.config,self.state,cycle,case,'candidate');contrast=inv.run(self.config,self.state,cycle,case,'contrast')
        self.assertTrue(candidate['passed'] and contrast['passed']);self.assertNotEqual(baseline['fixture_sha256'],candidate['fixture_sha256'])
        self.assertEqual(candidate['plan_sha256'],baseline['plan_sha256']);self.assertEqual(candidate['fixture_sha256'],contrast['fixture_sha256'])
        (case.parent/'check.py').write_text('raise SystemExit(0)')
        with self.assertRaisesRegex(ValueError,'Frozen'):inv.run(self.config,self.state,cycle,case,'candidate')

    def test_invoked_checker_cannot_be_declared_mutable(self):
        cycle = self.cycle(); case = self.fixture()
        data = inv.read(case); data['mutable_files'] = ['check.py']; case.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'immutable oracle'):
            inv.run(self.config, self.state, cycle, case, 'baseline')

    def test_private_project_case_executes_copy_without_mutating_dirty_original(self):
        source=self.repo/'implementation.py';source.write_text('valid=True')
        self.config['investigation_projects']={'fixture':str(self.repo)}
        case=self.fixture('import implementation;raise SystemExit(0 if implementation.valid else 1)')
        private=self.state/'investigations/cases/private-copy'
        inv.snapshot_project(self.config,'fixture',['implementation.py'],private)
        (private/'check.py').write_bytes((case.parent/'check.py').read_bytes())
        data=inv.read(case);data.update(files=['check.py','implementation.py','.snapshot.json'],evidence_scope='project_snapshot',project_snapshot='.snapshot.json',mutable_files=['implementation.py'])
        manifest=private/'case.json';manifest.write_text(json.dumps(data));cycle=self.cycle()
        result=inv.run(self.config,self.state,cycle,manifest,'baseline')
        self.assertTrue(result['passed']);self.assertTrue(result['originals_verified_unchanged'])
        self.assertEqual(source.read_text(),'valid=True')
        (private/'implementation.py').write_text('valid=False')
        candidate=inv.run(self.config,self.state,cycle,manifest,'candidate');self.assertFalse(candidate['passed'])
        self.assertEqual(source.read_text(),'valid=True')
        source.write_text('valid=False')
        with self.assertRaisesRegex(ValueError,'changed since snapshot'):inv.run(self.config,self.state,cycle,manifest,'contrast')

    def test_output_is_bounded_and_private_log_is_truncated(self):
        cycle=self.cycle();case=self.fixture('import sys;sys.stdout.write("x"*2000000)')
        result=inv.run(self.config,self.state,cycle,case,'baseline')
        self.assertTrue(result['output_limit_exceeded']);self.assertFalse(result['passed'])
        log=self.state/'investigations/logs'/result['run_sha256']/'stdout'
        self.assertLessEqual(log.stat().st_size,1000000)

    def test_exited_unreaped_group_is_complete_and_a_stuck_one_is_not(self):
        cycle=self.cycle();case=self.fixture('print("done")')
        real=os.killpg;seen={'refused':False,'polled':False}
        def unreaped(pid,sig):
            # Darwin's answer while the finished leader is still unreaped: refused until the runner polls again.
            if not seen['polled']:
                seen['refused']=True;raise PermissionError(1,'Operation not permitted')
            return real(pid,sig)
        def poll(process,original=subprocess.Popen.poll):
            seen['polled']=seen['polled'] or seen['refused'];return original(process)
        with patch.object(inv.os,'killpg',side_effect=unreaped),patch.object(subprocess.Popen,'poll',poll):
            result=inv.run(self.config,self.state,cycle,case,'baseline')
        self.assertTrue(seen['polled'])
        self.assertFalse(result['process_group_incomplete']);self.assertTrue(result['passed'])
        with patch.object(inv.os,'killpg',side_effect=PermissionError(1,'Operation not permitted')),patch.object(inv.time,'sleep'):
            result=inv.run(self.config,self.state,cycle,case,'candidate')
        self.assertTrue(result['process_group_incomplete']);self.assertFalse(result['passed'])

    @unittest.skipUnless(hasattr(os,'fork'),'POSIX process-group test')
    def test_successful_leader_cannot_leave_descendants_running(self):
        cycle = self.cycle(); case = self.fixture('import os,time\npid=os.fork()\nif pid==0: time.sleep(20)\nelse: print(pid,flush=True)')
        result = inv.run(self.config, self.state, cycle, case, 'baseline')
        self.assertTrue(result['process_group_incomplete']); self.assertFalse(result['passed'])
        self.assertEqual(result['outcome'], 'incomplete_process_group')
        pid = (self.state/'investigations/logs'/result['run_sha256']/'stdout').read_text().strip()
        process = subprocess.run(['ps', '-p', pid, '-o', 'stat='], capture_output=True, text=True)
        self.assertTrue(not process.stdout.strip() or process.stdout.strip().startswith('Z'), process.stdout)

    @unittest.skipUnless(hasattr(os,'fork'),'POSIX process-group test')
    def test_timeout_cleans_descendant_process_group(self):
        cycle=self.cycle();case=self.fixture('import os,time\npid=os.fork()\nif pid==0: time.sleep(20)\nelse: print(pid,flush=True);time.sleep(20)')
        data=inv.read(case);data['timeout_seconds']=1;case.write_text(json.dumps(data))
        result=inv.run(self.config,self.state,cycle,case,'baseline')
        self.assertTrue(result['timed_out'])
        log=self.state/'investigations/logs'/result['run_sha256']/'stdout'
        pid=log.read_text().strip()
        if pid:
            process=subprocess.run(['ps','-p',pid,'-o','stat='],capture_output=True,text=True)
            self.assertTrue(not process.stdout.strip() or process.stdout.strip().startswith('Z'),process.stdout)

    def test_timeout_is_not_success(self):
        cycle=self.cycle();case=self.fixture('import time; time.sleep(10)');data=inv.read(case);data['timeout_seconds']=1;case.write_text(json.dumps(data))
        run=inv.run(self.config,self.state,cycle,case,'baseline')
        self.assertTrue(run['timed_out']);self.assertFalse(run['passed'])


if __name__=='__main__':unittest.main()
