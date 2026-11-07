use codex_skills::message::{Effort, GenerationError, Options, generate_message};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};
use tempfile::TempDir;

const DIFF: &str = "diff --git a/src/limit.rs b/src/limit.rs\n--- a/src/limit.rs\n+++ b/src/limit.rs\n+reject oversized public diffs before generation\n";

/// Every flag the restricted call uses, as `claude --help` documents them.
const HELP: &str = "--print --setting-sources --tools --strict-mcp-config --disable-slash-commands --no-session-persistence --model --effort --output-format --verbose --json-schema";

/// A fake `claude` whose `--help` prints `help` (counting calls) and otherwise runs `body`.
fn fake_with_help(root: &TempDir, help: &str, body: &str) -> PathBuf {
    let path = root.path().join("claude");
    let helped = root.path().join("helped");
    fs::write(
        &path,
        format!(
            "#!/bin/sh\nif [ \"$1\" = --help ]; then echo x >> '{}'; printf '%s\\n' '{help}'; exit 0; fi\n{body}\n",
            helped.display()
        ),
    )
    .unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
    path
}
fn fake(root: &TempDir, body: &str) -> PathBuf {
    fake_with_help(root, HELP, body)
}
fn printing(root: &TempDir, output: &str, code: i32) -> PathBuf {
    let rec = root.path().join("rec");
    fs::create_dir_all(&rec).unwrap();
    let out = root.path().join("output");
    fs::write(&out, output).unwrap();
    let r = rec.display();
    fake(
        root,
        &format!(
            "pwd > '{r}/pwd'\nls -A > '{r}/ls'\nenv > '{r}/env'\nprintf '%s\\n' \"$@\" > '{r}/args'\ncat > '{r}/stdin'\ncat '{}'\nexit {code}",
            out.display()
        ),
    )
}
fn options(path: PathBuf, timeout: Duration) -> Options {
    Options {
        claude_path: path,
        model: "claude-haiku-5-5".into(),
        effort: Effort::Low,
        timeout,
        cancellation: None,
        api_key_file: None,
        api_fallback_file: None,
    }
}
fn init(tools: Value, mcp_servers: Value) -> String {
    json!({"type": "system", "subtype": "init", "tools": tools, "mcp_servers": mcp_servers})
        .to_string()
}
fn assistant(tool: &str) -> String {
    json!({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": tool, "input": {}}]}})
    .to_string()
}
fn result_event(structured: Value) -> String {
    json!({"type": "result", "subtype": "success", "is_error": false, "result": "",
           "structured_output": structured, "total_cost_usd": 0.001})
    .to_string()
}
/// A successful stream-json run: init exposing only StructuredOutput, its call, then the result.
fn result(structured: Value) -> String {
    [
        init(json!(["StructuredOutput"]), json!([])),
        assistant("StructuredOutput"),
        result_event(structured),
    ]
    .join("\n")
}
fn subject() -> Value {
    json!({"subject": "Reject oversized public diffs early", "body": ""})
}
fn generate(output: &str, code: i32) -> Result<codex_skills::CommitMessage, GenerationError> {
    let root = TempDir::new().unwrap();
    generate_message(
        DIFF,
        None,
        &options(printing(&root, output, code), Duration::from_secs(20)),
    )
}

#[test]
fn success_uses_isolated_invocation_and_returns_validated_message() {
    let root = TempDir::new().unwrap();
    let output = result(
        json!({"subject": "Reject oversized public diffs early", "body": "Bound input before starting the CLI."}),
    );
    let message = generate_message(
        DIFF,
        None,
        &options(printing(&root, &output, 0), Duration::from_secs(20)),
    )
    .unwrap();
    assert_eq!(message.subject, "Reject oversized public diffs early");
    assert_eq!(message.body, "Bound input before starting the CLI.");

    let rec = root.path().join("rec");
    let args: Vec<String> = fs::read_to_string(rec.join("args"))
        .unwrap()
        .lines()
        .map(str::to_owned)
        .collect();
    let schema_at = args.iter().position(|a| a == "--json-schema").unwrap();
    assert_eq!(
        &args[..schema_at],
        [
            "--print",
            "--setting-sources",
            "",
            "--tools",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--model",
            "claude-haiku-5-5",
            "--effort",
            "low",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
    );
    let schema: Value = serde_json::from_str(&args[schema_at + 1]).unwrap();
    assert_eq!(schema["additionalProperties"], false);
    assert_eq!(schema["required"], json!(["subject", "body"]));

    // Inherited variables pass through; parent-session ones never do.
    let env = fs::read_to_string(rec.join("env")).unwrap();
    for line in env.lines() {
        let key = line.split('=').next().unwrap();
        assert!(
            !["CLAUDECODE", "CLAUDE_EFFORT", "CLAUDE_CODE_EFFORT_LEVEL"].contains(&key)
                && !key.starts_with("CLAUDE_CODE_SESSION_")
                && !key.starts_with("CLAUDE_AGENT_SDK_"),
            "leaked {key}"
        );
    }
    for line in [
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
    ] {
        assert!(env.lines().any(|l| l == line), "{line}");
    }
    let manifest = std::env::var("CARGO_MANIFEST_DIR").unwrap();
    assert!(
        env.lines()
            .any(|l| l == format!("CARGO_MANIFEST_DIR={manifest}"))
    );

    // A fresh, empty working directory that is removed afterwards.
    let cwd = fs::read_to_string(rec.join("pwd")).unwrap();
    assert!(fs::read_to_string(rec.join("ls")).unwrap().is_empty());
    assert!(!Path::new(cwd.trim()).exists());

    let stdin = fs::read_to_string(rec.join("stdin")).unwrap();
    assert!(stdin.starts_with("Generate an informative Git commit message"));
    let payload: Value = serde_json::from_str(stdin.lines().last().unwrap()).unwrap();
    assert_eq!(payload, json!({"public_diff": DIFF}));
    assert!(!stdin.contains("was rejected"));
}

#[test]
fn retry_instruction_joins_the_trusted_prompt_before_the_diff() {
    let root = TempDir::new().unwrap();
    let output = result(subject());
    let instruction = "Do not include local paths, host names or credentials.";
    generate_message(
        DIFF,
        Some(instruction),
        &options(printing(&root, &output, 0), Duration::from_secs(20)),
    )
    .unwrap();
    let stdin = fs::read_to_string(root.path().join("rec/stdin")).unwrap();
    let (prompt, data) = stdin.trim_end().rsplit_once('\n').unwrap();
    assert!(
        prompt.ends_with(&format!("Body may be empty. {instruction}")),
        "{prompt}"
    );
    let payload: Value = serde_json::from_str(data).unwrap();
    assert_eq!(payload, json!({"public_diff": DIFF}));

    // --help is checked once per executable per process.
    let opts = options(root.path().join("claude"), Duration::from_secs(20));
    generate_message(DIFF, None, &opts).unwrap();
    let helped = fs::read_to_string(root.path().join("helped")).unwrap();
    assert_eq!(helped.lines().count(), 1);
}

#[test]
fn missing_help_flag_fails_before_generation() {
    let root = TempDir::new().unwrap();
    let ran = root.path().join("ran");
    let help = HELP.replace("--verbose ", "");
    let path = fake_with_help(&root, &help, &format!("touch '{}'", ran.display()));
    let e = generate_message(DIFF, None, &options(path, Duration::from_secs(20))).unwrap_err();
    assert!(e.to_string().contains("--verbose"), "{e}");
    assert!(!ran.exists());

    let root = TempDir::new().unwrap();
    let failing = root.path().join("claude");
    fs::write(
        &failing,
        format!("#!/bin/sh\nprintf '%s\\n' '{HELP}'\nexit 1\n"),
    )
    .unwrap();
    fs::set_permissions(&failing, fs::Permissions::from_mode(0o755)).unwrap();
    assert!(matches!(
        generate_message(DIFF, None, &options(failing, Duration::from_secs(20))),
        Err(GenerationError::Backend(_))
    ));
}

#[test]
fn exposed_or_used_tools_fail() {
    let ok = assistant("StructuredOutput");
    let done = result_event(subject());
    for stream in [
        [
            init(json!(["StructuredOutput", "Bash"]), json!([])),
            ok.clone(),
            done.clone(),
        ],
        [
            init(
                json!(["StructuredOutput"]),
                json!([{"name": "github", "status": "connected"}]),
            ),
            ok.clone(),
            done.clone(),
        ],
        [
            init(json!(["StructuredOutput"]), json!([])),
            assistant("Bash"),
            done.clone(),
        ],
    ] {
        let output = stream.join("\n");
        assert!(
            matches!(generate(&output, 0), Err(GenerationError::Backend(_))),
            "{output}"
        );
    }
}

#[test]
fn stream_tolerates_trailing_events_and_requires_init() {
    let trailing = format!(
        "{}\n{}\n",
        result(subject()),
        json!({"type": "system", "subtype": "hook_response"})
    );
    assert_eq!(
        generate(&trailing, 0).unwrap().subject,
        "Reject oversized public diffs early"
    );
    assert!(matches!(
        generate(&result_event(subject()), 0),
        Err(GenerationError::Backend(_))
    ));
}

#[test]
fn error_results_fail() {
    let error = json!({"type": "result", "subtype": "error_during_execution", "is_error": true,
                       "result": "overloaded", "api_error_status": 529})
    .to_string();
    let e = generate(&error, 1).unwrap_err();
    assert!(matches!(e, GenerationError::Backend(_)), "{e}");
    assert!(e.to_string().contains("529"));

    let auth = json!({"type": "result", "subtype": "success", "is_error": true,
                      "result": "Not logged in · Please run /login", "api_error_status": null})
    .to_string();
    assert!(matches!(
        generate(&auth, 1),
        Err(GenerationError::Authentication)
    ));

    // A success-shaped result still fails when the CLI exits unsuccessfully.
    let ok = result(subject());
    assert!(matches!(generate(&ok, 1), Err(GenerationError::Backend(_))));
}

#[test]
fn malformed_or_schema_violating_output_fails() {
    for output in [
        "not json",
        "",
        "{\"type\":\"assistant\",\"is_error\":false}",
    ] {
        assert!(
            matches!(generate(output, 0), Err(GenerationError::Backend(_))),
            "{output}"
        );
    }
    for structured in [
        Value::Null,
        json!({"subject": "Reject oversized public diffs early"}),
        json!({"subject": "Reject oversized public diffs early", "body": "", "extra": 1}),
        json!({"subject": "Too short", "body": ""}),
        json!({"subject": "Reject oversized public diffs early", "body": "x".repeat(12_001)}),
        // Schema-valid but generic, ungrounded, or claiming checks were run.
        json!({"subject": "Update the project files", "body": ""}),
        json!({"subject": "Describe unrelated widget work", "body": ""}),
        json!({"subject": "Reject oversized public diffs early", "body": "Tests passed."}),
    ] {
        assert!(
            matches!(
                generate(&result(structured.clone()), 0),
                Err(GenerationError::InvalidMessage)
            ),
            "{structured}"
        );
    }
}

#[test]
fn oversized_output_fails() {
    let root = TempDir::new().unwrap();
    let path = fake(&root, "cat > /dev/null\nhead -c 3000000 /dev/zero");
    assert!(matches!(
        generate_message(DIFF, None, &options(path, Duration::from_secs(20))),
        Err(GenerationError::OutputTooLarge)
    ));
}

#[test]
fn timeout_kills_the_cli() {
    let root = TempDir::new().unwrap();
    let path = fake(&root, "exec sleep 30");
    let start = Instant::now();
    assert!(matches!(
        generate_message(DIFF, None, &options(path, Duration::from_secs(1))),
        Err(GenerationError::Timeout)
    ));
    assert!(start.elapsed() < Duration::from_secs(10));
}

#[test]
fn configuration_is_validated() {
    let root = TempDir::new().unwrap();
    let claude = fake(&root, "exit 0");
    let valid = json!({"claude_path": claude, "model": "claude-haiku-5-5", "effort": "low"});
    let parsed = Options::from_value(&valid).unwrap();
    assert_eq!(parsed.claude_path, claude);
    assert_eq!(parsed.effort, Effort::Low);
    assert_eq!(parsed.timeout, Duration::from_secs(120));
    for effort in ["medium", "high", "xhigh", "max"] {
        let mut value = valid.clone();
        value["effort"] = json!(effort);
        assert!(Options::from_value(&value).is_ok(), "{effort}");
    }
    let mut timed = valid.clone();
    timed["timeout_seconds"] = json!(240);
    assert_eq!(
        Options::from_value(&timed).unwrap().timeout,
        Duration::from_secs(240)
    );

    let invalid = [
        json!({"claude_path": "bin/claude", "model": "claude-haiku-5-5", "effort": "low"}),
        json!({"claude_path": root.path().join("missing"), "model": "claude-haiku-5-5", "effort": "low"}),
        json!({"claude_path": root.path(), "model": "claude-haiku-5-5", "effort": "low"}),
        json!({"claude_path": claude, "model": "claude-haiku-5-5", "effort": "extreme"}),
        json!({"claude_path": claude, "model": "claude-haiku-5-5"}),
        json!({"claude_path": claude, "model": "", "effort": "low"}),
        json!({"claude_path": claude, "model": "haiku; rm", "effort": "low"}),
        json!({"claude_path": claude, "model": "claude-haiku-5-5", "effort": "low", "timeout_seconds": 0}),
        json!({"claude_path": claude, "model": "claude-haiku-5-5", "effort": "low", "codex_home": "~/.codex"}),
        json!({"codex_path": claude, "model": "claude-haiku-5-5", "effort": "low"}),
    ];
    for value in invalid {
        assert!(Options::from_value(&value).is_err(), "{value}");
    }
}

/// Transcript folders Claude Code keeps under `projects`, named after each session's working directory.
fn claude_projects() -> BTreeSet<String> {
    let config = std::env::var_os("CLAUDE_CONFIG_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(std::env::var_os("HOME").unwrap()).join(".claude"));
    fs::read_dir(config.join("projects"))
        .map(|entries| {
            entries
                .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
                .collect()
        })
        .unwrap_or_default()
}
fn sanitized(path: &Path) -> String {
    path.to_string_lossy()
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect()
}

fn is_executable(path: &Path) -> bool {
    fs::metadata(path).is_ok_and(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
}
/// The production default `<home>/.local/bin/claude`, then the first `claude` on `path`, as tests/test_claude_live.py resolves it.
fn resolve_cli(home: &Path, path: Option<&std::ffi::OsStr>) -> Option<PathBuf> {
    let default = home.join(".local/bin/claude");
    if is_executable(&default) {
        return Some(default);
    }
    std::env::split_paths(path?)
        .map(|dir| dir.join("claude"))
        .find(|candidate| is_executable(candidate))
}

#[test]
fn live_cli_resolution_prefers_the_default_location_then_path() {
    let root = TempDir::new().unwrap();
    let home = root.path().join("home");
    let bin = root.path().join("bin");
    let search = std::env::join_paths([root.path().join("missing"), bin.clone()]).unwrap();
    assert_eq!(resolve_cli(&home, Some(&search)), None);
    assert_eq!(resolve_cli(&home, None), None);
    let install = |path: &Path| {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, "#!/bin/sh\n").unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
    };
    install(&bin.join("claude"));
    assert_eq!(resolve_cli(&home, Some(&search)), Some(bin.join("claude")));
    install(&home.join(".local/bin/claude"));
    assert_eq!(
        resolve_cli(&home, Some(&search)),
        Some(home.join(".local/bin/claude"))
    );
}

/// Opt-in: calls the real Claude Code CLI with the caller's login (one small claude-haiku-5-5 call).
/// With RUN_CLAUDE_LIVE=1 a missing CLI fails rather than passing.
#[test]
fn live_generator_returns_a_validated_message() {
    if std::env::var("RUN_CLAUDE_LIVE").as_deref() != Ok("1") {
        eprintln!("skipped: set RUN_CLAUDE_LIVE=1 to call the real Claude Code CLI");
        return;
    }
    let home = PathBuf::from(std::env::var_os("HOME").expect("HOME is set"));
    let cli = resolve_cli(&home, std::env::var_os("PATH").as_deref()).unwrap_or_else(|| {
        panic!(
            "RUN_CLAUDE_LIVE=1 but no Claude Code CLI at {} or on PATH",
            home.join(".local/bin/claude").display()
        )
    });
    let before = claude_projects();
    let message = generate_message(DIFF, None, &options(cli, Duration::from_secs(180))).unwrap();
    assert!(!message.subject.is_empty());
    codex_skills::message::validate(&message).unwrap();

    // The generator runs in a fresh directory under the temporary root; no session there may persist.
    let temp = std::env::temp_dir();
    let roots = [sanitized(&temp), sanitized(&temp.canonicalize().unwrap())];
    let created: Vec<String> = claude_projects()
        .difference(&before)
        .filter(|name| roots.iter().any(|root| name.starts_with(root.as_str())))
        .cloned()
        .collect();
    assert!(created.is_empty(), "{created:?}");
}

/// A fake `claude` that answers by credential: with `ANTHROPIC_API_KEY` set it prints the file
/// `api` (a billing error unless a test rewrites it), otherwise a valid message. Each call appends
/// the key it saw, or `login`, to `calls`.
fn keyed(root: &TempDir) -> PathBuf {
    let r = root.path().display();
    fs::write(root.path().join("api"), billing_error()).unwrap();
    fs::write(root.path().join("login"), result(subject())).unwrap();
    fake(
        root,
        &format!(
            "cat > /dev/null\nif [ -n \"$ANTHROPIC_API_KEY\" ]; then echo \"$ANTHROPIC_API_KEY\" >> '{r}/calls'; cat '{r}/api'; exit 1; fi\necho login >> '{r}/calls'; cat '{r}/login'"
        ),
    )
}
fn billing_error() -> String {
    json!({"type": "result", "subtype": "success", "is_error": true,
           "result": "Credit balance is too low", "api_error_status": 400})
    .to_string()
}
fn keyed_options(root: &TempDir) -> Options {
    let key = root.path().join("key");
    fs::write(&key, "sk-test-one\n").unwrap();
    fs::set_permissions(&key, fs::Permissions::from_mode(0o600)).unwrap();
    Options {
        api_key_file: Some(key),
        api_fallback_file: Some(root.path().join("api-fallback.json")),
        ..options(keyed(root), Duration::from_secs(20))
    }
}
fn calls(root: &TempDir) -> Vec<String> {
    fs::read_to_string(root.path().join("calls"))
        .unwrap_or_default()
        .lines()
        .map(str::to_owned)
        .collect()
}
fn fallback(root: &TempDir) -> Option<Value> {
    let text = fs::read_to_string(root.path().join("api-fallback.json")).ok()?;
    Some(serde_json::from_str(&text).unwrap())
}

#[test]
fn api_key_bills_the_key_and_success_clears_the_fallback() {
    let root = TempDir::new().unwrap();
    let options = keyed_options(&root);
    fs::write(root.path().join("api"), result(subject())).unwrap();
    // The fake exits 1 for keyed calls; give the keyed path a success exit.
    let r = root.path().display();
    fs::write(
        &options.claude_path,
        format!(
            "#!/bin/sh\nif [ \"$1\" = --help ]; then printf '%s\\n' '{HELP}'; exit 0; fi\ncat > /dev/null\necho \"${{ANTHROPIC_API_KEY:-login}}\" >> '{r}/calls'; cat '{r}/api'\n"
        ),
    )
    .unwrap();
    fs::write(root.path().join("api-fallback.json"), "not json").unwrap();
    let message = generate_message(DIFF, None, &options).unwrap();
    assert_eq!(message.subject, "Reject oversized public diffs early");
    assert_eq!(calls(&root), ["sk-test-one"]);
    assert!(fallback(&root).is_none(), "success clears the record");
}

#[test]
fn billing_falls_back_to_the_login_and_sticks_until_the_key_changes() {
    let root = TempDir::new().unwrap();
    let options = keyed_options(&root);
    let started = chrono::Utc::now();
    let message = generate_message(DIFF, None, &options).unwrap();
    assert_eq!(message.subject, "Reject oversized public diffs early");
    assert_eq!(calls(&root), ["sk-test-one", "login"]);

    let record = fallback(&root).unwrap();
    assert_eq!(record["reason"], "billing");
    let meta = fs::metadata(options.api_key_file.as_ref().unwrap()).unwrap();
    use std::os::unix::fs::MetadataExt;
    let mtime_ns = i128::from(meta.mtime()) * 1_000_000_000 + i128::from(meta.mtime_nsec());
    assert_eq!(
        record["key"],
        format!("{}-{}-{}", meta.ino(), meta.size(), mtime_ns)
    );
    let parse = |field: &str| {
        chrono::DateTime::parse_from_rfc3339(record[field].as_str().unwrap())
            .unwrap()
            .with_timezone(&chrono::Utc)
    };
    let since = parse("since");
    assert!(since >= started - chrono::TimeDelta::seconds(1));
    let retry = parse("retry_at") - since;
    assert!(
        (retry - chrono::TimeDelta::days(1)).num_seconds().abs() <= 2,
        "{retry}"
    );

    // While the record stands the API is not tried at all.
    generate_message(DIFF, None, &options).unwrap();
    assert_eq!(calls(&root), ["sk-test-one", "login", "login"]);

    // A changed key file retries the API at once; a repeat failure keeps the first `since`.
    std::thread::sleep(Duration::from_millis(20));
    fs::write(options.api_key_file.as_ref().unwrap(), "sk-test-two\n").unwrap();
    generate_message(DIFF, None, &options).unwrap();
    assert_eq!(
        calls(&root),
        ["sk-test-one", "login", "login", "sk-test-two", "login"]
    );
    assert_eq!(fallback(&root).unwrap()["since"], record["since"]);
}

#[test]
fn an_expired_or_foreign_record_retries_the_api() {
    let root = TempDir::new().unwrap();
    let options = keyed_options(&root);
    generate_message(DIFF, None, &options).unwrap();
    let mut record = fallback(&root).unwrap();
    let past = (chrono::Utc::now() - chrono::TimeDelta::minutes(1))
        .to_rfc3339_opts(chrono::SecondsFormat::Secs, false);
    record["retry_at"] = json!(past);
    fs::write(root.path().join("api-fallback.json"), record.to_string()).unwrap();
    generate_message(DIFF, None, &options).unwrap();
    assert_eq!(
        calls(&root),
        ["sk-test-one", "login", "sk-test-one", "login"]
    );

    let mut record = fallback(&root).unwrap();
    record["key"] = json!("0-0-0");
    fs::write(root.path().join("api-fallback.json"), record.to_string()).unwrap();
    generate_message(DIFF, None, &options).unwrap();
    assert_eq!(calls(&root).len(), 6);
}

#[test]
fn authentication_falls_back_but_other_api_errors_do_not() {
    let root = TempDir::new().unwrap();
    let options = keyed_options(&root);
    let rejected = json!({"type": "result", "subtype": "success", "is_error": true,
                          "result": "Invalid API key · Fix external API key", "api_error_status": 401})
    .to_string();
    fs::write(root.path().join("api"), rejected).unwrap();
    generate_message(DIFF, None, &options).unwrap();
    assert_eq!(fallback(&root).unwrap()["reason"], "authentication");

    let other = TempDir::new().unwrap();
    let options = keyed_options(&other);
    let overloaded =
        json!({"type": "result", "subtype": "error_during_execution", "is_error": true,
                            "result": "overloaded", "api_error_status": 529})
        .to_string();
    fs::write(other.path().join("api"), overloaded).unwrap();
    let e = generate_message(DIFF, None, &options).unwrap_err();
    assert!(matches!(e, GenerationError::Backend(_)), "{e}");
    assert_eq!(calls(&other), ["sk-test-one"]);
    assert!(fallback(&other).is_none());
}

#[test]
fn api_success_keeps_another_key_files_record() {
    let root = TempDir::new().unwrap();
    let options = keyed_options(&root);
    fs::write(root.path().join("api"), result(subject())).unwrap();
    let r = root.path().display();
    fs::write(
        &options.claude_path,
        format!(
            "#!/bin/sh\nif [ \"$1\" = --help ]; then printf '%s\\n' '{HELP}'; exit 0; fi\ncat > /dev/null\ncat '{r}/api'\n"
        ),
    )
    .unwrap();
    let foreign = json!({"reason": "billing", "since": "2026-11-06T00:00:00+00:00",
                         "retry_at": "2099-01-01T00:00:00+00:00", "key": "1-2-3"});
    fs::write(root.path().join("api-fallback.json"), foreign.to_string()).unwrap();
    generate_message(DIFF, None, &options).unwrap();
    assert_eq!(fallback(&root).unwrap(), foreign);
}

#[test]
fn billing_errors_are_classified() {
    assert!(matches!(
        generate(&billing_error(), 1),
        Err(GenerationError::Billing)
    ));
    let payment = json!({"type": "result", "subtype": "success", "is_error": true,
                         "result": "Payment required", "api_error_status": 402})
    .to_string();
    assert!(matches!(
        generate(&payment, 1),
        Err(GenerationError::Billing)
    ));
    // A successful result that quotes the words is not a billing error.
    let quoted = [
        init(json!(["StructuredOutput"]), json!([])),
        json!({"type": "result", "subtype": "success", "is_error": false,
               "result": "Explain the credit balance check", "structured_output": subject()})
        .to_string(),
    ]
    .join("\n");
    assert!(matches!(
        generate(&quoted, 1),
        Err(GenerationError::Backend(_))
    ));
}

#[test]
fn an_exposed_key_file_is_refused_before_the_cli_runs() {
    let root = TempDir::new().unwrap();
    let options = keyed_options(&root);
    let key = options.api_key_file.as_ref().unwrap();
    fs::set_permissions(key, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(matches!(
        generate_message(DIFF, None, &options),
        Err(GenerationError::Backend(_))
    ));
    let link = root.path().join("link");
    std::os::unix::fs::symlink(key, &link).unwrap();
    fs::set_permissions(key, fs::Permissions::from_mode(0o600)).unwrap();
    let linked = Options {
        api_key_file: Some(link),
        ..keyed_options(&root)
    };
    assert!(generate_message(DIFF, None, &linked).is_err());
    assert!(calls(&root).is_empty());
}

#[test]
fn key_configuration_is_validated() {
    let root = TempDir::new().unwrap();
    let claude = fake(&root, "exit 0");
    let key = root.path().join("key");
    let fallback = root.path().join("api-fallback.json");
    let base = json!({"claude_path": claude, "model": "claude-haiku-5-5", "effort": "low"});
    let mut keyed = base.clone();
    keyed["api_key_file"] = json!(key);
    keyed["api_fallback_file"] = json!(fallback);
    let parsed = Options::from_value(&keyed).unwrap();
    assert_eq!(parsed.api_key_file.as_deref(), Some(key.as_path()));
    assert_eq!(
        parsed.api_fallback_file.as_deref(),
        Some(fallback.as_path())
    );
    assert!(Options::from_value(&base).unwrap().api_key_file.is_none());

    let mut relative = base.clone();
    relative["api_key_file"] = json!("key");
    let mut orphan = base.clone();
    orphan["api_fallback_file"] = json!(fallback);
    for value in [relative, orphan] {
        assert!(Options::from_value(&value).is_err(), "{value}");
    }
}

#[test]
fn live_generator_bills_a_configured_api_key() {
    if std::env::var("RUN_CLAUDE_LIVE").as_deref() != Ok("1") {
        eprintln!("skipped: set RUN_CLAUDE_LIVE=1 to call the real Claude Code CLI");
        return;
    }
    let Some(key) = std::env::var_os("CODEX_SKILLS_LIVE_API_KEY_FILE").map(PathBuf::from) else {
        eprintln!(
            "skipped: set CODEX_SKILLS_LIVE_API_KEY_FILE to a Claude API key file to check API billing"
        );
        return;
    };
    let home = PathBuf::from(std::env::var_os("HOME").expect("HOME is set"));
    let cli = resolve_cli(&home, std::env::var_os("PATH").as_deref()).expect("Claude Code CLI");
    // Record the real CLI's event stream to see which credential it used.
    let root = TempDir::new().unwrap();
    let stream = root.path().join("stream.jsonl");
    let before = claude_projects();
    let wrapper = fake_with_help(
        &root,
        HELP,
        &format!(
            "'{}' \"$@\" | tee '{}'; exit 0",
            cli.display(),
            stream.display()
        ),
    );
    let options = Options {
        api_key_file: Some(key),
        api_fallback_file: Some(root.path().join("api-fallback.json")),
        ..options(wrapper, Duration::from_secs(180))
    };
    let message = generate_message(DIFF, None, &options).unwrap();
    codex_skills::message::validate(&message).unwrap();
    let events: Vec<Value> = fs::read_to_string(&stream)
        .unwrap()
        .lines()
        .filter_map(|line| serde_json::from_str(line).ok())
        .collect();
    let init = events
        .iter()
        .find(|e| e["type"] == "system" && e["subtype"] == "init")
        .unwrap();
    assert_eq!(init["apiKeySource"], "ANTHROPIC_API_KEY");
    assert!(
        !root.path().join("api-fallback.json").exists(),
        "the API answered; no fallback"
    );
    let temp = std::env::temp_dir();
    let roots = [sanitized(&temp), sanitized(&temp.canonicalize().unwrap())];
    let created: Vec<String> = claude_projects()
        .difference(&before)
        .filter(|name| roots.iter().any(|root| name.starts_with(root.as_str())))
        .cloned()
        .collect();
    assert!(created.is_empty(), "{created:?}");
}
