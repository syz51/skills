Shared frontend/UI policy: [frontend-ui](../../../SKILL.md) and [framework boundaries](../../frameworks.md) govern design, scope, structure, tooling preferences, and verification. This is a versioned technical reference; load only relevant topics.

# Pinia

Pinia is the official state management library for Vue, designed to be intuitive and type-safe. It supports both Options API and Composition API styles, with first-class TypeScript support and devtools integration.

> The skill is based on Pinia v3.0.4, generated at 2026-01-28.

## Core References

| Topic | Description | Reference |
|-------|-------------|-----------|
| Stores | Defining stores, state, getters, actions, storeToRefs, subscriptions | [core-stores](references/core-stores.md) |

## Features

### Extensibility

| Topic | Description | Reference |
|-------|-------------|-----------|
| Plugins | Extend stores with custom properties, state, and behavior | [features-plugins](references/features-plugins.md) |

### Composability

| Topic | Description | Reference |
|-------|-------------|-----------|
| Composables | Using Vue composables within stores (VueUse, etc.) | [features-composables](references/features-composables.md) |
| Composing Stores | Store-to-store communication, avoiding circular dependencies | [features-composing-stores](references/features-composing-stores.md) |

## Best Practices

| Topic | Description | Reference |
|-------|-------------|-----------|
| Testing | Unit testing with @pinia/testing, mocking, stubbing | [best-practices-testing](references/best-practices-testing.md) |
| Outside Components | Using stores in navigation guards, plugins, middlewares | [best-practices-outside-component](references/best-practices-outside-component.md) |

## Advanced

| Topic | Description | Reference |
|-------|-------------|-----------|
| SSR | Server-side rendering, state hydration | [advanced-ssr](references/advanced-ssr.md) |
| Nuxt | Nuxt integration, auto-imports, SSR best practices | [advanced-nuxt](references/advanced-nuxt.md) |
| HMR | Hot module replacement for development | [advanced-hmr](references/advanced-hmr.md) |

## Key Recommendations

- **Prefer Setup Stores** for complex logic, composables, and watchers
- **Use `storeToRefs()`** when destructuring state/getters to preserve reactivity
- **Actions can be destructured directly** - they're bound to the store
- **Call stores inside functions** not at module scope, especially for SSR
- Preserve existing HMR support; add it when relevant to the project development flow
- Reuse `@pinia/testing` when installed and useful for the test; otherwise use the established testing setup
