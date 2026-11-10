"""Release gates against the actual guidance and synthetic evaluation artifacts."""
from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import prompt_review
import prompt_runtime
import public_export
import sync


class RepositoryContentTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / 'public/export.json').is_file() and (ROOT / '.git').exists(),
                         'needs the private export configuration and source checkout')
    def test_exported_files_pass_the_publication_scanner(self):
        """The legacy Python reference scanner over the working tree, uncommitted edits included.

        The Rust test committed_inventory_passes_the_publication_scanner runs the real
        exporter over the committed tree; this one checks the reference implementation.
        """
        config = json.loads((ROOT / 'public/export.json').read_text())
        config['source_repo'] = str(ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            # Snapshot the working tree through a throwaway index and object store, so the
            # repository is untouched and the files are scanned exactly as they would publish.
            objects = Path(temporary) / 'objects'
            objects.mkdir()
            alternate = public_export.git(ROOT, 'rev-parse', '--path-format=absolute', '--git-path', 'objects').decode().strip()
            environment = {'GIT_INDEX_FILE': str(Path(temporary) / 'index'), 'GIT_OBJECT_DIRECTORY': str(objects),
                           'GIT_ALTERNATE_OBJECT_DIRECTORIES': alternate}
            with patch.dict(os.environ, environment):
                public_export.git(ROOT, 'add', '-f', '--', *(entry['source'] for entry in config['files']))
                tree = public_export.git(ROOT, 'write-tree').decode().strip()
                files = public_export.snapshot(config, tree)
        self.assertEqual(len(files), len(config['files']))

    # Keyed to the private checkout, not to the workflow files, so a deleted workflow still fails here.
    @unittest.skipUnless((ROOT / 'public/export.json').is_file(),
                         'needs the private secret-scan workflows, which the public export leaves out')
    def test_circleci_secret_scan_runs_the_actions_command(self):
        """The CircleCI fallback mirrors the Actions "Scan history" job, whose status the merge guard matches by name."""
        step = 'name: Scan all branches with pinned gitleaks'

        def script(text, marker):
            """The literal block after `marker`, the first one below the scan step."""
            lines = text.splitlines()
            named = next(i for i, line in enumerate(lines) if line.strip().removeprefix('- ') == step)
            start = next(i for i in range(named, len(lines)) if lines[i].strip() == marker) + 1
            indent = len(lines[start]) - len(lines[start].lstrip())
            block = []
            for line in lines[start:]:
                if line.strip() and len(line) - len(line.lstrip()) < indent:
                    break
                block.append(line[indent:])
            return '\n'.join(block).strip()
        actions = (ROOT / '.github/workflows/secrets.yml').read_text()
        circleci = (ROOT / '.circleci/config.yml').read_text()
        self.assertIn('\n  gitleaks:\n    name: Scan history\n', actions)
        self.assertIn('\n      - scan-history:\n          name: Scan history\n', circleci)
        # Both clones need every branch for --log-opts="--all".
        self.assertIn('fetch-depth: 0\n', actions)
        self.assertIn('- checkout:\n          method: full\n', circleci)
        command = script(actions, 'run: |')
        self.assertIn('/gitleaks" git ', command)
        self.assertEqual(script(circleci, 'command: |'), command)

    def test_skill_references_resolve_inside_each_skill(self):
        sync.validate(ROOT)

    def test_typescript_enrollment_has_feedback_and_local_provenance(self):
        folder = ROOT / 'skills/typescript'
        skill = (folder / 'SKILL.md').read_text()
        self.assertIn('references/feedback.md', skill)
        self.assertIn('read_guidance.py', skill)
        provenance = json.loads((folder / 'sources.json').read_text())
        self.assertEqual(provenance['maintenance'], 'local')
        self.assertIsInstance(provenance['upstreamReview'], list)
        feedback = (folder / 'references/feedback.md').read_text()
        self.assertIn('skill-feedback/typescript/', feedback)
        self.assertIn('feedback/typescript/reviews/', feedback)
        self.assertNotIn('skill-feedback/tauri/', feedback)

    def test_prompt_bundle_and_hash_addressed_cases_are_valid(self):
        prompt_runtime.bundle_hash(ROOT, allow_legacy=False)
        cases = prompt_review.load_cases(ROOT)
        holdouts = prompt_review.load_cases(ROOT, holdouts=True)
        self.assertFalse({prompt_review.digest(c) for c in cases} & {prompt_review.digest(c) for c in holdouts})
