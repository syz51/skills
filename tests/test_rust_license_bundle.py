"""Release binary archives must retain third-party license text."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('rust_licenses', Path(__file__).parents[1] / 'scripts/bundle_rust_licenses.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RustLicenseBundleTests(unittest.TestCase):
    def test_preserves_notice_bytes_without_build_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            notice = 'Copyright Example\nPermission granted.\n'
            (root / 'LICENSE-MIT').write_text(notice)
            metadata = {'resolve': {'root': 'self'}, 'packages': [
                {'id': 'self', 'name': 'self', 'version': '1'},
                {'id': 'dep', 'name': 'dep', 'version': '1', 'manifest_path': str(root / 'Cargo.toml'), 'license': 'MIT'}]}
            result = module.bundle(metadata)
            self.assertIn(notice, result)
            self.assertNotIn(str(root), result)
            (root / 'LICENSE-MIT').unlink()
            with self.assertRaises(ValueError):
                module.bundle(metadata)
