# TypeScript skill and reference map

The TypeScript skill owns shared language, type-system, and checking decisions. Framework syntax, API examples, compiler integrations, and platform contracts remain with their maintained owners.

Resolve an owner through the available skill catalog, read its entrypoint, then load only the reference relevant to the task. Paths below are relative to that owner's skill directory, not this file. Respect the owner's invocation restrictions and the installed package versions. If an owner is unavailable, use official documentation matching the installed toolchain rather than guessing a plugin cache path.

For frontend work, read `frontend-ui` first. This map does not broaden other skills' triggers or require every owner to load for an ordinary TypeScript edit.

| Task or uncertainty | Owner | Targeted reference or boundary |
| --- | --- | --- |
| Vue SFC/template checking and compiler bridges | `frontend-ui` | `references/vue-typescript-tooling.md`; ordinary `.ts` success does not establish template coverage. |
| Vite configuration, environment types, and compiler integration | `frontend-ui` | `references/frameworks/vite/guide.md`, then `references/frameworks/vite/references/core-config.md` for configuration. Use the guide's environment reference for client/server exposure. |
| Nuxt preparation, generated types, and checking | `frontend-ui` | `references/frameworks/nuxt/guide.md`, then `references/frameworks/nuxt/references/core-cli.md`. |
| Vitest type-test discovery and assertion semantics | `frontend-ui` | `references/frameworks/vitest/references/advanced-type-testing.md`. |
| tsdown declaration output, exports, and bundling | `frontend-ui` | `references/frameworks/tsdown/guide.md`. Apply shared consumer checks from this skill without copying bundler-specific options. |
| React, Next.js, and TanStack framework contracts | `frontend-ui` | `references/frameworks.md`, then the relevant framework reference. |
| React review after editing multiple TSX components | `vercel:react-best-practices` | `SKILL.md`; preserve its existing review trigger and React-specific checklist. |
| Rust/TypeScript IPC and native runtime boundaries | `tauri` | `SKILL.md`; TypeScript compilation does not establish native-side correctness or serialized IPC compatibility. |
| AI SDK inferred UI/agent types and SDK APIs | `vercel:ai-sdk` | `SKILL.md`; retain SDK-specific inference and protocol guidance with the SDK. |
| Feature flags and `flags.ts` conventions | `vercel:flags-sdk` | `SKILL.md`; Flags Explorer export conventions are product-specific. |
| Vercel module/runtime, deployment, or durable execution integration | `vercel:vercel-cli`, `vercel:vercel-functions`, or `vercel:workflow` | The relevant `SKILL.md`; choose by the actual platform task. Platform build success does not replace semantic checking. |

Other product and artifact skills may contain TypeScript examples. Load those skills when the requested product workflow applies; the presence of an example does not make its API conventions general TypeScript policy. Leave versioned manuals and examples with their owners rather than copying them into this skill.
