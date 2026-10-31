"""License obligations must survive installing a single skill directory."""
from pathlib import Path
import json
import unittest

ROOT = Path(__file__).parents[1]


def published_file(relative):
    overlay = ROOT / 'public/files' / relative
    return overlay if overlay.is_file() else ROOT / relative


class SkillPackageTests(unittest.TestCase):
    def test_every_skill_contains_original_contribution_license(self):
        expected = published_file('LICENSE').read_bytes()
        for skill in ('frontend-ui', 'content-research-writer', 'find-skills', 'tauri', 'typescript'):
            with self.subTest(skill=skill):
                self.assertEqual(published_file(f'skills/{skill}/LICENSE').read_bytes(), expected)

    def test_typescript_public_inventory_contains_reviewed_entries(self):
        inventory = ROOT / 'public/export.json'
        if not inventory.is_file():
            self.skipTest('Private export inventory is checked before publication')
        files = json.loads(inventory.read_text())['files']
        entries = {entry['target']: entry['source'] for entry in files}
        # Public enrollment is explicit. New private skill files can evolve
        # without silently enrolling themselves for public disclosure.
        reviewed = {
            'skills/typescript/SKILL.md',
            'skills/typescript/sources.json',
            'skills/typescript/references/feedback.md',
            'skills/typescript/references/skill-map.md',
            'skills/typescript/references/tooling-and-public-types.md',
        }
        for relative in reviewed:
            with self.subTest(path=relative):
                self.assertEqual(entries.get(relative), relative)
        self.assertEqual(entries['skills/typescript/LICENSE'], 'public/files/skills/typescript/LICENSE')
        self.assertFalse(any(entry['source'].startswith('feedback/') for entry in files))

    def test_frontend_bundles_exact_required_upstream_notices(self):
        for name in ('antfu-skills', 'make-interfaces-feel-better', 'turborepo', 'uncodixfy'):
            with self.subTest(source=name):
                expected = published_file(f'licenses/{name}.txt').read_bytes()
                self.assertEqual(published_file(f'skills/frontend-ui/licenses/{name}.txt').read_bytes(), expected)

    def test_notices_and_preexisting_licenses_remain_in_packages(self):
        for skill in ('frontend-ui', 'find-skills'):
            with self.subTest(skill=skill):
                self.assertTrue(published_file(f'skills/{skill}/THIRD_PARTY_NOTICES.md').is_file())
        for name in ('skills/frontend-ui/licenses/frontend-design.txt',
                     'skills/find-skills/licenses/vercel-labs-skills.txt'):
            with self.subTest(notice=name):
                self.assertTrue(published_file(name).is_file())
