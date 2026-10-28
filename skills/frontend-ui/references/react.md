# React decisions and API boundaries

Locally maintained, on-demand guidance; facts checked **2026-10-26**. Recheck version-sensitive APIs against installed React, renderer, framework, and build integration. Preserve existing contracts unless migration is requested. Use the [framework routing guide](frameworks.md) for framework-specific work; React-wide APIs do not imply Next.js or TanStack Start behavior.

## Preserve identity before optimizing

State belongs to a component's type and position in the tree, with keys refining identity. Define components outside other components: recreating their type during render can reset state and DOM. Memoization cannot repair that identity problem. Use a deliberate key when a change should reset a subtree; avoid accidental resets while editing, reordering, or switching views. See [preserving and resetting state](https://react.dev/learn/preserving-and-resetting-state).

Derive display data during render instead of copying it into state through an Effect. Put interaction-specific work in handlers. Reserve Effects for synchronizing external systems, with dependencies and cleanup matching that synchronization. See [when Effects are unnecessary](https://react.dev/learn/you-might-not-need-an-effect). Where supported, [useEffectEvent](https://react.dev/reference/react/useEffectEvent) reads the latest committed values for event logic inside Effects. It cannot silence real dependencies, be passed to children or other Hooks, or serve as a stable callback identity.

## Actions need an execution and reconciliation model

React 19 [useActionState](https://react.dev/reference/react/useActionState) and [useOptimistic](https://react.dev/reference/react/useOptimistic) coordinate action state and optimistic UI. Dispatch through the supported Action/Transition context, including form action integration where appropriate. Model pending, failure, and authoritative server results: optimistic state is temporary, and successful mutation must reconcile the actual data owner. Client Actions do not require React Server Components.

[Transitions](https://react.dev/reference/react/useTransition) schedule interruptible rendering; they do not move synchronous computation to a worker or automatically solve asynchronous request ordering. Keep controlled input updates urgent, and handle stale responses or use an abstraction with the required ordering contract. Under the current API limitation, state updates after an `await` need another `startTransition` to be marked as Transitions. Verify rapid repeated interactions and failures, not only the first successful request.

## Distinguish hiding, rendering, and server execution

[Activity](https://react.dev/reference/react/Activity), available from React 19.2, preserves hidden state and DOM while cleaning up Effects, then recreates Effects on reveal. Hidden children can still render at lower priority; this is not unmounting or a guarantee of zero background work.

[ViewTransition](https://react.dev/reference/react/ViewTransition) and [Fragment refs](https://react.dev/reference/react/Fragment) are stable in React 19.3; do not label every use Canary-only. Gate adoption on the actual runtime and framework support, browser behavior where relevant, and reduced-motion requirements. A current documentation page does not establish availability in an older application.

[Server Components](https://react.dev/reference/rsc/server-components) are a React 19 application model, distinct from ordinary server rendering and streaming. Framework/bundler implementation APIs do not have the same minor-version stability guarantee. Follow the selected framework's serialization, server/client, and deployment contracts instead of inventing a portable RSC setup.

## Treat Compiler adoption as a build decision

React Compiler 1.0 is stable, but installing React 19 does not enable it. Prefer the project's existing integration and [version-matched setup](https://react.dev/learn/react-compiler/installation). The Babel compiler runs before other Babel transforms. `@vitejs/plugin-react` 6+ uses `reactCompilerPreset` with the documented Babel integration; its old inline Babel option was removed. React 17/18 need the matching compiler target and runtime; the default target is 19.

Use [incremental adoption](https://react.dev/learn/react-compiler/incremental-adoption) and diagnostics rather than deleting manual memoization wholesale. Current [Hooks lint rules](https://react.dev/reference/eslint-plugin-react-hooks) cover purity, immutability, render-time refs, static components, incompatible libraries, and preserving manual memoization. Existing `memo` does not prevent local-state or consumed-context updates. Use [React Performance Tracks](https://react.dev/reference/dev-tools/react-performance-tracks) in supported development/profiling builds to investigate; account for profiling overhead and verify improvements on the relevant user path.

## Apply external skills with publisher and API boundaries

Vercel maintains [React best practices](https://github.com/vercel-labs/agent-skills/blob/063bee94c3f4df8453406c830b0a7df0f2860278/skills/react-best-practices/SKILL.md) and [composition patterns](https://github.com/vercel-labs/agent-skills/blob/063bee94c3f4df8453406c830b0a7df0f2860278/skills/composition-patterns/SKILL.md); these are not React-team app-development skills. Evaluate their relevant rules against actual APIs and measured needs. Preserve supported [useContext](https://react.dev/reference/react/useContext), existing [forwardRef](https://react.dev/reference/react/forwardRef), and render props that supply data. Do not import Next.js cache/dynamic APIs, blanket barrel bans, hydration-warning suppression, or cross-request caches of private data into generic React guidance.
