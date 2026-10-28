#!/usr/bin/env python3
"""Publish the curated public inventory once per daily deadline, retrying failures."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

from public_export import ExportError, configuration, execute, locks, save, rust_cli


def latest_due(now):
    local = now.astimezone(ZoneInfo('Asia/Tokyo'))
    due = local.replace(hour=10, minute=17, second=0, microsecond=0)
    return due if local >= due else due - timedelta(days=1)


def run(config, now=None):
    config = configuration(config)
    now = now or datetime.now(timezone.utc)
    root = Path(config['state_dir']).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    due = latest_due(now)
    with locks([root / 'job.lock']):
        path = root / 'daily-state.json'
        state = json.loads(path.read_text()) if path.exists() else {}
        if state.get('completed_due') and datetime.fromisoformat(state['completed_due']) >= due:
            return {'outcome': 'not_due'}
        status = {'started_at': now.isoformat(), 'due': due.isoformat(), 'success': None}
        save(root / 'daily-status.json', status)
        try:
            result = execute(config, publish=True)
        except (ExportError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            status.update(success=False, error='Publication stopped; inspect configuration, candidate tests and export state.',
                          finished_at=datetime.now(timezone.utc).isoformat())
            save(root / 'daily-status.json', status)
            raise ExportError(status['error']) from error
        status.update(success=True, result=result, finished_at=datetime.now(timezone.utc).isoformat())
        save(root / 'daily-status.json', status)
        save(path, {'completed_due': due.isoformat()})
        return {'outcome': 'published' if result['changed'] else 'unchanged', **result}


def main():
    return rust_cli(['daily', *sys.argv[1:]])


if __name__ == '__main__':
    raise SystemExit(main())
