Shared frontend/UI policy: [frontend-ui](../../../SKILL.md) and [framework boundaries](../../frameworks.md) govern design, scope, structure, tooling preferences, and verification. This is a versioned technical reference; load only relevant topics.

# Vue implementation foundations

Prefer Composition API with `<script setup>` for new Vue projects; preserve established project conventions. Keep source state minimal, computed getters pure, async effects cleaned up, and props/events contracts explicit. Choose component and composable boundaries by responsibility, reuse, or complexity, with no component-count quota. Keep state, DOM behavior, and cleanup with their owning UI or workflow; expose focused state and actions while preserving necessary coordination. Review incidental forwarding before introducing shared context or merging useful boundaries.

## Foundations

Read the relevant foundation for the change, reusing material already in context:

- [Reactivity](references/reactivity.md)
- [SFC structure and template safety](references/sfc.md)
- [Component data flow](references/component-data-flow.md)
- [Composables](references/composables.md)

## Conditional topics

### Optional features

### 3.1 Standard optional features

Do not add these by default. Load the matching reference only when the requirement exists.

- Slots: parent needs to control child content/layout -> [component-slots](references/component-slots.md)
- Fallthrough attributes: wrapper/base components must forward attrs/events safely -> [component-fallthrough-attrs](references/component-fallthrough-attrs.md)
- Built-in component `<KeepAlive>` for stateful view caching -> [component-keep-alive](references/component-keep-alive.md)
- Built-in component `<Teleport>` for overlays/portals -> [component-teleport](references/component-teleport.md)
- Built-in component `<Suspense>` for async subtree fallback boundaries -> [component-suspense](references/component-suspense.md)
- Animation-related features: pick the simplest approach that matches the required motion behavior.
  - Built-in component `<Transition>` for enter/leave effects -> [transition](references/component-transition.md)
  - Built-in component `<TransitionGroup>` for animated list mutations -> [transition-group](references/component-transition-group.md)
  - Class-based animation for non-enter/leave effects -> [animation-class-based-technique](references/animation-class-based-technique.md)
  - State-driven animation for user-input-driven animation -> [animation-state-driven-technique](references/animation-state-driven-technique.md)

### 3.2 Less-common optional features

Use these only when there is explicit product or technical need.

- Directives: behavior is DOM-specific and not a good composable/component fit -> [directives](references/directives.md)
- Async components: heavy/rarely-used UI should be lazy loaded -> [component-async](references/component-async.md)
- Render functions only when templates cannot express the requirement -> [render-functions](references/render-functions.md)
- Plugins when behavior must be installed app-wide -> [plugins](references/plugins.md)
- State management patterns: app-wide shared state crosses feature boundaries -> [state-management](references/state-management.md)

### Performance

Performance work is a post-functionality pass. Do not optimize before core behavior is implemented and verified.

- Large list rendering bottlenecks -> [perf-virtualize-large-lists](references/perf-virtualize-large-lists.md)
- Static subtrees re-rendering unnecessarily -> [perf-v-once-v-memo-directives](references/perf-v-once-v-memo-directives.md)
- Over-abstraction in hot list paths -> [perf-avoid-component-abstraction-in-lists](references/perf-avoid-component-abstraction-in-lists.md)
- Expensive updates triggered too often -> [updated-hook-performance](references/updated-hook-performance.md)


## Verification

Verify the requested behavior and affected contracts with the smallest meaningful checks. When boundaries change, check state ownership, instance lifetimes, cleanup, and pending or cross-feature operations; a smaller API or file alone does not establish success. Preserve a working existing structure otherwise.
