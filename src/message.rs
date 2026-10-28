//! Generate commit text using only a supplied public diff.
use crate::{
    CommitMessage, Result,
    config::{Config, expand},
};
use codex_commit::{GenerationOptions, GenerationRequest, ReasoningEffort};
use serde::Deserialize;
use std::{path::PathBuf, time::Duration};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Settings {
    codex_path: PathBuf,
    codex_home: PathBuf,
    model: String,
    #[serde(default = "default_effort")]
    effort: String,
    #[serde(default = "default_timeout")]
    timeout_seconds: u64,
}
fn default_effort() -> String {
    "high".to_owned()
}
fn default_timeout() -> u64 {
    120
}

fn options(config: &Config) -> Result<GenerationOptions> {
    let settings: Settings = serde_json::from_value(
        config
            .generation
            .clone()
            .ok_or("Message generation must be configured")?,
    )?;
    let path = expand(&settings.codex_path)?;
    let home = expand(&settings.codex_home)?;
    if !path.is_absolute()
        || !path.is_file()
        || !home.is_absolute()
        || !home.is_dir()
        || settings.model.trim().is_empty()
        || !(1..=900).contains(&settings.timeout_seconds)
    {
        return Err("Invalid message-generation configuration".into());
    }
    let effort = match settings.effort.as_str() {
        "low" => ReasoningEffort::Low,
        "medium" => ReasoningEffort::Medium,
        "high" => ReasoningEffort::High,
        "xhigh" => ReasoningEffort::XHigh,
        _ => return Err("Unsupported message-generation effort".into()),
    };
    let mut options = GenerationOptions::new(path, home, settings.model);
    options.effort = effort;
    options.timeout = Duration::from_secs(settings.timeout_seconds);
    options.cancellation = crate::cancellation::token();
    Ok(options)
}

pub fn generate(config: &Config, diff: &str) -> Result<CommitMessage> {
    let message = codex_commit::generate_message(GenerationRequest { diff }, &options(config)?)?;
    message.validate()?;
    Ok(CommitMessage {
        subject: message.subject,
        body: message.body,
    })
}
