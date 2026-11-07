import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import capture
import evidence
import frontend_avoid
import prompt_runtime

LIST = '''# Frontend patterns to avoid

Visual patterns the user has declined as defaults.

## Patterns
- Purple-to-blue gradient hero backgrounds
'''
REPOSITORY = 'owner/dotfiles'
PATH = 'claude/frontend-avoid.md'

# Emulates the GitHub REST endpoints the proposal uses; every call is logged.
FAKE_GH = r'''#!PYTHON
import base64, json, pathlib, sys
root = pathlib.Path(__file__).parent
state = json.loads((root / 'github.json').read_text())
args = sys.argv[1:]
method, endpoint = args[args.index('--method') + 1], args[args.index('--method') + 2]
payload = json.loads(sys.stdin.read()) if '--input' in args else None
with (root / 'calls.jsonl').open('a') as log:
    log.write(json.dumps({'method': method, 'endpoint': endpoint, 'payload': payload}) + '\n')
fail = (root / 'fail').read_text() if (root / 'fail').exists() else None
if fail and method != 'GET' and fail in endpoint:
    print('HTTP 422: fixture rejection for ' + json.dumps(payload), file=sys.stderr)
    sys.exit(1)
endpoint = endpoint.split('/', 3)[3]
path, _, query = endpoint.partition('?')
params = dict(item.split('=', 1) for item in query.split('&') if item)
out = None
def entries(text):
    return [line for line in text.split('\n') if line.startswith('- ')]
if method == 'GET' and path.startswith('git/ref/heads/'):
    out = {'object': {'sha': state['refs'][path[len('git/ref/heads/'):]]}}
elif method == 'GET' and path.startswith('contents/'):
    ref = params['ref']
    text = state['files'][state['refs'].get(ref, ref)]
    out = {'content': base64.b64encode(text.encode()).decode(), 'sha': 'blob-' + state['refs'].get(ref, ref)}
elif method == 'GET' and path == 'pulls':
    head = params.get('head', '').partition(':')[2]
    out = [[p for p in state['pulls'] if not head or p['head']['ref'] == head]]
elif method == 'GET' and path.startswith('git/matching-refs/heads/'):
    prefix = path[len('git/matching-refs/heads/'):]
    out = [[{'ref': 'refs/heads/' + name} for name in state['refs'] if name.startswith(prefix)]]
elif method == 'GET' and path.startswith('pulls/') and path.endswith('/files'):
    pull = next(p for p in state['pulls'] if p['number'] == int(path.split('/')[1]))
    base = entries(state['files'][state['refs']['main']])
    added = [line for line in entries(state['files'][pull['head_sha']]) if line not in base]
    out = [[{'filename': 'claude/frontend-avoid.md', 'patch': '@@ -1 +1 @@\n' + ''.join('+' + line + '\n' for line in added)}]]
elif method == 'POST' and path == 'git/refs':
    state['refs'][payload['ref'][len('refs/heads/'):]] = payload['sha']
    out = {}
elif method == 'DELETE' and path.startswith('git/refs/heads/'):
    del state['refs'][path[len('git/refs/heads/'):]]
elif method == 'PUT' and path.startswith('contents/'):
    branch = payload['branch']
    assert payload['sha'] == 'blob-' + state['refs'][branch]
    commit = 'commit-' + str(len(state['files']))
    state['files'][commit] = base64.b64decode(payload['content']).decode()
    state['refs'][branch] = commit
    for pull in state['pulls']:
        if pull['head']['ref'] == branch:
            pull['head_sha'] = commit
    out = {}
elif method == 'POST' and path == 'pulls':
    number = len(state['pulls']) + 1
    out = {'number': number, 'state': 'open', 'draft': payload['draft'], 'base': payload['base'], 'body': payload['body'],
           'title': payload['title'], 'head': {'ref': payload['head'], 'repo': {'full_name': 'owner/dotfiles'}},
           'head_sha': state['refs'][payload['head']], 'html_url': 'https://github.test/pull/' + str(number),
           'created_at': '2026-11-0' + str(number) + 'T00:00:00Z'}
    state['pulls'].append(out)
elif method == 'PATCH' and path.startswith('pulls/'):
    pull = next(p for p in state['pulls'] if p['number'] == int(path.split('/')[1]))
    pull.update(payload)
    out = pull
else:
    sys.exit(2)
(root / 'github.json').write_text(json.dumps(state))
late = (root / 'fail-after').read_text() if (root / 'fail-after').exists() else None
if late and method != 'GET' and late in endpoint:
    sys.exit(1)
if out is not None:
    print(json.dumps(out))
'''.replace('PYTHON', sys.executable)


