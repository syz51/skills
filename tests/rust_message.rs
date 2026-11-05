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

    // --help is checked once per executable per process.
    let opts = options(root.path().join("claude"), Duration::from_secs(20));
    generate_message(DIFF, &opts).unwrap();
    let helped = fs::read_to_string(root.path().join("helped")).unwrap();
    assert_eq!(helped.lines().count(), 1);
}

#[test]
fn missing_help_flag_fails_before_generation() {
    let root = TempDir::new().unwrap();
    let ran = root.path().join("ran");
    let help = HELP.replace("--verbose ", "");
    let path = fake_with_help(&root, &help, &format!("touch '{}'", ran.display()));
    let e = generate_message(DIFF, &options(path, Duration::from_secs(20))).unwrap_err();
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
        generate_message(DIFF, &options(failing, Duration::from_secs(20))),
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
        generate_message(DIFF, &options(path, Duration::from_secs(20))),
        Err(GenerationError::OutputTooLarge)
    ));
}

#[test]
fn timeout_kills_the_cli() {
    let root = TempDir::new().unwrap();
    let path = fake(&root, "exec sleep 30");
    let start = Instant::now();
    assert!(matches!(
        generate_message(DIFF, &options(path, Duration::from_secs(1))),
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
    let message = generate_message(DIFF, &options(cli, Duration::from_secs(180))).unwrap();
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
