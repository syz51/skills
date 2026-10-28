//! A retryable daily publication deadline, independent of the external scheduler.
use crate::{CommitMessage, Result, config::Config, export};
use chrono::{DateTime, Days, FixedOffset, TimeZone, Utc};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{fs, path::Path};

const FAILURE: &str =
    "Publication stopped; inspect configuration, candidate tests and export state.";

pub fn latest_due(now: DateTime<Utc>) -> DateTime<FixedOffset> {
    let offset = FixedOffset::east_opt(9 * 3600).expect("Tokyo offset");
    let local = now.with_timezone(&offset);
    let today = offset
        .from_local_datetime(
            &local
                .date_naive()
                .and_hms_opt(10, 17, 0)
                .expect("daily time"),
        )
        .single()
        .expect("fixed offset has no ambiguous times");
    if local >= today {
        today
    } else {
        today.checked_sub_days(Days::new(1)).expect("current date")
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct State {
    completed_due: DateTime<FixedOffset>,
}

pub fn run(
    config: &Config,
    now: DateTime<Utc>,
    generate: &dyn Fn(&str) -> Result<CommitMessage>,
) -> Result<Value> {
    run_with(config, now, || {
        Ok(serde_json::to_value(export::execute(
            config, true, generate,
        )?)?)
    })
}

/// Injecting the publication transaction keeps deadline tests free of network/model calls.
pub fn run_with(
    config: &Config,
    now: DateTime<Utc>,
    action: impl FnOnce() -> Result<Value>,
) -> Result<Value> {
    fs::create_dir_all(&config.state_dir)?;
    let _lock = export::acquire(&[config.state_dir.join("job.lock")])?;
    let due = latest_due(now);
    let state_path = config.state_dir.join("daily-state.json");
    if read_state(&state_path)?.is_some_and(|state| state.completed_due >= due) {
        return Ok(json!({"outcome": "not_due"}));
    }
    let status_path = config.state_dir.join("daily-status.json");
    let mut status =
        json!({"started_at": now.to_rfc3339(), "due": due.to_rfc3339(), "success": null});
    export::save(&status_path, &status)?;
    let result = action().and_then(|value| {
        if value.as_object().is_none() || value.get("changed").and_then(Value::as_bool).is_none() {
            return Err("Invalid publication result".into());
        }
        Ok(value)
    });
    let result = match result {
        Ok(value) => value,
        Err(_) => {
            status["success"] = json!(false);
            status["error"] = json!(FAILURE);
            status["finished_at"] = json!(Utc::now().to_rfc3339());
            export::save(&status_path, &status)?;
            return Err(FAILURE.into());
        }
    };
    status["success"] = json!(true);
    status["result"] = result.clone();
    status["finished_at"] = json!(Utc::now().to_rfc3339());
    export::save(&status_path, &status)?;
    // Only a successfully completed transaction (including unchanged content) satisfies the deadline.
    export::save(&state_path, &json!({"completed_due": due.to_rfc3339()}))?;
    let changed = result["changed"].as_bool().expect("validated result");
    let mut output = result;
    output["outcome"] = json!(if changed { "published" } else { "unchanged" });
    Ok(output)
}

fn read_state(path: &Path) -> Result<Option<State>> {
    match fs::read(path) {
        Ok(bytes) => Ok(Some(serde_json::from_slice(&bytes)?)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}