def note(title, observation='The user asked for a calmer layout.'):
    return (f'# {title}\n- Recorded: 2026-11-01T00:00:00+00:00\n- Machine: host\n- Thread: session1\n- Turn: turn1\n'
            f'- Skill: frontend-ui; deployed-at-capture unavailable; loaded unknown\n- Task: Build a settings page.\n'
            f'- Observation: {observation}\n- Evidence: The user said so in the follow-up message.\n'
            f'- Cause: uncertain\n- Proposal: Preserve current guidance.\n')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.feedback = self.root / 'repo/feedback'
        (self.feedback / 'frontend-ui/notes/client-host').mkdir(parents=True)
        (self.feedback / 'frontend-ui/verifications/client-host').mkdir(parents=True)

    def add_note(self, title, status='supported', correction=None, truncated=False, checked=True, **kwargs):
        text = note(title, **kwargs)
        path = self.feedback / 'frontend-ui/notes/client-host' / (hashlib.sha256(text.encode()).hexdigest()[:16] + '.md')
        path.write_text(text)
        digest = hashlib.sha256(text.encode()).hexdigest()
        if checked:
            value = {'schema_version': 4, 'noteSha256': digest, 'sourceMachine': 'client-host', 'skill': 'frontend-ui',
                     'thread': 'session1', 'turn': 'turn1', 'sourceSha256': 'b' * 64,
                     'completed_at': '2026-11-01T00:00:00Z', 'checked_at': '2026-11-01T01:00:00Z', 'loadedReads': [],
                     'scope': evidence.ORIGINAL_SCOPE, 'checkerSha256': 'c' * 64, 'inputSha256': 'd' * 64,
                     'inputTruncated': truncated, 'supersedes': None, 'correction': None, 'evidenceSha256': 'e' * 64,
                     'followUps': [], 'status': status, 'summary': 'Faithfully attributed.'}
            if correction:
                value['correction'] = {'note': {'skill': 'frontend-ui', 'title': correction, 'task': 'Build a page.',
                                                'observation': 'Rejected as a default.', 'evidence': 'User message.',
                                                'cause': 'uncertain', 'proposal': 'Add to the list.'},
                                       'status': 'supported', 'summary': 'Corrected.', 'inputSha256': 'f' * 64,
                                       'inputTruncated': False, 'generationInputSha256': '9' * 64}
            data = (json.dumps(value) + '\n').encode()
            (self.feedback / 'frontend-ui/verifications/client-host' /
             (digest + '--' + hashlib.sha256(data).hexdigest() + '.json')).write_bytes(data)
        return digest


class ClassificationTests(Fixture):
    def test_fixed_contracts_separate_rejected_defaults_from_single_design_feedback(self):
        extract = prompt_runtime.ROLE_CONTRACTS['extract']
        self.assertIn('"Rejected default: <pattern>"', extract)
        self.assertIn('unwanted default for future interfaces, not only in the current design', extract)
        self.assertIn('Feedback on a single design is an ordinary note', extract)
        self.assertIn('without project, product, person or place names', extract)
        for role in ('verify', 'correct'):
            self.assertIn('as a default beyond the current design', prompt_runtime.ROLE_CONTRACTS[role])
        # The fixed contract reaches every rendered prompt, whatever the editable refinements say.
        self.assertIn(extract, prompt_runtime.render('extract', 'No refinements.'))

    def test_rejected_default_titles_are_recognized_without_failing_extraction(self):
        lesson = {'skill': 'frontend-ui', 'title': 'Rejected default: Glassmorphism cards over gradient backgrounds',
                  'task': 'Dashboard.', 'observation': 'Declined for future work.', 'evidence': 'User message 3.',
                  'cause': 'uncertain', 'proposal': 'Propose for the list.'}
        self.assertEqual(capture.rejected_default(lesson['title']), 'Glassmorphism cards over gradient backgrounds')
        for title in ('Rejected Default: Glass cards', 'rejected default:Glass cards', ' Rejected  default :  Glass cards'):
            self.assertEqual(capture.rejected_default(title), 'Glass cards', title)
        for title in ('Pricing cards felt too busy on this page', 'Not a rejected default: glass cards'):
            self.assertIsNone(capture.rejected_default(title), title)
        # A misused title must not fail the batch and cost a retried extraction; it is simply never proposed.
        misused = [{**lesson, 'skill': 'typescript'}, {**lesson, 'title': 'Rejected default: ' + 'x' * 300}]
        turn = {'verified_skills': ['frontend-ui', 'typescript']}
        self.assertEqual(capture.validate_notes({'notes': misused}, turn), misused)
        self.assertFalse(frontend_avoid.safe('x' * 300))

    def test_only_settled_rejected_default_notes_are_candidates(self):
        accepted = self.add_note('Rejected default: Emoji icons as section markers')
        self.add_note('Pricing cards felt too busy on this page', observation='The user disliked this one design.')
        self.add_note('Rejected default: Neon glow on buttons', status='partial')
        self.add_note('Rejected default: Oversized pill buttons', truncated=True)
        self.add_note('Rejected default: Tilted card stacks', checked=False)
        corrected = self.add_note('Layout feedback', status='partial',
                                  correction='Rejected default: Centered single-column landing pages')
        self.add_note('Rejected default: Dark mode only', status='unsupported', correction='Dark theme feedback on one page')
        other = self.feedback / 'typescript/notes/client-host/other.md'
        other.parent.mkdir(parents=True)
        other.write_text(note('Rejected default: Enums for every union'))
        self.assertEqual(frontend_avoid.candidates(self.feedback), {
            accepted: 'Emoji icons as section markers', corrected: 'Centered single-column landing pages'})


