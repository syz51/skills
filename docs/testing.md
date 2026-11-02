# Skill testing

## Offline regression and integration suite

Run from the repository root with Python, uv and Git available:

```sh
uv run --no-project python -m unittest discover -s tests
cargo fmt --all -- --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --locked
```

Python discovery never starts a live model. The activation scorer tests successful completed reads, helper continuation chunks, supported shell/Node formats, ordering, malformed or truncated traces, and false positives from printed headings, comments, failed calls and lookalike paths.

The lifecycle integration tests iterate over every maintained skill. They join observed reads, capture, atomic notes, original-source receipts, local Git transport, reviewed nested-reference changes, validated deployment and an explicit public snapshot. Model responses and dependency provisioning are fixtures; parsing, files, hashes, receipts and Git operations use the real modules. These tests establish pipeline behavior, not the quality of a model's extraction or review.

## Live activation evaluations

Inspect the cases without calling a model:

```sh
uv run --no-project python scripts/skill_evals.py plan --suite all
```

Then choose a new output directory under your temporary directory and run explicitly:

```sh
uv run --no-project python scripts/skill_evals.py run --suite all --output-dir "$TMPDIR/skill-evals-unique-run" --jobs 2
```

Live evaluation needs an authenticated Codex CLI and uses account quota. It inherits the configured model unless the caller supplies `--model`. The runner requires ephemeral sessions, JSONL traces and a read-only sandbox. Prompts prohibit editing, dependency installation, browsing, project commands, delegation and account actions. The runner checks fixture and skill-source immutability, bounds concurrency and time, and terminates timed-out process groups.

Installed mode tests the six maintained skills in the user's existing discovery directories and instruction configuration. Cases without a skill name exercise that complete configuration, including AGENTS.md dispatch; they do not isolate metadata-only selection. Public users need to configure the same dispatch behavior they intend to verify. No user configuration is changed.

To evaluate a candidate skill tree, add `--skills-root skills`. Candidate mode creates temporary project discovery links and project-local Rust/TypeScript/frontend dispatch pointing to that tree. Results identify this configuration separately from installed mode.

The matrix includes explicit and unnamed requests for all maintained skills, TypeScript module extensions, compiler-check coverage, public declarations, Rust Cargo/API/async/unsafe and selective library routing, Tauri composition, TSX/Vue composition and unrelated-task negative controls. Use repeated `--case <id>` selections, `--suite rust`, or `--suite typescript` for a narrower run.

Owned case files passed with `--cases` may target a nested reference directly, such as `rust/references/libraries/new.md`. Only requested extra references are snapshotted, and targets must resolve inside their registered skill. This lets new library guidance and its activation cases evolve inside the owned skill tree without changing the runner's root reference registry. Existing short target aliases remain supported.

## Interpreting evidence

Each case saves `trace.jsonl`, local stderr, `targets.json`, `case.json` and `result.json`; the run saves `summary.json`. The target and case files preserve the exact pre-run content and expectations for offline replay. The scorer binds observed content to those immutable snapshots. A path mention, started call, final claim, failed read or unrelated compiler dependency file cannot establish loading. Unsupported output formats remain unverified and fail required-read checks; uncertain reads of forbidden guidance cannot silently pass negative controls. Ordering requires complete earlier guidance before the later read.

Partial or unsupported file-read attempts also constrain negative and ordering results. A later complete read cannot erase an earlier uncertain read. Unsupported Node programs and opaque executable or interpreter source leave absence and ordering unverified unless bounded analysis establishes a non-reading or unrelated form. Neither a method-name search nor the lack of a literal full-path operand proves absence. Unresolved computed read paths remain uncertain even for short or empty excerpts. Supported unrelated file reads stay unrelated, and these conservative checks never establish positive loading evidence.

A command can read one file successfully and then fail on another. A nonzero exit prevents positive loading proof; nonempty visible output and attributable read uncertainty still constrain absence and ordering. Decorated output and merged diagnostics cannot establish absence by failing to match snapshot lines. Empty failures and provably disjoint literal reads remain distinct; merged diagnostic-only text may remain unverified when the command could read constrained guidance.

Inspect failed traces before changing an expectation. Add a synthetic regression for a legitimate new trace shape; never execute saved code or make a negative control pass by accepting printed source text. Passing finite cases establishes those observed runs, not universal activation on every model or prompt.

Raw traces can contain local instructions and paths. Keep them outside the repository and public inventory, inspect them locally, and delete the run directory when finished. Commit synthetic fixtures and concise verification records rather than real transcripts. Live tests are opt-in and are not silently added to credential-free CI.

The separate `prompt-evals/cases/` corpus tests bounded maintenance-role decisions; it is not a replacement for activation evaluations. `prompt-evals/holdouts/` contains separately reviewed cases withheld from candidate generation and its first approval. Both sets are hash-addressed; automatic publication cannot edit held-out cases. Promotion requires a repeatable visible-case gain, no lost baseline passes and all critical held-out cases passing. The maintenance task remains a tool-free decision proxy. Prompt-role validation respects each case's eligible skill scope, and evidence/evaluator fingerprints include the shared guidance parser so semantic changes invalidate applicable assessments.

This approach follows the [OpenAI skill evaluation guidance](https://developers.openai.com/blog/eval-skills).

## Investigation regression cases

The investigation suite checks frozen evidence and oracle identities, append-only event chains, coverage and carryover, real subprocess outcomes, bounded execution and unchanged original project copies. The owned `skills/frontend-ui/evaluations/focus-transition/` case exercises a conditional input in an isolated Chromium profile; `skills/content-research-writer/evaluations/failure-accounting/` checks an explicit estimand and justified exclusions. Each includes a deliberately failing baseline plus passing candidate and contrast. These are real synthetic fixture executions, not original-project replay or proof of guidance efficacy. A missing browser is an explicit blocker, never a passing observation.
