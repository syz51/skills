# Public publication

This repository receives a curated export from the author's private maintenance workspace. Its Git history is independent: private commits, branches, feedback, receipts, configuration, and logs are not mirrored.

## Routine

The local publication job checks daily at 10:17 Asia/Tokyo, with a catch-up check at login. It publishes only when the selected content changes. A manual publication uses the same checks:

```sh
uv run --no-project python scripts/public_export.py plan --config ~/.local/share/codex-skills/public-export/config.json
uv run --no-project python scripts/public_export.py publish --config ~/.local/share/codex-skills/public-export/config.json
```

The configuration and publication state stay private. The `plan` command reports selected paths without publishing. Installing the skills does not install this job. The local computer must be awake and online for publication; a missed or failed run is retried by the routine rather than treated as a successful publication.

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
