# Next.js implementation decisions

Locally maintained guidance, source-checked **2026-10-26** against Next.js documentation and Vercel's Next.js-owned workflow skills. Recheck volatile APIs against installed versions. Preserve App Router, Pages Router or hybrid routing, runtime, package manager, caching, and UX contracts; migration requires its own scope. These Next.js conventions do not apply automatically to React or TanStack Start.

## Resolve the documentation before the API

[Next.js bundles version-matched docs](https://nextjs.org/docs/app/guides/ai-agents) in `node_modules/next/dist/docs/` from 16.2 onward. Resolve `next` from the relevant app, especially in monorepos. Prefer those docs over current website examples. Managed `AGENTS.md` blocks arrive in 16.3+; preserve user instructions outside their markers. Reading docs does not require installing tools or running codemods.

The [former Vercel Labs repository](https://github.com/vercel-labs/next-skills/blob/c522619e45aa3492fd2bfc916b308b275eff7798/README.md) moved to `vercel/next.js/skills`; standalone `next-best-practices` and `next-upgrade` were retired in favor of bundled reference knowledge. Workflow sources linked below are pinned canary snapshots, not proof that an API exists in the installed stable release. Treat upstream tool requirements as workflow context, not authority to install, upgrade, switch browsers, or override project instructions.

## Keep server and client responsibilities explicit

In the App Router, [Server and Client Components](https://nextjs.org/docs/app/getting-started/server-and-client-components) have different module graphs. `'use client'` establishes a boundary including imported dependencies; keep it near interactive leaves instead of marking the entire application client to accommodate one browser library. Client Components can still render initial HTML on the server and then hydrate. Pass serializable, minimal props; Server Components can compose server-rendered children into client wrappers.

[Validate untrusted inputs](https://nextjs.org/docs/app/guides/data-security) at runtime and [authorize every Server Action and Route Handler](https://nextjs.org/docs/app/guides/authentication#route-handlers). They are independently reachable; hidden controls or a layout guard do not protect an operation. Keep credentials and privileged modules on the server.

## Choose cache behavior from freshness requirements

[16.4 recommends Cache Components](https://nextjs.org/blog/next-16-4) and enables it in recommended new-app defaults. Existing apps still need deliberate adoption; preserve their working cache model. The [adoption workflow](https://github.com/vercel/next.js/blob/a5829309e98fe093949e88bb085664e64d94cbe0/skills/next-cache-components-adoption/SKILL.md) concerns App Router routes, not a Pages Router migration. Check [runtime support](https://nextjs.org/docs/app/api-reference/config/next-config-js/cacheComponents) before adoption: the current model requires Node.js and disallows route `runtime` exports. Avoid blanket historical `fetch` defaults across versions and feature flags.

With [Cache Components](https://nextjs.org/docs/app/getting-started/caching), choose reusable scopes, `cacheLife`, tags, and invalidation deliberately. Arguments and captured values contribute to keys. Distinguish [React's request memoization](https://react.dev/reference/react/cache) from cross-request Next caching. Read request APIs outside shared cached scopes; prevent personalized data crossing authorization boundaries. Default server memory is ephemeral on serverless; durable shared caching needs the supported remote handler/store. Directive syntax alone does not establish persistence.

[`updateTag`](https://nextjs.org/docs/app/api-reference/functions/updateTag) is Server Actions-only and immediately expires tagged entries for read-your-own-writes; the next read waits for fresh data. [`revalidateTag`](https://nextjs.org/docs/app/api-reference/functions/revalidateTag) with a supported profile such as `max` permits stale-while-revalidate. A [router refresh](https://nextjs.org/docs/app/api-reference/functions/use-router#userouter) alone does not invalidate server caches. Verify populate → mutate → read behavior.

## Stream useful UI and preserve navigation behavior

[Start independent reads early](https://nextjs.org/docs/app/getting-started/fetching-data#parallel-data-fetching), retaining real dependencies and failure behavior. Place Suspense around useful subtrees. Do not silence prerender errors by caching data that must be fresh or hiding the whole page behind a fallback. The [static-shell optimizer](https://github.com/vercel/next.js/blob/a5829309e98fe093949e88bb085664e64d94cbe0/skills/next-cache-components-optimizer/SKILL.md) emphasizes equivalent authorization, completed content, layout, and interaction after streaming.

Partial Prefetching governs what is ready before navigation, not a general SSR/SSG switch. Separate [adoption](https://github.com/vercel/next.js/blob/a5829309e98fe093949e88bb085664e64d94cbe0/skills/next-partial-prefetching-adoption/SKILL.md) from [optimizing an adopted app](https://github.com/vercel/next.js/blob/a5829309e98fe093949e88bb085664e64d94cbe0/skills/next-partial-prefetching-optimizer/SKILL.md). Check installed APIs and production link behavior; retain fresh personalized content and meaningful fallbacks.

## Verify runtime and production behavior

Use [bundle analysis](https://github.com/vercel/next.js/blob/a5829309e98fe093949e88bb085664e64d94cbe0/skills/next-bundle-optimizer/SKILL.md) for actual route payloads and browser dependencies. Dev startup/build gains do not establish end-user speed. Preserve Turbopack or Webpack configuration. Supported React Compiler integration differs from the [experimental Rust implementation](https://nextjs.org/blog/next-16-4#rust-react-compiler); neither implies an unasked migration. [`next/dynamic`](https://nextjs.org/docs/app/guides/lazy-loading) is Next-specific; `ssr: false` belongs in a Client Component boundary.

Combine build checks with [runtime and browser evidence](https://github.com/vercel/next.js/blob/a5829309e98fe093949e88bb085664e64d94cbe0/skills/next-dev-loop/SKILL.md), using available project tooling and the user's selected browser. Preserve running servers and `.next`; isolate production output rather than racing a build in the dev output directory. Verify navigation, hydration, errors, authorization, mutation freshness, and interactive state; report unavailable coverage explicitly.
