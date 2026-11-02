---
name: rust
description: Implement, review, debug, or migrate Rust and its Cargo/toolchain configuration. Use for .rs code, Rust-specific Cargo.toml changes, features, compiler errors, linting, and Rust compatibility work; not unrelated work merely in a Rust repository. Also use for reviewing feedback to improve this skill.
---

# Rust

For saved guidance provenance, use `uv run --script ~/.local/share/codex-skills/repository/scripts/read_guidance.py <file>` when available, one file per invocation with at least 8000 output tokens. Follow `next_start` with `--start <offset>`; use the returned `content` as guidance. Otherwise read each file separately.

Use this skill for Rust language, ownership, API and Cargo decisions. For Tauri native integration, also follow `tauri`. For frontend work, read `frontend-ui` before language guidance and follow the relevant language skill; a Rust backend does not make frontend guidance relevant to every Rust change.

Load supporting guidance only when needed:

- Toolchains, editions, MSRV, workspace/feature configuration, dependency changes, or check selection: [Cargo and compatibility](references/cargo-and-compatibility.md).
- Public APIs, traits, conversions, error contracts, documentation, or compatibility: [APIs and errors](references/api-and-errors.md).
- Futures, task lifecycle, synchronization, or concurrent shutdown: [Async and concurrency](references/async-and-concurrency.md).
- Unsafe code, raw pointers, native handles or FFI: [Unsafe and FFI](references/unsafe-and-ffi.md).

## Establish the actual contract

Inspect the affected crate, workspace manifest, lockfile, toolchain configuration and existing CI/check commands. Respect the project's supported Rust versions, targets, features, runtime, lint policy and dependencies. Confirm version-sensitive APIs against the relevant toolchain or dependency documentation; do not upgrade a project to fit a preferred implementation.

## Make ownership and state clear

- Choose owned or borrowed values according to who retains data and how long it must live. Prefer ordinary lifetimes and local ownership; introduce shared ownership, interior mutability or dynamic dispatch when the actual boundary needs them.
- Resolve borrow errors by understanding aliases and lifetimes. A clone can be a clear, correct ownership choice; avoid copying large data merely to hide a design error, and do not turn clone removal into an unrelated optimization project.
- Use enums, newtypes and visibility to express meaningful states and protect established invariants. Avoid speculative trait layers, generic parameters and lifetime abstractions.
- Use `Result` and `Option` for expected failure or absence. Preserve useful error causes and the owning layer's recovery contract. An `unwrap` or `expect` needs a defensible invariant or test/setup context; neither blanket bans nor unchecked panics at external-input boundaries help correctness.
- Do not introduce unsafe code, unsafe trait implementations or unchecked conversions merely to bypass the compiler. Keep any necessary unsafe boundary small with explicit safety reasoning.

## Verify the affected behavior

Use the project's existing commands and the smallest meaningful crate/feature/target scope, including behavioral regression tests for non-trivial changes. Select formatting, compiler, Clippy, tests and documentation checks according to what changed. Build success is not runtime proof; host tests do not establish other target or MSRV support. Preserve existing lint policy instead of broadly suppressing diagnostics or adding stricter lint groups to unrelated code. Report the commands, configuration coverage and remaining limits.

## Learn from every use

At completion, assess whether this use revealed a correction, guidance gap, unnecessary prescribed work or verified reusable technique. Save concrete lessons through [Feedback capture and review](references/feedback.md); a routine task may yield no lesson. During read-only work, report material observations without saving unless feedback saving is authorized. Evidence-supported evolution covers this whole skill tree, including routing, references, owned scripts, metadata and evaluation fixtures. Edit canonical source through the managed review and release workflow, never installed releases. Preserve source provenance, privacy, authorization and project contracts.
