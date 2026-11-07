"""The public repository's secret scan: its CI job and its gitleaks allowlist."""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from test_skill_packages import ROOT, published_file

GITLEAKS_VERSION = '8.30.1'
LINUX_ARCHIVE_SHA256 = '551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb'


def ci_job(workflow, name):
    """The text of one job in a workflow: from its header to the next job's header."""
    start = workflow.index(f'\n  {name}:\n')
    following = re.search(r'\n  [A-Za-z0-9_-]+:\n', workflow[start + 1:])
    return workflow[start:start + 1 + following.start()] if following else workflow[start:]


class SecretScanTests(unittest.TestCase):
    def test_ci_scans_pushes_and_pull_requests_with_the_pinned_scanner_and_config_only(self):
        workflow = published_file('.github/workflows/ci.yml').read_text()
        triggers = workflow[workflow.index('\non:\n'):workflow.index('\njobs:\n')]
        self.assertIn('\n  pull_request:\n', triggers)
        self.assertIn('\n  push:\n    branches: [main]\n', triggers)
        job = ci_job(workflow, 'secrets')
        self.assertNotIn('\n    if:', job)
        self.assertIn('fetch-depth: 0', job)
        self.assertIn(f'releases/download/v{GITLEAKS_VERSION}/$archive', job)
        self.assertIn(f'archive=gitleaks_{GITLEAKS_VERSION}_linux_x64.tar.gz', job)
        self.assertIn(f"printf '%s  %s\\n' {LINUX_ARCHIVE_SHA256} \"$archive\" | sha256sum --check", job)
        scan = job[job.index('"$scratch/gitleaks" git'):]
        scan = scan[:scan.index('\n', scan.index('--gitleaks-ignore-path'))]
        self.assertIn('--config .gitleaks.toml', scan)
        self.assertIn('--ignore-gitleaks-allow', scan)
        self.assertIn('--gitleaks-ignore-path "$scratch"', scan)
        self.assertNotIn('--max-decode-depth', scan)
        self.assertIn('GITLEAKS="$scratch/gitleaks" python3 -B -m unittest discover -s tests -p test_secret_scan.py', job)

    def test_private_inventory_publishes_the_scan_config_and_its_test(self):
        inventory = ROOT / 'public/export.json'
        if not inventory.is_file():
            self.skipTest('Private export inventory is checked before publication')
        entries = {entry['target']: entry['source'] for entry in json.loads(inventory.read_text())['files']}
        self.assertEqual(entries.get('.gitleaks.toml'), 'public/files/.gitleaks.toml')
        self.assertEqual(entries.get('.github/workflows/ci.yml'), 'public/files/.github/workflows/ci.yml')
        self.assertEqual(entries.get('tests/test_secret_scan.py'), 'tests/test_secret_scan.py')
        self.assertEqual(entries.get('tests/test_skill_packages.py'), 'tests/test_skill_packages.py')

    def test_private_config_allowlists_are_the_public_ones(self):
        private = ROOT / '.gitleaks.toml'
        if not (ROOT / 'public/files/.gitleaks.toml').is_file() or not private.is_file():
            self.skipTest('The private scan config is checked before publication')
        public = tomllib.loads(published_file('.gitleaks.toml').read_text())['allowlists']
        for allowlist in tomllib.loads(private.read_text())['allowlists']:
            with self.subTest(allowlist=allowlist['description']):
                self.assertIn(allowlist, public)

    def test_config_allows_only_manifest_hashes_and_catches_planted_secrets(self):
        explicit = os.environ.get('GITLEAKS')
        binary = explicit or shutil.which('gitleaks')
        if not binary:
            self.skipTest(f'needs gitleaks {GITLEAKS_VERSION} on PATH or in GITLEAKS')
        version = subprocess.run([binary, 'version'], capture_output=True, text=True, check=True).stdout.strip()
        if version != GITLEAKS_VERSION:
            if explicit:
                self.fail(f'GITLEAKS is gitleaks {version}; this test needs {GITLEAKS_VERSION}')
            self.skipTest(f'gitleaks on PATH is {version}; this test needs {GITLEAKS_VERSION}')
        digest = hashlib.sha256(b'synthetic reference').hexdigest()
        # Built at run time, so this file holds no token for a scanner to find.
        token = 'ghp' + '_' + 'Q7vK2mX9pL4rT8wZ1nB6yH3cF5jD0sGaE2uR'
        # Paths naming an API, as real manifests do, make these hashes look like API keys.
        allowed = {
            '.public-export.json': json.dumps({'files': {'scripts/claude_api.py': digest}}, indent=2) + '\n',
            'skills/example/sources.json': json.dumps(
                {'sources': {'up': {'sha256': {'references/api-and-errors.md': digest}}}}, indent=2) + '\n',
        }
        planted = {
            'notes.txt': f'token = "{token}"\n',
            'docs/config.md': f'api_key = "{digest}"\n',
            # A token in an allowlisted manifest is still reported by its own rule.
            'skills/example/sources.json': json.dumps(
                {'sources': {'up': {'sha256': {'references/api-and-errors.md': digest}}, 'auth': token}},
                indent=2) + '\n',
        }
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / 'repo'
            report = Path(temporary) / 'report.json'
            repo.mkdir()
            shutil.copy(published_file('.gitleaks.toml'), repo / '.gitleaks.toml')
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)

            def commit(files, message):
                for relative, text in files.items():
                    (repo / relative).parent.mkdir(parents=True, exist_ok=True)
                    (repo / relative).write_text(text)
                subprocess.run(['git', '-C', str(repo), 'add', '-A'], check=True)
                subprocess.run(['git', '-C', str(repo), '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                                'commit', '-q', '-m', message], check=True)

            def scan():
                report.unlink(missing_ok=True)
                # The flags public CI passes; the default decode depth.
                result = subprocess.run([binary, 'git', '--config', '.gitleaks.toml', '--no-banner', '--redact=100',
                                         '--ignore-gitleaks-allow', '--gitleaks-ignore-path', temporary,
                                         '--report-format', 'json', '--report-path', str(report)],
                                        cwd=repo, capture_output=True, text=True)
                findings = json.loads(report.read_text()) if report.is_file() else []
                return result.returncode, sorted({(f['RuleID'], f['File']) for f in findings})

            commit(allowed, 'manifests')
            self.assertEqual(scan(), (0, []))
            commit(planted, 'planted')
            self.assertEqual(scan(), (1, [('generic-api-key', 'docs/config.md'), ('github-pat', 'notes.txt'),
                                          ('github-pat', 'skills/example/sources.json')]))


if __name__ == '__main__':
    unittest.main()
