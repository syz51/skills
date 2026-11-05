from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / 'scripts/validate_skill.py'
UV = shutil.which('uv')


@unittest.skipIf(UV is None, 'uv is unavailable')
class ValidateSkillTests(unittest.TestCase):
    """Run the validator the way sync.py does, with PyYAML supplied by uv."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.skill = Path(self.temp.name) / 'example-skill'
        self.skill.mkdir()

    def validate(self, folder=None):
        return subprocess.run([UV, 'run', '--with', 'pyyaml', '--python', sys.executable, '--script', str(VALIDATOR),
                               str(folder or self.skill)], capture_output=True, text=True, timeout=120)

    def write(self, frontmatter, body='# Example\n\nUse the example.\n'):
        (self.skill / 'SKILL.md').write_text('---\n' + frontmatter + '\n---\n' + body)

    def assert_rejected(self, message):
        result = self.validate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(message, result.stdout)

    def test_valid_skill_passes(self):
        self.write('name: example-skill\ndescription: >-\n  Explain an example when asked.\nlicense: MIT\n'
                   'allowed-tools: Read\nmetadata:\n  owner: tests',
                   '# Example\n\n```md\n[TODO: shown as an example]\n```\n')
        result = self.validate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Skill is valid', result.stdout)

    def test_repository_skills_pass(self):
        for folder in sorted((ROOT / 'skills').iterdir()):
            with self.subTest(skill=folder.name):
                result = self.validate(folder)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_skill_file_or_frontmatter_fails(self):
        self.assert_rejected('SKILL.md not found')
        (self.skill / 'SKILL.md').write_text('# Example\n')
        self.assert_rejected('YAML frontmatter')
        (self.skill / 'SKILL.md').write_text('---\nname: example\n')
        self.assert_rejected('YAML frontmatter')
        self.write('- name\n- description')
        self.assert_rejected('YAML mapping')
        self.write('name: [unclosed')
        self.assert_rejected('Invalid YAML')

    def test_bad_names_fail(self):
        for name, message in (('Example_Skill', 'hyphen-case'), ('-example', 'hyphen'), ('example-', 'hyphen'),
                              ('ex--ample', 'consecutive'), ('a' * 65, 'maximum is 64'), ('""', "'name'"),
                              ('42', "'name'")):
            with self.subTest(name=name):
                self.write('name: ' + name + '\ndescription: Explain an example.')
                self.assert_rejected(message)
        self.write('description: Explain an example.')
        self.assert_rejected("'name'")
        self.write('name: ' + 'a' * 64 + '\ndescription: Explain an example.')
        self.assertEqual(self.validate().returncode, 0)

    def test_bad_descriptions_fail(self):
        for description, message in (('Use <tag> markup', 'angle brackets'), ('Use a -> b', 'angle brackets'),
                                     ('x' * 1025, 'maximum is 1024'), ('"[TODO: describe]"', 'TODO'),
                                     ('[one, two]', "'description'")):
            with self.subTest(description=description[:20]):
                self.write('name: example-skill\ndescription: ' + description)
                self.assert_rejected(message)
        self.write('name: example-skill')
        self.assert_rejected("'description'")

    def test_unexpected_key_fails(self):
        self.write('name: example-skill\ndescription: Explain an example.\nversion: 1')
        self.assert_rejected('Unexpected frontmatter key(s): version')

    def test_unfinished_placeholder_in_instructions_fails(self):
        self.write('name: example-skill\ndescription: Explain an example.', '# Example\n\n[TODO: write steps]\n')
        self.assert_rejected('TODO placeholder')


if __name__ == '__main__':
    unittest.main()
