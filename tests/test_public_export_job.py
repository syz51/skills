from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import public_export_job as job


class DailyExportTests(unittest.TestCase):
    def test_due_uses_tokyo_deadline(self):
        before = datetime(2026, 10, 28, 1, 16, tzinfo=timezone.utc)
        after = before.replace(minute=17)
        self.assertEqual(job.latest_due(before).day, 27)
        self.assertEqual(job.latest_due(after).day, 28)

    def test_success_skips_repeat_and_next_day_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {'schema_version': 1, 'source_repo': folder, 'target_url': 'https://github.com/example/skills.git',
                      'author_name': 'Example', 'author_email': 'example@example.invalid',
                      'state_dir': folder, 'files': [{'source': 'skills/test.md', 'target': 'skills/test.md'}]}
            now = datetime(2026, 10, 28, 2, tzinfo=timezone.utc)
            with patch.object(job, 'execute', return_value={'changed': False}) as execute:
                self.assertEqual(job.run(config, now)['outcome'], 'unchanged')
                self.assertEqual(job.run(config, now)['outcome'], 'not_due')
                self.assertEqual(job.run(config, now.replace(day=29))['outcome'], 'unchanged')
                self.assertEqual(execute.call_count, 2)

    def test_failure_keeps_deadline_pending_without_private_error(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {'schema_version': 1, 'source_repo': folder, 'target_url': 'https://github.com/example/skills.git',
                      'author_name': 'Example', 'author_email': 'example@example.invalid',
                      'state_dir': folder, 'files': [{'source': 'skills/test.md', 'target': 'skills/test.md'}]}
            now = datetime(2026, 10, 28, 2, tzinfo=timezone.utc)
            with patch.object(job, 'execute', side_effect=job.ExportError('private details')):
                with self.assertRaises(job.ExportError):
                    job.run(config, now)
            self.assertFalse((Path(folder) / 'daily-state.json').exists())
            status = json.loads((Path(folder) / 'daily-status.json').read_text())
            self.assertIs(status['success'], False)
            self.assertNotIn('private details', status['error'])
            with patch.object(job, 'execute', return_value={'changed': True}):
                self.assertEqual(job.run(config, now)['outcome'], 'published')
