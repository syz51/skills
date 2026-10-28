# Skills

Agent skills and the tools used to improve them through feedback, source verification, and bounded evaluations.

| Skill | Purpose |
| --- | --- |
| [frontend-ui](skills/frontend-ui/SKILL.md) | Frontend design, implementation, and framework guidance |
| [content-research-writer](skills/content-research-writer/SKILL.md) | Evidence-grounded research, drafting, editing, and review |
| [find-skills](skills/find-skills/SKILL.md) | Discover skills when explicitly requested |
| [tauri](skills/tauri/SKILL.md) | Tauri guidance alongside frontend-ui |

## Install skills

With Bun installed, run:

```sh
bunx skills add syz51/skills
```

Choose the skills and agents you want in the installer. Alternatively, copy a selected `skills/<name>/` directory into your agent's skill discovery directory, preserving its references. Tauri also uses frontend-ui.

Installing skill files does not configure background jobs or start transcript capture. The optional guidance reader and feedback integration use the separate maintenance setup described below.

## How the skills evolve

The Python scripts implement a feedback loop: completed session → abstract feedback → source verification → reviewed skill change → validated release. Additional tools compare upstream references and evaluate bounded changes to the extraction, verification, correction, and maintenance prompts.

The source and tests are published as a reference implementation of a macOS/Unix, two-host maintenance workflow. It retains fixed `client-host` and `maintainer-host` roles and expects explicit configuration, Git repositories, a separate skill validator, and an authenticated Codex CLI. It is not a turnkey service. No real feedback history, transcript evidence, deployment approval marker, machine configuration, or operational logs are included.

**Capture and verification send redacted completed-session text to a Codex model.** Redaction is not a guarantee that all sensitive information has been removed. Review the data flow and choose appropriate source directories before explicitly enabling these commands. Capture currently supports saved Codex and Claude Code sessions.

See [Evolution tools](docs/evolution.md) for commands and limitations, and [Public publication](docs/publication.md) for the daily export process.

## Development

Use Python with `uv` and Git available on a Unix-like system:

```sh
uv run --no-project python -m unittest discover -s tests
```

The tests use synthetic fixtures and temporary repositories. Runtime model-based evaluations require separate authentication and may use account quota; the offline suite does not establish that a prompt improves real tasks.

## License

Original contributions are MIT licensed. Incorporated third-party material retains its own terms and notices; see [Third-party notices](THIRD_PARTY_NOTICES.md). Source attribution and recorded revisions remain alongside the skills.
