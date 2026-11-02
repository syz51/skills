# Cargo and compatibility

Read the actual crate and workspace contract before selecting code or checks: `Cargo.toml`, workspace inheritance, `rust-toolchain.toml`/`rust-toolchain`, `.cargo/config.toml`, lockfile, lint settings and CI. Edition and minimum supported Rust version (MSRV) are distinct. A current local compiler cannot establish MSRV compatibility.

- Preserve supported consumers and targets. Check an API's stabilization version when the declared MSRV matters. Test the stated MSRV when feasible; otherwise disclose that it was not exercised. Do not silently change `rust-version`, edition, dependency resolution or lockfile policy.
- Features affect dependency resolution and conditional compilation. Inspect default and optional features and workspace resolver behavior. Select supported combinations; `--all-features` does not exercise disabled-feature paths and may request combinations the project does not support. `--no-default-features` also needs an actual supported configuration.
- Follow package scripts, task runners and CI first. Otherwise choose among `cargo check -p <crate>`, scoped tests, `cargo fmt --all -- --check`, Clippy and documentation tests according to the change. Add `--locked`, feature or target flags when required by the project. Do not mechanically run an entire matrix for a small change.
- `cargo check` skips code generation; it is not a substitute for behavior tests or a deployable build. Unit/integration tests and doctests cover different surfaces. Cross-compilation is not execution on that platform.
- Respect existing warnings policy. Clippy's restriction group contains conflicting lints; select lints deliberately rather than enabling the entire group. Lint flags and configuration can vary by toolchain.
- Dependency additions, lockfile updates and build scripts can change runtime, target or MSRV compatibility. Keep them scoped, reuse project choices, and distinguish unavailable native prerequisites from product failures.

Sources: [Cargo Rust version](https://doc.rust-lang.org/cargo/reference/rust-version.html), [Cargo features](https://doc.rust-lang.org/cargo/reference/features.html), [Clippy usage](https://doc.rust-lang.org/clippy/usage.html). Consult documentation matching the project version when details are uncertain.
