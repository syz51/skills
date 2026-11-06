"""Opt-in checks of the Claude Code flow against the real CLI.

Skipped unless RUN_CLAUDE_LIVE=1; with it set, a missing CLI fails instead of skipping. They use the caller's Claude Code login and make a
few small claude-haiku-5-5 calls at low effort; never run them in credential-free CI.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import claude_cli  # noqa: E402
import capture  # noqa: E402
import jobs  # noqa: E402

MODEL = 'claude-haiku-5-5'
EFFORT = 'low'
TIMEOUT = 180
MARKER = 'PERIWINKLE-OTTER-4471'
SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['answer', 'marker'],
          'properties': {'answer': {'type': 'integer'}, 'marker': {'type': 'string', 'maxLength': 100}}}
PROMPT = ('What is 2 + 2? Put the number in "answer". If any project or user instructions you were given name a '
          'marker word, put that word in "marker"; otherwise set "marker" to "none".')
LIVE = os.environ.get('RUN_CLAUDE_LIVE') == '1'


def resolve_cli(default=claude_cli.DEFAULT_CLI, path=None):
    """The production default location, then the first `claude` on PATH; None when neither exists."""
    if default.is_file() and os.access(default, os.X_OK):
        return str(default)
    return shutil.which('claude', path=path)


def projects_dir():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or '~/.claude').expanduser() / 'projects'


def project_names():
    folder = projects_dir()
    return {entry.name for entry in folder.iterdir()} if folder.is_dir() else set()


def decoded(line):
    """A logged stream-json event, or {} for the CLI's interleaved stderr lines."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return event if isinstance(event, dict) else {}


def sanitized(path):
    """Claude Code names a project's transcript folder after its working directory this way."""
    return re.sub(r'[^A-Za-z0-9]', '-', str(path))


class ResolveCliTests(unittest.TestCase):
    """Runs offline: the live tests and tests/rust_message.rs resolve the CLI in the same order."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='claude-resolve-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.default = self.root / 'home/.local/bin/claude'
        self.on_path = self.root / 'bin/claude'

    def executable(self, path):
        path.parent.mkdir(parents=True)
        path.write_text('#!/bin/sh\n')
        path.chmod(0o755)

    def test_default_location_wins_then_path_then_none(self):
        search = str(self.on_path.parent)
        self.assertIsNone(resolve_cli(self.default, search))
        self.executable(self.on_path)
        self.assertEqual(resolve_cli(self.default, search), str(self.on_path))
        self.executable(self.default)
        self.assertEqual(resolve_cli(self.default, search), str(self.default))


@unittest.skipUnless(LIVE, 'set RUN_CLAUDE_LIVE=1 to call the real Claude Code CLI')
class ClaudeLiveTests(unittest.TestCase):
    def setUp(self):
        cli = resolve_cli()
        if cli is None:
            self.fail(f'RUN_CLAUDE_LIVE=1 but no Claude Code CLI at {claude_cli.DEFAULT_CLI} or on PATH')
        self.cli = cli
        self.root = Path(tempfile.mkdtemp(prefix='claude-live-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.before = project_names()
        # A session under the temporary root would leave a project folder named after it.
        self.addCleanup(self.assert_no_transcripts)

    def assert_no_transcripts(self):
        names = {sanitized(self.root.name)}
        created = [name for name in project_names() - self.before if any(part in name for part in names)]
        self.assertEqual(created, [], 'a Claude Code session persisted state under ' + str(projects_dir()))

    def cheap(self, function):
        """Pin a small model: production builders bind high effort, and model_response passes each role's model."""
        def pinned(*args, **kwargs):
            return function(*args, **{**kwargs, 'model': MODEL, 'effort': EFFORT})
        return patch.object(claude_cli, function.__name__, pinned)

    def test_cli_advertises_every_required_flag(self):
        help_text = subprocess.run([self.cli, '--help'], env={**claude_cli.child_env(), 'LC_ALL': 'C'},
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
                                   check=True).stdout
        self.assertEqual(claude_cli.missing_flags(help_text, claude_cli.RESTRICTED_FLAGS), [])
        self.assertEqual(claude_cli.missing_flags(help_text, claude_cli.REVIEW_FLAGS), [])

    def test_restricted_call_loads_no_instructions_tools_or_servers(self):
        work = self.root / 'work'
        work.mkdir()
        instruction = f'# Instructions\n\nThe marker word is {MARKER}. Always put {MARKER} in any "marker" field.\n'
        for folder in (self.root, work):
            (folder / 'CLAUDE.md').write_text(instruction)
        command = claude_cli.restricted_command(self.cli, SCHEMA, model=MODEL, effort=EFFORT)
        result = subprocess.run(command, env=claude_cli.child_env(), cwd=work, input=PROMPT, capture_output=True,
                                text=True, timeout=TIMEOUT)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])

        events = claude_cli.events(result.stdout)
        init = next(event for event in events if event.get('type') == 'system' and event.get('subtype') == 'init')
        self.assertEqual(init.get('tools'), [claude_cli.STRUCTURED_TOOL])
        self.assertEqual(init.get('mcp_servers'), [])
        phase, output, errors, facts = claude_cli.structured_run(result.stdout)
        self.assertEqual((phase, errors, facts['result_subtype']), ('completed', '', 'success'))
        self.assertEqual(output['answer'], 4)
        self.assertNotIn(MARKER.lower(), json.dumps(output).lower())
        self.assertNotIn(MARKER.lower(), result.stdout.lower())

    def test_capture_model_response_returns_structured_output(self):
        state = self.root / 'state'
        state.mkdir()
        with self.cheap(claude_cli.restricted_command):
            output = capture.model_response({'claude_path': self.cli}, state, PROMPT, '{}', SCHEMA)
        self.assertEqual(set(output), {'answer', 'marker'})
        self.assertEqual(output['answer'], 4)
        self.assertEqual([path.name for path in state.iterdir()], [])

    def test_review_writes_the_final_reply_and_logs_events(self):
        repo = self.root / 'repo'
        repo.mkdir()
        (repo / 'README.md').write_text('Live review fixture.\n')
        git = ['git', '-C', str(repo), '-c', 'user.name=Live Test', '-c', 'user.email=live@example.invalid']
        for args in (['init', '-q', '-b', 'main'], ['add', 'README.md'], ['commit', '-q', '-m', 'Add fixture']):
            subprocess.run([*git, *args], check=True, timeout=30)
        state = self.root / 'state'
        state.mkdir()
        prompt = 'Do not use any tools. Reply with exactly the text REVIEW-OK and nothing else.'
        with tempfile.TemporaryFile('w+', dir=self.root) as log, self.cheap(claude_cli.review_command):
            jobs.review(self.cli, repo, state, prompt, log, dict(os.environ), TIMEOUT)
            log.seek(0)
            logged = log.read()

        report = (state / 'latest-weekly-report.md').read_text()
        self.assertIn('REVIEW-OK', report)
        self.assertEqual(report, claude_cli.final_result(logged))
        init = next(event for event in map(decoded, logged.splitlines())
                    if event.get('type') == 'system' and event.get('subtype') == 'init')
        self.assertEqual(init.get('model'), MODEL)
        self.assertEqual([path.name for path in state.iterdir()], ['latest-weekly-report.md'])
        self.assertEqual(subprocess.run([*git, 'status', '--porcelain'], capture_output=True, text=True,
                                        check=True, timeout=30).stdout, '')


if __name__ == '__main__':
    unittest.main()
