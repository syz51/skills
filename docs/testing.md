# Skill testing

## Offline regression and integration suite

Run from the repository root with Python, uv and Git available:

```sh
uv run --no-project python -m unittest discover -s tests
cargo fmt --all -- --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --locked
```

Python discovery never starts a live model. The Claude Code activation scorer tests Skill tool injections, complete and partial Read results, forbidden and denied reads, unexpected tools, ordering, unsuccessful sessions and assistant prose against synthetic streams and a sanitized live sample. The Codex scorer, kept for historical rollouts, tests completed reads, helper continuation chunks, supported shell/Node formats, ordering, malformed or truncated traces, and false positives from printed headings, comments, failed calls and lookalike paths.

The lifecycle integration tests iterate over every maintained skill. They join observed reads, capture, atomic notes, original-source receipts, local Git transport, reviewed nested-reference changes, validated deployment and an explicit public snapshot. Model responses and dependency provisioning are fixtures; parsing, files, hashes, receipts and Git operations use the real modules. These tests establish pipeline behavior, not the quality of a model's extraction or review.

## Live Claude Code checks

The offline suite exercises the Claude Code invocations only against fake CLIs. To check them against the installed CLI, run explicitly:

```sh
RUN_CLAUDE_LIVE=1 uv run --no-project --python 3.12 python -B -m unittest tests.test_claude_live
RUN_CLAUDE_LIVE=1 cargo test --locked --test rust_message live_
```

Both look for the CLI at `~/.local/bin/claude`, then for the first `claude` on `PATH`. Without `RUN_CLAUDE_LIVE=1` the live checks skip; with it set, a missing CLI fails the run instead of skipping. The exporters remove `RUN_CLAUDE_LIVE` from the environment of the candidate checks they run, so publication never makes model calls. They use your Claude Code login and make a few small `claude-haiku-5-5` calls at low effort, plus two extraction calls on the extract role's model and effort, so they cost a little quota and are never part of CI. They verify that `claude --help` still advertises every flag the restricted call and maintainer review rely on; that a restricted call loads no CLAUDE.md from its directory or parent, exposes only the StructuredOutput tool and no MCP servers, and returns the schema-shaped answer; that the capture extractor returns that answer end to end and removes its working directory; that extraction titles a visual pattern the user rejects for future interfaces `Rejected default: <pattern>` and gives feedback on one design an ordinary title; that the maintainer review, run under its sandbox profile (the host's `review_settings`, or the maintainer's audit profile, which must exist), writes its final reply as the report and copies the event stream to the job log; and that the commit-message generator returns a validated message. With `CODEX_SKILLS_LIVE_API_KEY_FILE` also set to a Claude API key file, the Rust check confirms that generation with `api_key_file` runs the CLI on that key (`apiKeySource` is `ANTHROPIC_API_KEY`), billing the key's Console account rather than the login. Each check removes its temporary directories and fails if a session it started leaves a transcript folder under `~/.claude/projects`.

## Live activation evaluations

Inspect the cases without calling a model:

```sh
uv run --no-project python scripts/skill_evals.py plan --suite all
```

Then choose a new output directory under your temporary directory and run explicitly:

```sh
uv run --no-project python scripts/skill_evals.py run --suite all --output-dir "$TMPDIR/skill-evals-unique-run" --jobs 2
```

Live evaluation needs an authenticated Claude Code CLI (`claude`, by default at `~/.local/bin/claude`; pass `--claude <path>` otherwise) and uses account quota. It inherits the configured model and effort unless the caller supplies `--model` or `--effort`. The runner reads `claude --help` and fails closed unless every flag it relies on is present. Each case runs `claude --print --output-format stream-json --verbose` in its fixture with the prompt on stdin, `--no-session-persistence`, `--strict-mcp-config` (no MCP servers), `--tools Read,Skill` and `--permission-mode dontAsk`. No exposed tool can write, run commands, browse or delegate, and anything that would prompt is denied. Beyond the fixture, `--add-dir` grants Read access only to the evaluated skill folders. The child environment drops variables from any parent Claude Code session and disables auto-memory, so nothing is written under `~/.claude/projects`. Prompts prohibit editing, dependency installation, browsing, project commands, delegation and account actions. The runner rejects sessions whose init event reports other tools, MCP servers, another permission mode or a missing maintained skill. It checks fixture and skill-source immutability, bounds concurrency and time, and terminates timed-out process groups.

These self-contained evaluations ask the model to load skills with the Skill tool and to read further guidance and project files with one complete Read call per file rather than content searches. This bounds negative read evidence; it is an evaluation protocol, not a restriction on ordinary project work. Unsupported or unattributable reads remain unverified rather than being treated as safe.

Installed mode tests the six maintained skills in the user's Claude Code configuration: skills under `~/.claude/skills` (or `$CLAUDE_CONFIG_DIR/skills`) and the user's settings, plugins, hooks and CLAUDE.md. Cases without a skill name exercise that complete configuration, including CLAUDE.md dispatch; they do not isolate metadata-only selection. Public users need to configure the same dispatch behavior they intend to verify. No user configuration is changed.

