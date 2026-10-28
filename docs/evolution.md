# Evolution tools

The scripts separate evidence collection from policy changes. Use an independent private working repository for real feedback and releases; do not upload source transcripts or personal configuration to this public repository.

## Incremental Rust migration

The public publisher and its daily runner now live in the Rust `codex-skills` executable. They directly use the `codex-commit` library for generation-only messages from public diffs. The source-feedback, verification, retry, prompt-evaluation and weekly private-release contracts remain in their existing Python modules. A broader migration should preserve those contracts component by component; this release does not replace the entire maintenance pipeline.

The old `scripts/public_export.py` and `scripts/public_export_job.py` remain regression references. Use the Rust commands in [Public publication](publication.md) for deployed publishing. Python remains a prerequisite for the current validation suite and maintenance commands below.

## Components

- `read_guidance.py` returns bounded guidance text and hashes of exactly what was returned.
- `capture.py` discovers completed saved turns and extracts bounded, abstract feedback through a restricted Codex invocation.
- `evidence.py` checks notes against originating completed turns and retains immutable assessments; eligible corrections receive a separate check.
- `upstream.py` compares recorded upstream revisions with current source repositories for review.
- `sync.py` transports immutable feedback and validates, publishes, and installs approved skill releases.
- `jobs.py` coordinates the configured synchronization and weekly maintainer cycles.
- `prompt_runtime.py` loads bounded editable prompts beneath fixed policies and checks approved bundles.
- `prompt_review.py` compares proposed prompt changes against immutable synthetic cases before a controlled promotion.

The scripts preserve the reference host names `client-host` and `maintainer-host`. They assume Unix file locking and atomic filesystem operations. Scheduling and monitoring are platform-specific and must be configured separately.

## Configuration and prerequisites

Read the script entrypoints before enabling any command that reads sessions, calls a model, writes feedback, or pushes Git commits. The pipeline requires Python, `uv`, Git, an authenticated Codex CLI, and a separately supplied skill validator. Publication and deployment refuse to run without `--validator`; this repository does not provide that validator or a deployment approval marker.

The configured repository, feedback directory, installation root, session roots, host role, and executable paths must belong to your own setup. The `jobs.py` wrapper requires `repo_path`, `host_id`, `python_path`, `uv_path`, `codex_path`, `validator_path`, and `role`. The maintainer role is tied to `maintainer-host`. Default storage is under `~/.local/share/codex-skills/`; `CODEX_HOME` controls the Codex session and feedback defaults, and Claude Code sources use `CLAUDE_CONFIG_DIR` where configured.

Capture's historical default lower bound and source-retention behavior are reference deployment choices: inspect them before a first run. Keep authentication outside the repository. Installing skills alone enables none of these commands.

## Commands

From the working checkout, these are examples of supported commands, not a setup script:

```sh
# Read-only bounded guidance with an exact-content fingerprint.
uv run --no-project python scripts/read_guidance.py skills/tauri/SKILL.md

# Collect completed source turns and write feedback; makes model calls.
uv run --no-project python scripts/capture.py --config /path/to/private-config.json

# Verify local notes against source evidence; makes model calls and writes receipts.
uv run --no-project python scripts/evidence.py --config /path/to/private-config.json

# Read effective existing assessments without a model call.
uv run --no-project python scripts/evidence.py --report /path/to/private-feedback

# Fetch and compare recorded public upstream sources; writes local review artifacts.
uv run --no-project python scripts/upstream.py --skill frontend-ui

# Inspect prompt-review state without starting an evaluation.
uv run --no-project python scripts/prompt_review.py inspect --config /path/to/private-config.json

# Maintainer only: evaluate a bounded candidate and potentially publish it.
uv run --no-project python scripts/prompt_review.py review --config /path/to/private-config.json --timeout 900
```

`sync.py` exposes `upload`, `collect`, `publish`, and `deploy`; these can change Git branches or installations. `jobs.py sync` runs capture, verification, upload, and deployment stages, while `jobs.py weekly` may review and publish changes. They are not inspection commands. Read the configuration and authorization boundaries before invoking them.

## Evidence and evaluation limits

Original notes are preserved separately from later source assessments and proposed corrections. Missing or truncated evidence stays unresolved. A later report is not proof that an earlier action occurred, and a repeated assessment of the same case is not an independent example.

Prompt review uses repeated baseline/candidate comparisons, fixed policy contracts, no-regression checks, and independent judgment. Its maintenance task is a synthetic decision proxy without tools. Passing it does not demonstrate improved runtime behavior, prevent every privacy leak, or replace observation on real authorized tasks.
