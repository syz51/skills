# Unsafe and FFI

Establish the invariant and why safe code cannot meet the boundary before introducing unsafe operations. An unsafe block does not relax Rust's validity, aliasing or lifetime requirements.

- Explain the safety of each unsafe operation at its boundary: validity, alignment, initialization, aliasing, lifetime and ownership as relevant. A `SAFETY` comment must state the actual proof, not merely repeat the operation.
- A safe public wrapper must uphold its invariants for every safe caller. An unsafe function must document the caller's obligations; its implementation must still justify its own unsafe operations under those obligations.
- For FFI, verify ABI, representation, ownership transfer, allocator/deallocator pairing, nullability, buffer lengths, callbacks, thread affinity and unwinding rules against the native API. Native handles and pointers need explicit lifetime and cleanup ownership.
- Keep unsafe code and unsafe trait implementations small and auditable. Do not infer soundness from successful compilation, benchmarks, linting or a single test.
- Use focused runtime tests and available tools such as Miri or sanitizers when they address the actual risk and the environment supports them. They are supplementary evidence, not proof of soundness; report unsupported FFI/target paths. Do not change toolchains or install tooling without task authorization.
- The Rustonomicon is useful background but acknowledges incomplete/outdated sections. Prefer the Rust Reference when those sources disagree and verify unstable or unsettled semantics rather than asserting guarantees.

Sources: [Rust Reference: undefined behavior](https://doc.rust-lang.org/reference/behavior-considered-undefined.html), [Rustonomicon introduction](https://doc.rust-lang.org/nomicon/).
