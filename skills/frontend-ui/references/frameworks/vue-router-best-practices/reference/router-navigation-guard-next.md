---
title: Vue Router Navigation Guard next() Control Flow
impact: HIGH
impactDescription: Missing or duplicate next() calls can leave navigation unresolved or produce errors
type: gotcha
tags: [vue3, vue-router, navigation-guards, migration, async]
---

# Vue Router Navigation Guard next() Control Flow

**Impact: HIGH** - Vue Router still supports the optional third `next` argument. Missing or duplicate calls can break navigation. Prefer return-based control flow for new ordinary guards, but preserve working callback-style guards and the `beforeRouteEnter` instance callback. Check the installed version's contract before changing a guard.

## Task Checklist

- [ ] Choose return-based control flow for new ordinary guards where appropriate
- [ ] Preserve supported next() behavior, including beforeRouteEnter instance callbacks
- [ ] Use async/await pattern for asynchronous checks
- [ ] Return false to cancel, return route to redirect, return nothing to proceed

## The Problem

```javascript
// WRONG: The authenticated path never resolves navigation
router.beforeEach((to, from, next) => {
  if (!isAuthenticated) {
    next('/login')  // Easy to forget this call
  }
  // BUG: next() not called when authenticated - navigation hangs!
})

// WRONG: Multiple next() calls
router.beforeEach((to, from, next) => {
  if (!isAuthenticated) {
    next('/login')
  }
  next()  // BUG: Called twice when not authenticated!
})

// WRONG: next() in async code without proper handling
router.beforeEach(async (to, from, next) => {
  const user = await fetchUser()
  if (!user) {
    next('/login')
  }
  next()  // Still gets called even after redirect!
})
```

## Solution: Use Return-Based Guards

```javascript
// CORRECT: Return-based syntax (modern Vue Router 4+)
router.beforeEach((to, from) => {
  if (!isAuthenticated) {
    return '/login'  // Redirect
  }
  // Return nothing (undefined) to proceed
})

// CORRECT: Return false to cancel navigation
router.beforeEach((to, from) => {
  if (hasUnsavedChanges) {
    return false  // Cancel navigation
  }
})

// CORRECT: Async with return-based syntax
router.beforeEach(async (to, from) => {
  const user = await fetchUser()
  if (!user) {
    return { name: 'Login', query: { redirect: to.fullPath } }
  }
  // Proceed with navigation
})
```

## Return Values Explained

```javascript
router.beforeEach((to, from) => {
  // Return nothing/undefined - allow navigation
  return

  // Return false - cancel navigation, stay on current route
  return false

  // Return string path - redirect to path
  return '/login'

  // Return route object - redirect with full control
  return { name: 'Login', query: { redirect: to.fullPath } }

  // Return Error - cancel and trigger router.onError()
  return new Error('Navigation cancelled')
})
```

## Supported Callback-Style Guards

When a guard accepts `next`, resolve each execution path once:

```javascript
// CORRECT: Exactly one next() call per code path
router.beforeEach((to, from, next) => {
  if (!isAuthenticated) {
    next('/login')
    return  // CRITICAL: Exit after calling next()
  }

  if (!hasPermission(to)) {
    next('/forbidden')
    return  // CRITICAL: Exit after calling next()
  }

  next()  // Only reached if all checks pass
})
```

For an Options API `beforeRouteEnter` guard that needs the entering component, keep its callback:

```javascript
beforeRouteEnter(to, from, next) {
  next(instance => {
    instance.refreshForRoute(to)
  })
}
```

Other guards do not support this instance callback. Removing it as a blanket migration can discard required behavior.

## Error Handling Pattern

```javascript
router.beforeEach(async (to, from) => {
  try {
    await validateAccess(to)
    // Proceed
  } catch (error) {
    if (error.status === 401) {
      return '/login'
    }
    if (error.status === 403) {
      return '/forbidden'
    }
    // Log error and proceed anyway (or return false)
    console.error('Access validation failed:', error)
    return false
  }
})
```

## Key Points

1. **Prefer return-based syntax** - Cleaner, less error-prone, modern standard
2. **Resolve callback-style guards once** - Avoid missing or overlapping next() calls
3. **Always return/exit after redirect** - Prevent multiple navigation actions
4. **Async guards work naturally** - Just return the redirect route or nothing
5. **Test relevant paths** - Proceed, redirect, cancel, async failure, and any component-instance callback

## Reference
- [Vue Router Navigation Guards](https://router.vuejs.org/guide/advanced/navigation-guards.html)
- [Supported optional next argument](https://router.vuejs.org/guide/advanced/navigation-guards.html#optional-third-argument-next)
- [beforeRouteEnter instance callbacks](https://router.vuejs.org/guide/advanced/navigation-guards.html#using-the-options-api)