class PlanTests(unittest.TestCase):
    def test_existing_and_proposed_patterns_and_cited_notes_are_skipped(self):
        accepted = {'1' * 64: 'purple-to-blue  gradient hero backgrounds.', '2' * 64: 'Emoji icons as section markers',
                    '3' * 64: 'Neon glow on buttons', '4' * 64: 'Tilted card stacks', '5' * 64: 'emoji icons as section markers',
                    '6' * 64: 'Glass panels', '7' * 64: 'Avatar stacks from client-host', '8' * 64: 'Icons by @someone',
                    '9' * 64: 'Rounded hero images'}
        proposals = [{'entries': ['Neon glow on buttons'], 'notes': set()}, {'entries': [], 'notes': {'4' * 64}},
                     {'entries': ['Glass panels'], 'notes': set(), 'state': 'closed'}]
        # Proposed earlier and since reworded in its pull request: the local record still knows it.
        new, unsafe = frontend_avoid.plan(LIST, accepted, proposals, proposed={'rounded hero images'})
        self.assertEqual(new, [('Emoji icons as section markers', ['2' * 64, '5' * 64])])
        self.assertEqual(unsafe, 2)

    def test_only_plain_word_patterns_reach_the_public_list(self):
        private = (*frontend_avoid.PRIVATE, 'octo', 'alice', 'Alice-Laptop')
        for pattern in ('Emoji icons as section markers', 'Thin 1px borders, muted (grey) text', '玻璃拟态卡片', 'Drop-shadow-heavy cards'):
            self.assertTrue(frontend_avoid.safe(pattern, private), pattern)
        for pattern in ('Dashboard cards on 192.168.1.20', 'Glass cards like in src/acme/Hero.tsx', 'Cards like example.dev',
                        'Hero like the octo site', 'Panels alice liked', 'Layout from alice-laptop', 'Icons by @someone',
                        'Cards: big', 'snake_case labels', 'Tabs\tand spaces', 'ab', 'Avatar stacks from client-host'):
            self.assertFalse(frontend_avoid.safe(pattern, private), pattern)

    def test_entries_are_appended_inside_the_patterns_section(self):
        self.assertEqual(frontend_avoid.append_entries('## Patterns\n- A\n\nClosing note.\n', ['B']),
                         '## Patterns\n- A\n- B\n\nClosing note.\n')
        self.assertEqual(frontend_avoid.append_entries('# T\n\n## Patterns\n', ['A']), '# T\n\n## Patterns\n- A\n')
        text = '# T\n\n## Patterns\n- A\n\n## Notes\nKeep.\n'
        self.assertEqual(frontend_avoid.append_entries(text, ['B', 'C']), '# T\n\n## Patterns\n- A\n- B\n- C\n\n## Notes\nKeep.\n')
        self.assertEqual(frontend_avoid.entries(text), ['A'])
        with self.assertRaises(ValueError):
            frontend_avoid.entries('# T\n- A\n')

    def test_body_holds_only_counts_and_note_hashes(self):
        body = frontend_avoid.pr_body({'a' * 64, 'b' * 64}, 1)
        self.assertEqual(frontend_avoid.cited_notes(body), {'a' * 64, 'b' * 64})
        self.assertEqual(frontend_avoid.cited_notes(body.replace('\n', '\r\n')), {'a' * 64, 'b' * 64})
        self.assertIn('Entries in this proposal: 1. Source notes: 2.', body)


