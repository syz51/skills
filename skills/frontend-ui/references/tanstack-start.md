# TanStack Start decisions and API boundaries

Locally maintained, on-demand React Start guidance; facts checked **2026-10-26**. Recheck installed Start, Router, Query, bundler, and deployment-adapter versions. Preserve existing contracts unless migration is requested. Use the [framework routing guide](frameworks.md) and [React guide](react.md) for adjacent concerns. The current [Start overview](https://tanstack.com/start/latest/docs/framework/react/overview) marks Start stable; React Server Components remain experimental and opt-in. Older beta labels elsewhere do not establish the current release contract.

## Resolve build and deployment contracts first

Read the project's configuration and [build guide](https://tanstack.com/start/latest/docs/framework/react/build-from-scratch), then the selected [hosting integration](https://tanstack.com/start/latest/docs/framework/react/guide/hosting). In Vite, Start precedes the React plugin; follow adapter-specific ordering as well. Current Vite migration examples do not authorize an unrelated upgrade. Do not transplant legacy Vinxi commands, Nitro output paths, or another adapter's start command. Confirm the actual server artifact and runtime.

Use package-matched TanStack-owned skills, including the pinned [React Start skill](https://github.com/TanStack/router/blob/da2adec76dbe6c0da6d6a0d5640f7b9388217f45/packages/react-start/skills/react-start/SKILL.md) and its linked execution, server-function, middleware, and deployment guidance. Newer examples use `.validator()` where older releases use `.inputValidator()`; resolve the installed API through its types rather than mechanically rewriting working code.

## Isomorphic loaders are not private server boundaries

Under Start's [execution model](https://tanstack.com/start/latest/docs/framework/react/guide/execution-model), route loaders and `beforeLoad` can run during SSR and client navigation. Put private operations behind `createServerFn`; `createServerOnlyFn` enforces execution location but does not create an RPC endpoint. Keep request context isolated per request, not in shared mutable module state. `VITE_` environment variables are public client inputs. Isolate browser-only work behind `ClientOnly` or another narrow execution boundary.

Check [import protection](https://tanstack.com/start/latest/docs/framework/react/guide/import-protection) in production: development warnings or mocks do not prove a server import is absent from a client build. Test direct loads and client navigation because they exercise different execution paths.

## Validate and authorize independently reachable endpoints

[Server functions](https://tanstack.com/start/latest/docs/framework/react/guide/server-functions) need runtime input validation; TypeScript annotations and identity validators do not validate network input. Authorize each private RPC or server route independently: a route guard does not protect an endpoint called directly. Treat client-supplied context, including middleware `sendContext`, as untrusted. Follow [middleware](https://tanstack.com/start/latest/docs/framework/react/guide/middleware) boundaries when adding trusted server-derived context.

Creating `src/start.ts` changes default server-function CSRF setup: preserve/add `createCsrfMiddleware` with the documented server-function handler filter instead of suppressing the warning. Keep server functions within their same-origin application contract; use server routes for deliberately public or cross-origin endpoints.

## Choose rendering and cache ownership deliberately

[Selective SSR](https://tanstack.com/start/latest/docs/framework/react/guide/selective-ssr) has distinct modes: `ssr: 'data-only'` runs server data loading without server-rendering the route component; `ssr: false` skips server `beforeLoad` and loader execution too. Prefer a narrow client-only boundary for a browser-dependent widget before disabling an entire route's SSR. Preserve the required server-rendered document shell even when the root route component's SSR is disabled.

Router [data loading](https://tanstack.com/router/latest/docs/framework/react/guide/data-loading) runs `beforeLoad` serially before parallel loaders. Keep guards/context work small. Make `loaderDeps` deterministic and side-effect-free, including search values that affect the loaded result so cache identity matches the request.

Query is optional. When using [external data loading](https://tanstack.com/router/latest/docs/framework/react/guide/external-data-loading), identify whether Router or Query owns freshness. Create a fresh QueryClient per server request/router, hydrate through the actual integration, and share query keys/options between loaders and components. Await critical prefetches; handle rejection of deferred work. When Query owns freshness, version-matched integrations commonly use `defaultPreloadStaleTime: 0` so Router does not suppress Query's checks. Match installed Query APIs instead of forcing a migration from examples. After mutation, invalidate the actual cache owner: Router invalidation alone may reuse fresh Query data. See the pinned [Router/Query composition skill](https://github.com/TanStack/router/blob/da2adec76dbe6c0da6d6a0d5640f7b9388217f45/packages/react-router/skills/compositions/router-query/SKILL.md).

## Verify the real runtime

[Experimental RSC](https://tanstack.com/start/latest/docs/framework/react/guide/server-components) requires Start's own plugin and serialization contract; ordinary SSR streaming is not RSC, and Next.js conventions do not transfer automatically. Verify affected server execution, hydration, navigation, validation, cache freshness, and the production adapter path. A type-check or jsdom-only test cannot establish those runtime contracts. State which paths remain unverified; documentation-backed capabilities are not local performance measurements.
