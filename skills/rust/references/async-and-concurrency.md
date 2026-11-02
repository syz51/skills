# Async and concurrency

Identify the existing runtime, task ownership and shutdown contract before changing asynchronous work. Async is not automatically faster and does not require adopting a new runtime.

- Keep blocking I/O and sustained computation from starving executor threads. Use the runtime's established blocking/worker boundary when needed; verify its cancellation and shutdown behavior rather than assuming abort stops blocking work.
- Treat each suspension point as a possible cancellation boundary. Check partial reads/writes, resource ownership and externally visible state when a future is dropped or a selection branch loses. Retrying must respect the operation's actual idempotency.
- Inspect guards and borrowed state held across `.await`. Keep synchronous lock scopes short and release guards before suspension when possible. Use async-aware synchronization where the runtime and required critical section warrant it; changing mutex type alone does not establish freedom from deadlocks.
- Preserve task lifecycle: who observes errors, joins tasks, signals shutdown and owns resources? A dropped handle or fire-and-forget spawn may detach work. Avoid silently losing task failures.
- `Send`/`Sync` constraints describe thread-safety contracts. Do not add unsafe implementations, unnecessary `Arc<Mutex<_>>`, or a different runtime merely to satisfy a bound. Understand the executor and ownership requirements first.
- Test the behavior that changed: cancellation, shutdown, contention, ordering or error propagation. Prefer deterministic synchronization over arbitrary sleeps. A passing happy-path async test does not establish race freedom or cancellation safety.

Source: [Asynchronous Programming in Rust](https://rust-lang.github.io/async-book/part-guide/more-async-await.html). Consult the project's installed runtime documentation for runtime-specific guarantees.
