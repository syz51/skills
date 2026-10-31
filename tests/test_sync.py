import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import evidence

spec = importlib.util.spec_from_file_location('sync', Path(__file__).parents[1] / 'scripts/sync.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)

class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.remote = self.root / 'remote.git'
        subprocess.run(['git', 'init', '--bare', '--initial-branch=main', str(self.remote)], check=True, capture_output=True)
        # Auto-maintenance must finish before temporary repositories are removed.
        for key in ('gc.autoDetach', 'maintenance.autoDetach'):
            sync.git(self.remote, 'config', key, 'false')
        self.repo = self.root / 'repo'
        subprocess.run(['git', 'clone', str(self.remote), str(self.repo)], check=True, capture_output=True)
        for key in ('gc.autoDetach', 'maintenance.autoDetach'):
            sync.git(self.repo, 'config', key, 'false')
        sync.git(self.repo, 'config', 'user.email', 'test@example.invalid')
        sync.git(self.repo, 'config', 'user.name', 'Test')
        (self.repo / 'README.md').write_text('fixture\n')
        sync.git(self.repo, 'add', '.')
        sync.git(self.repo, 'commit', '-m', 'Initialize')
        sync.git(self.repo, 'push', 'origin', 'main')
        self.validator = self.root / 'validator.py'
        self.validator.write_text('import sys\nfrom pathlib import Path\nassert (Path(sys.argv[1]) / "SKILL.md").exists()\n')
        self.runner_cache = self.root / 'validator-dependency-ready'
        self.runner_failure = self.root / 'validator-dependency-failure'
        runner_dir = self.root / 'bin'
        runner_dir.mkdir()
        runner = runner_dir / 'uv'
        runner.write_text(f'''#!{sys.executable}
import pathlib, subprocess, sys
args = sys.argv[1:]
cache = pathlib.Path({str(self.runner_cache)!r})
failure = pathlib.Path({str(self.runner_failure)!r})
if failure.exists() or ('--offline' in args and not cache.exists()):
    print('Dependency setup failed: https://private:secret@registry.invalid/pyyaml', file=sys.stderr)
    raise SystemExit(1)
assert args[0] == 'run' and args[args.index('--with') + 1] == 'pyyaml'
cache.write_text('dependency available')
python = args[args.index('--python') + 1]
script = args.index('--script') + 1
raise SystemExit(subprocess.run([python, *args[script:]]).returncode)
''')
        runner.chmod(0o755)
        runner_path = patch.dict(os.environ, {'PATH': str(runner_dir) + os.pathsep + os.environ.get('PATH', '')})
        runner_path.start()
        self.addCleanup(runner_path.stop)
        self.args = SimpleNamespace(repo=self.repo, host_id='client-host', feedback_dir=self.root / 'feedback', install_root=self.root / 'installed', validator=self.validator, bootstrap=False)

    def tearDown(self):
        self.temp.cleanup()

    def note(self, content='feedback', host='client-host', name='same.md'):
        self.args.host_id = host
        path = self.args.feedback_dir / 'frontend-ui' / 'notes' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def commit(self, message='Update'):
        sync.git(self.repo, 'add', '.')
        sync.git(self.repo, 'commit', '-m', message)

    def release(self):
        for skill in sync.SKILLS:
            folder = self.repo / 'skills' / skill
            folder.mkdir(parents=True)
            (folder / 'SKILL.md').write_text('---\nname: ' + skill + '\ndescription: Fixture\n---\n# Fixture\n')
        self.commit()
        self.args.bootstrap = True
        self.assertTrue(sync.publish(self.args))
        self.args.bootstrap = False

    def prompt_release(self):
        sync.prompt_runtime.copy_bundle(sync.prompt_runtime.DEFAULT_ROOT, self.repo)
        self.commit('Add bounded prompt bundle')
        sync.git(self.repo, 'push', 'origin', 'main')

    def test_new_tauri_skill_publishes_and_deploys_from_existing_release(self):
        old_registry = tuple(skill for skill in sync.SKILLS if skill != 'tauri')
        with patch.object(sync, 'SKILLS', old_registry):
            self.release()
            self.assertTrue(sync.deploy(self.args))
        previous = (self.args.install_root / 'current').resolve()
        folder = self.repo / 'skills/tauri'
        folder.mkdir()
        (folder / 'SKILL.md').write_text('---\nname: tauri\ndescription: Native applications\n---\n# Tauri\n')
        review = self.repo / 'feedback/tauri/reviews/enrollment.md'
        review.parent.mkdir(parents=True)
        review.write_text('Explicit enrollment review.\n')
        self.commit('Enroll Tauri with review')
        self.assertTrue(sync.publish(self.args))
        self.assertTrue(sync.deploy(self.args))
        current = self.args.install_root / 'current'
        self.assertNotEqual(current.resolve(), previous)
        self.assertEqual((current / 'skills/tauri/SKILL.md').read_bytes(), (folder / 'SKILL.md').read_bytes())
        self.assertTrue((previous / 'skills/frontend-ui/SKILL.md').is_file())
        self.assertEqual((self.args.feedback_dir / 'tauri/reviews/enrollment.md').read_bytes(), review.read_bytes())

    def test_typescript_enrollment_and_supporting_file_evolution(self):
        old_registry = tuple(skill for skill in sync.SKILLS if skill != 'typescript')
        with patch.object(sync, 'SKILLS', old_registry):
            self.release()
            self.assertTrue(sync.deploy(self.args))
        previous = (self.args.install_root / 'current').resolve()
        previous_entrypoint = (previous / 'skills/frontend-ui/SKILL.md').read_bytes()
        folder = self.repo / 'skills/typescript'
        files = {
            'SKILL.md': b'---\nname: typescript\ndescription: TypeScript guidance\n---\n# TypeScript\n',
            'references/tooling-and-public-types.md': b'# Consumer checks\n',
            'references/nested/coverage.md': b'# Check scope\n',
            'scripts/check-types.py': b'print("check types")\n',
            'assets/templates/consumer.ts': b'export const name: string = "fixture";\n',
            'agents/openai.yaml': b'policy:\n  allow_implicit_invocation: true\n',
        }
        for name, content in files.items():
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        review = self.repo / 'feedback/typescript/reviews/enrollment.md'
        review.parent.mkdir(parents=True)
        review.write_text('Reviewed the complete TypeScript skill tree.\n')
        self.commit('Enroll TypeScript with supporting files')
        self.assertTrue(sync.publish(self.args))
        self.assertTrue(sync.deploy(self.args))
        current = self.args.install_root / 'current'
        enrolled = current.resolve()
        self.assertNotEqual(enrolled, previous)
        for name, content in files.items():
            with self.subTest(enrollment=name):
                self.assertEqual((current / 'skills/typescript' / name).read_bytes(), content)
        self.assertEqual((previous / 'skills/frontend-ui/SKILL.md').read_bytes(), previous_entrypoint)
        self.assertFalse((previous / 'skills/typescript').exists())
        self.assertEqual((self.args.feedback_dir / 'typescript/reviews/enrollment.md').read_bytes(), review.read_bytes())

        # References and owned code can evolve without changing the entrypoint,
        # but still need the same per-skill review as an entrypoint edit.
        updated = {
            'references/nested/coverage.md': b'# Check scope\nVerify the affected consumer.\n',
            'scripts/check-types.py': b'print("check the affected package")\n',
        }
        for name, content in updated.items():
            (folder / name).write_bytes(content)
        self.commit('Revise TypeScript reference and owned helper')
        with self.assertRaisesRegex(sync.SyncError, 'review record'):
            sync.publish(self.args)
        self.assertEqual(current.resolve(), enrolled)
        evolution_review = review.with_name('supporting-files.md')
        evolution_review.write_text('Reviewed nested reference and authored helper changes.\n')
        self.commit('Review TypeScript supporting-file changes')
        self.assertTrue(sync.publish(self.args))
        self.assertTrue(sync.deploy(self.args))
        self.assertNotEqual(current.resolve(), enrolled)
        for name, content in {**files, **updated}.items():
            with self.subTest(evolution=name):
                self.assertEqual((current / 'skills/typescript' / name).read_bytes(), content)
        for name in updated:
            self.assertEqual((enrolled / 'skills/typescript' / name).read_bytes(), files[name])
        self.assertEqual((self.args.feedback_dir / 'typescript/reviews/supporting-files.md').read_bytes(), evolution_review.read_bytes())

    def test_deploy_propagates_prompt_bundle_and_refuses_installed_edits(self):
        self.release()
        self.prompt_release()
        self.assertTrue(sync.deploy(self.args))
        current = self.args.install_root / 'current'
        manifest = json.loads((current / 'manifest.json').read_text())
        self.assertEqual(manifest['promptsSha256'], sync.prompt_runtime.bundle_hash(self.repo))
        self.assertEqual(sync.prompt_runtime.bundle_hash(current), manifest['promptsSha256'])
        (current / 'prompts/extract.md').write_text('Local prompt edit')
        with self.assertRaisesRegex(sync.SyncError, 'Installed prompts'):
            sync.deploy(self.args)

    def test_deploy_rejects_changed_immutable_prompt_release(self):
        self.release()
        self.prompt_release()
        self.assertTrue(sync.deploy(self.args))
        current = self.args.install_root / 'current'
        release = current.resolve()
        current.unlink()
        (release / 'prompts/correct.md').unlink()
        with self.assertRaises(sync.SyncError):
            sync.deploy(self.args)

    def test_validate_rejects_partial_prompt_bundle(self):
        self.release()
        (self.repo / 'prompts').mkdir()
        (self.repo / 'prompts/extract.md').write_text('Partial bundle')
        with self.assertRaisesRegex(sync.SyncError, 'Invalid prompt bundle'):
            sync.validate(self.repo)

    def test_publish_still_rejects_prompt_changes(self):
        self.release()
        self.prompt_release()
        (self.repo / 'prompts/extract.md').write_text('Maintenance must not publish prompt changes')
        with self.assertRaises(sync.SyncError):
            sync.publish(self.args)

    def receipt(self, content='feedback', host='client-host'):
        digest = sync.digest(content.encode())
        path = self.args.feedback_dir / 'frontend-ui/verifications' / (digest + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {'schema_version': 1, 'noteSha256': digest, 'sourceMachine': host, 'skill': 'frontend-ui',
                 'thread': 'thread1', 'turn': 'turn1', 'sourceSha256': 'a' * 64,
                 'completed_at': '2026-10-14T01:00:00Z', 'checked_at': '2026-10-15T01:00:00Z',
                 'loadedReads': [], 'status': 'supported', 'summary': 'Original correction matches; outcome remains reported.',
                 'scope': 'original-turn messages; artifacts and runtime outcomes not replayed'}
        path.write_text(json.dumps(value))
        return path

    def publish_review(self, content, names=('review.md',)):
        directory = self.repo / 'feedback/frontend-ui/reviews'
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            (directory / name).write_text(content)
        self.commit('Review ' + content)
        self.assertTrue(sync.publish(self.args))
        return sync.git(self.repo, 'rev-parse', 'HEAD').decode().strip()

    def interrupted_deploy(self, destination=None):
        destination = destination or self.args.install_root / 'current'
        replace = sync.os.replace
        def interrupt(source, target):
            if Path(target) == destination:
                raise OSError('Interrupted deployment fixture')
            return replace(source, target)
        with patch.object(sync.os, 'replace', side_effect=interrupt):
            with self.assertRaisesRegex(OSError, 'Interrupted deployment fixture'):
                sync.deploy(self.args)

    def test_upload_is_idempotent_and_preserves_source(self):
        path = self.note()
        self.assertTrue(sync.upload(self.args))
        first = sync.git(self.repo, 'ls-remote', 'origin', 'refs/heads/inbox/client-host')
        self.assertTrue(sync.upload(self.args))
        self.assertEqual(first, sync.git(self.repo, 'ls-remote', 'origin', 'refs/heads/inbox/client-host'))
        self.assertEqual(path.read_text(), 'feedback')

    def test_upload_snapshots_receipts_before_staging_notes(self):
        self.note()
        self.receipt()
        source = self.args.feedback_dir / 'frontend-ui/notes'
        original_glob = Path.glob
        arrived = False
        def concurrent_arrival(path, pattern, **options):
            nonlocal arrived
            snapshot = list(original_glob(path, pattern, **options))
            if path == source and pattern == '*.md' and not arrived:
                arrived = True
                self.note('late feedback', name='late.md')
                self.receipt('late feedback')
            return iter(snapshot)
        with patch.object(Path, 'glob', concurrent_arrival):
            self.assertTrue(sync.upload(self.args))
        self.assertTrue(arrived)
        self.assertTrue(sync.fetch(self.repo))
        ref = 'refs/remotes/origin/inbox/client-host'
        self.assertEqual({data for _, data, _ in sync.note_records(self.repo, ref)}, {b'feedback'})
        self.assertEqual(len(list(sync.verification_records(self.repo, ref))), 1)
        self.assertTrue(sync.upload(self.args))
        self.assertTrue(sync.fetch(self.repo))
        self.assertEqual({data for _, data, _ in sync.note_records(self.repo, ref)}, {b'feedback', b'late feedback'})
        self.assertEqual(len(list(sync.verification_records(self.repo, ref))), 2)

    def test_upload_origin_uses_the_exact_note_bytes_staged(self):
        note = self.note()
        self.receipt('changed during upload')
        original_read = Path.read_bytes
        def change_after_read(path):
            data = original_read(path)
            if path == note:
                note.write_text('changed during upload')
            return data
        with patch.object(Path, 'read_bytes', change_after_read):
            with self.assertRaisesRegex(sync.SyncError, 'no originating note'):
                sync.upload(self.args)
        self.assertFalse(sync.git(self.repo, 'ls-remote', 'origin', 'refs/heads/inbox/client-host'))
        self.assertTrue(sync.upload(self.args))
        self.assertTrue(sync.fetch(self.repo))
        self.assertEqual(len(list(sync.verification_records(self.repo, 'refs/remotes/origin/inbox/client-host'))), 1)

    def test_verified_changed_note_preserves_history_through_deployment(self):
        self.release()
        original = self.note()
        original_receipt = self.receipt()
        original_receipt_bytes = original_receipt.read_bytes()
        self.assertTrue(sync.upload(self.args))
        original.write_text('updated feedback')
        self.receipt('updated feedback')
        self.note('unrelated feedback', name='unrelated.md')
        self.receipt('unrelated feedback')
        self.assertTrue(sync.upload(self.args))
        ref = 'refs/remotes/origin/inbox/client-host'
        self.assertTrue(sync.fetch(self.repo))
        notes = list(sync.note_records(self.repo, ref, 'client-host'))
        self.assertEqual({data for _, data, _ in notes}, {b'feedback', b'updated feedback', b'unrelated feedback'})
        changed = next(name for name, data, _ in notes if data == b'updated feedback')
        self.assertEqual(Path(changed).name, 'same--' + sync.digest(b'updated feedback') + '.md')
        receipts = dict(sync.verification_records(self.repo, ref, 'client-host'))
        saved = 'feedback/frontend-ui/verifications/client-host/' + original_receipt.name
        self.assertEqual(receipts[saved], original_receipt_bytes)
        self.assertEqual(original_receipt.read_bytes(), original_receipt_bytes)
        inbox = sync.git(self.repo, 'rev-parse', ref)
        self.assertTrue(sync.upload(self.args))
        self.assertEqual(sync.git(self.repo, 'ls-remote', 'origin', 'refs/heads/inbox/client-host').split()[0], inbox.strip())
        self.assertTrue(sync.collect(self.args))
        self.assertTrue(sync.publish(self.args))
        self.assertTrue(sync.deploy(self.args))
        self.assertEqual(len(list(sync.verification_records(self.repo, 'HEAD'))), 3)

    def test_edited_alias_recreates_pruned_transport_inbox(self):
        self.release()
        for host in sync.HOSTS:
            self.note(host=host)
            self.receipt(host=host)
            self.assertTrue(sync.upload(self.args))
        self.assertTrue(sync.collect(self.args))
        self.assertTrue(sync.publish(self.args))
        self.assertTrue(sync.deploy(self.args))
        alias = self.repo / 'feedback/frontend-ui/notes/maintainer-host/same.json'
        self.assertTrue(alias.is_file())
        self.assertFalse(alias.with_suffix('.md').exists())
        sync.git(self.repo, 'push', 'origin', ':refs/heads/inbox/maintainer-host')
        self.note('edited alias feedback', host='maintainer-host')
        self.receipt('edited alias feedback', host='maintainer-host')
        self.assertTrue(sync.upload(self.args))
        self.assertTrue(sync.fetch(self.repo))
        ref = 'refs/remotes/origin/inbox/maintainer-host'
        records = list(sync.note_records(self.repo, ref, 'maintainer-host'))
        self.assertEqual({data for _, data, _ in records}, {b'feedback', b'edited alias feedback'})
        edited = next(name for name, data, _ in records if data == b'edited alias feedback')
        self.assertEqual(Path(edited).name, 'same--' + sync.digest(b'edited alias feedback') + '.md')
        self.assertEqual(len(list(sync.verification_records(self.repo, ref, 'maintainer-host'))), 2)
        before = sync.git(self.repo, 'ls-remote', 'origin', 'refs/heads/inbox/maintainer-host')
        self.assertTrue(sync.upload(self.args))
        self.assertEqual(sync.git(self.repo, 'ls-remote', 'origin', 'refs/heads/inbox/maintainer-host'), before)
        self.assertTrue(sync.collect(self.args))
        self.assertTrue(sync.publish(self.args))
        self.assertTrue(sync.deploy(self.args))

    def test_receipt_origin_cannot_borrow_another_host_or_missing_note(self):
        self.release()
        note = self.note(host='maintainer-host')
        receipt = self.receipt(host='maintainer-host')
        self.assertTrue(sync.upload(self.args))
        self.assertTrue(sync.collect(self.args))
        self.assertTrue(sync.publish(self.args))
        note.unlink()
        self.args.host_id = 'client-host'
        self.receipt(host='client-host')
        with self.assertRaisesRegex(sync.SyncError, 'no originating note'):
            sync.upload(self.args)
        receipt.unlink()
        self.receipt('never saved', host='client-host')
        with self.assertRaisesRegex(sync.SyncError, 'no originating note'):
            sync.upload(self.args)

    def test_cold_validator_dependency_cache_allows_publish_and_deploy(self):
        self.assertFalse(self.runner_cache.exists())
        self.release()
        self.assertTrue(self.runner_cache.exists())
        self.runner_cache.unlink()
        self.assertTrue(sync.deploy(self.args))
        self.assertTrue(self.runner_cache.exists())
        current = self.args.install_root / 'current'
        self.assertEqual(json.loads((current / 'manifest.json').read_text())['commit'],
                         sync.git(self.repo, 'rev-parse', 'HEAD').decode().strip())

    def test_validator_and_dependency_failures_preserve_installed_release(self):
        self.release()
        self.assertTrue(sync.deploy(self.args))
        current = self.args.install_root / 'current'
        previous = current.resolve()
        previous_manifest = (current / 'manifest.json').read_bytes()
        (self.repo / 'skills/frontend-ui/SKILL.md').write_text('Reviewed next release')
        review = self.repo / 'feedback/frontend-ui/reviews/validator-recovery.md'
        review.parent.mkdir(parents=True)
        review.write_text('Reviewed update')
        self.commit()
        self.assertTrue(sync.publish(self.args))
        valid_validator = self.validator.read_text()
        for failure in ('dependency', 'validator'):
            with self.subTest(failure=failure):
                if failure == 'dependency':
                    self.runner_failure.touch()
                else:
                    self.validator.write_text('import sys\nprint("private validation output")\nsys.exit(1)\n')
                try:
                    with self.assertRaisesRegex(sync.SyncError, 'Skill validation command failed for frontend-ui') as caught:
                        sync.deploy(self.args)
                    self.assertNotIn('private', str(caught.exception))
                    self.assertNotIn('secret', str(caught.exception))
                    self.assertEqual(current.resolve(), previous)
                    self.assertEqual((current / 'manifest.json').read_bytes(), previous_manifest)
                finally:
                    self.runner_failure.unlink(missing_ok=True)
                    self.validator.write_text(valid_validator)
        self.assertTrue(sync.deploy(self.args))
        self.assertNotEqual(current.resolve(), previous)

    def test_collect_deduplicates_and_preserves_conflicts(self):
        self.note()
        sync.upload(self.args)
        self.note(host='maintainer-host')
        sync.upload(self.args)
        sync.collect(self.args)
        self.assertEqual(len(list((self.repo / 'feedback').rglob('*.md'))), 1)
        self.assertEqual(len(list((self.repo / 'feedback').rglob('*.json'))), 2)
        sync.collect(self.args)
        self.assertFalse(sync.git(self.repo, 'status', '--porcelain').strip())
        self.note('different', host='maintainer-host')
        sync.upload(self.args)
        sync.collect(self.args)
        notes = list((self.repo / 'feedback').rglob('*.md'))
        self.assertEqual({p.read_text() for p in notes}, {'feedback', 'different'})
        for note in notes:
            meta = json.loads(note.with_suffix('.json').read_text())
            self.assertEqual(meta['sha256'], sync.digest(note.read_bytes()))

    def test_deduplicated_host_receipts_publish_and_deploy_with_exact_provenance(self):
        self.release()
        original_receipts = {}
        for host in sync.HOSTS:
            self.note(host=host)
            path = self.receipt(host=host)
            original_receipts[host] = path.read_bytes()
            self.assertTrue(sync.upload(self.args))
        self.assertTrue(sync.collect(self.args))
        self.assertEqual(len(list((self.repo / 'feedback').rglob('*.md'))), 1)
        notes = list(sync.note_records(self.repo, 'HEAD'))
        self.assertEqual({meta['sourceMachine'] for _, _, meta in notes}, set(sync.HOSTS))
        self.assertEqual({data for _, data, _ in notes}, {b'feedback'})
        receipts = list(sync.verification_records(self.repo, 'HEAD'))
        self.assertEqual(len(receipts), 2)
        for name, data in receipts:
            self.assertEqual(data, original_receipts[Path(name).parts[3]])
        report = evidence.assessment_inventory(self.repo / 'feedback')
        self.assertEqual((report['notes'], report['assessments']), (1, 2))
        self.assertTrue(sync.publish(self.args))
        published = sync.git(self.repo, 'rev-parse', 'HEAD')
        self.assertTrue(sync.deploy(self.args))
        self.assertEqual(json.loads((self.args.install_root / 'current/manifest.json').read_text())['commit'], published.decode().strip())
        self.assertTrue(sync.collect(self.args))
        self.assertTrue(sync.publish(self.args))
        self.assertEqual(sync.git(self.repo, 'rev-parse', 'HEAD'), published)
        self.assertFalse(sync.git(self.repo, 'status', '--porcelain').strip())

    def test_unicode_and_control_characters_in_note_names_survive_full_transport(self):
        self.release()
        names = ('反馈.md', 'with\ttab.md', 'with\nnewline.md', 'literal[glob].md')
        for name in names:
            self.note(content='Feedback for ' + name, name=name)
            self.receipt(content='Feedback for ' + name)
        self.assertTrue(sync.upload(self.args))
        self.assertTrue(sync.collect(self.args))
        records = list(sync.note_records(self.repo, 'HEAD'))
        self.assertEqual({meta['filename'] for _, _, meta in records}, set(names))
        for name in names:
            self.assertEqual((self.repo / 'feedback/frontend-ui/notes/client-host' / name).read_text(), 'Feedback for ' + name)
        self.assertEqual(len(list(sync.verification_records(self.repo, 'HEAD'))), len(names))
        review = self.repo / 'feedback/frontend-ui/reviews/评审\n结果.md'
        review.parent.mkdir(parents=True)
        review.write_text('Reviewed the policy update.')
        (self.repo / 'skills/frontend-ui/SKILL.md').write_text('Reviewed policy update')
        self.commit('Reviewed update with a Unicode review path')
        self.assertTrue(sync.publish(self.args))
        self.assertTrue(sync.deploy(self.args))
        self.assertEqual((self.args.feedback_dir / 'frontend-ui/reviews' / review.name).read_text(), review.read_text())
        self.assertTrue(sync.collect(self.args))
        self.assertFalse(sync.git(self.repo, 'status', '--porcelain').strip())

    def test_provenance_alias_requires_exact_validated_canonical_note(self):
        self.note()
        self.assertTrue(sync.upload(self.args))
        self.assertTrue(sync.collect(self.args))
        canonical = self.repo / 'feedback/frontend-ui/notes/client-host/same.json'
        metadata = {**json.loads(canonical.read_text()), 'sourceMachine': 'maintainer-host'}
        alias = self.repo / 'feedback/frontend-ui/notes/maintainer-host/same.json'
        alias.parent.mkdir(parents=True)
        alias.write_text(json.dumps(metadata))
        self.commit('Preserve duplicate host provenance')
        self.assertEqual(len(list(sync.note_records(self.repo, 'HEAD', 'maintainer-host'))), 1)
        wrong_name = alias.with_name('unrelated.json')
        alias.rename(wrong_name)
        self.commit('Incorrect alias basename fixture')
        with self.assertRaisesRegex(sync.SyncError, 'provenance filename'):
            list(sync.note_records(self.repo, 'HEAD'))
        wrong_name.rename(alias)
        self.commit('Restore alias basename fixture')
        collision = alias.with_name('same--' + metadata['sha256'] + '.json')
        alias.rename(collision)
        self.commit('Collision alias basename fixture')
        collision_records = list(sync.note_records(self.repo, 'HEAD', 'maintainer-host'))
        self.assertEqual(Path(collision_records[0][0]).name, 'same--' + metadata['sha256'] + '.md')
        collision.rename(alias)
        self.commit('Restore original alias fixture')
        for changes in ({'filename': 'different.md'}, {'sha256': 'a' * 64},
                        {'sourceMachine': 'client-host'}, {'skill': 'find-skills'}):
            with self.subTest(changes=changes):
                alias.write_text(json.dumps({**metadata, **changes}))
                self.commit('Invalid alias fixture')
                with self.assertRaisesRegex(sync.SyncError, 'provenance'):
                    list(sync.note_records(self.repo, 'HEAD'))
        alias.unlink()
        alias.symlink_to('../client-host/same.json')
        self.commit('Symlink alias fixture')
        with self.assertRaisesRegex(sync.SyncError, 'Unsafe provenance'):
            list(sync.note_records(self.repo, 'HEAD'))

    def test_offline_upload_keeps_buffer(self):
        path = self.note()
        sync.git(self.repo, 'remote', 'set-url', 'origin', str(self.root / 'missing.git'))
        self.assertFalse(sync.upload(self.args))
        self.assertEqual(path.read_text(), 'feedback')

    def test_source_verifications_transport_immutably_and_bind_to_source_host_note(self):
        self.note()
        digest = sync.digest(b'feedback')
        receipt = self.args.feedback_dir / 'frontend-ui/verifications' / (digest + '.json')
        receipt.parent.mkdir(parents=True)
        value = {'schema_version': 1, 'noteSha256': digest, 'sourceMachine': 'client-host', 'skill': 'frontend-ui',
                 'thread': 'thread1', 'turn': 'turn1', 'sourceSha256': 'a' * 64,
                 'completed_at': '2026-10-14T01:00:00Z', 'checked_at': '2026-10-15T01:00:00Z',
                 'loadedReads': [], 'status': 'supported', 'summary': 'Original correction matches; outcome remains reported.',
                 'scope': 'original-turn messages; artifacts and runtime outcomes not replayed'}
        receipt.write_text(json.dumps(value))
        sync.upload(self.args)
        sync.collect(self.args)
        target = self.repo / 'feedback/frontend-ui/verifications/client-host' / receipt.name
        self.assertEqual(target.read_bytes(), receipt.read_bytes())
        sync.upload(self.args)
        previous = receipt.read_bytes()
        reassessment = {**value, 'schema_version': 2, 'status': 'partial', 'checked_at': '2026-10-16T01:00:00Z',
                        'checkerSha256': 'b' * 64, 'inputSha256': 'c' * 64, 'inputTruncated': False,
                        'supersedes': sync.digest(previous)}
        data = (json.dumps(reassessment) + '\n').encode()
        new = receipt.with_name(digest + '--' + sync.digest(data) + '.json')
        new.write_bytes(data)
        sync.upload(self.args)
        sync.collect(self.args)
        records = list(sync.verification_records(self.repo, 'HEAD'))
        self.assertEqual(len(records), 2)
        report = evidence.assessment_inventory(self.repo / 'feedback')
        self.assertEqual(report['latest'][0]['status'], 'partial')
        self.assertEqual(report['latest'][0]['supersedes'], sync.digest(previous))
        self.assertEqual(target.read_bytes(), previous)
        self.assertEqual((target.parent / new.name).read_bytes(), data)
        corrective = {**reassessment, 'schema_version': 3, 'supersedes': sync.digest(data),
            'checked_at': '2026-10-16T02:00:00Z', 'correction': {
                'note': {'skill': 'frontend-ui', 'title': 'Qualify reported checks', 'task': 'Review an interface update.',
                         'observation': 'A correction was reported.', 'evidence': 'No installed result was supplied.',
                         'cause': 'The runtime cause is unknown.', 'proposal': 'Keep reported checks qualified.'},
                'status': 'supported', 'summary': 'The corrected attribution is supported.',
                'inputSha256': 'd' * 64, 'inputTruncated': False, 'generationInputSha256': 'e' * 64}}
        corrected_data = (json.dumps(corrective) + '\n').encode()
        corrected = receipt.with_name(digest + '--' + sync.digest(corrected_data) + '.json')
        corrected.write_bytes(corrected_data)
        sync.upload(self.args)
        sync.collect(self.args)
        current = evidence.assessment_inventory(self.repo / 'feedback')
        self.assertEqual((current['assessments'], current['corrected'], current['unresolved']), (3, 1, 0))
        self.assertEqual(current['latest'][0]['correction'], corrective['correction'])
        self.assertEqual((target.parent / corrected.name).read_bytes(), corrected_data)
        self.assertEqual(target.read_bytes(), previous)
        self.assertEqual((target.parent / new.name).read_bytes(), data)
        followed = {**corrective, 'schema_version': 4, 'supersedes': sync.digest(corrected_data),
            'checked_at': '2026-10-16T03:00:00Z', 'evidenceSha256': 'f' * 64,
            'scope': evidence.FOLLOWUP_SCOPE, 'followUps': [{'thread': 'thread1', 'turn': 'followup1',
                'sourceSha256': '1' * 64, 'completed_at': '2026-10-15T01:00:00Z'}]}
        followed_data = (json.dumps(followed) + '\n').encode()
        follow_path = receipt.with_name(digest + '--' + sync.digest(followed_data) + '.json')
        follow_path.write_bytes(followed_data)
        sync.upload(self.args)
        sync.collect(self.args)
        updated = evidence.assessment_inventory(self.repo / 'feedback')
        self.assertEqual((updated['assessments'], updated['corrected'], updated['unresolved']), (4, 1, 0))
        self.assertEqual(updated['latest'][0]['followUps'], followed['followUps'])
        self.assertEqual((target.parent / follow_path.name).read_bytes(), followed_data)
        receipt.write_text(json.dumps({**value, 'summary': 'Changed claim'}))
        with self.assertRaisesRegex(sync.SyncError, 'Immutable source verification'):
            sync.upload(self.args)
        receipt.write_text(json.dumps({**value, 'sourceMachine': 'maintainer-host'}))
        with self.assertRaises(ValueError):
            sync.upload(self.args)

    def test_publish_rejects_out_of_scope_and_unsafe_links(self):
        self.release()
        (self.repo / 'unexpected').write_text('no')
        self.commit()
        with self.assertRaisesRegex(sync.SyncError, 'Out-of-scope'):
            sync.publish(self.args)
        sync.git(self.repo, 'reset', '--hard', 'origin/main')
        (self.repo / 'skills/frontend-ui/SKILL.md').write_text('[escape](../../../README.md)')
        review = self.repo / 'feedback/frontend-ui/reviews/review.md'
        review.parent.mkdir(parents=True)
        review.write_text('Reviewed')
        self.commit()
        with self.assertRaisesRegex(sync.SyncError, 'unsafe'):
            sync.publish(self.args)

    def test_publish_requires_review_and_rejects_symlink(self):
        self.release()
        (self.repo / 'skills/frontend-ui/SKILL.md').write_text('changed')
        self.commit()
        with self.assertRaisesRegex(sync.SyncError, 'review record'):
            sync.publish(self.args)
        review = self.repo / 'feedback/frontend-ui/reviews/review.md'
        review.parent.mkdir(parents=True)
        review.write_text('Reviewed')
        (self.repo / 'skills/frontend-ui/unsafe').symlink_to('/tmp')
        self.commit()
        with self.assertRaisesRegex(sync.SyncError, 'symlink'):
            sync.publish(self.args)

    def test_upload_default_follows_migrated_codex_home(self):
        home = self.root / 'home'
        canonical = home / '.config/codex'
        notes = canonical / 'skill-feedback/frontend-ui/notes'
        notes.mkdir(parents=True)
        (home / '.codex').symlink_to(canonical)
        note = notes / 'migration.md'
        note.write_text('Migration feedback')
        with patch.object(Path, 'home', return_value=home), patch.dict(os.environ, {'CODEX_HOME': ''}), patch.object(sys, 'argv', ['sync.py', 'upload', '--repo', str(self.repo)]):
            self.assertEqual(sync.main(), 0)
        sync.git(self.repo, 'fetch', 'origin')
        self.assertEqual(sync.git(self.repo, 'show', 'origin/inbox/client-host:feedback/frontend-ui/notes/client-host/migration.md'), note.read_bytes())

    def test_upload_rejects_feedback_root_symlink_before_copy(self):
        self.note()
        external = self.root / 'external'
        external.mkdir()
        with sync.worktree(self.repo, 'origin/main') as checkout:
            (checkout / 'feedback').symlink_to(external)
            sync.git(checkout, 'add', '.')
            sync.git(checkout, 'commit', '-m', 'Malicious inbox')
            sync.git(checkout, 'push', 'origin', 'HEAD:refs/heads/inbox/client-host')
        with self.assertRaisesRegex(sync.SyncError, 'symlink'):
            sync.upload(self.args)
        self.assertEqual(list(external.iterdir()), [])

    def test_deploy_rejects_feedback_root_and_local_ancestor_symlink(self):
        self.release()
        sync.deploy(self.args)
        previous = (self.args.install_root / 'current').resolve()
        external = self.root / 'external'
        external.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(external)
        self.args.feedback_dir = alias / 'feedback'
        with self.assertRaisesRegex(sync.SyncError, 'symlink'):
            sync.deploy(self.args)
        self.assertEqual(list(external.iterdir()), [])
        self.assertEqual((self.args.install_root / 'current').resolve(), previous)
        self.args.feedback_dir = self.root / 'feedback'
        (self.repo / 'feedback').symlink_to(external)
        self.commit()
        sync.git(self.repo, 'push', 'origin', 'main')
        with self.assertRaisesRegex(sync.SyncError, 'symlink'):
            sync.deploy(self.args)
        self.assertEqual((self.args.install_root / 'current').resolve(), previous)

    def test_noop_publish_preserves_manifest_and_validates_existing_release(self):
        self.release()
        (self.repo / 'skills/frontend-ui/SKILL.md').write_text('Reviewed policy update')
        review = self.repo / 'feedback/frontend-ui/reviews/review.md'
        review.parent.mkdir(parents=True)
        review.write_text('Reviewed')
        self.commit()
        sync.publish(self.args)
        marker = self.repo / 'release.json'
        manifest = marker.read_bytes()
        head = sync.git(self.repo, 'rev-parse', 'HEAD')
        self.assertTrue(json.loads(manifest)['reviews'])
        self.assertTrue(sync.publish(self.args))
        self.assertEqual(marker.read_bytes(), manifest)
        self.assertEqual(sync.git(self.repo, 'rev-parse', 'HEAD'), head)
        invalid = json.loads(manifest)
        invalid['skillsSha256'] = 'invalid'
        marker.write_text(json.dumps(invalid))
        self.commit()
        sync.git(self.repo, 'push', 'origin', 'main')
        with self.assertRaisesRegex(sync.SyncError, 'does not approve'):
            sync.publish(self.args)

    def test_publish_rejects_manifest_symlink_without_touching_target(self):
        self.release()
        external = self.root / 'external'
        external.write_bytes(b'valuable local data')
        marker = self.repo / 'release.json'
        marker.unlink()
        marker.symlink_to(external)
        self.commit()
        with self.assertRaisesRegex(sync.SyncError, 'regular file'):
            sync.publish(self.args)
        self.assertEqual(external.read_bytes(), b'valuable local data')

    def test_markdown_examples_do_not_count_as_local_references(self):
        self.release()
        path = self.repo / 'skills/frontend-ui/SKILL.md'
        path.write_text('```md\n![](/example.png)\n```\n`[example](missing.md)`')
        sync.validate(self.repo)
        path.write_text('[broken](missing.md)')
        with self.assertRaisesRegex(sync.SyncError, 'Broken'):
            sync.validate(self.repo)

    def test_deploy_last_known_good_and_local_edits(self):
        self.release()
        self.assertTrue(sync.deploy(self.args))
        current = self.args.install_root / 'current'
        previous = current.resolve()
        (self.repo / 'skills/frontend-ui/SKILL.md').write_text('unreviewed')
        self.commit()
        sync.git(self.repo, 'push', 'origin', 'main')
        with self.assertRaisesRegex(sync.SyncError, 'does not approve'):
            sync.deploy(self.args)
        self.assertEqual(current.resolve(), previous)
        sync.git(self.repo, 'remote', 'set-url', 'origin', str(self.root / 'missing.git'))
        self.assertFalse(sync.deploy(self.args))
        self.assertEqual(current.resolve(), previous)
        sync.git(self.repo, 'remote', 'set-url', 'origin', str(self.remote))
        (current / 'skills/frontend-ui/SKILL.md').write_text('local edit')
        with self.assertRaisesRegex(sync.SyncError, 'local edits'):
            sync.deploy(self.args)

    def test_deploy_refuses_review_overwrite_before_switch(self):
        self.release()
        sync.deploy(self.args)
        previous = (self.args.install_root / 'current').resolve()
        review = self.repo / 'feedback/frontend-ui/reviews/review.md'
        review.parent.mkdir(parents=True)
        review.write_text('Reviewed')
        (self.repo / 'skills/frontend-ui/SKILL.md').write_text('new')
        self.commit()
        sync.publish(self.args)
        local = self.args.feedback_dir / 'frontend-ui/reviews/review.md'
        local.parent.mkdir(parents=True)
        local.write_text('local edits')
        with self.assertRaisesRegex(sync.SyncError, 'review edits'):
            sync.deploy(self.args)
        self.assertEqual((self.args.install_root / 'current').resolve(), previous)
        self.assertEqual(local.read_text(), 'local edits')

    def test_deploy_updates_untouched_previous_reviews_and_is_idempotent(self):
        self.release()
        review = self.repo / 'feedback/frontend-ui/reviews/评审\n结果.md'
        review.parent.mkdir(parents=True)
        review.write_text('review v1')
        self.commit()
        self.assertTrue(sync.publish(self.args))
        self.assertTrue(sync.deploy(self.args))
        local = self.args.feedback_dir / 'frontend-ui/reviews' / review.name
        self.assertEqual(local.read_text(), 'review v1')
        review.write_text('review v2')
        (self.repo / 'skills/frontend-ui/SKILL.md').write_text('reviewed skill v2')
        self.commit()
        self.assertTrue(sync.publish(self.args))
        self.assertTrue(sync.deploy(self.args))
        self.assertEqual(local.read_text(), 'review v2')
        current = (self.args.install_root / 'current').resolve()
        self.assertTrue(sync.deploy(self.args))
        self.assertEqual((self.args.install_root / 'current').resolve(), current)
        self.assertEqual(local.read_text(), 'review v2')

    def test_review_conflicts_are_preflighted_before_any_review_changes(self):
        self.release()
        directory = self.repo / 'feedback/frontend-ui/reviews'
        directory.mkdir(parents=True)
        for name in ('a.md', 'z.md'):
            (directory / name).write_text('review v1')
        self.commit()
        self.assertTrue(sync.publish(self.args))
        self.assertTrue(sync.deploy(self.args))
        current = (self.args.install_root / 'current').resolve()
        local = self.args.feedback_dir / 'frontend-ui/reviews'
        for name in ('a.md', 'z.md', 'new.md'):
            (directory / name).write_text('review v2')
        self.commit()
        self.assertTrue(sync.publish(self.args))
        (local / 'z.md').write_text('actual local edit')
        with self.assertRaisesRegex(sync.SyncError, 'review edits'):
            sync.deploy(self.args)
        self.assertEqual((self.args.install_root / 'current').resolve(), current)
        self.assertEqual((local / 'a.md').read_text(), 'review v1')
        self.assertEqual((local / 'z.md').read_text(), 'actual local edit')
        self.assertFalse((local / 'new.md').exists())
        (local / 'z.md').write_text('review v1')
        self.assertTrue(sync.deploy(self.args))
        self.assertTrue(all((local / name).read_text() == 'review v2' for name in ('a.md', 'z.md', 'new.md')))

    def test_review_update_requires_an_available_previous_published_baseline(self):
        self.release()
        review = self.repo / 'feedback/frontend-ui/reviews/review.md'
        review.parent.mkdir(parents=True)
        review.write_text('review v1')
        self.commit()
        self.assertTrue(sync.publish(self.args))
        self.assertTrue(sync.deploy(self.args))
        current = (self.args.install_root / 'current').resolve()
        manifest = current / 'manifest.json'
        original_manifest = manifest.read_text()
        review.write_text('review v2')
        self.commit()
        self.assertTrue(sync.publish(self.args))
        for commit in (None, 'HEAD', 'f' * 40):
            with self.subTest(commit=commit):
                value = json.loads(original_manifest)
                value['commit'] = commit
                manifest.write_text(json.dumps(value))
                with self.assertRaisesRegex(sync.SyncError, 'review edits'):
                    sync.deploy(self.args)
                self.assertEqual((self.args.feedback_dir / 'frontend-ui/reviews/review.md').read_text(), 'review v1')
                self.assertEqual((self.args.install_root / 'current').resolve(), current)
        manifest.write_text(original_manifest)
        self.assertTrue(sync.deploy(self.args))

    def test_review_recovery_accepts_interrupted_copies_after_main_advances(self):
        self.release()
        first = self.publish_review('review v1')
        self.assertTrue(sync.deploy(self.args))
        current = self.args.install_root / 'current'
        local = self.args.feedback_dir / 'frontend-ui/reviews/review.md'
        self.publish_review('review v2')
        self.interrupted_deploy()
        self.assertEqual(json.loads((current / 'manifest.json').read_text())['commit'], first)
        self.assertEqual(local.read_text(), 'review v2')
        latest = self.publish_review('review v3')
        self.assertTrue(sync.deploy(self.args))
        self.assertEqual(local.read_text(), 'review v3')
        self.assertEqual(json.loads((current / 'manifest.json').read_text())['commit'], latest)

    def test_review_journal_records_only_validated_git_blob_bytes(self):
        self.release()
        self.publish_review('review v1')
        self.assertTrue(sync.deploy(self.args))
        previous = (self.args.install_root / 'current').resolve()
        self.publish_review('review v2')
        original_read = Path.read_bytes
        def changed_checkout(path):
            if path.parts[-5:] == ('checkout', 'feedback', 'frontend-ui', 'reviews', 'review.md'):
                return b'unverifiable checkout bytes'
            return original_read(path)
        with patch.object(Path, 'read_bytes', changed_checkout):
            with self.assertRaisesRegex(sync.SyncError, 'Git blob'):
                sync.deploy(self.args)
        self.assertEqual((self.args.install_root / 'current').resolve(), previous)
        self.assertEqual((self.args.feedback_dir / 'frontend-ui/reviews/review.md').read_text(), 'review v1')
        self.assertFalse((self.args.install_root / 'review-deploy.json').exists())

    def test_review_recovery_survives_process_kill_before_current_switch(self):
        self.release()
        first = self.publish_review('review v1')
        self.assertTrue(sync.deploy(self.args))
        self.publish_review('review v2')
        harness = self.root / 'killed-deploy.py'
        harness.write_text(f'''import os, signal, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, {str(Path(sync.__file__).parent)!r})
import sync
args = SimpleNamespace(repo=Path({str(self.repo)!r}),
    feedback_dir=Path({str(self.args.feedback_dir)!r}),
    install_root=Path({str(self.args.install_root)!r}), validator=Path({str(self.validator)!r}))
replace = sync.os.replace
def kill_before_switch(source, target):
    if Path(target) == args.install_root / 'current':
        os.kill(os.getpid(), signal.SIGKILL)
    return replace(source, target)
sync.os.replace = kill_before_switch
sync.deploy(args)
''')
        child_tmp = self.root / 'child-tmp'
        child_tmp.mkdir()
        result = subprocess.run([sys.executable, str(harness)], capture_output=True,
                                env={**os.environ, 'TMPDIR': str(child_tmp)})
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr.decode())
        self.assertTrue((self.args.install_root / '.current-next').is_symlink())
        current = self.args.install_root / 'current'
        local = self.args.feedback_dir / 'frontend-ui/reviews/review.md'
        self.assertEqual(json.loads((current / 'manifest.json').read_text())['commit'], first)
        self.assertEqual(local.read_text(), 'review v2')
        latest = self.publish_review('review v3')
        self.assertTrue(sync.deploy(self.args))
        self.assertEqual(local.read_text(), 'review v3')
        self.assertEqual(json.loads((current / 'manifest.json').read_text())['commit'], latest)
        self.assertFalse((self.args.install_root / '.current-next').exists())

    def test_review_recovery_retains_both_versions_across_repeated_interruptions(self):
        self.release()
        first = self.publish_review('review v1', ('a.md', 'z.md'))
        self.assertTrue(sync.deploy(self.args))
        local = self.args.feedback_dir / 'frontend-ui/reviews'
        self.publish_review('review v2', ('a.md', 'z.md'))
        self.interrupted_deploy()
        self.publish_review('review v3', ('a.md', 'z.md'))
        self.interrupted_deploy(local / 'z.md')
        self.assertEqual((local / 'a.md').read_text(), 'review v3')
        self.assertEqual((local / 'z.md').read_text(), 'review v2')
        self.assertEqual(json.loads((self.args.install_root / 'current/manifest.json').read_text())['commit'], first)
        self.publish_review('review v4', ('a.md', 'z.md'))
        self.assertTrue(sync.deploy(self.args))
        self.assertTrue(all((local / name).read_text() == 'review v4' for name in ('a.md', 'z.md')))

    def test_interrupted_review_recovery_refuses_real_edits_and_missing_journal(self):
        self.release()
        self.publish_review('review v1')
        self.assertTrue(sync.deploy(self.args))
        self.publish_review('review v2')
        self.interrupted_deploy()
        journal = self.args.install_root / 'review-deploy.json'
        saved = journal.read_bytes()
        local = self.args.feedback_dir / 'frontend-ui/reviews/review.md'
        previous = (self.args.install_root / 'current').resolve()
        self.publish_review('review v3')
        local.write_text('actual local edits')
        with self.assertRaisesRegex(sync.SyncError, 'review edits'):
            sync.deploy(self.args)
        self.assertEqual(local.read_text(), 'actual local edits')
        self.assertEqual(journal.read_bytes(), saved)
        local.write_text('review v2')
        journal.unlink()
        with self.assertRaisesRegex(sync.SyncError, 'review edits'):
            sync.deploy(self.args)
        self.assertEqual(local.read_text(), 'review v2')
        self.assertEqual((self.args.install_root / 'current').resolve(), previous)

    def test_review_recovery_rejects_malformed_or_unverifiable_journal(self):
        self.release()
        self.publish_review('review v1')
        self.assertTrue(sync.deploy(self.args))
        review = self.repo / 'feedback/frontend-ui/reviews/review.md'
        review.unlink()
        review.symlink_to('../../../README.md')
        self.commit('Unsafe historical review fixture')
        unsafe = sync.git(self.repo, 'rev-parse', 'HEAD').decode().strip()
        review.unlink()
        self.publish_review('review v2')
        self.interrupted_deploy()
        journal = self.args.install_root / 'review-deploy.json'
        saved = journal.read_text()
        value = json.loads(saved)
        name = 'feedback/frontend-ui/reviews/review.md'
        self.publish_review('review v3')
        invalid = ['{', json.dumps({**value, 'schema_version': 9}),
                   json.dumps({**value, 'base_commit': 'HEAD'}),
                   json.dumps({**value, 'target_commit': 'f' * 40}),
                   json.dumps({**value, 'feedback_dir': str(self.root / 'another-feedback')}),
                   json.dumps({**value, 'reviews': {'../escape': value['reviews'][name]}})]
        unpublished = sync.git(self.repo, 'commit-tree', 'HEAD^{tree}', '-p', 'HEAD', '-m', 'Unpublished release').decode().strip()
        invalid.append(json.dumps({**value, 'target_commit': unpublished}))
        for change in ({'commit': 'HEAD'}, {'commit': 'f' * 40}, {'sha256': 'a' * 64}):
            altered = json.loads(saved)
            altered['reviews'][name]['after'].update(change)
            invalid.append(json.dumps(altered))
        altered = json.loads(saved)
        altered['reviews'][name]['before'] = {'commit': unsafe, 'sha256': sync.digest(b'../../../README.md')}
        invalid.append(json.dumps(altered))
        for serialized in invalid:
            with self.subTest(serialized=serialized):
                journal.write_text(serialized)
                with self.assertRaisesRegex(sync.SyncError, 'review recovery'):
                    sync.deploy(self.args)
                self.assertEqual((self.args.feedback_dir / 'frontend-ui/reviews/review.md').read_text(), 'review v2')
        journal.write_text(saved)
        self.assertTrue(sync.deploy(self.args))

    def test_deleted_partial_review_retains_proof_for_later_reintroduction(self):
        self.release()
        self.publish_review('review v1')
        self.assertTrue(sync.deploy(self.args))
        self.publish_review('review v2')
        self.interrupted_deploy()
        review = self.repo / 'feedback/frontend-ui/reviews/review.md'
        local = self.args.feedback_dir / 'frontend-ui/reviews/review.md'
        review.unlink()
        self.commit('Remove review upstream')
        self.assertTrue(sync.publish(self.args))
        self.assertTrue(sync.deploy(self.args))
        journal = self.args.install_root / 'review-deploy.json'
        retained = json.loads(journal.read_text())['reviews']
        self.assertEqual(len(retained), 1)
        self.assertIsNone(retained['feedback/frontend-ui/reviews/review.md']['after'])
        self.assertEqual(local.read_text(), 'review v2')
        self.assertTrue(sync.deploy(self.args))
        self.publish_review('review v3')
        self.assertTrue(sync.deploy(self.args))
        self.assertEqual(local.read_text(), 'review v3')
        self.assertFalse(journal.exists())

    def test_deleted_cached_review_edits_are_preserved_and_not_reauthorized(self):
        self.release()
        self.publish_review('review v1')
        self.assertTrue(sync.deploy(self.args))
        review = self.repo / 'feedback/frontend-ui/reviews/review.md'
        local = self.args.feedback_dir / 'frontend-ui/reviews/review.md'
        review.unlink()
        self.commit('Remove review upstream')
        self.assertTrue(sync.publish(self.args))
        self.assertTrue(sync.deploy(self.args))
        local.write_text('actual local edits')
        self.assertTrue(sync.deploy(self.args))
        self.assertEqual(local.read_text(), 'actual local edits')
        self.publish_review('review v2')
        with self.assertRaisesRegex(sync.SyncError, 'review edits'):
            sync.deploy(self.args)
        self.assertEqual(local.read_text(), 'actual local edits')

    def test_completed_review_journal_never_authorizes_a_user_revert(self):
        self.release()
        self.publish_review('review v1')
        self.assertTrue(sync.deploy(self.args))
        self.publish_review('review v2')
        self.interrupted_deploy()
        journal = self.args.install_root / 'review-deploy.json'
        pending = journal.read_bytes()
        self.assertTrue(sync.deploy(self.args))
        # A kill after the current switch but before cleanup leaves this journal.
        journal.write_bytes(pending)
        local = self.args.feedback_dir / 'frontend-ui/reviews/review.md'
        local.write_text('review v1')
        self.publish_review('review v3')
        with self.assertRaisesRegex(sync.SyncError, 'review edits'):
            sync.deploy(self.args)
        self.assertEqual(local.read_text(), 'review v1')

if __name__ == '__main__':
    unittest.main()
