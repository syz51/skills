# Axum

Scope: Axum 0.8; checked 2026-11-02 against package-maintained Axum 0.8.9 documentation. Do not apply older major-version route or handler examples without checking compatibility.

- Follow the existing router/state composition. `Router<S>` describes missing state of type `S`; supplying state with `with_state` changes that contract. Prefer typed `State` for application state; request extensions are a separate mechanism whose missing values can fail at runtime.
- Axum 0.8 route captures use braces, as in `/items/{id}`. Check nesting, fallback, method handling and path parameters in the affected router instead of replacing route syntax from an older example.
- Extractors run in argument order. Body-consuming extractors such as `Json` must be last; the request body can only be consumed once. Parts-only extraction, body parsing and domain validation are distinct boundaries.
- A handler's `Result<_, E>` with an `IntoResponse` error describes an HTTP response, not a Tower service failure. Handle middleware/service errors at their actual boundary so they become the intended response. Preserve rejection status, response shape and information-disclosure policy.
- Middleware ordering and whether it applies before or after route matching affect behavior. Use the existing Tower/Axum integration and enabled features; load Tokio guidance when runtime/task behavior changes, or Serde guidance when the wire contract changes.
- Test through the router with representative requests: valid input, extraction failure, expected error response, state and route/middleware behavior. Compiling a handler alone does not verify routing or HTTP semantics.

Sources: [Axum 0.8.9](https://docs.rs/axum/0.8.9/axum/index.html), [extractors](https://docs.rs/axum/0.8.9/axum/extract/index.html), [error handling](https://docs.rs/axum/0.8.9/axum/error_handling/index.html). The crate overview and equivalent latest extractor documentation were checked; verify the affected API against the project's resolved version.
