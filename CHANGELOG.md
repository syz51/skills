# Changelog

## Unreleased

- Remove all privacy checks from the pipeline. The static note checks rejected the model's paraphrases of its own wording, product names and dates and left notes pending. Notes, source-check summaries and corrections are now validated for structure and skill scope only; transcript text reaches the model unredacted; the fixed policy contract that every prompt renders, the daily maintainer's included, and the extract, verify and correct prompts drop their privacy instructions, and the maintainer policy no longer calls its records sanitized; source checks and corrections no longer retry with a rejection class; investigation records and prompt candidates no longer reject URLs, paths or credential patterns; and the Rust and Python exporters no longer scan files or commit messages for private identifiers or credentials. Configured replacements, dropped metadata keys and commit-message quality and attribution checks remain.
- Add opt-in live Claude Code checks (`RUN_CLAUDE_LIVE=1`). They find the CLI at `~/.local/bin/claude` or on `PATH` and fail when it is missing; the exporters remove the opt-in from their candidate checks, so publication never makes model calls.
- Run capture, source verification, prompt review, daily maintenance, skill evaluations and public commit-message generation through the Claude Code CLI instead of the Codex CLI. Restricted calls load no settings, instructions, tools or MCP servers and fail closed on any tool use; the publisher no longer depends on `codex-commit`, and its `generation` configuration takes `claude_path`, `model`, `effort` and `timeout_seconds`. Skills are validated with the bundled `scripts/validate_skill.py`. Historical Codex session rollouts remain capture sources.
- Refresh Rust dependencies and CI tooling; migrate SHA-256 formatting while preserving stored hashes. Building from source now requires Rust 1.88 or newer.
- Require fresh capture and evidence results before completing maintenance jobs.
- Enforce supporting-file and source-enrollment boundaries during skill publication.
- Run Rust formatting, strict Clippy and tests before publishing managed Rust snapshots.
- Add held-out prompt evaluation and recovery proofs without changing fixed policy.
- Correct skill-review guidance and include provenance-verified UnoCSS references.

## 0.1.0

- Add the Rust `codex-skills` public publisher and daily deadline runner.
- Generate informative commit subjects and bodies through the `codex-commit` library, using only staged public changes.
- Preserve explicit inventories, privacy checks, independent public history, managed-file conflict checks, failed-run retries and no-op publication.
- Keep capture, evidence verification and weekly private maintenance in Python during the incremental migration.
- Publish native Linux x86-64 and macOS Apple Silicon/Intel archives with checksums.
