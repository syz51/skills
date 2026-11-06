# Public publication

This repository receives a curated export from the author's private maintenance workspace. Its Git history is independent: private commits, branches, feedback, receipts, configuration, and logs are not mirrored.

## Routine

The local publication job checks daily at 10:17 Asia/Tokyo, with a catch-up check at login. It publishes only when the selected content changes. A manual publication uses the same checks:

```sh
codex-skills plan --config ~/.local/share/codex-skills/public-export/config.json
codex-skills publish --config ~/.local/share/codex-skills/public-export/config.json
```

The Rust executable also provides `codex-skills daily --config <path>` for the local scheduler. The installed executable is `~/.local/bin/codex-skills`. The former Python exporter and daily wrapper are retained as regression references; production publication uses the Rust binary.

The configuration and publication state stay private. The `plan` command reports selected paths without publishing. Installing the skills does not install this job. The local computer must be awake and online for publication; a missed or failed run is retried by the routine rather than treated as a successful publication.

Publication waits up to five minutes for the shared maintenance lock, checking cancellation while waiting. Publication and daily-job locks remain nonblocking. A daily attempt still blocked by another maintenance or publication transaction records `outcome: "deferred"` and `reason: "publication_lock_held"`, with `success: null`. It leaves the deadline pending for the next scheduled attempt. Lock I/O errors remain failures. A second daily process cannot replace the active process's status.

## Message generation

Configure the Claude Code executable, model, effort and optional timeout explicitly in the private generation configuration. Also configure `python_path` for the existing candidate-validation suite. The publisher runs the Claude Code CLI directly; generation receives the staged public diff, excluding generated manifest noise. Private commit messages and private repository history are never used as message context.

Generation runs in a fresh empty directory with a minimal environment, no tools, MCP servers, slash commands, user settings or instructions, and no saved session. It uses the signed-in Claude subscription and its usage limits. Returned subject/body text must pass quality, attribution and private-identifier/credential checks. When the scanner rejects a message, generation runs again with an instruction to leave out local paths, host names and credentials, up to three attempts in all. Empty, generic or otherwise invalid messages, and a message rejected on every attempt, stop publication and leave the deadline pending; there is no generic fallback. The exporter retains ownership of all Git operations. Plan and unchanged runs do not generate messages.

Ordinary generation excludes the generated manifest. When an imported public contribution requires only refreshed manifest fingerprints, generation receives that public bookkeeping diff so the reconciliation commit can explain the actual remaining change.

## Publication boundary

An explicit file inventory selects skills, supporting code, tests, synthetic evaluation cases, and public documentation. New files require an inventory change. Personal host labels are replaced with reference role names; personal backup metadata is removed while upstream provenance is retained. Files must be UTF-8 text. After those replacements and removals, content checks reject known private identifiers and credential patterns. Identifiers match in any case but replacements match exactly, so an unreplaced case variant is rejected. A lowercase users route counts as a home path only when it starts a path and a name follows: `/users/:id` and `/api/users/` publish, while a top-level users sign-in route is rejected. The candidate's unit tests must pass before a public commit is pushed.

Changed snapshots containing `Cargo.toml` also run formatting, strict Clippy and locked Rust tests before commit-message generation or push. These checks share a ten-minute deadline; failures retain bounded local diagnostics and stop publication. Rust, Cargo and the pinned toolchain must be available on the publishing host. Public-owned code is excluded from candidate execution.

These checks reduce exposure risk but do not prove that arbitrary content is safe. New sources and license changes need human review. The three retained UnoCSS snapshots are explicitly enrolled after their original hashes and bundled MIT license were matched to a pinned upstream revision recorded in source metadata.

The exporter manages only its selected paths and its manifest. Other public-owned files are preserved. There are no force pushes. If a managed public file changes independently, publication stops so the change can be imported and reconciled in the private source. Changes to private review records alone do not require a public commit.

## Branch protection and public CI

The history ruleset blocks force pushes and deletion of `main` for everyone, including repository administrators. A separate ruleset requires the public CI checks for ordinary changes, with a repository-admin bypass for the existing owner-operated daily publisher. The publisher uses the same owner SSH credential as before; it is not a separately scoped bot identity. The only current repository administrator is the owner.

The exporter runs its local checks before pushing. Public CI independently runs on pull requests and every push to `main`, checking the final public tree, including public-owned additions. The local exporter executes only the managed snapshot so public-owned tests cannot run with private credentials. Administrators can bypass the required-check ruleset, but cannot bypass the separate history ruleset. The owner should use the passing public checks when reviewing contributions.

## Contributions

Public contributions are reviewed here. After a change is merged, the maintainer imports the relevant content into the private working source and reconciles the publication baseline before resuming exports. A routine must never overwrite an independent public change merely because its private copy is older.

A successful public export means the checked public content was published. It does not mean another machine has installed that version or that a synthetic evaluation has established improved real-world behavior.

## Generation configuration

The Rust configuration retains the existing schema-1 inventory and state paths. Add an explicit Python executable for legacy candidate checks and a generation section:

```json
{
  "python_path": "python3",
  "generation": {
    "claude_path": "/absolute/path/to/claude",
    "model": "claude-haiku-5-5",
    "effort": "high",
    "timeout_seconds": 240
  }
}
```

Merge those fields into your reviewed configuration; this fragment is not a complete inventory. `claude_path` must be an absolute path to an existing file, and the Claude Code CLI must already be signed in for the publishing user. `effort` is one of `low`, `medium`, `high`, `xhigh` or `max`; `timeout_seconds` (1 to 900, default 120) bounds the whole call. Unknown fields are rejected. Missing or expired authentication stops publication; sign in with the normal Claude Code CLI and retry. No generic message is substituted. `plan`, unchanged publication, and a satisfied daily deadline do not call the model.

## Weekly investigation boundary

Private weekly publication follows isolated review and a machine-readable investigation gate. All six owned skill trees may change, including first-party fixtures; source identities, root policy and immutable notes/receipts remain protected. Sanitized investigation events are append-only private evidence and are excluded from this public export. New public fixture and tool files still require explicit inventory enrollment. Reverting guidance means a reviewed forward commit, never a force push or history rewrite.
