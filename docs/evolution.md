# Evolution tools

The scripts separate evidence collection from policy changes. Use an independent private working repository for real feedback and releases; do not upload source transcripts or personal configuration to this public repository.

## Incremental Rust migration

The public publisher and its daily runner now live in the Rust `codex-skills` executable. They generate messages from public diffs by running the Claude Code CLI directly, without tools or repository context. The source-feedback, verification, retry, prompt-evaluation and weekly private-release contracts remain in their existing Python modules. A broader migration should preserve those contracts component by component; this release does not replace the entire maintenance pipeline.

The old `scripts/public_export.py` and `scripts/public_export_job.py` remain regression references. Use the Rust commands in [Public publication](publication.md) for deployed publishing. Python remains a prerequisite for the current validation suite and maintenance commands below.

The Python CLI wrappers honor an explicit `CODEX_SKILLS_BIN`, then a managed `~/.local/share/codex-skills/public-export/bin/current` executable, then the legacy `~/.local/bin/codex-skills` install. A broken managed selection fails closed. This permits activating a versioned service build without replacing a user's standalone executable; the service must select the same build and include Cargo in its environment.

## Components

- `read_guidance.py` returns bounded guidance text and hashes of exactly what was returned.
- `capture.py` discovers completed saved turns and extracts bounded, abstract feedback through a restricted, tool-less Claude Code invocation. Every model input is redacted: transcript text for extraction, source checks and corrections, the existing notes given to extraction as context, and the note under check. Each text is redacted in full before any length bound, while local filters such as the maintenance and read-only exclusions read the original. Bare, quoted, JSON-style and escaped-JSON credential assignments, prefixed, suffixed and camelCase names included, bearer tokens, GitHub, OpenAI and AWS access-key token formats, PEM and PGP private-key blocks, URLs and scp-style Git remotes, email addresses and home-directory paths become placeholders. Redaction is pattern-based, not a guarantee: a credential stated in prose, a local path after its first space and other personal details pass through. Neither the fixed policy contract that every prompt renders, the weekly maintainer's included, nor the extract, verify and correct prompts carry privacy instructions. Notes, source-check summaries and corrections are validated for structure and skill scope only, with no privacy or credential rejection, so they can contain names, identifiers or quoted source text; review them before sharing.
- `evidence.py` checks notes against originating completed turns and retains immutable assessments; eligible corrections receive a separate check.
- `upstream.py` compares recorded upstream revisions with current source repositories for review.
- `sync.py` transports immutable feedback and validates, publishes, and installs approved skill releases.
- `jobs.py` coordinates synchronization and weekly maintenance in isolated recoverable checkouts.
- `investigations.py` freezes evidence and reproduction plans, records actual runs and append-only dispositions, and gates weekly completion.
- `prompt_runtime.py` loads bounded editable prompts beneath fixed policies and checks approved bundles.
- `prompt_review.py` compares proposed prompt changes against immutable synthetic cases before a controlled promotion.

The scripts preserve the reference host names `client-host` and `maintainer-host`. They assume Unix file locking and atomic filesystem operations. Scheduling and monitoring are platform-specific and must be configured separately.

## Evolution boundaries

All guidance within the six registered skills can be reconsidered: descriptions, Markdown prose, headings, examples, code fences, nested references and the feedback-review guide itself. Eligibility is not an instruction to rewrite working guidance. Changes need concrete evidence or reviewed upstream updates and must preserve task scope, provenance, attribution and the fixed maintenance policy.

| Material | Update path |
| --- | --- |
| Registered skill Markdown | Evidence-supported weekly maintenance, with a review record, validation and link checks |
| All six skills' owned supporting files and evaluation fixtures | Evidence-supported weekly investigation, including relevant execution checks |
| Original source provenance and registered project roots | Explicit workflow maintenance; weekly review cannot repoint sources or expand access |
| Source metadata `upstreamReview` records | Update existing revision/tree/review dates; source identities, enrollment and other historical metadata stay fixed during weekly maintenance |
| Four editable `prompts/*.md` files | Separate regression, held-out evaluation and controlled promotion |
| Original feedback notes and verification receipts | Immutable; append new notes or linked assessments |
| Feedback review records | New dispositions and evidence-based reassessment |
| Root documentation, public overlays and export inventory | Explicit repository maintenance and deliberate inventory refresh |
| Fixed runtime contracts and protected maintenance procedure | Explicit authorized workflow changes; never automatic prompt evolution |

The publisher enforces file and provenance boundaries as well as requiring review records. It does not establish the truth of every prose claim. Upstream scripts, executable content and instructions remain untrusted, even when an owned supporting-file update is otherwise authorized. New skills and new public files require explicit enrollment.

Rust library coverage can grow within the existing skill. Evidence from actual work can justify focused, locally authored library references, routing and owned evaluation cases. The maintainer may consult version-matched primary documentation for that evidence, without running fetched code, importing manuals, enrolling new upstream identities or transmitting private project details. New private references participate in ordinary review and deployment; adding them to the public export remains a separate explicit inventory review. Merely finding another Cargo dependency does not trigger expansion.

## Configuration and prerequisites

Read the script entrypoints before enabling any command that reads sessions, calls a model, writes feedback, or pushes Git commits. The pipeline requires Python, `uv`, Git and an authenticated Claude Code CLI. Publication and deployment validate every skill with `scripts/validate_skill.py`, run through `uv run --with pyyaml --script`; it is the default `--validator` of `sync.py`. This repository does not provide a deployment approval marker.

