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

Installed mode tests the five maintained skills in the user's existing discovery directories and instruction configuration. Cases without a skill name exercise that complete configuration, including AGENTS.md dispatch; they do not isolate metadata-only selection. Public users need to configure the same dispatch behavior they intend to verify. No user configuration is changed.

To evaluate a candidate skill tree, add `--skills-root skills`. Candidate mode creates temporary project discovery links and project-local TypeScript/frontend dispatch pointing to that tree. Results identify this configuration separately from installed mode.

The matrix includes explicit and unnamed requests for all maintained skills, TypeScript module extensions, compiler-check coverage, public declarations, TSX/Vue composition and unrelated-task negative controls. Use repeated `--case <id>` selections or `--suite typescript` for a narrower run.

## Interpreting evidence

Each case saves `trace.jsonl`, local stderr, `targets.json`, `case.json` and `result.json`; the run saves `summary.json`. The target and case files preserve the exact pre-run content and expectations for offline replay. The scorer binds observed content to those immutable snapshots. A path mention, started call, final claim, failed read or unrelated compiler dependency file cannot establish loading. Unsupported output formats remain unverified and fail required-read checks; uncertain reads of forbidden guidance cannot silently pass negative controls. Ordering requires complete earlier guidance before the later read.

Partial or unsupported file-read attempts also constrain negative and ordering results. A later complete read cannot erase an earlier uncertain read. Unsupported Node programs leave absence and ordering unverified unless bounded analysis establishes a non-reading or unrelated form; a method-name search is insufficient. Unresolved computed read paths remain uncertain even for short or empty excerpts. Supported unrelated file reads stay unrelated, and these conservative checks never establish positive loading evidence.

Inspect failed traces before changing an expectation. Add a synthetic regression for a legitimate new trace shape; never execute saved code or make a negative control pass by accepting printed source text. Passing finite cases establishes those observed runs, not universal activation on every model or prompt.

Raw traces can contain local instructions and paths. Keep them outside the repository and public inventory, inspect them locally, and delete the run directory when finished. Commit synthetic fixtures and concise verification records rather than real transcripts. Live tests are opt-in and are not silently added to credential-free CI.

The separate `prompt-evals/` corpus tests bounded maintenance-role decisions; it is not a replacement for activation evaluations. Prompt-role validation respects each case's eligible skill scope, and evidence/evaluator fingerprints include the shared guidance parser so semantic changes invalidate applicable assessments.

This approach follows the [OpenAI skill evaluation guidance](https://developers.openai.com/blog/eval-skills).
