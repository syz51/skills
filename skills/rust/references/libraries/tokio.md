# Tokio

Scope: Tokio 1.x; checked 2026-11-02 against package-maintained Tokio 1.54.1 documentation. Match the affected project's resolved version and features before using an API.

- Inspect the existing runtime/executor and enabled features. Reuse its task and shutdown model rather than introducing a nested runtime or enabling `full` for a small API need.
- A spawned task starts independently; dropping its `JoinHandle` detaches it rather than canceling it. Decide who observes join failures and the task's own `Result`, and who requests cancellation and waits for completion.
- A started `spawn_blocking` operation cannot be stopped by aborting its handle. Use an operation-specific cooperative stop or bounded work where needed; account for blocking work during runtime shutdown. Bound CPU-heavy concurrency rather than assuming the blocking pool is a CPU scheduler.
- Inspect cancellation safety at each awaited operation, especially losing `select!` branches and partially completed I/O. Check the particular API's guarantees; not every receive/read/write operation behaves identically.
- Keep synchronous mutex guards out of suspension points when possible. An async mutex is useful when a guard must span `.await`, but it does not repair lock ordering, unnecessary contention or an unclear state model. `Send` bounds are not a reason for unsafe trait implementations.
- Test task completion/error observation, cancellation and shutdown with deterministic signals or channels rather than arbitrary sleeps. A timeout test does not establish that blocking work was stopped.

Sources: [JoinHandle](https://docs.rs/tokio/1.54.1/tokio/task/struct.JoinHandle.html), [spawn_blocking](https://docs.rs/tokio/1.54.1/tokio/task/fn.spawn_blocking.html). [Mutex](https://docs.rs/tokio/1.54.1/tokio/sync/struct.Mutex.html), [select!](https://docs.rs/tokio/1.54.1/tokio/macro.select.html). The equivalent latest pages were checked and identified version 1.54.1; use the pinned documentation or the project's resolved version for future work.
