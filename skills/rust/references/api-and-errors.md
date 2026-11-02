# APIs and errors

Public contracts include ownership, lifetimes, trait implementations, errors, feature gates and documented panic behavior, not just function names. Preserve supported callers and the project's compatibility policy.

- Choose borrowed parameters when the operation only needs access and owned parameters when it retains or consumes data. Avoid forcing caller allocation or cloning without a reason. Use generics to express a real relationship or supported flexibility, not anticipated extension points.
- Use standard conversion and iterator traits when they express the intended contract. Implement common traits only when their semantics make sense; deriving `Copy`, `Default`, equality or ordering is an API choice.
- Keep error types meaningful for callers who need to distinguish cases. Preserve the source chain and context at boundaries where it helps diagnosis. Follow existing application/library error conventions; no particular error crate is required by this skill.
- Validate external values at their owning boundary; represent established invariants with types or constructors where useful. Do not repeat validation inside trusted code. Document actual errors, panics and safety preconditions when they are part of the contract.
- Keep visibility deliberate. Consider downstream use before changing trait bounds, enum variants, auto-traits, exported types or feature behavior. Prefer behavioral and compile-time consumer regression cases when those are the changed contract.
- Rustdoc examples should show supported use and be tested when relevant. A prose guideline or clean compiler result does not establish source or semantic compatibility.

Source: [Rust API Guidelines](https://rust-lang.github.io/api-guidelines/checklist.html). Apply its library guidance selectively; it does not mandate every checklist item for private application code.
