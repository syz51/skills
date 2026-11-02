# Serde and serde_json

Scope: Serde 1.x and serde_json 1.x; checked 2026-11-02 against Serde's maintained attribute/lifetime documentation and package documentation. Resolve each project's actual patch versions/features; a `serde_json` dependency may use a local alias.

- Use a typed wire representation when the external shape is known; use `Value` for genuinely dynamic data or an existing boundary that needs it. Deserialization establishes the represented shape, not domain invariants absent from that representation. Validate those once at the owning boundary.
- Treat field names, defaults, aliases, enum tagging and unknown-field behavior as compatibility choices. A field rename or a new required field can break saved data or consumers. Test old accepted input and intended output, not only a round trip produced by the new serializer.
- `deny_unknown_fields` is a deliberate strictness decision, not an automatic hardening step; it can reject future fields. Serde does not support combining it with `flatten`. Preserve the existing compatibility policy.
- Borrowing deserialization ties returned data to the input lifetime. Use `Deserialize<'de>` when caller-owned input can outlive the result, and `DeserializeOwned` when parsing must produce data independent of temporary input. Do not add `'static` or unnecessary copies to hide a lifetime mismatch.
- Preserve parsing context and useful errors. Avoid silently replacing invalid input with defaults, panicking on external JSON, or serializing internal error details into public responses without the owning contract.
- Feature choices can affect output and consumer behavior; for example, inspect whether JSON map order is a relied-upon contract before changing `preserve_order`. Test representative malformed input, absent/default fields and wire compatibility where those change.

Sources: [Serde attributes](https://serde.rs/attributes.html), [container attributes](https://serde.rs/container-attrs.html), [deserializer lifetimes](https://serde.rs/lifetimes.html), [serde_json](https://docs.rs/serde_json/1.0.154/serde_json/). No documentation snapshot is bundled.
