use chrono::{TimeZone, Utc};
use codex_skills::{config::Config, daily};
use serde_json::{Value, json};
use std::{cell::Cell, fs};
use tempfile::TempDir;

fn config(root: &TempDir) -> Config {
    Config::from_value(json!({
        "schema_version": 1, "source_repo": root.path(), "state_dir": root.path(),
        "target_url": "https://github.com/example/skills.git", "author_name": "Example",
        "author_email": "example@example.invalid", "files": [{"source": "skills/test.md", "target": "skills/test.md"}]
    })).unwrap()
}

#[test]
fn deadline_observes_tokyo_date_and_exact_minute() {
    let before = Utc.with_ymd_and_hms(2026, 10, 28, 1, 16, 59).unwrap();
    let after = Utc.with_ymd_and_hms(2026, 10, 28, 1, 17, 0).unwrap();
    assert_eq!(
        daily::latest_due(before).to_rfc3339(),
        "2026-10-27T10:17:00+09:00"
    );
    assert_eq!(
        daily::latest_due(after).to_rfc3339(),
        "2026-10-28T10:17:00+09:00"
    );
}

#[test]
fn unchanged_success_skips_repeats_and_next_day_runs() {
    let root = TempDir::new().unwrap();
    let config = config(&root);
    let calls = Cell::new(0);
    let now = Utc.with_ymd_and_hms(2026, 10, 28, 2, 0, 0).unwrap();
    let action = || {
        calls.set(calls.get() + 1);
        Ok(json!({"changed": false}))
    };
    assert_eq!(
        daily::run_with(&config, now, action).unwrap()["outcome"],
        "unchanged"
    );
    assert_eq!(
        daily::run_with(&config, now, action).unwrap()["outcome"],
        "not_due"
    );
    assert_eq!(
        daily::run_with(&config, now + chrono::Duration::days(1), action).unwrap()["outcome"],
        "unchanged"
    );
    assert_eq!(calls.get(), 2);
}

#[test]
fn failed_publication_is_retryable_and_diagnostics_stay_static() {
    let root = TempDir::new().unwrap();
    let config = config(&root);
    let now = Utc.with_ymd_and_hms(2026, 10, 28, 2, 0, 0).unwrap();
    assert!(daily::run_with(&config, now, || Err("private diagnostic".into())).is_err());
    assert!(!root.path().join("daily-state.json").exists());
    let status = fs::read_to_string(root.path().join("daily-status.json")).unwrap();
    assert!(!status.contains("private diagnostic"));
    assert_eq!(
        serde_json::from_str::<Value>(&status).unwrap()["success"],
        false
    );
    assert_eq!(
        daily::run_with(&config, now, || Ok(json!({"changed": true}))).unwrap()["outcome"],
        "published"
    );
}

#[test]
fn corrupt_state_and_invalid_result_do_not_complete_deadline() {
    let root = TempDir::new().unwrap();
    let config = config(&root);
    let now = Utc.with_ymd_and_hms(2026, 10, 28, 2, 0, 0).unwrap();
    fs::write(root.path().join("daily-state.json"), "{}").unwrap();
    assert!(
        daily::run_with(&config, now, || panic!(
            "must not publish with corrupt state"
        ))
        .is_err()
    );
    fs::remove_file(root.path().join("daily-state.json")).unwrap();
    assert!(daily::run_with(&config, now, || Ok(json!({"changed": "false"}))).is_err());
    assert!(!root.path().join("daily-state.json").exists());
}

#[test]
fn overlapping_job_never_calls_publication() {
    let root = TempDir::new().unwrap();
    let config = config(&root);
    let now = Utc.with_ymd_and_hms(2026, 10, 28, 2, 0, 0).unwrap();
    let _lock = codex_skills::export::acquire(&[root.path().join("job.lock")]).unwrap();
    assert!(daily::run_with(&config, now, || panic!("overlapping job must not run")).is_err());
    assert!(!root.path().join("daily-state.json").exists());
}

#[test]
fn cli_help_and_invalid_config_have_expected_exit_codes() {
    use std::process::Command;
    let binary = env!("CARGO_BIN_EXE_codex-skills");
    let help = Command::new(binary).arg("--help").output().unwrap();
    assert!(help.status.success());
    assert!(String::from_utf8_lossy(&help.stdout).contains("daily"));
    let root = TempDir::new().unwrap();
    let path = root.path().join("private-config.json");
    fs::write(&path, "private diagnostic data").unwrap();
    let failed = Command::new(binary)
        .args(["publish", "--config"])
        .arg(path)
        .output()
        .unwrap();
    assert_eq!(failed.status.code(), Some(1));
    assert!(failed.stdout.is_empty());
    assert!(!String::from_utf8_lossy(&failed.stderr).contains("private diagnostic data"));
}

#[test]
fn termination_cancels_a_running_git_process() {
    use std::{
        os::unix::fs::PermissionsExt,
        process::{Command, Stdio},
        thread,
        time::{Duration, Instant},
    };
    let root = TempDir::new().unwrap();
    let fake = root.path().join("git");
    fs::write(
        &fake,
        "#!/bin/sh\nprintf '%s\\n' \"$$\" > \"$CODEX_SKILLS_TEST_PID\"\nexec sleep 30\n",
    )
    .unwrap();
    fs::set_permissions(&fake, fs::Permissions::from_mode(0o755)).unwrap();
    let pidfile = root.path().join("child.pid");
    let path = root.path().join("config.json");
    fs::write(&path, serde_json::to_vec(&json!({"schema_version":1,"source_repo":root.path(),"state_dir":root.path().join("state"),"target_url":"https://github.com/example/skills.git","author_name":"Test","author_email":"test@example.invalid","files":[{"source":"skills/test.md","target":"skills/test.md"}]})).unwrap()).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_codex-skills"))
        .args(["plan", "--config"])
        .arg(path)
        .env("CODEX_SKILLS_TEST_PID", &pidfile)
        .env(
            "PATH",
            format!(
                "{}:{}",
                root.path().display(),
                std::env::var("PATH").unwrap()
            ),
        )
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let started = Instant::now();
    while !pidfile.exists() && started.elapsed() < Duration::from_secs(5) {
        thread::sleep(Duration::from_millis(10));
    }
    if !pidfile.exists() {
        let _ = child.kill();
        let _ = child.wait();
        panic!("Git subprocess did not start");
    }
    let pid = fs::read_to_string(&pidfile).unwrap();
    Command::new("/bin/kill")
        .args(["-TERM", &child.id().to_string()])
        .status()
        .unwrap();
    let deadline = Instant::now();
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if deadline.elapsed() > Duration::from_secs(5) {
            let _ = Command::new("/bin/kill")
                .args(["-KILL", pid.trim()])
                .status();
            let _ = child.kill();
            let _ = child.wait();
            panic!("Cancellation did not terminate the publisher");
        }
        thread::sleep(Duration::from_millis(10));
    };
    assert!(!status.success());
    assert!(
        !Command::new("/bin/kill")
            .args(["-0", pid.trim()])
            .stderr(Stdio::null())
            .status()
            .unwrap()
            .success()
    );
    assert!(!root.path().join("state/export-state.json").exists());
}