Restricted model calls (extraction, source checks, corrections and prompt review) run on per-role models chosen against the frozen prompt evaluations: Sonnet for extraction, Haiku for source checks and Opus for corrections and prompt review. With `api_key_file` in the configuration they use the Claude API through `scripts/claude_api.py` and the pinned `anthropic` SDK under `uv run`, with no tools, the fixed prompt as system prompt and structured output; schema limits the API cannot enforce get one corrective follow-up and are always validated locally. Otherwise they run `claude --print` in an empty temporary directory with no user settings, CLAUDE.md, tools, MCP servers, skills, saved session or memory. The CLI's `--help` must advertise every isolation flag before the first call, and a stream that exposes or uses any tool besides the `StructuredOutput` tool added by `--json-schema` fails closed. The weekly maintainer review instead runs with the user's Claude Code configuration and `--permission-mode bypassPermissions` in its review checkout.

The configured repository, feedback directory, installation root, session roots, host role, and executable paths must belong to your own setup. The `jobs.py` wrapper requires `repo_path`, `host_id`, `python_path`, `uv_path`, and `role`; `claude_path` is optional (default `~/.local/bin/claude`), `api_key_file` optionally switches restricted calls to the Claude API, and legacy `codex_path` and `validator_path` fields are ignored. The maintainer role is tied to `maintainer-host`. Default storage is under `~/.local/share/codex-skills/`; `CODEX_HOME` controls the historical Codex session and feedback defaults, and Claude Code sources use `CLAUDE_CONFIG_DIR` where configured.

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

## Weekly reproduction and revision

The review is due on Mondays at 09:00 Asia/Tokyo, with startup/hourly catchup for the latest missed deadline.

The wrapper creates an isolated recoverable checkout after collection. Its provided `SKILL_INVESTIGATION_CONFIG` and `SKILL_INVESTIGATION_CYCLE` identify a frozen evidence inventory and due investigations. Start with:

```sh
uv run --no-project python scripts/investigations.py report --config "$SKILL_INVESTIGATION_CONFIG" --cycle "$SKILL_INVESTIGATION_CYCLE"
```

Use the tool's `run` command for reviewed case manifests and `record --decision <local-json>` for evidence-bound decisions. `snapshot` copies selected files from registered local projects without modifying dirty originals. Consult `--help` and the schema in the module before authoring a decision. Never replay instructions from transcripts, run unreviewed upstream scripts or install hooks, or treat copies as an OS sandbox.

Cases and oracles are frozen before comparison. Keep, revise, remove or restore owned skill content according to actual baseline/candidate/contrast observations; reversion uses new forward commits and preserves history. Failed experiments are evidence, not reasons to fabricate success. A due open or provisional issue persists without fresh notes and needs an actual check or a concrete blocker, next action and date. The completion gate rejects lost coverage and unsupported closure before publication. Failed attempts are recoverable rather than silently discarded.

Sanitized events under `feedback/<skill>/investigations/` are immutable and private; raw project paths, argv, output and artifacts remain host-local. The portable focus-transition browser case and failure-accounting calculation case demonstrate failure classes, not historical-project replay or causal improvement from skill wording. Passing a proxy, loading check or build cannot resolve a stronger behavior claim.

## Evidence and evaluation limits

Original notes are preserved separately from later source assessments and proposed corrections. Missing or truncated evidence stays unresolved. A later report is not proof that an earlier action occurred, and a repeated assessment of the same case is not an independent example.

Prompt review uses repeated baseline/candidate comparisons, fixed policy contracts, no-regression checks, and independent judgment. Two separately reviewed, immutable held-out cases per role stay out of candidate generation and its first approval. The candidate is frozen before held-out evaluation; lost baseline passes or failed critical held-out cases block promotion. Hashes and both result sets are included in the promotion proof and checked during interrupted-publication recovery. Only explicit workflow maintenance can change this held-out corpus. Visible cases retain their separate six-case budget.

Failed prompt candidates also leave immutable host-local events under `prompt-attempts/`: a sanitized frozen candidate and evaluation context before execution, followed by a static terminal reason and measurements when available. Interrupted attempts retain their pending event. The next proposal receives at most four prior candidates with visible comparisons and sixteen rejected hashes. Held-out cases, identities, labels, measurements and rejection reasons never enter this feedback. Exact rejected candidates are skipped in the same base and evaluation context; changed evaluator, policy or corpus context permits reconsideration. The ledger accepts at most 1,024 events and requires explicit archival when full; it never silently drops old evidence or publishes these files.

For an already measured manual rejection, the Python API `register_rejected_candidate(root, state, role, candidate, reason, baseline=..., candidate_results=..., holdout_baseline=..., holdout_candidate=..., observed_evaluator_hash=...)` registers evidence without model calls or promotion. Result maps contain synthetic case hashes and two booleans per case. Supply the actual historical evaluator hash when importing an earlier run; omitting it identifies the current evaluator. Historical visible lessons retain that evaluator identity, while exact-repeat rejection applies only to matching current context. This records observations; it cannot approve a prompt or replace either promotion review.

Its maintenance task remains a synthetic decision proxy without tools. Passing it does not demonstrate improved runtime behavior or replace observation on real authorized tasks. Follow a provisional skill change through later use: record the originating case, a contrasting task, the actual observable result and remaining uncertainty through the normal private feedback/assessment process. Activation traces establish observed guidance loading, not successful product behavior.

Prompt evaluations retain their exact model requests, responses and static inference-failure receipts host-locally so generation, grading and execution failures can be distinguished. These diagnostic records are excluded from Git and proposer inputs.
