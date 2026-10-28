import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import prompt_runtime as runtime


class PromptRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.bundle(self.source, 'source')
        (self.source / 'maintenance-prompt.md').write_text('Protected maintenance procedure.')
        self.install = self.root / 'install'
        self.current = self.install / 'current'
        self.current.mkdir(parents=True)
        (self.current / 'manifest.json').write_text('{}')
        self.config = {'repo_path': str(self.source), 'install_root': str(self.install)}

    def bundle(self, root, text):
        (root / 'prompts').mkdir()
        for role, name in runtime.PROMPT_PATHS.items():
            (root / name).write_text(text + ' ' + role)

    def approve(self):
        (self.current / 'manifest.json').write_text(json.dumps({'promptsSha256': runtime.bundle_hash(self.current)}))

    def test_explicit_source_and_legacy_fallback(self):
        (self.current / 'manifest.json').write_text('{"commit":"legacy"}')
        self.assertIn('source extract', runtime.load('extract', self.config))
        self.bundle(self.current, 'managed')
        self.approve()
        self.assertIn('source extract', runtime.load('extract', self.config, root=self.source))

    def test_current_selection_and_protected_maintenance(self):
        self.bundle(self.current, 'managed')
        self.approve()
        self.assertIn('managed extract', runtime.load('extract', self.config))
        rendered = runtime.load('maintain', self.config)
        self.assertLess(rendered.index('Protected maintenance procedure.'), rendered.index('managed maintain'))
        self.assertIn('takes precedence', rendered)
        self.assertIn('No tools or actions', runtime.load('verify', self.config))

    def test_tampered_and_missing_approved_bundle_never_fallback(self):
        self.bundle(self.current, 'managed')
        self.approve()
        path = self.current / runtime.PROMPT_PATHS['extract']
        path.write_text('changed')
        with self.assertRaises(ValueError):
            runtime.load('extract', self.config)
        for item in (self.current / 'prompts').iterdir():
            item.unlink()
        (self.current / 'prompts').rmdir()
        with self.assertRaises(ValueError):
            runtime.load('extract', self.config)

    def test_missing_manifest_and_broken_current_rejected(self):
        (self.current / 'manifest.json').unlink()
        with self.assertRaisesRegex(ValueError, 'manifest'):
            runtime.load('extract', self.config)
        self.current.rmdir()
        self.current.symlink_to(self.root / 'missing-release')
        with self.assertRaisesRegex(ValueError, 'manifest'):
            runtime.load('extract', self.config)

    def test_configured_home_paths_expand(self):
        from unittest.mock import patch
        relative_source = '~/' + str(self.source.relative_to(self.root))
        relative_install = '~/' + str(self.install.relative_to(self.root))
        with patch.dict('os.environ', {'HOME': str(self.root)}):
            config = {'repo_path': relative_source, 'install_root': relative_install}
            self.assertIn('source extract', runtime.load('extract', config))
            self.assertIn('source extract', runtime.load('extract', root=relative_source))

    def test_unapproved_bundle_rejected(self):
        self.bundle(self.current, 'managed')
        (self.current / 'manifest.json').write_text('{}')
        with self.assertRaises(ValueError):
            runtime.load('extract', self.config)

    def test_nonobject_managed_manifest_never_falls_back(self):
        for value in ([], None, 'legacy', 1):
            with self.subTest(value=value):
                (self.current / 'manifest.json').write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    runtime.load('extract', self.config)

    def test_partial_unknown_symlink_directory_empty_oversize_and_utf8_rejected(self):
        path = self.source / runtime.PROMPT_PATHS['extract']
        original = path.read_bytes()
        for mode in ('partial', 'unknown', 'symlink', 'directory', 'empty', 'oversize', 'utf8'):
            with self.subTest(mode=mode):
                path.unlink()
                if mode == 'partial':
                    pass
                elif mode == 'unknown':
                    path.write_bytes(original)
                    (self.source / 'prompts/extra.md').write_text('extra')
                elif mode == 'symlink':
                    path.symlink_to(self.source / runtime.PROMPT_PATHS['verify'])
                elif mode == 'directory':
                    path.mkdir()
                else:
                    path.write_bytes({'empty': b' ', 'oversize': b'a' * (runtime.MAX_PROMPT_BYTES + 1), 'utf8': b'\xff'}[mode])
                with self.assertRaises((ValueError, UnicodeError)):
                    runtime.bundle_hash(self.source)
                if path.is_dir() and not path.is_symlink():
                    path.rmdir()
                elif path.exists() or path.is_symlink():
                    path.unlink()
                (self.source / 'prompts/extra.md').unlink(missing_ok=True)
                path.write_bytes(original)

    def test_bundle_directory_symlink_rejected(self):
        (self.root / 'linked').mkdir()
        (self.root / 'linked/prompts').symlink_to(self.source / 'prompts', target_is_directory=True)
        with self.assertRaises(ValueError):
            runtime.bundle_hash(self.root / 'linked')

    def test_hash_copy_and_legacy(self):
        self.assertIsNone(runtime.bundle_hash(self.current))
        self.assertEqual(runtime.copy_bundle(self.source, self.current), runtime.bundle_hash(self.source))
        self.assertEqual(runtime.bundle_hash(self.current), runtime.bundle_hash(self.source))
        (self.current / 'prompts/verify.md').write_text('changed verifier')
        self.assertNotEqual(runtime.bundle_hash(self.current), runtime.bundle_hash(self.source))
        with self.assertRaises(ValueError):
            runtime.copy_bundle(self.source, self.current)
