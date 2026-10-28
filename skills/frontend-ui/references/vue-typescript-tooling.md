# Vue and TypeScript tooling decisions

Locally maintained cross-tool guidance for new projects, toolchain upgrades, and performance recommendations. Release and compatibility facts checked **2026-10-26**; recheck them when applying this reference. Preserve legacy projects' versions, contracts, and user choices unless migration is requested. Use the [framework references](frameworks.md) as version-matched API manuals, not upgrade instructions. Source-backed capabilities below do not establish measured application performance.

## Rendering determines the framework

Choose plain Vue/Vite when client rendering and the existing backend/deployment contract suffice. Consider Nuxt when integrated rendering, server endpoints, or deployment conventions solve an actual need. Nuxt's per-route SSR, prerendering, and CSR have different contracts: build-time HTML can use static hosting, request-time rendering needs a runtime, and server caching/ISR depends on the deployment adapter and cache behavior. Confirm freshness and authenticated-content boundaries before caching. See [Nuxt rendering](https://nuxt.com/docs/4.x/guide/concepts/rendering).

[Lazy hydration](https://nuxt.com/docs/4.x/directory-structure/app/components#delayed-or-lazy-hydration) delays activating server-rendered components; code splitting delays loading code. Neither is equivalent to experimental server islands, which introduce their own server/request contract. Choose each for the work it actually defers.

## Prove Vue type-checking coverage

Vite transpiles TypeScript without type-checking. A command-line check for Vue must include SFC templates, typically through `vue-tsc`; plain `tsc` success cannot establish `.vue` coverage. See [Vue's TypeScript guide](https://vuejs.org/guide/typescript/overview.html).

[TypeScript 7](https://devblogs.microsoft.com/typescript/announcing-typescript-7-0/) is a released native compiler, but `vue-tsc` 3.x uses the older JavaScript compiler APIs ([entry point](https://raw.githubusercontent.com/vuejs/language-tools/master/packages/tsc/index.ts)). A broad peer range or successful plain `.ts` check does not prove stock TS7 compatibility. Keep a compatible TS6 path where needed. The [native bridge](https://github.com/vuejs/language-tools/issues/5381) is a distinct integration, not stock TS7 support inferred from its release. [TS7.1 beta content mappers](https://devblogs.microsoft.com/typescript/announcing-typescript-7-1-beta/) and the [Vue Language Tools v4 draft](https://github.com/vuejs/language-tools/pull/6170) describe future integration, not proof of a production default.

When testing a toolchain change, deliberately introduce a template type error in disposable scratch, verify the effective CLI command catches it, then remove the probe. Verify editor diagnostics against the actual editor/compiler pairing when available; report unverified editor coverage separately. Dependency resolution alone is insufficient.

## Separate bundler changes from runtime gains

[Vite 8](https://vite.dev/blog/announcing-vite8) includes Rolldown/Oxc through ordinary `vite`; the older `rolldown-vite` alias is not a required adoption step. Follow installed-version configuration and framework/plugin compatibility. Bundled development and chunk import maps [launched experimentally](https://vite.dev/blog/announcing-vite8-1); recheck their current status and plugin limitations before enabling them. Measure startup, HMR, and build time separately from shipped bundle size, browser runtime, and field metrics. Faster builds alone cannot establish faster user interactions.

## Evaluate Vapor against real components

[Vue 3.6 RC Vapor](https://github.com/vuejs/core/releases/tag/v3.6.0-rc.1) compiles a supported component subset to direct DOM operations. A pure Vapor application can avoid the VDOM runtime; mixed Vapor/VDOM applications retain interop/runtime costs, so bundle benefits differ. Check actual component libraries, Options API, and render-function assumptions rather than assuming compatibility. [Nuxt's integration](https://nuxt.com/docs/4.x/guide/concepts/vuejs-development#vapor-mode) was experimental and interop-only as checked. Benchmark a representative path before recommending adoption; retain the established renderer when benefits or compatibility are unproven.

## Preserve template linting and formatter coverage

[Oxlint's supported script checks](https://oxc.rs/compatibility.html) do not cover Vue template linting. Retain `eslint-plugin-vue` when template rules are needed, scoping overlapping checks deliberately. [Oxfmt's `.vue` wrapper](https://oxc.rs/docs/guide/usage/formatter/language-support) is Prettier-backed while embedded JS/TS blocks use the native formatter. Those Prettier-backed formats require Node.js and the npm-distributed package; the standalone binary skips them. Verify the actual executable and SFC coverage before replacing a formatter. General native-tool speed claims are not Vue-specific benchmarks.

## Separate routing, client state, and server data

[Router 5](https://router.vuejs.org/guide/migration/v4-to-v5) incorporates file-based routing and typed routes; its instructions supersede separate `unplugin-vue-router` setup for v5 only. Preserve v4 contracts. Gate experimental data loaders separately from stable routing features.

Keep local/shared client state distinct from server-data caching, invalidation, and refetching. [Pinia Colada](https://pinia-colada.esm.dev/nuxt.html) or [TanStack Vue Query](https://tanstack.com/query/latest/docs/framework/vue/overview) are optional alternatives when server-state behavior warrants them. Nuxt's native fetching can suffice for simple page data; avoid adding a query dependency solely because a store exists.

## Verify integrated tooling by effective behavior

[Vite+](https://viteplus.dev/guide) optionally integrates tooling and [supports Bun package management](https://viteplus.dev/guide/package-management). That does not prove `.vue` template check coverage: inspect effective commands and reuse the negative probe above. Avoid duplicating tools already supplied by the chosen integration. Use browser tests or [Vitest Browser Mode](https://vitest.dev/blog/vitest-5) when native browser behavior matters, matching the installed runner. Evaluate representative user paths and realistic data/devices rather than selecting a stack from synthetic headlines alone.
