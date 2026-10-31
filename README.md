# Skills

[![CI](https://github.com/syz51/skills/actions/workflows/ci.yml/badge.svg)](https://github.com/syz51/skills/actions/workflows/ci.yml)

Agent skills and the tools used to improve them through feedback, source verification, and bounded evaluations.

| Skill | Purpose |
| --- | --- |
| [frontend-ui](skills/frontend-ui/SKILL.md) | Frontend design, implementation, and framework guidance |
| [content-research-writer](skills/content-research-writer/SKILL.md) | Evidence-grounded research, drafting, editing, and review |
| [find-skills](skills/find-skills/SKILL.md) | Discover skills when explicitly requested |
| [tauri](skills/tauri/SKILL.md) | Tauri guidance alongside frontend-ui |
| [typescript](skills/typescript/SKILL.md) | Shared TypeScript language, tooling, public-type checks, and integration references |

## Install skills

Run the [Skills CLI](https://github.com/vercel-labs/skills) with a package runner you already have:

```sh
npx skills add syz51/skills
```

You can also use `pnpm dlx skills add syz51/skills` or `bunx skills add syz51/skills`. If the CLI is already installed globally, run `skills add syz51/skills` directly.

Choose the skills and agents you want in the installer. Alternatively, copy a selected `skills/<name>/` directory into your agent's skill discovery directory, preserving its references. Tauri also uses frontend-ui; TypeScript work on frontends applies both typescript and frontend-ui.

Installing skill files does not configure background jobs or start transcript capture. The optional guidance reader and feedback integration use the separate maintenance setup described below.

## Install the publisher

The `codex-skills` Rust binary publishes the curated public export and runs its daily deadline check. Download a native archive and `SHA256SUMS` from [Releases](https://github.com/syz51/skills/releases), verify the archive against the checksum file, and install the executable into a directory on `PATH`, such as `~/.local/bin`.

With Rust installed, build the tagged source instead:

```sh
cargo install --git https://github.com/syz51/skills --tag v0.1.0 --locked codex-skills
```

The publisher requires Git, Python for candidate validation, authenticated Codex CLI for message generation, and a deliberately configured private export inventory. Installing it does not configure jobs or enable transcript capture. See [Public publication](docs/publication.md) for its boundaries.

## How the skills evolve

The existing Python maintenance scripts implement a feedback loop: completed session → abstract feedback → source verification → reviewed skill change → validated release. The public exporter and daily deadline runner are implemented in Rust, using the released `codex-commit` library to generate informative messages from public diffs. Capture, source verification and weekly skill maintenance remain Python. Additional tools compare upstream references and evaluate bounded changes to the extraction, verification, correction, and maintenance prompts.

The source and tests are published as a reference implementation of a macOS/Unix, two-host maintenance workflow. It retains fixed `client-host` and `maintainer-host` roles and expects explicit configuration, Git repositories, a separate skill validator, and an authenticated Codex CLI. It is not a turnkey service. No real feedback history, transcript evidence, deployment approval marker, machine configuration, or operational logs are included.

**Capture and verification send redacted completed-session text to a Codex model.** Redaction is not a guarantee that all sensitive information has been removed. Review the data flow and choose appropriate source directories before explicitly enabling these commands. Capture currently supports saved Codex and Claude Code sessions.

See [Evolution tools](docs/evolution.md) for commands and limitations, and [Public publication](docs/publication.md) for the daily export process.

## Development

Use Python with `uv` and Git available on a Unix-like system:

```sh
uv run --no-project python -m unittest discover -s tests
cargo fmt --all -- --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --locked
```

The tests use synthetic fixtures and temporary repositories. Runtime model-based evaluations require separate authentication and may use account quota; the offline suite does not establish that a prompt improves real tasks.

See [Skill testing](docs/testing.md) for the opt-in live activation matrix, trace evidence, negative controls, composition checks and verification limits.

## Contributing and security

See [Contributing](CONTRIBUTING.md) for checks and the export workflow, and [Security policy](SECURITY.md) for private vulnerability reports.

## License

Original contributions are MIT licensed. Incorporated third-party material retains its own terms and notices; see [Third-party notices](THIRD_PARTY_NOTICES.md). Source attribution and recorded revisions remain alongside the skills.