To evaluate a candidate skill tree, add `--skills-root skills`. Candidate mode links each candidate skill into the fixture as a project skill (`.claude/skills/<name>`), writes a project CLAUDE.md with the Rust/TypeScript/frontend dispatch, and passes `--setting-sources project`. Claude Code then loads no user skills, user CLAUDE.md, user settings or user plugins; the session lists only the candidate skills and Claude Code's built-in ones, and the runner fails a case if any other skill from the user skill directory appears. Results identify this configuration separately from installed mode.

Claude Code also reads project instructions and configuration (CLAUDE.md, CLAUDE.local.md, `.claude/` and, through its built-in AGENTS.md plugin, AGENTS.md) from ancestor directories of the fixture, so the runner refuses an output directory inside such a project; case fixtures may not contain those files either.

The matrix includes explicit and unnamed requests for all maintained skills, TypeScript module extensions, compiler-check coverage, public declarations, Rust Cargo/API/async/unsafe and selective library routing, Tauri composition, TSX/Vue composition and unrelated-task negative controls. Use repeated `--case <id>` selections, `--suite rust`, or `--suite typescript` for a narrower run.

Owned case files passed with `--cases` may target a nested reference directly, such as `rust/references/libraries/new.md`. Only requested extra references are snapshotted, and targets must resolve inside their registered skill. This lets new library guidance and its activation cases evolve inside the owned skill tree without changing the runner's root reference registry. Existing short target aliases remain supported.

## Interpreting evidence

Each case saves `trace.jsonl` (the Claude Code stream-json output), local stderr, `targets.json`, `case.json` and `result.json`; the run saves `summary.json`. The target and case files preserve the exact pre-run content and expectations for offline replay. The scorer binds observed content to those immutable snapshots. Evidence is tool output the session actually returned: a Skill tool injection whose body equals the snapshot's SKILL.md after its YAML frontmatter (whose name and description the session lists as skill metadata), attributed to the successful Skill call that loaded it, or a Read result's raw content, which must match the snapshot's lines exactly. Line-chunked Reads count as complete only when together they cover the whole file. The line-numbered tool_result text alone is not evidence. A path mention, requested tool call, final claim, failed or denied read, or unrelated compiler dependency file cannot establish loading, and a session without a successful result fails. Unsupported output formats remain unverified and fail required-read checks; uncertain reads of forbidden guidance cannot silently pass negative controls. Ordering requires complete earlier guidance before the later read.

Partial, failed or unsupported read attempts also constrain negative and ordering results. A Skill or Read request for forbidden guidance fails the case even when it was denied, and a later complete read cannot erase an earlier uncertain read. Any other tool, subagent activity or a path that could name any target leaves every target unverified. Ordering counts from the later guide's request, so requesting both guides at once does not establish order. Unrelated file reads stay unrelated, and these conservative checks never establish positive loading evidence.

`activation_trace.score_trace` keeps the Codex CLI JSONL rules for historical Codex rollouts: a nonzero exit prevents positive loading proof, while nonempty output, unsupported Node programs, opaque executables and unresolved computed paths leave absence and ordering unverified.

Inspect failed traces before changing an expectation. Add a synthetic regression for a legitimate new trace shape; never execute saved code or make a negative control pass by accepting printed source text. Passing finite cases establishes those observed runs, not universal activation on every model or prompt.

Raw traces can contain local instructions and paths. Keep them outside the repository and public inventory, inspect them locally, and delete the run directory when finished. Commit synthetic fixtures and concise verification records rather than real transcripts. Live tests are opt-in and are not silently added to credential-free CI.

The separate `prompt-evals/cases/` corpus tests bounded maintenance-role decisions; it is not a replacement for activation evaluations. `prompt-evals/holdouts/` contains separately reviewed cases withheld from candidate generation and its first approval. Both sets are hash-addressed; automatic publication cannot edit held-out cases. Promotion requires a repeatable visible-case gain, no lost baseline passes and all critical held-out cases passing. The maintenance task remains a tool-free decision proxy. Prompt-role validation respects each case's eligible skill scope, and evidence/evaluator fingerprints include the shared guidance parser so semantic changes invalidate applicable assessments.

## Investigation regression cases

The investigation suite checks frozen evidence and oracle identities, append-only event chains, coverage and carryover, real subprocess outcomes, bounded execution and unchanged original project copies. The owned `skills/frontend-ui/evaluations/focus-transition/` case exercises a conditional input in an isolated Chromium profile; `skills/content-research-writer/evaluations/failure-accounting/` checks an explicit estimand and justified exclusions. Each includes a deliberately failing baseline plus passing candidate and contrast. These are real synthetic fixture executions, not original-project replay or proof of guidance efficacy. A missing browser is an explicit blocker, never a passing observation.
