# Evolution tools

The scripts separate evidence collection from policy changes. Use an independent private working repository for real feedback and releases; do not upload source transcripts or personal configuration to this public repository.

## Incremental Rust migration

The public publisher and its daily runner now live in the Rust `codex-skills` executable. They directly use the `codex-commit` library for generation-only messages from public diffs. The source-feedback, verification, retry, prompt-evaluation and weekly private-release contracts remain in their existing Python modules. A broader migration should preserve those contracts component by component; this release does not replace the entire maintenance pipeline.

The old `scripts/public_export.py` and `scripts/public_export_job.py` remain regression references. Use the Rust commands in [Public publication](publication.md) for deployed publishing. Python remains a prerequisite for the current validation suite and maintenance commands below.

The Python CLI wrappers honor an explicit `CODEX_SKILLS_BIN`, then a managed `~/.local/share/codex-skills/public-export/bin/current` executable, then the legacy `~/.local/bin/codex-skills` install. A broken managed selection fails closed. This permits activating a versioned service build without replacing a user's standalone executable; the service must select the same build and include Cargo in its environment.

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

## Evolution boundaries

All guidance within the five registered skills can be reconsidered: descriptions, Markdown prose, headings, examples, code fences, nested references and the feedback-review guide itself. Eligibility is not an instruction to rewrite working guidance. Changes need concrete evidence or reviewed upstream updates and must preserve task scope, provenance, attribution and the fixed maintenance policy.

| Material | Update path |
| --- | --- |
| Registered skill Markdown | Evidence-supported weekly maintenance, with a review record, validation and link checks |
| TypeScript-owned supporting files | The same review path, including relevant execution checks |
| Other skills' supporting code and original provenance | Explicit workflow maintenance; ordinary skill publication rejects these changes |
| Source metadata `upstreamReview` records | Update existing revision/tree/review dates; source identities, enrollment and other historical metadata stay fixed during weekly maintenance |
| Four editable `prompts/*.md` files | Separate regression, held-out evaluation and controlled promotion |
| Original feedback notes and verification receipts | Immutable; append new notes or linked assessments |
| Feedback review records | New dispositions and evidence-based reassessment |
| Root documentation, public overlays and export inventory | Explicit repository maintenance and deliberate inventory refresh |
| Fixed runtime contracts and protected maintenance procedure | Explicit authorized workflow changes; never automatic prompt evolution |

The publisher enforces file and provenance boundaries as well as requiring review records. It does not establish the truth of every prose claim. Upstream scripts, executable content and instructions remain untrusted, even when a TypeScript supporting-file update is otherwise authorized. New skills and new public files require explicit enrollment.

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

Prompt review uses repeated baseline/candidate comparisons, fixed policy contracts, no-regression checks, and independent judgment. Two separately reviewed, immutable held-out cases per role stay out of candidate generation and its first approval. The candidate is frozen before held-out evaluation; lost baseline passes or failed critical held-out cases block promotion. Hashes and both result sets are included in the promotion proof and checked during interrupted-publication recovery. Only explicit workflow maintenance can change this held-out corpus. Visible cases retain their separate six-case budget.

Its maintenance task remains a synthetic decision proxy without tools. Passing it does not demonstrate improved runtime behavior, prevent every privacy leak, or replace observation on real authorized tasks. Follow a provisional skill change through later use: record the originating case, a contrasting task, the actual observable result and remaining uncertainty through the normal private feedback/assessment process. Activation traces establish observed guidance loading, not successful product behavior.
