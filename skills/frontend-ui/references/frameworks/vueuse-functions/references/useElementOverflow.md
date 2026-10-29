---
category: Elements
---

# useElementOverflow

Reactive element's overflow state.

## Usage

Keep the measured content independent of its overflow state. Render any overflow indicator outside the measured element so it cannot change the measurement and cause repeated state changes.

```vue
<script setup lang="ts">
import { useElementOverflow } from '@vueuse/core'
import { useTemplateRef } from 'vue'

const el = useTemplateRef('el')
const { isXOverflowed } = useElementOverflow(el)
</script>

<template>
  <div ref="el" style="width: 100px; overflow: hidden; white-space: nowrap;">
    Some words may be too long to show here.
  </div>
  <p v-if="isXOverflowed">
    The text overflows horizontally.
  </p>
</template>
```

## Type Declarations

```ts
export interface UseElementOverflowOptions extends ConfigurableWindow {
  /**
   * Use MutationObserver to observe the target and its children.
   *
   * @default false
   */
  observeMutation?: boolean | MutationObserverInit
  /**
   * Callback when observer triggered.
   */
  onUpdated?: ResizeObserverCallback | MutationCallback
}
/**
 * react a dom's overflow state
 * @see https://vueuse.org/useElementOverflow
 * @param target
 * @param option
 */
export declare function useElementOverflow(
  target: MaybeComputedElementRef,
  option?: UseElementOverflowOptions,
): {
  isXOverflowed: Readonly<ShallowRef<boolean, boolean>>
  isYOverflowed: Readonly<ShallowRef<boolean, boolean>>
  stop: typeof stop
  update: () => void
}
export type UseElementOverflowReturn = ReturnType<typeof useElementOverflow>
```
