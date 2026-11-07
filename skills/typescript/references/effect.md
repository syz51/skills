# Effect

Follow Effect where the project already uses it. Elsewhere, Effect is not the default: adopt it only where its typed errors, services, resource safety, or structured concurrency clearly pay for the added dependency and abstraction, and then incrementally at clear boundaries; preserve project requirements, framework contracts, and established integrations. Keep pure calculations and view logic ordinary TypeScript where an Effect adds no useful contract.

## Match the project

- Inspect installed `effect`, platform, and testing packages before selecting APIs. Use documentation, source examples, and any available `LLMS.md` matching those versions; v3 and v4 APIs are not interchangeable. Do not upgrade a major version or vendor a repository merely to follow a guide. Treat available library source as read-only reference and import from normal package dependencies. See [official documentation](https://effect.website/docs/) and [Effect's agent guidance](https://effect.website/blog/the-one-weird-git-trick-that-makes-coding-agents-more-effect-ive).
- Preserve the package manager, runtime, lockfile, and existing build/check commands. Use Bun for new JavaScript/TypeScript tooling unless the project requires otherwise.

## Compose inside, run at the boundary

- Compose operations as Effects, using generators or combinators according to project conventions. Keep `run*` calls at application or framework entrypoints rather than nesting `runPromise` inside Effect logic. Expose a Promise where the host requires one, with explicit error translation and cancellation ownership. Follow [running effects](https://effect.website/docs/v4/getting-started/running-effects) for v4; select the matching documentation for other versions.
- Represent expected domain failures in the typed error channel. Wrap throwing or rejecting SDK operations at their owning boundary using version-correct constructors. Recover only where recovery is meaningful; preserve defects and interruption rather than converting every failure into success. See [creating effects](https://effect.website/docs/v4/getting-started/creating-effects) and [expected errors versus defects](https://effect.website/docs/v4/error-management/two-error-types).
- Reuse service definitions, layers, and runtime lifetimes. Give acquired resources scoped cleanup, bound parallel work, and keep child work attached to its owning lifecycle. Retry recoverable operations only when repetition is safe, with a bounded retry policy and cancellation. See [concurrency](https://effect.website/docs/v4/concurrency/basic-concurrency) and the [v3 resource guide](https://effect.website/docs/v3/resource-management/introduction); verify equivalent APIs for the installed major.
- Reuse existing boundary schemas. For new Effect boundaries, prefer Effect Schema when it fits; avoid validating the same trusted value again. The [v3 Schema introduction](https://effect.website/docs/v3/schema/introduction) is version-specific.
- Preserve framework rendering, hook, cache, and request lifecycles. The Effect library does not replace React's `useEffect`; integrate at the appropriate lifecycle boundary.

## Verify behavior

Run the affected project's type checks and focused tests for success, expected failures, interruption, and cleanup where relevant. Reuse its runner and compatible Effect testing helpers; a Bun test suite does not require installing `@effect/vitest`. Report the behavior actually checked and remaining integration limits.
