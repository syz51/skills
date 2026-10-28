Shared frontend/UI policy: [frontend-ui](../../../SKILL.md) and [framework boundaries](../../frameworks.md) govern design, scope, structure, tooling preferences, and verification. This is a versioned technical reference; load only relevant topics.

> The skill is based on Nitro v3 (beta, 3.0.260903), generated at 2026-09-25.

Nitro is a framework-agnostic, deployment-agnostic server toolkit powered by [H3](https://h3.dev) v2, [unstorage](https://unstorage.unjs.io), and Vite/Rolldown/Rollup. It powers Nuxt and works standalone. From one codebase it builds optimized output for Node.js, Bun, Deno, Cloudflare, Vercel, Netlify, and more.

Key capabilities:
- **Filesystem routing** with H3 v2 event handlers, dynamic params, and method suffixes.
- **Route rules** for declarative caching, headers, redirects, proxying, and CORS. Authentication uses executable middleware; see [routing](references/core-routing.md#middleware).
- **Caching** layer (cached handlers/functions, SWR) on top of **unstorage** KV.
- **Runtime config** overridable via `NITRO_*` env vars.
- **Tasks** (on-demand + scheduled/cron), **WebSockets**/SSE, a SQL **database** layer, and **OpenAPI** auto-docs.
- **Plugins & lifecycle hooks**, custom **renderer**/**server entry**, and portable **deployment presets**.

> Check the installed Nitro version and runtime before applying these examples. In this v3 beta snapshot, `nitropack` is renamed to `nitro` with H3 v2 (`Request`/`Response`); APIs require explicit imports, directory scanning is opt-in via `serverDir`, `useKV` from `nitro/kv` replaces `useStorage`, `kv` replaces the `storage` config alias, and cache `swr` defaults to `false`. For an actual v2 → v3 migration, consult [advanced-migration](references/advanced-migration.md) against the target version. These changes do not require migrating an existing project.

## Core

| Topic | Description | Reference |
|-------|-------------|-----------|
| Routing | File-based routes, `defineHandler`, params, middleware, route rules, errors | [core-routing](references/core-routing.md) |
| Configuration | `nitro.config.ts`, `defineConfig`, key options, runtime config | [core-configuration](references/core-configuration.md) |
| KV Storage | `useKV`, `kv` mounts, drivers, dynamic mounts | [core-storage](references/core-storage.md) |
| Cache | `defineCachedHandler`, `defineCachedFunction`, SWR, invalidation | [core-cache](references/core-cache.md) |
| Assets | Public assets, compression, server assets via `useKV` | [core-assets](references/core-assets.md) |
| Rendering | Renderer (HTML/SSR), server entry, framework integration | [core-rendering](references/core-rendering.md) |

## Features

| Topic | Description | Reference |
|-------|-------------|-----------|
| Plugins & Hooks | `definePlugin`, runtime lifecycle hooks, error capture | [features-plugins](references/features-plugins.md) |
| Tasks | On-demand & scheduled (cron) tasks, `runTask` | [features-tasks](references/features-tasks.md) |
| WebSocket & SSE | `defineWebSocketHandler`, pub/sub, namespaces, event streams | [features-websocket](references/features-websocket.md) |
| Database | Built-in SQL layer via db0, `useDatabase`, connectors | [features-database](references/features-database.md) |
| OpenAPI | Auto spec from `defineRouteMeta`, Scalar/Swagger UIs | [features-openapi](references/features-openapi.md) |

## Advanced / Deployment

| Topic | Description | Reference |
|-------|-------------|-----------|
| Deployment Presets | Runtimes & providers, compatibility dates, platform integration | [deploy-presets](references/deploy-presets.md) |
| v2 → v3 Migration | Package rename, `nitro/*` imports, H3 v2 API, preset changes | [advanced-migration](references/advanced-migration.md) |
