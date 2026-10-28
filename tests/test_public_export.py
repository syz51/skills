import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import public_export as export


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.remote = self.root / 'remote.git'
        export.git(self.root, 'init', '--bare', '--initial-branch=main', str(self.remote))
        export.git(self.source, 'init', '--initial-branch=main')
        export.git(self.source, 'config', 'user.name', 'Test')
        export.git(self.source, 'config', 'user.email', 'test@example.invalid')
        (self.source / 'skills').mkdir()
        (self.source / 'skills/guide.md').write_text('Public guidance\n')
        (self.source / 'tests').mkdir()
        (self.source / 'tests/test_fixture.py').write_text('import unittest\nclass Test(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n')
        (self.source / 'feedback').mkdir()
        (self.source / 'feedback/private.md').write_text('Private material\n')
        self.commit()
        self.config = {'schema_version': 1, 'author_name': 'Public Maintainer', 'author_email': 'maintainer@users.noreply.github.com', 'source_repo': str(self.source), 'target_url': 'https://github.com/example/skills.git',
                       'state_dir': str(self.root / 'state'), 'files': [{'source': 'skills/guide.md', 'target': 'skills/guide.md'},
                       {'source': 'tests/test_fixture.py', 'target': 'tests/test_fixture.py'}]}
        original = export.git
        def transport(repo, *args):
            if args[:3] == ('remote', 'add', 'origin'):
                args = (*args[:3], str(self.remote))
            return original(repo, *args)
        self.transport = patch.object(export, 'git', side_effect=transport)
        self.transport.start()
        self.addCleanup(self.transport.stop)

    def commit(self):
        export.git(self.source, 'add', '.')
        export.git(self.source, 'commit', '-m', 'Private commit context')

    def head(self):
        return export.git(self.remote, 'rev-parse', 'main').strip()

    def public_edit(self, path, content):
        work = self.root / ('edit-' + str(len(list(self.root.glob('edit-*')))))
        export.git(self.root, 'clone', str(self.remote), str(work))
        export.git(work, 'config', 'user.name', 'Test')
        export.git(work, 'config', 'user.email', 'test@example.invalid')
        if content is None:
            (work / path).unlink()
        else:
            (work / path).write_text(content)
        export.git(work, 'add', '.')
        export.git(work, 'commit', '-m', 'Public change')
        export.git(work, 'push', 'origin', 'main')

    def test_no_private_history_and_idempotence(self):
        first = export.execute(self.config, True)
        self.assertTrue(first['changed'])
        self.assertEqual(export.git(self.remote, 'rev-list', '--count', 'main').strip(), b'1')
        self.assertNotIn('feedback/', '\n'.join(export.tree(self.remote, 'main')))
        self.assertNotIn(b'Private commit context', export.git(self.remote, 'log', '--format=%B', 'main'))
        self.assertFalse(export.execute(self.config, True)['changed'])
        self.assertEqual(self.head().decode(), first['public_commit'])

    def test_reject_paths_urls_and_duplicates(self):
        for path in ('../secret', '/absolute', 'skills/../secret', 'skills//x', '.git/config', '.GIT/config', 'skills\\file'):
            with self.subTest(path=path), self.assertRaises(export.ExportError):
                export.relative(path)
        for url in ('https://token@github.com/example/skills.git', 'file:///tmp/repo', 'git@evil.example:example/skills.git'):
            with self.subTest(url=url), self.assertRaises(export.ExportError):
                export.configuration(dict(self.config, target_url=url))
        with self.assertRaises(export.ExportError):
            export.configuration(dict(self.config, files=self.config['files'] * 2))
        with self.assertRaises(export.ExportError):
            export.configuration(dict(self.config, files=[{'source': 'feedback/private.md', 'target': 'public.md'}]))

    def test_symlink_and_dirty_source_rejected(self):
        (self.source / 'skills/link.md').symlink_to('guide.md')
        with self.assertRaises(export.ExportError):
            export.execute(self.config)
        self.commit()
        config = dict(self.config, files=[{'source': 'skills/link.md', 'target': 'guide.md'}])
        with self.assertRaises(export.ExportError):
            export.execute(config)

    def test_private_scanner(self):
        for text in ('/Us' + 'ers/alice/file', 'roy' + '-mac', 'sunmacbook' + '-air', 'ghp_' + 'a' * 36, '-----BEGIN ' + 'PRIVATE KEY-----'):
            with self.subTest(text_length=len(text)), self.assertRaises(export.ExportError):
                export.scan('test.md', text.encode())
        export.scan('test.md', b'Use FAKE_TOKEN in examples.')

    def test_managed_divergence_stops(self):
        export.execute(self.config, True)
        self.public_edit('skills/guide.md', 'Public edit\n')
        before = self.head()
        with self.assertRaises(export.ExportError):
            export.execute(self.config, True)
        self.assertEqual(self.head(), before)

    def test_public_owned_file_preserved_and_managed_deletion(self):
        export.execute(self.config, True)
        self.public_edit('README.md', 'Community documentation\n')
        config = dict(self.config, files=self.config['files'][1:])
        export.execute(config, True)
        self.assertEqual(export.git(self.remote, 'show', 'main:README.md'), b'Community documentation\n')
        self.assertNotIn('skills/guide.md', export.tree(self.remote, 'main'))
        self.assertFalse(export.execute(config, True)['changed'])

    def test_imported_public_contribution_resumes_publication(self):
        export.execute(self.config, True)
        self.public_edit('skills/guide.md', 'Community improvement\n')
        (self.source / 'skills/guide.md').write_text('Community improvement\n')
        fixture = self.source / 'tests/test_fixture.py'
        fixture.write_text(fixture.read_text() + '# Independent private improvement\n')
        self.commit()
        self.assertTrue(export.execute(self.config, True)['changed'])
        self.assertEqual(export.git(self.remote, 'show', 'main:skills/guide.md'), b'Community improvement\n')
        self.assertFalse(export.execute(self.config, True)['changed'])

    def test_imported_public_deletion_resumes_publication(self):
        export.execute(self.config, True)
        self.public_edit('skills/guide.md', None)
        (self.source / 'skills/guide.md').unlink()
        self.commit()
        config = dict(self.config, files=self.config['files'][1:])
        self.assertTrue(export.execute(config, True)['changed'])
        self.assertNotIn('skills/guide.md', export.tree(self.remote, 'main'))

    def test_partial_public_import_still_blocks(self):
        export.execute(self.config, True)
        self.public_edit('skills/guide.md', 'Community improvement\n')
        self.public_edit('tests/test_fixture.py', '# Another unimported public change\n')
        (self.source / 'skills/guide.md').write_text('Community improvement\n')
        self.commit()
        before = self.head()
        with self.assertRaises(export.ExportError):
            export.execute(self.config, True)
        self.assertEqual(self.head(), before)

    def test_explicit_enrollment_adopts_identical_public_addition(self):
        export.execute(self.config, True)
        name = 'skills/community.md'
        self.public_edit(name, 'Community guide\n')
        (self.source / name).write_text('Community guide\n')
        (self.source / 'skills/guide.md').write_text('Independent private improvement\n')
        self.commit()
        config = dict(self.config, files=[*self.config['files'], {'source': name, 'target': name}])
        self.assertTrue(export.execute(config, True)['changed'])
        self.assertEqual(export.git(self.remote, 'show', 'main:' + name), b'Community guide\n')
        self.assertFalse(export.execute(config, True)['changed'])

    def test_failed_tests_do_not_publish(self):
        export.execute(self.config, True)
        before = self.head()
        (self.source / 'tests/test_fixture.py').write_text('raise RuntimeError("fixture failure")\n')
        self.commit()
        with self.assertRaises(export.ExportError):
            export.execute(self.config, True)
        self.assertEqual(before, self.head())

    def test_recover_successful_push_with_old_or_missing_state(self):
        export.execute(self.config, True)
        state_path = self.root / 'state/export-state.json'
        state_path.unlink()
        self.assertFalse(export.execute(self.config, True)['changed'])
        previous = json.loads(state_path.read_text())
        (self.source / 'skills/guide.md').write_text('Updated guidance\n')
        self.commit()
        with patch.object(export, 'save', side_effect=OSError('failure')):
            with self.assertRaises(OSError):
                export.execute(self.config, True)
        self.assertEqual(json.loads(state_path.read_text()), previous)
        self.assertFalse(export.execute(self.config, True)['changed'])
        self.assertEqual(json.loads(state_path.read_text())['public_commit'], self.head().decode())

    def test_push_race_rejected(self):
        export.execute(self.config, True)
        (self.source / 'skills/guide.md').write_text('Updated guidance\n')
        self.commit()
        original = export.git
        raced = []
        def racing(repo, *args):
            if args[:3] == ('push', 'origin', 'HEAD:refs/heads/main') and not raced:
                raced.append(True)
                self.public_edit('README.md', 'Concurrent public change\n')
            return original(repo, *args)
        with patch.object(export, 'git', side_effect=racing), self.assertRaises(export.ExportError):
            export.execute(self.config, True)
        self.assertEqual(export.git(self.remote, 'show', 'main:skills/guide.md'), b'Public guidance\n')

    def test_manifest_tampering_stops(self):
        export.execute(self.config, True)
        self.public_edit(export.MANIFEST, '{"schema_version":1,"files":{}}\n')
        with self.assertRaises(export.ExportError):
            export.execute(self.config, True)

    def test_new_destination_cannot_overwrite_public_file(self):
        export.execute(self.config, True)
        self.public_edit('README.md', 'Public-owned\n')
        config = dict(self.config, files=[*self.config['files'], {'source': 'skills/guide.md', 'target': 'README.md'}])
        # Use a distinct source, because duplicate sources are prohibited too.
        (self.source / 'public').mkdir()
        (self.source / 'public/README.md').write_text('Replacement\n')
        self.commit()
        config['files'][-1]['source'] = 'public/README.md'
        with self.assertRaises(export.ExportError):
            export.execute(config, True)

    def test_plan_does_not_push_or_save_state(self):
        result = export.execute(self.config)
        self.assertTrue(result['changed'])
        self.assertFalse((self.root / 'state/export-state.json').exists())
        self.assertEqual(export.git(self.remote, 'for-each-ref'), b'')

    def test_recursive_metadata_cleanup_is_scoped(self):
        folder = self.source / 'skills/example'
        folder.mkdir()
        metadata = {'backupPath': '/Us' + 'ers/private', 'upstream': {'revision': 'abc', 'originalPath': '/Us' + 'ers/private'}, 'items': [{'archivePath': 'private', 'license': 'MIT'}]}
        (folder / 'sources.json').write_text(json.dumps(metadata))
        self.commit()
        config = dict(self.config, json_drop_keys=['backupPath', 'originalPath', 'archivePath'], files=[{'source': 'skills/example/sources.json', 'target': 'sources.json'}])
        files = export.snapshot(config, export.clean_source(self.source))
        self.assertEqual(json.loads(files['sources.json']), {'upstream': {'revision': 'abc'}, 'items': [{'license': 'MIT'}]})
        for key in ('fetch_source', 'json_drop_keys'):
            with self.assertRaises(export.ExportError):
                export.configuration(dict(self.config, **{key: 'invalid'}))

    def test_fetch_source_pins_remote_without_changing_checkout(self):
        private_remote = self.root / 'private.git'
        export.git(self.root, 'clone', '--bare', str(self.source), str(private_remote))
        export.git(self.source, 'config', 'remote.origin.url', str(private_remote))
        local_head = export.clean_source(self.source)
        work = self.root / 'upstream-work'
        export.git(self.root, 'clone', str(private_remote), str(work))
        export.git(work, 'config', 'user.name', 'Test')
        export.git(work, 'config', 'user.email', 'test@example.invalid')
        (work / 'skills/guide.md').write_text('Remote maintained guidance\n')
        export.git(work, 'add', '.')
        export.git(work, 'commit', '-m', 'Private maintainer change')
        export.git(work, 'push', 'origin', 'main')
        result = export.execute(dict(self.config, fetch_source=True), True)
        self.assertEqual(export.clean_source(self.source), local_head)
        self.assertNotEqual(result['source_commit'], local_head)
        self.assertEqual(export.git(self.remote, 'show', 'main:skills/guide.md'), b'Remote maintained guidance\n')

    def test_public_owned_test_is_preserved_but_never_executed(self):
        export.execute(self.config, True)
        malicious = 'raise RuntimeError("Public-owned test must not run")\n'
        self.public_edit('tests/test_extra.py', malicious)
        (self.source / 'skills/guide.md').write_text('Updated managed guidance\n')
        self.commit()
        export.execute(self.config, True)
        self.assertEqual(export.git(self.remote, 'show', 'main:tests/test_extra.py'), malicious.encode())
        self.assertNotIn(b'Public-owned test must not run', (self.root / 'state/candidate-tests.log').read_bytes())

    def test_zero_discovered_tests_cannot_publish(self):
        (self.source / 'tests/test_fixture.py').write_text('# No actual tests\n')
        self.commit()
        with self.assertRaises(export.ExportError):
            export.execute(self.config, True)
        self.assertEqual(export.git(self.remote, 'for-each-ref'), b'')

    def test_public_author_is_explicit(self):
        export.execute(self.config, True)
        self.assertEqual(export.git(self.remote, 'log', '-1', '--format=%an <%ae>', 'main').strip(),
                         b'Public Maintainer <maintainer@users.noreply.github.com>')
        for name, value in [('author_name', ''), ('author_email', 'not-an-email'), ('author_name', 'bad\nname')]:
            with self.assertRaises(export.ExportError):
                export.configuration(dict(self.config, **{name: value}))
