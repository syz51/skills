use crate::Result;
use serde::Deserialize;
use serde_json::{Map, Value};
use std::{
    collections::HashSet,
    fs,
    path::{Path, PathBuf},
};
use unicode_normalization::UnicodeNormalization;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Entry {
    pub source: String,
    pub target: String,
}
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub schema_version: u32,
    pub source_repo: PathBuf,
    pub state_dir: PathBuf,
    pub cycle_lock: Option<PathBuf>,
    pub target_url: String,
    pub author_name: String,
    pub author_email: String,
    pub files: Vec<Entry>,
    #[serde(default)]
    pub fetch_source: bool,
    #[serde(default)]
    pub replacements: Map<String, Value>,
    #[serde(default)]
    pub json_drop_keys: Vec<String>,
    #[serde(default = "python")]
    pub python_path: PathBuf,
    pub generation: Option<Value>,
}
fn python() -> PathBuf {
    "python3".into()
}
pub fn expand(path: &Path) -> Result<PathBuf> {
    if path == Path::new("~") {
        return Ok(std::env::var_os("HOME").ok_or("HOME unavailable")?.into());
    }
    if let Ok(rest) = path.strip_prefix("~/") {
        return Ok(PathBuf::from(std::env::var_os("HOME").ok_or("HOME unavailable")?).join(rest));
    }
    Ok(path.to_owned())
}
pub fn identity(path: &str) -> String {
    let normalized: String = path.nfc().collect();
    let folded = caseless::default_case_fold_str(&normalized);
    folded.nfc().collect()
}
pub fn relative(path: &str) -> Result<()> {
    if path.is_empty()
        || path.starts_with('/')
        || path.contains('\\')
        || path.contains('\0')
        || path
            .split('/')
            .any(|p| p.is_empty() || p == "." || p == ".." || identity(p) == ".git")
    {
        return Err("Invalid inventory path".into());
    }
    Ok(())
}
impl Config {
    pub fn load(path: &Path) -> Result<Self> {
        Self::from_value(serde_json::from_slice(&fs::read(expand(path)?)?)?)
    }
    pub fn from_value(value: Value) -> Result<Self> {
        let mut c: Self = serde_json::from_value(value)?;
        let url = regex::Regex::new(
            r"^(?:https://github\.com/|git@github\.com:)[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+\.git$",
        )?;
        if c.schema_version != 1 || !url.is_match(&c.target_url) || c.files.is_empty() {
            return Err("Invalid schema, destination or inventory".into());
        }
        if [&c.author_name, &c.author_email]
            .iter()
            .any(|v| v.trim().is_empty() || v.chars().any(|x| matches!(x, '\n' | '\r' | '<' | '>')))
            || !regex::Regex::new(r"^[^\s@]+@[^\s@]+$")?.is_match(&c.author_email)
        {
            return Err("Invalid public author identity".into());
        }
        let mut sources = HashSet::new();
        let mut targets = HashSet::new();
        for e in &c.files {
            relative(&e.source)?;
            relative(&e.target)?;
            let root = e.source.split('/').next().unwrap();
            if ![
                "skills",
                "scripts",
                "tests",
                "prompts",
                "prompt-evals",
                "public",
                "src",
            ]
            .contains(&root)
                && ![
                    "maintenance-prompt.md",
                    "Cargo.toml",
                    "Cargo.lock",
                    "rust-toolchain.toml",
                    ".gitignore",
                    "CHANGELOG.md",
                ]
                .contains(&e.source.as_str())
            {
                return Err("Source outside publication scope".into());
            }
            if !sources.insert(e.source.clone())
                || !targets.insert(identity(&e.target))
                || identity(&e.target) == ".public-export.json"
            {
                return Err("Duplicate or reserved inventory path".into());
            }
        }
        for a in &targets {
            for b in &targets {
                if a != b && a.starts_with(&(b.to_owned() + "/")) {
                    return Err("Overlapping inventory paths".into());
                }
            }
        }
        if c.replacements
            .iter()
            .any(|(k, v)| k.is_empty() || !v.is_string())
            || c.json_drop_keys.iter().any(String::is_empty)
        {
            return Err("Invalid transforms".into());
        }
        if c.source_repo.as_os_str().is_empty()
            || c.state_dir.as_os_str().is_empty()
            || c.python_path.as_os_str().is_empty()
            || c.cycle_lock
                .as_ref()
                .is_some_and(|p| p.as_os_str().is_empty())
        {
            return Err("Invalid configuration path".into());
        }
        c.source_repo = expand(&c.source_repo)?.canonicalize()?;
        c.state_dir = expand(&c.state_dir)?;
        c.cycle_lock = c.cycle_lock.as_deref().map(expand).transpose()?;
        c.python_path = expand(&c.python_path)?;
        Ok(c)
    }
}
