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

## Message generation

Configure the Codex executable, authentication home, model, reasoning effort and timeout explicitly in the private generation configuration. Also configure `python_path` for the existing candidate-validation suite. The publisher uses the exact released `codex-commit` library; generation receives the staged public diff, excluding generated manifest noise. Private commit messages and private repository history are never used as message context.

Generation uses an isolated, ephemeral context without repository tools. It can consume Codex account quota. Returned subject/body text must pass validation and privacy checks. Empty, generic or invalid messages stop publication and leave the deadline pending; there is no generic fallback. The exporter retains ownership of all Git operations. Plan and unchanged runs do not generate messages.

Ordinary generation excludes the generated manifest. When an imported public contribution requires only refreshed manifest fingerprints, generation receives that public bookkeeping diff so the reconciliation commit can explain the actual remaining change.

## Publication boundary

An explicit file inventory selects skills, supporting code, tests, synthetic evaluation cases, and public documentation. New files require an inventory change. Personal host labels are replaced with reference role names; personal backup metadata is removed while upstream provenance is retained. Content checks reject known private identifiers and credential patterns, and the candidate's unit tests must pass before a public commit is pushed.

These checks reduce exposure risk but do not prove that arbitrary content is safe. New sources and license changes need human review. The initial export omits unpinned copied UnoCSS reference files pending provenance review.

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
    "codex_path": "~/.local/bin/codex",
    "codex_home": "~/.codex",
    "model": "gpt-6-luna",
    "effort": "high",
    "timeout_seconds": 240
  }
}
```

Merge those fields into your reviewed configuration; this fragment is not a complete inventory. The Codex executable must resolve to an absolute existing file after expanding `~`, and the explicit auth home must already contain valid authentication. Generation uses an isolated authentication context, not that home's instructions or plugins. Expired authentication stops publication; authenticate the configured source home with the normal Codex CLI and retry. No generic message is substituted. `plan`, unchanged publication, and a satisfied daily deadline do not call the model.
