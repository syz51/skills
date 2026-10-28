# Contributing

Issues and pull requests are welcome for the skills, references, and evolution tools. Keep a change focused on an observed problem and explain the resulting behavior. Cite version-matched primary sources when changing technical guidance; preserve upstream attribution and license notices.

## Check a change

Use Python, Git, and `uv` on a Unix-like system:

```sh
uv run --no-project python -m unittest discover -s tests
```

CI runs the offline suite on Python 3.13 and 3.14, including checks against the actual skill references, packaged license notices, prompt bundle, and synthetic evaluation cases. Add focused tests for non-trivial logic or regressions. A synthetic evaluation is not evidence of improved behavior on real tasks.

Do not enable transcript capture, model evaluation, or publishing to test an ordinary contribution. Use synthetic fixtures and temporary repositories. Never include credentials, real feedback, private transcripts, operational logs, or personal machine configuration. Report security problems through the [security policy](SECURITY.md).

## Exported files and upstream contributions

This public repository receives selected content from the maintainer's private working source. After merging a public contribution, the maintainer imports its relevant content into that source before the next export. New files also require explicit publication-inventory enrollment by the maintainer; the routine preserves public-owned additions rather than silently overwriting them.

Do not edit `.public-export.json`: it is the exporter's generated content manifest. A managed public edit that has not been imported stops subsequent publication. See [Public publication](docs/publication.md) for the boundary and daily routine.
