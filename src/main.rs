use clap::{Args, Parser, Subcommand};
use codex_skills::{Result, config::Config, daily, export, message};
use std::{path::PathBuf, process::ExitCode};

#[derive(Parser)]
#[command(
    version,
    about = "Publish a curated public skills snapshot with informative commit messages"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Inspect the selected public changes without publishing or generating a message.
    Plan(Options),
    /// Validate and publish changed public content.
    Publish(Options),
    /// Publish when the daily Tokyo deadline remains due.
    Daily(Options),
}

#[derive(Args)]
struct Options {
    #[arg(long, value_name = "PATH")]
    config: PathBuf,
}

fn execute(command: Command) -> Result<serde_json::Value> {
    let options = match &command {
        Command::Plan(options) | Command::Publish(options) | Command::Daily(options) => options,
    };
    let config = Config::load(&options.config)?;
    let generate = |diff: &str| message::generate(&config, diff);
    match command {
        Command::Plan(_) => Ok(serde_json::to_value(export::execute(
            &config, false, &generate,
        )?)?),
        Command::Publish(_) => Ok(serde_json::to_value(export::execute(
            &config, true, &generate,
        )?)?),
        Command::Daily(_) => daily::run(&config, chrono::Utc::now(), &generate),
    }
}

fn main() -> ExitCode {
    if codex_skills::cancellation::install().is_err() {
        eprintln!("Could not initialize publication cancellation.");
        return ExitCode::FAILURE;
    }
    match execute(Cli::parse().command) {
        Ok(value) => match serde_json::to_string_pretty(&value) {
            Ok(output) => {
                println!("{output}");
                ExitCode::SUCCESS
            }
            Err(_) => {
                eprintln!("Could not serialize publication result.");
                ExitCode::FAILURE
            }
        },
        Err(error) => {
            if error.is::<serde_json::Error>() {
                eprintln!("Publication failed: invalid JSON configuration or local state.");
            } else {
                eprintln!("Publication failed: {error}");
            }
            ExitCode::FAILURE
        }
    }
}
