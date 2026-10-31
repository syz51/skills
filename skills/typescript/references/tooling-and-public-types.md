# Tooling and public type contracts

Use this reference when changing compiler configuration or verification scope, producing a library, or changing a public type contract. Choose checks proportionate to the affected contract.

## Establish what is checked

- Follow the affected package's scripts and effective configuration: `extends`, project `references`, included files, exclusions, and any command-line overrides. A configuration near the repository root may not govern a particular source file.
- Establish which referenced projects the invoked command visits. A successful root command does not by itself prove that every package or file category was checked.
- Distinguish semantic type checking from transpilation, bundling, declaration generation, framework preparation, and runtime tests. A successful task in one category does not establish the others.
- Generated framework types can be a prerequisite for checking. Use the project's existing preparation and check sequence, and confirm that generated output is included where required. Do not add generators or dependencies merely to follow a generic recipe.
- Check the installed compiler and checker integration when changing toolchains. Package compatibility claims, version ranges, and successful checks of ordinary `.ts` files do not establish coverage for framework templates or generated code.
- When coverage remains uncertain, use a disposable deliberate type error in the affected file category: observe the intended check fail, remove the probe, and rerun the check. Keep the probe outside unrelated work and clean up its output. Do this only when the extra evidence resolves a material uncertainty.

## Verify a library as its consumer sees it

- Preserve supported consumers, module formats, package exports, and declaration entrypoints. Consider both runtime import resolution and the consumer compiler's type resolution.
- Verify the built package through the supported entrypoints in a representative consumer context when changing published exports or declarations. Source-level tests can bypass packaging errors through workspace aliases or direct source imports.
- Confirm that the intended declarations and runtime files are included in the package and resolve together. Declaration emission alone does not prove that published exports point to usable files.
- Exercise the module formats and consumer environments affected by the change; do not claim compatibility for environments that were not checked.
- Follow the project's existing packaging and consumer-test conventions. A private application does not need declaration output or a new publishing workflow merely because it uses TypeScript.

## Test the public type behavior

- Confirm that the effective type-check command includes the type-test files and their intended configuration. A test filename or runtime test pass alone does not establish compiler execution.
- Cover accepted calls and intentionally rejected usage when both define the public contract. Keep runtime behavior tests separate from compile-time acceptance checks.
- Choose assertions that express the intended relationship. Assignability or subset matching can accept types that an equality assertion would reject; neither relationship is universally preferable.
- Treat type assertions exposed by a test library as compiler checks only when the appropriate checker actually runs. Executing assertion-shaped code at runtime does not prove the type relationship.
- Keep an expected-error directive next to the operation intended to fail. Ensure the checked error is relevant to that contract, and that removing the error causes the negative test to fail rather than silently dropping coverage.
- Reuse the project's type-test runner and conventions. Consult the owning runner's reference for its file discovery, assertion semantics, and checker options before changing them.

## Report the evidence

State the commands, package and file-category scope, and consumer paths actually checked. Separate source checking, type-contract tests, built-package resolution, and runtime results when their coverage differs.
