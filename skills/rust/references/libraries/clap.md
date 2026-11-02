# Clap

Scope: Clap 4.x; checked 2026-11-02 against package-maintained Clap 4.6.7 derive tutorial. Match the project's resolved version, enabled features and existing derive/builder style.

- Define the command-line contract first: option versus positional argument, flag action, cardinality, repeat behavior, defaults and relationships. Derive field types supply defaults for this behavior; inspect those defaults before adding attributes or changing a field type.
- A boolean presence flag and an option accepting a boolean value have different token consumption. Select the intended `ArgAction` and arity; test the exact argv sequence. Also test variadic options followed by positional values so a prompt or filename is not accidentally consumed as another option value.
- Use an appropriate value parser or `ValueEnum` for the boundary's constraints. Keep domain validation with the layer that owns it, rather than repeating established parsing invariants throughout the application.
- Defaults can change whether an option is required and whether omission is distinguishable from an explicit value. Preserve compatibility, environment-variable feature requirements and the precedence the application promises.
- Use `try_parse_from`/the non-exiting parsing API for parser-level tests and recoverable library callers; reserve process-exiting parsing for the actual CLI boundary. Follow existing error/help/version exit behavior.
- Run command-definition assertions when the project uses them, plus focused argv tests for accepted values, invalid input, conflicts/requires, defaults and help. Successful argument parsing does not prove the command's side effects are correct.

Source: [Clap derive tutorial](https://docs.rs/clap/4.6.7/clap/_derive/_tutorial/index.html). [Parser](https://docs.rs/clap/4.6.7/clap/trait.Parser.html), [ArgAction](https://docs.rs/clap/4.6.7/clap/enum.ArgAction.html). The equivalent latest pages were checked and identified version 4.6.7. Prefer the project's resolved version when following API examples.