class ProposalTests(Fixture):
    def setUp(self):
        super().setUp()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.bin = self.root / 'gh'
        self.bin.mkdir()
        (self.bin / 'gh').write_text(FAKE_GH)
        (self.bin / 'gh').chmod(0o755)
        self.github = {'refs': {'main': 'base1'}, 'files': {'base1': LIST}, 'pulls': []}
        (self.bin / 'github.json').write_text(json.dumps(self.github))
        self.config = {'repo_path': str(self.root / 'repo'), 'frontend_avoid': {
            'repository': REPOSITORY, 'path': PATH, 'gh_path': str(self.bin / 'gh')}}

    def calls(self):
        log = self.bin / 'calls.jsonl'
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def github_state(self):
        return json.loads((self.bin / 'github.json').read_text())

    def test_creates_one_draft_pull_request_without_note_text_and_never_writes_main(self):
        observation = 'In the 2026 launch thread for Acme the user said gradients look cheap, see /tmp/acme/brief.md.'
        first = self.add_note('Rejected default: Emoji icons as section markers', observation=observation)
        self.add_note('Pricing cards felt too busy on this page')
        summary = frontend_avoid.propose(self.config, self.state)
        self.assertEqual((summary['outcome'], summary['entries'], summary['candidates']), ('created', 1, 1))
        github = self.github_state()
        pull = github['pulls'][0]
        self.assertTrue(pull['draft'])
        self.assertEqual(pull['base'], 'main')
        self.assertTrue(pull['head']['ref'].startswith('skill-feedback/frontend-avoid-'))
        self.assertEqual(github['refs']['main'], 'base1')
        self.assertEqual(github['files']['base1'], LIST)
        self.assertEqual(github['files'][github['refs'][pull['head']['ref']]],
                         LIST + '- Emoji icons as section markers\n')
        writes = [call for call in self.calls() if call['method'] != 'GET']
        self.assertEqual([(c['method'], c['endpoint'].split('/', 3)[3].split('/')[0]) for c in writes],
                         [('POST', 'git'), ('PUT', 'contents'), ('POST', 'pulls')])
        self.assertTrue(all((c['payload'] or {}).get('branch', '') != 'main' for c in writes))
        # The body names notes only by hash: no note, transcript or task text.
        text = (self.feedback / 'frontend-ui/notes/client-host').glob('*.md')
        for line in (line for path in text for line in path.read_text().splitlines()[1:]):
            value = line.split(': ', 1)[-1]
            self.assertNotIn(value, pull['body'])
        for fragment in ('Acme', 'launch', 'gradients look cheap', '/tmp', 'Emoji', 'Pricing'):
            self.assertNotIn(fragment, pull['body'])
        self.assertEqual(frontend_avoid.cited_notes(pull['body']), {first})
        self.assertNotIn('Emoji', pull['title'])

    def test_nothing_new_opens_nothing_and_settled_notes_skip_github(self):
        self.add_note('Rejected default: purple-to-blue gradient hero backgrounds')
        self.assertEqual(frontend_avoid.propose(self.config, self.state)['outcome'], 'unchanged')
        self.assertFalse([call for call in self.calls() if call['method'] != 'GET'])
        count = len(self.calls())
        self.assertEqual(frontend_avoid.propose(self.config, self.state)['outcome'], 'unchanged')
        self.assertEqual(len(self.calls()), count)
        (self.bin / 'calls.jsonl').unlink()
        # No accepted notes at all: no GitHub request.
        self.assertEqual(frontend_avoid.propose({**self.config, 'repo_path': str(self.root / 'missing')}, self.root / 'state'),
                         {'candidates': 0, 'entries': 0, 'unsafe': 0, 'outcome': 'unchanged', 'pull_request': None})
        self.assertEqual(self.calls(), [])

    def test_new_patterns_join_the_open_proposal_and_declined_ones_stay_out(self):
        first = self.add_note('Rejected default: Emoji icons as section markers')
        frontend_avoid.propose(self.config, self.state)
        second = self.add_note('Rejected default: Neon glow on buttons')
        summary = frontend_avoid.propose(self.config, self.state)
        self.assertEqual((summary['outcome'], summary['entries']), ('updated', 1))
        github = self.github_state()
        self.assertEqual(len(github['pulls']), 1)
        pull = github['pulls'][0]
        self.assertEqual(github['files'][github['refs'][pull['head']['ref']]],
                         LIST + '- Emoji icons as section markers\n- Neon glow on buttons\n')
        self.assertEqual(frontend_avoid.cited_notes(pull['body']), {first, second})
        self.assertIn('Entries in this proposal: 2. Source notes: 2.', pull['body'])
        # Closed without merging: the same pattern from a new note is not proposed again.
        pull['state'] = 'closed'
        (self.bin / 'github.json').write_text(json.dumps(github))
        self.add_note('Rejected default: neon glow on buttons.', observation='Repeated later.')
        self.assertEqual(frontend_avoid.propose(self.config, self.state)['outcome'], 'unchanged')
        self.assertEqual(len(self.github_state()['pulls']), 1)

    def test_failed_pull_request_removes_its_branch_and_retries_next_run(self):
        self.add_note('Rejected default: Emoji icons as section markers')
        (self.bin / 'fail').write_text('pulls')
        with self.assertRaisesRegex(RuntimeError, r'^GitHub request failed: POST pulls$'):
            frontend_avoid.propose(self.config, self.state)
        self.assertEqual(self.github_state()['refs'], {'main': 'base1'})
        self.assertFalse((self.state / 'frontend-avoid-state.json').exists())
        (self.bin / 'fail').unlink()
        self.assertEqual(frontend_avoid.propose(self.config, self.state)['outcome'], 'created')

    def test_pull_request_created_despite_a_failed_response_keeps_its_branch(self):
        self.add_note('Rejected default: Emoji icons as section markers')
        (self.bin / 'fail-after').write_text('pulls')
        with self.assertRaises(RuntimeError):
            frontend_avoid.propose(self.config, self.state)
        github = self.github_state()
        self.assertEqual(len(github['pulls']), 1)
        self.assertIn(github['pulls'][0]['head']['ref'], github['refs'])
        (self.bin / 'fail-after').unlink()
        # The next run finds the open proposal instead of opening another.
        self.assertEqual(frontend_avoid.propose(self.config, self.state)['outcome'], 'unchanged')
        self.assertEqual(len(self.github_state()['pulls']), 1)

    def test_orphaned_proposal_branches_are_removed_but_others_kept(self):
        github = self.github_state()
        github['refs'].update({'skill-feedback/frontend-avoid-20261101-000000': 'base1', 'feature': 'base1'})
        (self.bin / 'github.json').write_text(json.dumps(github))
        self.add_note('Rejected default: Emoji icons as section markers')
        frontend_avoid.propose(self.config, self.state)
        refs = self.github_state()['refs']
        self.assertNotIn('skill-feedback/frontend-avoid-20261101-000000', refs)
        self.assertIn('feature', refs)
        self.assertEqual(len([name for name in refs if name.startswith(frontend_avoid.BRANCH_PREFIX)]), 1)

    def test_body_that_lost_citations_is_repaired_and_repository_case_is_ignored(self):
        first = self.add_note('Rejected default: Emoji icons as section markers')
        frontend_avoid.propose(self.config, self.state)
        github = self.github_state()
        github['pulls'][0]['body'] = 'Edited on GitHub.\r\n'
        (self.bin / 'github.json').write_text(json.dumps(github))
        (self.state / 'frontend-avoid-state.json').unlink()
        config = {**self.config, 'frontend_avoid': {**self.config['frontend_avoid'], 'repository': 'Owner/Dotfiles'}}
        self.assertEqual(frontend_avoid.propose(config, self.state)['outcome'], 'repaired')
        github = self.github_state()
        self.assertEqual(len(github['pulls']), 1)
        self.assertEqual(frontend_avoid.cited_notes(github['pulls'][0]['body']), {first})

    def test_configuration_cannot_target_the_proposal_branches_or_escape_the_repository(self):
        for settings in ({'base': 'skill-feedback/frontend-avoid-x'}, {'path': '../x.md'}, {'repository': 'owner'},
                         {'gh_path': 'gh'}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                frontend_avoid.propose({**self.config, 'frontend_avoid': {**self.config['frontend_avoid'], **settings}},
                                       self.state)


if __name__ == '__main__':
    unittest.main()
