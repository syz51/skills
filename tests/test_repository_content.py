"""Release gates against the actual guidance and synthetic evaluation artifacts."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import prompt_review
import prompt_runtime
import sync


class RepositoryContentTests(unittest.TestCase):
    def test_skill_references_resolve_inside_each_skill(self):
        sync.validate(ROOT)

    def test_prompt_bundle_and_hash_addressed_cases_are_valid(self):
        prompt_runtime.bundle_hash(ROOT, allow_legacy=False)
        prompt_review.load_cases(ROOT)
