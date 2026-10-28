import contextlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import upstream


def run(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class UpstreamChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        # A local repository stands in for GitHub; its directory name ends in .git like a GitHub URL.
        self.source = self.root / 'owner' / 'demo.git'
        self.source.mkdir(parents=True)
        run('git', 'init', '-q', '-b', 'main', cwd=self.source)
        run('git', 'config', 'user.email', 'test@example.com', cwd=self.source)
        run('git', 'config', 'user.name', 'Test', cwd=self.source)
        run('git', 'config', 'uploadpack.allowFilter', 'true', cwd=self.source)
        self.write({'skills/demo/SKILL.md': '# Demo\n', 'skills/demo/references/a.md': 'old\n', 'README.md': 'readme\n'})
        self.commit('Initial skill')
        self.base_revision = run('git', 'rev-parse', 'HEAD', cwd=self.source)
        self.base_tree = run('git', 'rev-parse', 'HEAD:skills/demo', cwd=self.source)
        self.url = 'file://' + str(self.source)
        self.repo = self.root / 'skills-repo'
        (self.repo / 'skills' / 'find-skills').mkdir(parents=True)
        self.cache = self.root / 'cache'
        local = mock.patch.multiple(upstream, REPOSITORY=re.compile(r'file://.*/([\w.-]+)/([\w.-]+)\.git'),
                                    PROTOCOLS=('-c', 'protocol.allow=always'))
        local.start()
        self.addCleanup(local.stop)

    def write(self, files):
        for name, text in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def commit(self, message):
        run('git', 'add', '-A', cwd=self.source)
        run('git', 'commit', '-q', '-m', message, cwd=self.source)

    def record(self, **overrides):
        return {'id': 'demo', 'repository': self.url, 'path': 'skills/demo', 'local': 'references/demo',
                'revision': self.base_revision, 'tree': self.base_tree, 'reviewedAt': '2026-10-01T00:00:00+00:00', **overrides}

    def check(self, *records):
        (self.repo / 'skills/find-skills/sources.json').write_text(json.dumps({'upstreamReview': list(records)}))
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', ['upstream.py', '--repo', str(self.repo), '--cache', str(self.cache)]), \
                contextlib.redirect_stdout(output):
            self.assertEqual(upstream.main(), 0)
        return json.loads(output.getvalue())

    def test_unchanged_source_reports_current_state(self):
        report = self.check(self.record())
        source = report['sources'][0]
        self.assertEqual(source['status'], 'unchanged')
        self.assertEqual(source['current'], {'revision': self.base_revision, 'tree': self.base_tree})
        self.assertEqual(report['counts'], {'unchanged': 1})

    def test_changed_source_patch_applies_to_the_reviewed_snapshot(self):
        self.write({'skills/demo/SKILL.md': '# Demo\n\nNew rule.\n', 'skills/demo/references/b.md': 'new\n'})
        (self.source / 'skills/demo/references/a.md').unlink()
        self.commit('Update skill')
        self.write({'README.md': 'unrelated\n'})
        self.commit('Update readme')
        source = self.check(self.record())['sources'][0]
        self.assertEqual(source['status'], 'changed')
        self.assertEqual(sorted(item['path'] for item in source['files']),
                         ['SKILL.md', 'references/a.md', 'references/b.md'])
        self.assertEqual([line.split(' ', 2)[2] for line in source['commits']], ['Update skill'])
        snapshot = self.root / 'snapshot'
        snapshot.mkdir()
        (snapshot / 'references').mkdir()
        (snapshot / 'SKILL.md').write_text('# Demo\n')
        (snapshot / 'references/a.md').write_text('old\n')
        run('git', 'apply', source['patch'], cwd=snapshot)
        self.assertEqual((snapshot / 'SKILL.md').read_text(), '# Demo\n\nNew rule.\n')
        self.assertEqual((snapshot / 'references/b.md').read_text(), 'new\n')
        self.assertFalse((snapshot / 'references/a.md').exists())

    def test_cached_mirror_sees_later_commits_and_drops_stale_patches(self):
        self.write({'skills/demo/SKILL.md': '# Demo v2\n'})
        self.commit('First update')
        first = self.check(self.record())['sources'][0]
        self.assertEqual(first['status'], 'changed')
        reviewed = run('git', 'rev-parse', 'HEAD', cwd=self.source)
        reviewed_tree = run('git', 'rev-parse', 'HEAD:skills/demo', cwd=self.source)
        self.assertEqual(self.check(self.record(revision=reviewed, tree=reviewed_tree))['sources'][0]['status'], 'unchanged')
        self.assertFalse(Path(first['patch']).exists())
        self.write({'skills/demo/SKILL.md': '# Demo v3\n'})
        self.commit('Second update')
        later = self.check(self.record(revision=reviewed, tree=reviewed_tree))['sources'][0]
        self.assertEqual(later['status'], 'changed')
        self.assertEqual(len(later['commits']), 1)

    def test_unknown_or_vanished_baselines_list_current_files(self):
        report = self.check(self.record(revision=None, tree=None, reviewedAt=None),
                            self.record(id='gone', tree='0' * 40, revision='0' * 40))
        unknown, gone = report['sources']
        self.assertEqual(unknown['status'], 'baseline-unknown')
        self.assertEqual(unknown['files'], ['SKILL.md', 'references/a.md'])
        self.assertEqual(gone['status'], 'baseline-unknown')
        self.assertIn('unavailable upstream', gone['note'])

    def test_snapshot_hashing_preserves_literal_filenames_across_batches(self):
        snapshot = self.repo / 'skills/find-skills/references/demo'
        (snapshot / 'references').mkdir(parents=True)
        (snapshot / 'SKILL.md').write_text('# Demo\n')
        (snapshot / 'references/a.md').write_text('old\n')
        names = ('local\nnotes.md', 'with\ttab.md', 'café.md', 'quote"\\file.md') + tuple(
            f'extra-{index}.md' for index in range(65))
        for name in names:
            (snapshot / name).write_text('Local guidance\n')
        self.write({'skills/demo/SKILL.md': '# Demo v2\n'})
        self.commit('Update upstream guidance')
        result = self.check(self.record())['sources'][0]
        self.assertEqual(result['status'], 'changed')
        self.assertEqual(result['localEdits'], {
            'modified': [], 'upstreamOnly': [], 'localOnly': sorted(names)})

    def test_unknown_baseline_lists_literal_unicode_and_control_filenames(self):
        name = '参考\n"结果.md'
        self.write({'skills/demo/' + name: 'Guidance\n'})
        self.commit('Add a literal filename')
        result = self.check(self.record(revision=None, tree=None, reviewedAt=None))['sources'][0]
        self.assertEqual(result['status'], 'baseline-unknown')
        self.assertEqual(set(result['files']), {'SKILL.md', 'references/a.md', name})
        self.assertEqual(result['fileCount'], 3)

    def test_removed_path_and_repository_root(self):
        root_tree = run('git', 'rev-parse', 'HEAD^{tree}', cwd=self.source)
        self.write({'LICENSE': 'MIT\n'})
        self.commit('Add license')
        removed, whole = self.check(self.record(path='skills/missing'),
                                    self.record(id='root', path='', tree=root_tree))['sources']
        self.assertEqual(removed['status'], 'removed')
        self.assertEqual(whole['status'], 'changed')
        self.assertEqual([item['path'] for item in whole['files']], ['LICENSE'])

    def test_unreachable_repository_is_attempted_once(self):
        missing = 'file://' + str(self.root / 'owner' / 'absent.git')
        with mock.patch.object(upstream, 'refresh', wraps=upstream.refresh) as refresh:
            report = self.check(self.record(repository=missing), self.record(id='again', repository=missing))
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual([source['status'] for source in report['sources']], ['error', 'error'])


    def test_special_files_are_flagged(self):
        (self.source / 'skills/demo/run.sh').write_text('#!/bin/sh\n')
        (self.source / 'skills/demo/run.sh').chmod(0o755)
        (self.source / 'skills/demo/link').symlink_to('../../../etc')
        (self.source / 'skills/demo/image.png').write_bytes(bytes(range(256)))
        self.write({'skills/demo/references/a.md': 'new\n'})
        self.commit('Add files')
        source = self.check(self.record())['sources'][0]
        modes = {item['path']: (item['mode'], item['binary']) for item in source['files']}
        self.assertEqual(modes['run.sh'], ('100755', False))
        self.assertEqual(modes['link'], ('120000', False))
        self.assertTrue(modes['image.png'][1])
        self.assertEqual(sorted(source['special']), ['image.png', 'link', 'run.sh'])
        patch = Path(source['patch']).read_text()
        self.assertIn('a/references/a.md', patch)
        for name in ('run.sh', 'link', 'image.png'):
            self.assertNotIn(name, patch)

    def test_removed_links_and_dropped_executable_bits_are_special(self):
        (self.source / 'skills/demo/alias.md').symlink_to('SKILL.md')
        (self.source / 'skills/demo/tool.sh').write_text('#!/bin/sh\n')
        (self.source / 'skills/demo/tool.sh').chmod(0o755)
        self.commit('Add link and tool')
        revision = run('git', 'rev-parse', 'HEAD', cwd=self.source)
        tree = run('git', 'rev-parse', 'HEAD:skills/demo', cwd=self.source)
        (self.source / 'skills/demo/alias.md').unlink()
        (self.source / 'skills/demo/tool.sh').chmod(0o644)
        self.write({'skills/demo/references/a.md': 'new\n'})
        self.commit('Remove link and executable bit')
        source = self.check(self.record(revision=revision, tree=tree))['sources'][0]
        self.assertEqual(sorted(source['special']), ['alias.md', 'tool.sh'])
        patch = Path(source['patch']).read_text()
        self.assertNotIn('alias.md', patch)
        self.assertNotIn('tool.sh', patch)
        self.assertIn('references/a.md', patch)

    def test_non_utf8_commit_subject_does_not_stop_the_run(self):
        self.write({'skills/demo/SKILL.md': '# Demo v2\n'})
        run('git', 'add', '-A', cwd=self.source)
        message = self.root / 'message'
        message.write_bytes(b'caf\xe9 update\n')
        subprocess.run(['git', 'commit', '-q', '-F', str(message)], cwd=self.source, check=True, capture_output=True)
        source = self.check(self.record())['sources'][0]
        self.assertEqual(source['status'], 'changed')
        self.assertEqual(len(source['commits']), 1)
        self.assertEqual(upstream.text(b'caf\xe9 \n'), 'caf\ufffd')

    def test_renamed_default_branch_is_followed(self):
        self.check(self.record())
        run('git', 'branch', '-m', 'main', 'trunk', cwd=self.source)
        self.write({'skills/demo/SKILL.md': '# Demo on trunk\n'})
        self.commit('Trunk update')
        source = self.check(self.record())['sources'][0]
        self.assertEqual(source['status'], 'changed')
        self.assertEqual(source['current']['revision'], run('git', 'rev-parse', 'trunk', cwd=self.source))

    def test_interrupted_clone_is_replaced(self):
        broken = self.cache / 'owner__demo.git'
        broken.mkdir(parents=True)
        (broken / 'objects').mkdir()
        self.assertEqual(self.check(self.record())['sources'][0]['status'], 'unchanged')
        self.assertTrue((broken / 'HEAD').is_file())

    def test_inherited_git_environment_is_ignored(self):
        with mock.patch.dict('os.environ', {'GIT_DIR': str(self.root / 'nowhere'), 'GIT_CONFIG_PARAMETERS': "'protocol.allow'='always'"}):
            self.assertEqual(self.check(self.record())['sources'][0]['status'], 'unchanged')

    def test_only_https_transport_is_allowed(self):
        with mock.patch.object(upstream, 'PROTOCOLS', ('-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always')):
            source = self.check(self.record())['sources'][0]
        self.assertEqual(source['status'], 'error')
        self.assertFalse((self.cache / 'owner__demo.git').exists())

    def test_local_snapshot_differences_are_reported(self):
        snapshot = self.repo / 'skills/find-skills/references/demo'
        (snapshot / 'references').mkdir(parents=True)
        (snapshot / 'guide.md').write_text('# Adapted\n')
        (snapshot / 'references/a.md').write_text('locally fixed\n')
        self.write({'skills/demo/SKILL.md': '# Demo v2\n'})
        self.commit('Update')
        source = self.check(self.record())['sources'][0]
        self.assertEqual(source['localEdits'], {'modified': ['references/a.md'], 'upstreamOnly': ['SKILL.md'],
                                                'localOnly': ['guide.md']})
        self.assertNotIn('localEdits', self.check(self.record(local='.'))['sources'][0])


class RecordValidation(unittest.TestCase):
    def valid(self, **overrides):
        return {'id': 'vue', 'repository': 'https://github.com/antfu/skills.git', 'path': 'skills/vue',
                'local': 'references/frameworks/vue', 'revision': 'a' * 40, 'tree': 'b' * 40,
                'reviewedAt': '2026-10-08T08:49:15+00:00', **overrides}

    def test_accepts_github_https_records_and_unknown_baselines(self):
        self.assertIsNone(upstream.invalid(self.valid(), set()))
        self.assertIsNone(upstream.invalid(self.valid(revision=None, tree=None, reviewedAt=None), set()))
        self.assertIsNone(upstream.invalid(self.valid(path=''), set()))

    def test_rejects_unsafe_or_incomplete_records(self):
        for overrides in ({'repository': 'git@github.com:antfu/skills.git'}, {'repository': 'http://github.com/antfu/skills.git'},
                          {'repository': 'https://github.com/antfu/skills.git/../x.git'}, {'repository': 'file:///tmp/x.git'},
                          {'repository': 'https://example.com/antfu/skills.git'}, {'path': '../outside'}, {'path': '/etc'},
                          {'revision': None}, {'tree': 'abc'}, {'id': 'bad id'}, {'local': None},
                          {'local': '../other-skill'}, {'local': '/tmp'}):
            with self.subTest(overrides=overrides):
                self.assertIsNotNone(upstream.invalid(self.valid(**overrides), set()))
        self.assertIsNotNone(upstream.invalid(self.valid(), {'vue'}))

    def test_invalid_records_are_reported_without_fetching(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            (repo / 'skills/find-skills').mkdir(parents=True)
            (repo / 'skills/find-skills/sources.json').write_text(json.dumps({'upstreamReview': [self.valid(repository='https://example.com/x/y.git')]}))
            output = io.StringIO()
            with mock.patch.object(sys, 'argv', ['upstream.py', '--repo', str(repo), '--cache', str(repo / 'cache')]), \
                    mock.patch.object(upstream, 'refresh') as refresh, contextlib.redirect_stdout(output):
                upstream.main()
            refresh.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())['sources'][0]['status'], 'error')


if __name__ == '__main__':
    unittest.main()
