---
name: typescript
description: Implement, review, debug, or migrate TypeScript across frontend, backend, libraries, and scripts. Use for .ts, .tsx, .mts, .cts, tsconfig, and TypeScript tooling or type-checking changes; not unrelated work merely in a TypeScript repository. Also use for reviewing feedback to improve this skill.
---

# TypeScript

For saved guidance provenance, use `uv run --script ~/.local/share/codex-skills/repository/scripts/read_guidance.py <file>` when available, one file per invocation with at least 8000 output tokens. Follow `next_start` with `--start <offset>`; use the returned `content` as guidance. Otherwise read each file separately.

Use this skill for language, type-system, and type-checking decisions. For frontend work, read `frontend-ui` first and retain its UI and framework policy; apply this skill alongside it.

Load supporting guidance only when the task needs it:

- Compiler configuration, check coverage, declaration output, published packages, or public type contracts: [Tooling and public types](references/tooling-and-public-types.md).
- Framework, SDK, native, or platform integration: [Skill and reference map](references/skill-map.md), then the relevant owner's guidance.

## Establish the actual contract

- Inspect the relevant package scripts, compiler version, runtime, and effective `tsconfig` inheritance before choosing an implementation or check.
- Respect the package's module format, module resolution, target, library types, and supported consumers. A monorepo root configuration may not describe the affected package.
- Match documentation to the installed toolchain when a compiler feature or integration is uncertain. Do not upgrade dependencies or change unrelated compiler settings to fit a preferred pattern.

## Keep types useful and honest

- Treat untyped external input as `unknown` until its owning boundary establishes a usable type. Reuse existing schemas, parsers, and validated contracts rather than adding duplicate guards inside trusted code.
- Prefer inference for local values. Give exported APIs deliberate contracts where that stabilizes consumer behavior or prevents accidental widening; follow the project's declaration conventions.
- Model correlated states with discriminated unions when they prevent invalid combinations. Use generics to preserve meaningful relationships between inputs and outputs, rather than as speculative flexibility.
- Narrow values through control flow or real validation. An assertion, non-null assertion, or `any` must have a concrete invariant or interop reason; do not use one merely to hide a compiler error.
- Keep unavoidable unsoundness at a small, identifiable interop boundary. A type predicate must actually establish the type it promises.
- Preserve established choices between `type`, `interface`, enums, and equivalent representations unless the task reveals a specific correctness or compatibility problem.

## Preserve runtime and module behavior

- Types and assertions do not validate runtime data. Keep runtime validation separate from compile-time assignability, including when using `satisfies`.
- Use type-only imports where appropriate for the package's compiler and module settings. Preserve required side effects and follow the actual runtime or bundler's import-extension and resolution rules.
- For library changes, consider exported declarations and downstream assignability as well as runtime behavior. Do not accidentally expose private implementation types or break supported consumers.
- Keep async return and error contracts accurate; a declared return type does not make an unchecked runtime value safe.

## Verify the affected path

- Use the project's existing type-check command and the relevant package or project-reference scope. Check inherited configuration and included files when coverage is unclear.
- Successful transpilation, bundling, or execution does not prove type safety. Use the integration owner's guidance for framework files, templates, and generated types.
- Fix the underlying type or runtime contract rather than weakening strictness, broadening types, or adding suppressions solely to pass a check. Preserve necessary existing interop exceptions.
- Run focused regression tests for non-trivial or risky behavior through the existing runner. Add type-level regression coverage when a public type contract is the behavior under test and the project has a suitable convention.
- Report which type checks and behavioral checks ran and any coverage limits. Do not claim an entire monorepo or framework template path was checked from a narrower command.

## Learn from real use

After meaningful authorized work, capture a concrete correction, guidance gap, or verified technique using [Feedback capture and review](references/feedback.md). Skip routine notes. Evidence-supported evolution covers this whole skill: its description and entrypoint, all nested references, and any owned supporting scripts, agent metadata, templates, or assets. Preserve source provenance, privacy, authorization, and runtime contracts. Edit canonical source through the managed review and release workflow; do not edit installed releases.
