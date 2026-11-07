use crate::{
    CommitMessage, Result,
    config::{Config, identity, relative},
};
use fs2::FileExt;
use nix::{
    sys::signal::{Signal, killpg},
    unistd::Pid,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::os::unix::{fs::PermissionsExt, process::CommandExt};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    process::{Child, Command, ExitStatus, Stdio},
    thread,
    time::{Duration, Instant},
};

pub const MANIFEST: &str = ".public-export.json";
/// Message generations allowed when the publication scanner rejects the generated text.
pub const MESSAGE_ATTEMPTS: usize = 3;
/// Added to the generation prompt after the publication scanner rejects a message.
pub const SCAN_RETRY: &str = "The previous message for this diff was rejected because it contained a private identifier or credential. Do not include local paths, host names, private repository names or credentials.";
pub type Files = BTreeMap<String, Vec<u8>>;
/// Expected contention with another publication or maintenance transaction.
#[derive(Debug)]
pub struct LockBusy;
impl std::fmt::Display for LockBusy {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Publication deferred: job lock held")
    }
}
impl std::error::Error for LockBusy {}

pub struct Locks {
    _files: Vec<File>,
}
impl Drop for Locks {
    fn drop(&mut self) {
        // Unlock the shared open-file description before closing it: another
        // thread may be forking a child which briefly inherits the descriptor.
        for file in self._files.iter().rev() {
            let _ = FileExt::unlock(file);
        }
    }
}
pub fn acquire(paths: &[PathBuf]) -> Result<Locks> {
    let mut locks = Locks { _files: Vec::new() };
    for p in paths {
        let f = OpenOptions::new().create(true).append(true).open(p)?;
        lock_file(&f, Duration::ZERO, &|| false)?;
        locks._files.push(f);
    }
    Ok(locks)
}
// Only the shared maintenance lock waits; publication/job locks stay nonblocking.
fn lock_file(file: &File, timeout: Duration, cancelled: &dyn Fn() -> bool) -> Result<()> {
    let start = Instant::now();
    loop {
        if cancelled() {
            return Err("Publication cancelled".into());
        }
        match file.try_lock_exclusive() {
            Ok(()) => return Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                if start.elapsed() >= timeout {
                    return Err(LockBusy.into());
                }
            }
            Err(error) => return Err(error.into()),
        }
        thread::sleep(Duration::from_millis(50).min(timeout.saturating_sub(start.elapsed())));
    }
}

pub fn save(path: &Path, value: &impl Serialize) -> Result<()> {
    let mut f = tempfile::NamedTempFile::new_in(path.parent().ok_or("State parent missing")?)?;
    serde_json::to_writer_pretty(&mut f, value)?;
    f.write_all(b"\n")?;
    f.as_file().sync_all()?;
    f.persist(path).map_err(|e| e.error)?;
    Ok(())
}
fn bytes(value: &impl Serialize) -> Result<Vec<u8>> {
    fn canonical(v: Value) -> Value {
        match v {
            Value::Object(m) => Value::Object(
                m.into_iter()
                    .map(|(k, v)| (k, canonical(v)))
                    .collect::<BTreeMap<_, _>>()
                    .into_iter()
                    .collect(),
            ),
            Value::Array(a) => Value::Array(a.into_iter().map(canonical).collect()),
            v => v,
        }
    }
    let text = serde_json::to_string_pretty(&canonical(serde_json::to_value(value)?))?;
    let mut ascii = String::new();
    for c in text.chars() {
        if (c as u32) >= 127 {
            let mut units = [0u16; 2];
            for unit in c.encode_utf16(&mut units) {
                ascii.push_str(&format!("\\u{unit:04x}"));
            }
        } else {
            ascii.push(c)
        }
    }
    ascii.push('\n');
    Ok(ascii.into_bytes())
}
fn hash(data: &[u8]) -> String {
    Sha256::digest(data)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
fn terminate_group(child: &mut Child) {
    let _ = killpg(Pid::from_raw(child.id() as i32), Signal::SIGKILL);
    let _ = child.kill();
    let _ = child.wait();
}
fn command(mut cmd: Command, timeout: Duration) -> Result<(ExitStatus, Vec<u8>, Vec<u8>)> {
    if crate::cancellation::requested() {
        return Err("Publication cancelled".into());
    }
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    let mut child = cmd.spawn().map_err(|_| "Unable to start subprocess")?;
    let out = child.stdout.take().ok_or("Missing stdout")?;
    let err = child.stderr.take().ok_or("Missing stderr")?;
    let (tx, rx) = std::sync::mpsc::channel();
    let err_tx = tx.clone();
    thread::spawn(move || {
        let mut b = Vec::new();
        let r = out
            .take(64 * 1024 * 1024 + 1)
            .read_to_end(&mut b)
            .and_then(|_| {
                if b.len() > 64 * 1024 * 1024 {
                    Err(std::io::Error::other("Subprocess output limit exceeded"))
                } else {
                    Ok(b)
                }
            });
        let _ = tx.send((false, r));
    });
    thread::spawn(move || {
        let mut b = Vec::new();
        let r = err
            .take(64 * 1024 * 1024 + 1)
            .read_to_end(&mut b)
            .and_then(|_| {
                if b.len() > 64 * 1024 * 1024 {
                    Err(std::io::Error::other("Subprocess output limit exceeded"))
                } else {
                    Ok(b)
                }
            });
        let _ = err_tx.send((true, r));
    });
    let start = Instant::now();
    let mut status = None;
    let mut stdout = None;
    let mut stderr = None;
    loop {
        if status.is_none() {
            status = child.try_wait()?;
        }
        while let Ok((is_err, result)) = rx.try_recv() {
            let result = match result {
                Ok(v) => v,
                Err(_) => {
                    terminate_group(&mut child);
                    return Err("Subprocess output failed or exceeded limit".into());
                }
            };
            if is_err {
                stderr = Some(result)
            } else {
                stdout = Some(result)
            }
        }
        if status.is_some() && stdout.is_some() && stderr.is_some() {
            break;
        }
        if start.elapsed() > timeout || crate::cancellation::requested() {
            terminate_group(&mut child);
            return Err(if crate::cancellation::requested() {
                "Publication cancelled"
            } else {
                "Subprocess timed out"
            }
            .into());
        }
        thread::sleep(Duration::from_millis(10));
    }
    Ok((
        status.ok_or("Missing child status")?,
        stdout.ok_or("Missing stdout")?,
        stderr.ok_or("Missing stderr")?,
    ))
}
pub fn git(repo: &Path, args: &[&str]) -> Result<Vec<u8>> {
    let mut c = Command::new("git");
    c.args(["-c", "core.hooksPath=/dev/null", "-C"])
        .arg(repo)
        .args(args)
        .env("GIT_TERMINAL_PROMPT", "0");
    let (status, out, _) = command(c, Duration::from_secs(120))?;
    if !status.success() {
        return Err(format!(
            "Git operation failed: {}",
            args.first().unwrap_or(&"unknown")
        )
        .into());
    }
    Ok(out)
}
fn text(repo: &Path, args: &[&str]) -> Result<String> {
    Ok(String::from_utf8(git(repo, args)?)?.trim_end().to_owned())
}
pub fn scan(path: &str, data: &[u8]) -> Result<()> {
    let s = std::str::from_utf8(data).map_err(|_| format!("Non-text publication file: {path}"))?;
    // Markers match in any case, but replacements do not, so an unreplaced case variant is
    // rejected. A lowercase users segment is also a common web route, so its case variants
    // count only where a home path would start: not after a path segment, and followed by a
    // name rather than a route parameter.
    let hosts = [
        ["roy", "-mac"].concat(),
        ["sunmacbook", "-air"].concat(),
        ["syz51/", "codex-skills"].concat(),
    ]
    .map(|marker| regex::escape(&marker))
    .join("|");
    let private = regex::Regex::new(&format!(
        "{}|(?i:{hosts}|(?:^|[^\\w.-])/us{}[a-z_])",
        ["/Us", "ers/"].concat(),
        "ers/"
    ))?;
    let secret = regex::Regex::new(
        r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-(?:proj-)?[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16})\b",
    )?;
    if private.is_match(s) || secret.is_match(s) {
        return Err(
            format!("Publication rejected: {path} (private identifier or credential)").into(),
        );
    }
    Ok(())
}
fn clean(repo: &Path) -> Result<String> {
    if text(repo, &["branch", "--show-current"])? != "main"
        || !git(repo, &["status", "--porcelain"])?.is_empty()
    {
        return Err("Source requires clean committed main".into());
    }
    text(repo, &["rev-parse", "HEAD"])
}
fn scrub(v: &mut Value, drop: &[String]) {
    match v {
        Value::Object(m) => {
            m.retain(|k, _| !drop.contains(k));
            for x in m.values_mut() {
                scrub(x, drop)
            }
        }
        Value::Array(a) => {
            for x in a {
                scrub(x, drop)
            }
        }
        _ => {}
    }
}
pub fn snapshot(c: &Config, rev: &str) -> Result<Files> {
    let mut files = Files::new();
    for e in &c.files {
        let info = git(&c.source_repo, &["ls-tree", "-z", rev, "--", &e.source])?;
        let entries: Vec<_> = info.split(|x| *x == 0).filter(|x| !x.is_empty()).collect();
        if entries.len() != 1
            || !(entries[0].starts_with(b"100644 blob ") || entries[0].starts_with(b"100755 blob "))
        {
            return Err(format!("Source not a committed regular file: {}", e.source).into());
        }
        let mut s = String::from_utf8(git(
            &c.source_repo,
            &["show", &format!("{rev}:{}", e.source)],
        )?)?;
        if e.source.starts_with("skills/")
            && e.source.ends_with("/sources.json")
            && !c.json_drop_keys.is_empty()
        {
            let mut v: Value = serde_json::from_str(&s)?;
            scrub(&mut v, &c.json_drop_keys);
            s = String::from_utf8(bytes(&v)?)?;
        }
        for (old, new) in &c.replacements {
            s = s.replace(old, new.as_str().ok_or("Invalid replacement")?);
        }
        scan(&e.target, s.as_bytes())?;
        files.insert(e.target.clone(), s.into_bytes());
    }
    Ok(files)
}
#[derive(Clone, Debug, PartialEq)]
struct Item {
    mode: String,
    oid: String,
}
fn tree(repo: &Path, rev: &str) -> Result<BTreeMap<String, Item>> {
    let mut map = BTreeMap::new();
    for e in git(repo, &["ls-tree", "-rz", rev])?
        .split(|b| *b == 0)
        .filter(|s| !s.is_empty())
    {
        let i = e
            .iter()
            .position(|b| *b == b'\t')
            .ok_or("Invalid Git tree")?;
        let fields = std::str::from_utf8(&e[..i])?.split(' ').collect::<Vec<_>>();
        map.insert(
            String::from_utf8(e[i + 1..].to_vec())?,
            Item {
                mode: fields[0].into(),
                oid: fields[2].into(),
            },
        );
    }
    Ok(map)
}
pub fn manifest(files: &Files) -> Result<Vec<u8>> {
    bytes(
        &json!({"schema_version":1,"files":files.iter().map(|(p,b)|(p.clone(),hash(b))).collect::<BTreeMap<_,_>>()}),
    )
}
fn read_manifest(repo: &Path, rev: &str) -> Result<BTreeMap<String, String>> {
    let t = tree(repo, rev)?;
    if t.get(MANIFEST).map(|x| x.mode.as_str()) != Some("100644") {
        return Err("Missing trusted manifest".into());
    }
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct M {
        schema_version: u32,
        files: BTreeMap<String, String>,
    }
    let m: M = serde_json::from_slice(&git(repo, &["show", &format!("{rev}:{MANIFEST}")])?)?;
    if m.schema_version != 1 {
        return Err("Invalid manifest schema".into());
    }
    for (p, h) in &m.files {
        relative(p)?;
        if identity(p) == MANIFEST
            || t.get(p).map(|x| x.mode.as_str()) != Some("100644")
            || hash(&git(repo, &["show", &format!("{rev}:{p}")])?) != *h
        {
            return Err("Invalid trusted manifest".into());
        }
    }
    Ok(m.files)
}
fn matches(repo: &Path, rev: &str, path: &str, data: &[u8], t: &BTreeMap<String, Item>) -> bool {
    t.get(path).is_some_and(|i| i.mode == "100644")
        && git(repo, &["show", &format!("{rev}:{path}")]).is_ok_and(|b| b == data)
}
/// Public commits the private source has imported, one full commit ID per line, with `#`
/// comments. A public edit of a managed file needs no record when the export already contains
/// it; the record covers the rest, such as an imported edit the private source then revised,
/// a mode change or content the export rewrites.
pub const IMPORTED: &str = "public/imported-commits";
pub type Imported = BTreeSet<String>;
pub fn imported(source: &Path, rev: &str) -> Result<Imported> {
    if git(source, &["ls-tree", "-z", rev, "--", IMPORTED])?.is_empty() {
        return Ok(Imported::new());
    }
    let text = String::from_utf8(git(source, &["show", &format!("{rev}:{IMPORTED}")])?)?;
    let id = regex::Regex::new(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")?;
    let mut commits = Imported::new();
    for line in text
        .lines()
        .map(|l| l.split('#').next().unwrap_or_default().trim())
    {
        if line.is_empty() {
            continue;
        }
        if !id.is_match(line) {
            return Err(format!("Invalid {IMPORTED} entry: {line}").into());
        }
        commits.insert(line.to_owned());
    }
    Ok(commits)
}
/// Whether the export already contains a public edit of a managed file: merging the edit into
/// the export, with the last trusted export as base, leaves the export unchanged. A merge
/// conflict means it does not; any other Git failure stops publication.
fn contained(repo: &Path, base: &Item, public: &Item, data: &[u8]) -> Result<bool> {
    if public.mode != "100644" {
        return Ok(false);
    }
    let dir = tempfile::tempdir()?;
    let paths = [
        dir.path().join("export"),
        dir.path().join("base"),
        dir.path().join("public"),
    ];
    fs::write(&paths[0], data)?;
    fs::write(&paths[1], git(repo, &["cat-file", "blob", &base.oid])?)?;
    fs::write(&paths[2], git(repo, &["cat-file", "blob", &public.oid])?)?;
    let mut c = Command::new("git");
    c.args(["-c", "core.hooksPath=/dev/null", "-C"])
        .arg(repo)
        .args(["merge-file", "--stdout"])
        .args(&paths)
        .env("GIT_TERMINAL_PROMPT", "0");
    let (status, merged, _) = command(c, Duration::from_secs(120))?;
    match status.code() {
        Some(0) => Ok(merged == data),
        // merge-file exits with the number of conflicts, capped at 127.
        Some(1..=127) => Ok(false),
        _ => Err("Git operation failed: merge-file".into()),
    }
}
pub fn prepare(
    repo: &Path,
    files: &Files,
    previous: Option<&str>,
    imported: &Imported,
) -> Result<bool> {
    let mut old = BTreeMap::new();
    let current;
    if !git(
        repo,
        &[
            "for-each-ref",
            "--format=%(refname)",
            "refs/remotes/origin/main",
        ],
    )?
    .is_empty()
    {
        git(repo, &["checkout", "-B", "main", "origin/main"])?;
        current = tree(repo, "HEAD")?;
        let exact = matches(repo, "HEAD", MANIFEST, &manifest(files)?, &current)
            && files
                .iter()
                .all(|(p, b)| matches(repo, "HEAD", p, b, &current));
        if let Some(prev) = previous {
            git(repo, &["merge-base", "--is-ancestor", prev, "HEAD"])?;
            old = read_manifest(repo, prev)?;
            let trusted = tree(repo, prev)?;
            if exact {
                if old
                    .keys()
                    .any(|p| !files.contains_key(p) && current.contains_key(p))
                {
                    return Err("Unexpected retained managed path".into());
                }
                return Ok(false);
            }
            if current.get(MANIFEST) != trusted.get(MANIFEST) {
                return Err("Public manifest changed".into());
            }
            for p in old.keys() {
                if current.get(p) == trusted.get(p)
                    || (!current.contains_key(p) && !files.contains_key(p))
                {
                    continue;
                }
                if files
                    .get(p)
                    .is_some_and(|data| matches(repo, "HEAD", p, data, &current))
                {
                    continue;
                }
                let commits = text(repo, &["rev-list", &format!("{prev}..HEAD"), "--", p])?;
                let missing: Vec<_> = commits
                    .lines()
                    .filter(|commit| !imported.contains(*commit))
                    .map(|commit| &commit[..12])
                    .collect();
                if !commits.is_empty() && missing.is_empty() {
                    continue;
                }
                if let (Some(data), Some(public)) = (files.get(p), current.get(p)) {
                    let base = trusted.get(p).ok_or("Invalid trusted manifest")?;
                    if contained(repo, base, public, data).map_err(|error| {
                        format!("Could not compare the public edit of {p}: {error}")
                    })? {
                        continue;
                    }
                }
                return Err(format!(
                    "Public managed file changed; import it, then record its public commits in \
                     {IMPORTED}: {p} ({})",
                    missing.join(", ")
                )
                .into());
            }
        } else {
            if exact {
                return Ok(false);
            }
            return Err("Existing public repository requires trusted state".into());
        }
    } else {
        if previous.is_some() {
            return Err("Public main disappeared".into());
        }
        git(repo, &["checkout", "--orphan", "main"])?;
        current = BTreeMap::new();
    }
    let mut aliases = BTreeMap::new();
    for name in current.keys().chain(files.keys()) {
        let mut prefix = String::new();
        for part in name.split('/') {
            if !prefix.is_empty() {
                prefix.push('/')
            }
            prefix.push_str(part);
            let key = identity(&prefix);
            if aliases
                .get(&key)
                .is_some_and(|original| original != &prefix)
            {
                return Err("Case-colliding public path component".into());
            }
            aliases.insert(key, prefix.clone());
        }
    }
    for (p, b) in files {
        for name in current.keys() {
            if !old.contains_key(name) && name != MANIFEST {
                if identity(p) == identity(name) && p != name {
                    return Err("Case-colliding public path".into());
                }
                if p == name && !matches(repo, "HEAD", p, b, &current) {
                    return Err("Publication collides with public-owned file".into());
                }
                let a = identity(p);
                let n = identity(name);
                if a.starts_with(&(n.clone() + "/")) || n.starts_with(&(a + "/")) {
                    return Err("Publication collides with public-owned path".into());
                }
            }
        }
    }
    let removed: Vec<_> = old
        .keys()
        .filter(|p| !files.contains_key(*p) && current.contains_key(*p))
        .collect();
    for p in &removed {
        let path = repo.join(p);
        fs::remove_file(path)?;
    }
    for p in &removed {
        let path = repo.join(p);
        for parent in path.ancestors().skip(1).take_while(|p| *p != repo) {
            match fs::remove_dir(parent) {
                Ok(()) => (),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
                Err(error) if error.kind() == std::io::ErrorKind::DirectoryNotEmpty => break,
                Err(error) => return Err(error.into()),
            }
        }
    }
    for (p, b) in files
        .iter()
        .chain(std::iter::once((&MANIFEST.to_owned(), &manifest(files)?)))
    {
        let path = repo.join(p);
        fs::create_dir_all(path.parent().ok_or("Parent missing")?)?;
        fs::write(&path, b)?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o644))?;
    }
    git(repo, &["add", "--all"])?;
    let staged_tree = text(repo, &["write-tree"])?;
    let indexed = tree(repo, &staged_tree)?;
    for (path, data) in files
        .iter()
        .chain(std::iter::once((&MANIFEST.to_owned(), &manifest(files)?)))
    {
        if !matches(repo, &staged_tree, path, data, &indexed) {
            return Err("Indexed public content differs from the validated snapshot; reconcile Git ignore or attribute policy".into());
        }
    }
    if old
        .keys()
        .any(|path| !files.contains_key(path) && indexed.contains_key(path))
    {
        return Err("Indexed public deletion differs from the validated snapshot".into());
    }
    Ok(!git(repo, &["diff", "--cached", "--name-only"])?.is_empty())
}
#[derive(Serialize)]
pub struct ExportResult {
    pub changed: bool,
    pub file_count: usize,
    pub source_commit: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub public_commit: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub paths: Option<Vec<String>>,
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct State {
    target_url: String,
    public_commit: String,
    source_commit: String,
}
// Validate only the managed snapshot, never public-owned additions. Recheck Rust
// on every changed Rust-containing export: shared Markdown/scripts/config can
// affect tests too, and no-op exports still skip all candidate execution.
/// Opt-in live tests make real model calls; candidate checks never inherit it.
const LIVE_TEST_OPT_IN: &str = "RUN_CLAUDE_LIVE";
fn candidate_check(program: &Path) -> Command {
    let mut cmd = Command::new(program);
    cmd.env_remove(LIVE_TEST_OPT_IN);
    cmd
}
fn rust_checks(validation: &Path, state: &Path, cargo: &Path) -> Result<()> {
    let deadline = Instant::now() + Duration::from_secs(600);
    let checks: &[&[&str]] = &[
        &["fmt", "--all", "--", "--check"],
        &[
            "clippy",
            "--all-targets",
            "--locked",
            "--",
            "-D",
            "warnings",
        ],
        &["test", "--locked"],
    ];
    let mut log = Vec::new();
    for args in checks {
        log.extend(format!("cargo {}\n", args.join(" ")).bytes());
        let mut cmd = candidate_check(cargo);
        cmd.args(*args)
            .current_dir(validation)
            .env("CARGO_TARGET_DIR", validation.join("target"));
        let outcome = match command(cmd, deadline.saturating_duration_since(Instant::now())) {
            Ok((status, out, err)) => {
                log.extend(out);
                log.extend(err);
                if status.success() {
                    Ok(())
                } else {
                    Err("Public candidate Rust checks failed".into())
                }
            }
            Err(error) => {
                // Static subprocess diagnostics; no model or private source text.
                log.extend(error.to_string().bytes());
                Err(error)
            }
        };
        record(&state.join("candidate-rust-tests.log"), &log, outcome)?;
    }
    Ok(())
}

/// Keep the last 64 KiB of a candidate check's output as its log, replacing any older
/// attempt's. A failed write is reported along with the check's outcome, never instead of it.
fn record(log: &Path, output: &[u8], outcome: Result<()>) -> Result<()> {
    let written = fs::write(log, &output[output.len().saturating_sub(65536)..]);
    match (outcome, written) {
        (outcome, Ok(())) => outcome,
        (Ok(()), Err(write)) => Err(write.into()),
        (Err(error), Err(write)) => {
            Err(format!("{error}; writing {} also failed: {write}", log.display()).into())
        }
    }
}

/// The gitleaks release whose rules and flags the published `.gitleaks.toml` is tested with.
pub const GITLEAKS_VERSION: &str = "8.30.1";
/// The snapshot's scan config; public CI scans with the same file.
pub const SCAN_CONFIG: &str = ".gitleaks.toml";
/// Scan the staged public commit before any candidate code runs and before any message is
/// generated or anything is pushed: public history cannot be rewritten, so public CI's scan
/// finds leaks too late. Exemptions come only from the snapshot's config: inline
/// `gitleaks:allow` comments are ignored and a `.gitleaksignore` in the checkout stops
/// publication, as do a missing config, a missing binary or another version.
fn secret_scan(
    gitleaks: &Path,
    repo: &Path,
    config: Option<&Vec<u8>>,
    scratch: &Path,
    state: &Path,
) -> Result<()> {
    let run = || -> Result<(ExitStatus, Vec<u8>)> {
        let config = config.ok_or(format!("Public candidate has no {SCAN_CONFIG}"))?;
        // gitleaks always applies a .gitleaksignore at the root of the scanned folder.
        if repo.join(".gitleaksignore").symlink_metadata().is_ok() {
            return Err(
                "Public candidate has a .gitleaksignore; exemptions belong in .gitleaks.toml"
                    .into(),
            );
        }
        let mut version = candidate_check(gitleaks);
        version.arg("version");
        let (status, out, _) = command(version, Duration::from_secs(30))?;
        let found = String::from_utf8_lossy(&out).trim().to_owned();
        if !status.success() || found != GITLEAKS_VERSION {
            return Err(
                format!("Secret scan needs gitleaks {GITLEAKS_VERSION}, found {found:?}").into(),
            );
        }
        // A folder of its own holds the config and is the ignore path, so it has no
        // .gitleaksignore and nothing the candidate wrote can change either.
        fs::create_dir(scratch)?;
        let config_path = scratch.join(SCAN_CONFIG);
        fs::write(&config_path, config)?;
        // gitleaks reads `git diff`, which shows only "Binary files differ" for a file
        // whose attributes say `-diff` or `binary`, or that holds a NUL byte. These
        // highest-precedence attributes make git show every file's text during the scan.
        let attributes = repo.join(".git/info/attributes");
        fs::create_dir_all(repo.join(".git/info"))?;
        fs::write(&attributes, "* diff\n")?;
        let mut scan = candidate_check(gitleaks);
        scan.args([
            "git",
            "--pre-commit",
            "--staged",
            "--no-banner",
            "--no-color",
            "--verbose",
            "--redact=100",
            "--ignore-gitleaks-allow",
            "--exit-code",
            "1",
        ])
        .arg("--config")
        .arg(&config_path)
        .arg("--gitleaks-ignore-path")
        .arg(scratch)
        .arg(".")
        .current_dir(repo);
        let (status, mut out, err) = command(scan, Duration::from_secs(120))?;
        fs::remove_file(&attributes)?;
        out.extend(err);
        Ok((status, out))
    };
    let (output, outcome) = match run() {
        Ok((status, out)) if status.success() => (out, Ok(())),
        Ok((_, out)) => (out, Err("Public candidate secret scan failed".into())),
        Err(error) => (
            format!("{error}; publication stopped.\n").into_bytes(),
            Err(error),
        ),
    };
    // Redacted findings: rule, file and line, never the matched secret.
    record(&state.join("candidate-secret-scan.log"), &output, outcome)
}

pub fn execute(
    c: &Config,
    publish: bool,
    generate: &dyn Fn(&str, Option<&str>) -> Result<CommitMessage>,
) -> Result<ExportResult> {
    execute_with_destination(c, publish, generate, &c.target_url)
}
fn execute_with_destination(
    c: &Config,
    publish: bool,
    generate: &dyn Fn(&str, Option<&str>) -> Result<CommitMessage>,
    destination: &str,
) -> Result<ExportResult> {
    execute_with_checks(c, publish, generate, destination, Path::new("cargo"))
}
fn execute_with_checks(
    c: &Config,
    publish: bool,
    generate: &dyn Fn(&str, Option<&str>) -> Result<CommitMessage>,
    destination: &str,
    cargo: &Path,
) -> Result<ExportResult> {
    fs::create_dir_all(&c.state_dir)?;
    let mut locks = acquire(&[c.state_dir.join("export.lock")])?;
    if let Some(path) = &c.cycle_lock {
        let file = OpenOptions::new().create(true).append(true).open(path)?;
        lock_file(
            &file,
            Duration::from_secs(300),
            &crate::cancellation::requested,
        )?;
        locks._files.push(file);
    }
    let _locks = locks;
    let local = clean(&c.source_repo)?;
    let mut revision = local.clone();
    if c.fetch_source {
        git(
            &c.source_repo,
            &[
                "fetch",
                "origin",
                "refs/heads/main:refs/remotes/origin/main",
            ],
        )?;
        revision = text(&c.source_repo, &["rev-parse", "refs/remotes/origin/main"])?;
        git(
            &c.source_repo,
            &["merge-base", "--is-ancestor", &local, &revision],
        )?;
    }
    let files = snapshot(c, &revision)?;
    let state_path = c.state_dir.join("export-state.json");
    let state: Option<State> = if state_path.exists() {
        Some(serde_json::from_slice(&fs::read(&state_path)?)?)
    } else {
        None
    };
    if let Some(s) = &state
        && (s.target_url != c.target_url
            || !regex::Regex::new(r"^[0-9a-f]{40,64}$")?.is_match(&s.public_commit))
    {
        return Err("Publication state mismatch".into());
    }
    let tmp = tempfile::tempdir()?;
    let repo = tmp.path().join("checkout");
    fs::create_dir(&repo)?;
    git(&repo, &["init", "--initial-branch=main"])?;
    git(&repo, &["remote", "add", "origin", destination])?;
    git(&repo, &["fetch", "origin"])?;
    let changed = prepare(
        &repo,
        &files,
        state.as_ref().map(|s| s.public_commit.as_str()),
        &imported(&c.source_repo, &revision)?,
    )?;
    let mut result = ExportResult {
        changed,
        file_count: files.len(),
        source_commit: revision.clone(),
        public_commit: None,
        paths: None,
    };
    if !publish {
        result.paths = Some(files.keys().cloned().collect());
        return Ok(result);
    }
    if changed {
        secret_scan(
            &c.gitleaks_path,
            &repo,
            files.get(SCAN_CONFIG),
            &tmp.path().join("secret-scan"),
            &c.state_dir,
        )?;
        let validation = tmp.path().join("validation");
        fs::create_dir(&validation)?;
        for (p, b) in &files {
            let target = validation.join(p);
            fs::create_dir_all(target.parent().ok_or("Parent missing")?)?;
            fs::write(target, b)?;
        }
        let runner = "import os,sys,unittest;sys.path.insert(0,os.getcwd());suite=unittest.defaultTestLoader.discover('tests');count=suite.countTestCases();print('Discovered tests:',count);sys.exit(0 if count and unittest.TextTestRunner().run(suite).wasSuccessful() else 1)";
        let mut cmd = candidate_check(&c.python_path);
        cmd.args(["-I", "-c", runner])
            .current_dir(&validation)
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("UV_OFFLINE", "1");
        let (output, outcome) = match command(cmd, Duration::from_secs(600)) {
            Ok((status, mut out, err)) => {
                out.extend(err);
                let outcome = if status.success() {
                    Ok(())
                } else {
                    Err("Public candidate tests failed".into())
                };
                (out, outcome)
            }
            // The process could not start or could not return its output.
            Err(error) => (
                b"Public candidate test subprocess failed to start or complete; publication stopped.\n"
                    .to_vec(),
                Err(error),
            ),
        };
        record(&c.state_dir.join("candidate-tests.log"), &output, outcome)?;
        if files.contains_key("Cargo.toml") {
            rust_checks(&validation, &c.state_dir, cargo)?;
        }
        let mut diff = git(
            &repo,
            &[
                "diff",
                "--cached",
                "--no-ext-diff",
                "--no-textconv",
                "--",
                ".",
                ":(exclude).public-export.json",
            ],
        )?;
        if diff.is_empty() {
            let staged_paths = git(&repo, &["diff", "--cached", "--name-only", "-z"])?;
            if staged_paths != format!("{MANIFEST}\0").as_bytes() {
                return Err("No meaningful public change is available for generation".into());
            }
            // A fully imported public change can require only new ownership
            // fingerprints. That public bookkeeping diff is the actual change.
            diff = git(
                &repo,
                &[
                    "diff",
                    "--cached",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--",
                    MANIFEST,
                ],
            )?;
        }
        if diff.len() > 512 * 1024 {
            return Err("Public diff exceeds generation limit".into());
        }
        let staged = git(&repo, &["write-tree"])?;
        let refs = git(
            &repo,
            &["for-each-ref", "--format=%(refname) %(objectname)"],
        )?;
        let diff = std::str::from_utf8(&diff)?;
        let mut instruction = None;
        let mut attempt = 1;
        // A scanner rejection regenerates with an instruction; other invalid messages stop.
        let message = loop {
            let message = generate(diff, instruction)?;
            if git(&repo, &["write-tree"])? != staged
                || git(
                    &repo,
                    &["for-each-ref", "--format=%(refname) %(objectname)"],
                )? != refs
                || !git(
                    &repo,
                    &["diff", "--no-ext-diff", "--no-textconv", "--name-only"],
                )?
                .is_empty()
                || !git(&repo, &["ls-files", "--others", "--exclude-standard"])?.is_empty()
                || text(&repo, &["remote", "get-url", "origin"])? != destination
            {
                return Err("Public candidate changed during message generation".into());
            }
            message_quality(&message)?;
            match scan_message(&message) {
                Ok(()) => break message,
                Err(_) if attempt < MESSAGE_ATTEMPTS => {
                    attempt += 1;
                    instruction = Some(SCAN_RETRY);
                }
                Err(error) => {
                    return Err(
                        format!("{error}; all {MESSAGE_ATTEMPTS} generation attempts").into(),
                    );
                }
            }
        };
        if clean(&c.source_repo)? != local
            || (c.fetch_source
                && text(&c.source_repo, &["rev-parse", "refs/remotes/origin/main"])? != revision)
        {
            return Err("Private source changed during publication".into());
        }
        git(&repo, &["config", "user.name", &c.author_name])?;
        git(&repo, &["config", "user.email", &c.author_email])?;
        let message_path = tmp.path().join("message.txt");
        fs::write(
            &message_path,
            format!("{}\n\n{}\n", message.subject, message.body),
        )?;
        git(
            &repo,
            &[
                "-c",
                "commit.gpgSign=false",
                "commit",
                "--file",
                message_path.to_str().ok_or("Invalid message path")?,
            ],
        )?;
        git(&repo, &["push", "origin", "HEAD:refs/heads/main"])?;
    }
    let commit = text(&repo, &["rev-parse", "HEAD"])?;
    save(
        &state_path,
        &State {
            target_url: c.target_url.clone(),
            public_commit: commit.clone(),
            source_commit: revision,
        },
    )?;
    result.public_commit = Some(commit);
    Ok(result)
}
/// Quality, attribution and publication-scanner checks for a generated commit message.
pub fn validate_message(m: &CommitMessage) -> Result<()> {
    message_quality(m)?;
    scan_message(m)
}
fn message_quality(m: &CommitMessage) -> Result<()> {
    let s = m.subject.trim();
    let lower = s.to_lowercase();
    if s.len() < 12
        || s.len() > 100
        || s.chars().any(char::is_control)
        || [
            "update public skills and evolution tools",
            "update files",
            "update skills",
            "update code",
            "changes",
            "misc updates",
        ]
        .contains(&lower.as_str())
        || m.body.len() > 16000
        || m.body
            .chars()
            .any(|c| c.is_control() && !matches!(c, '\n' | '\t'))
    {
        return Err("Uninformative or invalid commit message".into());
    }
    let all = format!("{}\n{}", m.subject, m.body);
    let l = all.to_lowercase();
    if l.contains("co-authored-by:") || l.contains("generated-by:") || l.contains("t3-thread://") {
        return Err("Attribution or session link rejected".into());
    }
    Ok(())
}
fn scan_message(m: &CommitMessage) -> Result<()> {
    scan(
        "commit message",
        format!("{}\n{}", m.subject, m.body).as_bytes(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        Mutex,
        atomic::{AtomicUsize, Ordering},
    };
    #[test]
    fn sha256_preserves_lowercase_zero_padded_hex() {
        assert_eq!(
            hash(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            hash(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }
    fn fixture() -> (tempfile::TempDir, Config, PathBuf) {
        let t = tempfile::tempdir().unwrap();
        let source = t.path().join("source");
        let remote = t.path().join("remote.git");
        fs::create_dir(&source).unwrap();
        git(&source, &["init", "--initial-branch=main"]).unwrap();
        git(&source, &["config", "user.name", "Test"]).unwrap();
        git(&source, &["config", "user.email", "test@example.invalid"]).unwrap();
        for (p, data) in [
            ("skills/guide.md", "Public guide\n"),
            (
                "tests/test_fixture.py",
                "import unittest\nclass Tests(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n",
            ),
            ("feedback/private.md", "Private source context"),
            (
                "public/files/.gitleaks.toml",
                "[extend]\nuseDefault = true\n",
            ),
        ] {
            let p = source.join(p);
            fs::create_dir_all(p.parent().unwrap()).unwrap();
            fs::write(p, data).unwrap();
        }
        git(&source, &["add", "."]).unwrap();
        git(&source, &["commit", "-m", "Private source context"]).unwrap();
        git(
            t.path(),
            &[
                "init",
                "--bare",
                "--initial-branch=main",
                remote.to_str().unwrap(),
            ],
        )
        .unwrap();
        let mut c=Config::from_value(json!({"schema_version":1,"source_repo":source,"state_dir":t.path().join("state"),"author_name":"Test","author_email":"test@example.invalid","target_url":"https://github.com/example/skills.git","files":[{"source":"public/files/.gitleaks.toml","target":".gitleaks.toml"},{"source":"skills/guide.md","target":"skills/guide.md"},{"source":"tests/test_fixture.py","target":"tests/test_fixture.py"}]})).unwrap();
        c.gitleaks_path = fake_gitleaks(t.path(), GITLEAKS_VERSION, 0);
        (t, c, remote)
    }
    /// Write an executable shell script that exits at once when its first argument is
    /// `--probe`, then wait until it can run. A thread that forks while the file is open for
    /// writing holds it until the child execs, and running it before then fails with
    /// "Text file busy".
    fn fake_program(path: &Path, body: &str) {
        fs::write(
            path,
            format!("#!/bin/sh\n[ \"$1\" = --probe ] && exit 0\n{body}"),
        )
        .unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
        while let Err(error) = Command::new(path).arg("--probe").status() {
            assert_eq!(
                error.kind(),
                std::io::ErrorKind::ExecutableFileBusy,
                "{error}"
            );
            thread::sleep(Duration::from_millis(10));
        }
    }
    /// A gitleaks stand-in that reports `version`, records every scan's arguments in
    /// `gitleaks-calls` and the scan's git attributes in `gitleaks-attributes` beside it,
    /// and exits with `scan_exit`.
    fn fake_gitleaks(dir: &Path, version: &str, scan_exit: i32) -> PathBuf {
        let path = dir.join("gitleaks");
        let calls = dir.join("gitleaks-calls");
        let attributes = dir.join("gitleaks-attributes");
        fake_program(
            &path,
            &format!(
                "if [ \"$1\" = version ]; then echo {version}; exit 0; fi\necho \"$*\" >> '{}'\ncat .git/info/attributes > '{}'\necho 'finding: README.md'\nexit {scan_exit}\n",
                calls.display(),
                attributes.display()
            ),
        );
        path
    }
    fn scan_calls(dir: &Path) -> Vec<String> {
        fs::read_to_string(dir.join("gitleaks-calls"))
            .map(|calls| calls.lines().map(str::to_owned).collect())
            .unwrap_or_default()
    }
    fn assert_scan_blocked(config: &Config, remote: &Path, reason: &str) {
        let Err(error) = execute_with_destination(
            config,
            true,
            &|_, _| panic!("A failed secret scan must precede generation"),
            remote.to_str().unwrap(),
        ) else {
            panic!("The export published despite the secret scan");
        };
        let error = error.to_string();
        assert!(error.contains(reason), "{error}");
        assert!(git(remote, &["for-each-ref"]).unwrap().is_empty());
        assert!(!config.state_dir.join("export-state.json").exists());
    }
    #[test]
    fn secret_scan_checks_the_staged_candidate_before_generation_and_noop_skips_it() {
        let (temp, config, remote) = fixture();
        let generate = |_: &str, _: Option<&str>| {
            assert_eq!(scan_calls(temp.path()).len(), 1);
            Ok(message())
        };
        execute_with_destination(&config, true, &generate, remote.to_str().unwrap()).unwrap();
        let calls = scan_calls(temp.path());
        assert_eq!(calls.len(), 1);
        let args: Vec<&str> = calls[0].split(' ').collect();
        assert_eq!(
            &args[..10],
            [
                "git",
                "--pre-commit",
                "--staged",
                "--no-banner",
                "--no-color",
                "--verbose",
                "--redact=100",
                "--ignore-gitleaks-allow",
                "--exit-code",
                "1"
            ]
        );
        // The config and the ignore path are a folder of their own, not a checkout's.
        assert_eq!(args[10], "--config");
        assert!(
            args[11].ends_with("/secret-scan/.gitleaks.toml"),
            "{}",
            args[11]
        );
        assert_eq!(args[12], "--gitleaks-ignore-path");
        assert_eq!(Path::new(args[13]), Path::new(args[11]).parent().unwrap());
        assert_eq!(args[14], ".");
        assert_eq!(
            fs::read_to_string(temp.path().join("gitleaks-attributes")).unwrap(),
            "* diff\n"
        );
        assert_eq!(
            fs::read_to_string(config.state_dir.join("candidate-secret-scan.log")).unwrap(),
            "finding: README.md\n"
        );
        execute_with_destination(&config, true, &generate, remote.to_str().unwrap()).unwrap();
        assert_eq!(scan_calls(temp.path()), calls);
    }
    #[test]
    fn secret_scan_failure_blocks_generation_and_push() {
        let (temp, mut config, remote) = fixture();
        config.gitleaks_path = fake_gitleaks(temp.path(), GITLEAKS_VERSION, 1);
        assert_scan_blocked(&config, &remote, "Public candidate secret scan failed");
        assert_eq!(scan_calls(temp.path()).len(), 1);
        assert_eq!(
            fs::read_to_string(config.state_dir.join("candidate-secret-scan.log")).unwrap(),
            "finding: README.md\n"
        );
        // The scan comes first, so no candidate code ran.
        assert!(!config.state_dir.join("candidate-tests.log").exists());
    }
    #[test]
    fn a_gitleaksignore_in_the_candidate_stops_publication() {
        let (temp, mut config, remote) = fixture();
        let ignore = config.source_repo.join("public/files/.gitleaksignore");
        fs::write(&ignore, "skills/guide.md:generic-api-key:2\n").unwrap();
        git(&config.source_repo, &["add", "."]).unwrap();
        git(&config.source_repo, &["commit", "-m", "Ignore"]).unwrap();
        config.files.push(crate::config::Entry {
            source: "public/files/.gitleaksignore".into(),
            target: ".gitleaksignore".into(),
        });
        assert_scan_blocked(&config, &remote, "Public candidate has a .gitleaksignore");
        assert!(scan_calls(temp.path()).is_empty());
    }
    #[test]
    fn secret_scan_refuses_another_version_a_missing_binary_or_config() {
        let (temp, mut config, remote) = fixture();
        let log = config.state_dir.join("candidate-secret-scan.log");
        fs::create_dir_all(&config.state_dir).unwrap();
        fs::write(&log, "stale success").unwrap();
        config.gitleaks_path = fake_gitleaks(temp.path(), "8.31.0", 0);
        assert_scan_blocked(&config, &remote, "needs gitleaks 8.30.1, found \"8.31.0\"");
        assert!(scan_calls(temp.path()).is_empty());
        assert!(
            fs::read_to_string(&log)
                .unwrap()
                .ends_with("publication stopped.\n")
        );

        fs::write(&log, "stale success").unwrap();
        config.gitleaks_path = temp.path().join("missing-gitleaks");
        assert_scan_blocked(&config, &remote, "Unable to start subprocess");
        assert_eq!(
            fs::read_to_string(&log).unwrap(),
            "Unable to start subprocess; publication stopped.\n"
        );

        config.gitleaks_path = fake_gitleaks(temp.path(), GITLEAKS_VERSION, 0);
        config.files.retain(|entry| entry.target != SCAN_CONFIG);
        assert_scan_blocked(&config, &remote, "Public candidate has no .gitleaks.toml");
        assert!(scan_calls(temp.path()).is_empty());
    }
    #[test]
    fn a_failed_log_write_keeps_the_check_outcome() {
        let temp = tempfile::tempdir().unwrap();
        let log = temp.path().join("missing/candidate.log");
        let error = record(
            &log,
            b"out",
            Err("Public candidate secret scan failed".into()),
        )
        .unwrap_err()
        .to_string();
        assert!(
            error.starts_with("Public candidate secret scan failed; writing ")
                && error.contains("also failed"),
            "{error}"
        );
        assert!(record(&log, b"out", Ok(())).is_err());
        let log = temp.path().join("candidate.log");
        let output = vec![b'x'; 70_000];
        record(&log, &output, Ok(())).unwrap();
        assert_eq!(fs::read(&log).unwrap().len(), 65536);
    }
    /// The real scanner, when `GITLEAKS` names gitleaks 8.30.1: a planted key in a
    /// managed file stops the export, and the clean candidate then publishes.
    #[test]
    fn real_gitleaks_stops_a_planted_key_before_push() {
        let Some(binary) = std::env::var_os("GITLEAKS") else {
            eprintln!("skipped: set GITLEAKS to gitleaks {GITLEAKS_VERSION}");
            return;
        };
        let (_temp, mut config, remote) = fixture();
        config.gitleaks_path = binary.into();
        // A key the exporter's own pattern scan does not know, so only gitleaks stops it.
        let key = hash(b"synthetic credential");
        let guide = config.source_repo.join("skills/guide.md");
        fs::write(&guide, format!("Public guide\napi_key = \"{key}\"\n")).unwrap();
        git(&config.source_repo, &["commit", "-am", "Plant"]).unwrap();
        assert_scan_blocked(&config, &remote, "Public candidate secret scan failed");
        let log = fs::read_to_string(config.state_dir.join("candidate-secret-scan.log")).unwrap();
        assert!(
            log.contains("generic-api-key") && !log.contains(&key) && !log.contains('\x1b'),
            "{log}"
        );
        // Neither attributes that hide a file's diff nor a NUL byte hide the key.
        let attributes = config.source_repo.join("public/files/.gitattributes");
        fs::write(&attributes, "*.md -diff\n").unwrap();
        git(&config.source_repo, &["add", "."]).unwrap();
        git(&config.source_repo, &["commit", "-m", "Attributes"]).unwrap();
        config.files.push(crate::config::Entry {
            source: "public/files/.gitattributes".into(),
            target: ".gitattributes".into(),
        });
        assert_scan_blocked(&config, &remote, "Public candidate secret scan failed");
        fs::write(&attributes, "").unwrap();
        fs::write(&guide, format!("Public guide\0\napi_key = \"{key}\"\n")).unwrap();
        git(&config.source_repo, &["commit", "-am", "Binary"]).unwrap();
        assert_scan_blocked(&config, &remote, "Public candidate secret scan failed");
        fs::write(&guide, "Public guide\n").unwrap();
        git(&config.source_repo, &["commit", "-am", "Remove"]).unwrap();
        let generate = |_: &str, _: Option<&str>| Ok(message());
        assert!(
            execute_with_destination(&config, true, &generate, remote.to_str().unwrap())
                .unwrap()
                .changed
        );
    }
    /// Private identifiers and credentials the scanner rejects, built so this file passes it.
    /// Case variants are included: replacements match exactly, so these stay unreplaced.
    fn private_samples() -> [String; 12] {
        [
            ["-----BEGIN ENCRYPTED ", "PRIVATE KEY-----"].concat(),
            ["-----BEGIN PGP ", "PRIVATE KEY BLOCK-----"].concat(),
            ["/Us", "ers/alice/x"].concat(),
            ["roy", "-mac"].concat(),
            ["syz51/", "codex-skills"].concat(),
            format!("ghp_{}", "a".repeat(36)),
            ["-----BEGIN ", "PRIVATE KEY-----"].concat(),
            ["Roy", "-Mac"].concat(),
            ["SunMacBook", "-Air"].concat(),
            ["SYZ51/", "Codex-Skills"].concat(),
            ["/us", "ers/alice"].concat(),
            ["(/US", "ERS/alice/x)"].concat(),
        ]
    }
    #[test]
    fn scanner_markers_ignore_case_but_web_routes_pass() {
        for sample in private_samples() {
            assert!(scan("guide.md", sample.as_bytes()).is_err(), "{sample}");
        }
        for route in [
            "path: '/users/:id'",
            "`/users/${id}` and `/users/1`",
            "server/api/users/[id].put.ts - PUT /api/users/:id",
            "https://api.github.com/users/octocat",
            "pages/users/profile.vue",
        ] {
            assert!(scan("guide.md", route.as_bytes()).is_ok(), "{route}");
        }
    }
    #[test]
    fn snapshot_scans_files_after_replacements_and_drops() {
        let (_t, mut c, _remote) = fixture();
        let host = ["sunmacbook", "-air"].concat();
        c.replacements.insert(host.clone(), json!("client-host"));
        c.json_drop_keys = vec!["backupPath".into()];
        c.files.push(crate::config::Entry {
            source: "skills/sources.json".into(),
            target: "skills/sources.json".into(),
        });
        // Replacements and json_drop_keys run before the scan, so these publish.
        let guide = format!("Checked on {host} with FAKE_TOKEN.\n");
        fs::write(c.source_repo.join("skills/guide.md"), &guide).unwrap();
        let backup = ["/Us", "ers/alice/backup"].concat();
        fs::write(
            c.source_repo.join("skills/sources.json"),
            json!({"backupPath": backup, "items": [{"backupPath": "x", "license": "MIT"}]})
                .to_string(),
        )
        .unwrap();
        git(&c.source_repo, &["add", "."]).unwrap();
        git(
            &c.source_repo,
            &["commit", "-m", "Add replaced identifiers"],
        )
        .unwrap();
        let files = snapshot(&c, "HEAD").unwrap();
        assert_eq!(
            String::from_utf8(files["skills/guide.md"].clone()).unwrap(),
            guide.replace(&host, "client-host")
        );
        let metadata: Value = serde_json::from_slice(&files["skills/sources.json"]).unwrap();
        assert_eq!(metadata, json!({"items": [{"license": "MIT"}]}));
        // A local path, a hostname no replacement covers, the private repository slug,
        // a token or a private key fails the snapshot.
        for sample in private_samples() {
            fs::write(
                c.source_repo.join("skills/guide.md"),
                format!("See {sample}\n"),
            )
            .unwrap();
            git(
                &c.source_repo,
                &["commit", "-am", "Add a private identifier"],
            )
            .unwrap();
            let error = snapshot(&c, "HEAD").unwrap_err().to_string();
            assert!(
                error.contains("Publication rejected: skills/guide.md"),
                "{error}"
            );
        }
    }
    #[test]
    fn commit_messages_reject_identifiers_credentials_attribution_and_low_quality() {
        let kept = CommitMessage {
            subject: "Document the maintainer host role".into(),
            body: "Mention the client-host reference and FAKE_TOKEN.".into(),
        };
        assert!(validate_message(&kept).is_ok());
        for sample in private_samples() {
            for m in [
                CommitMessage {
                    subject: format!("Document {sample}"),
                    body: kept.body.clone(),
                },
                CommitMessage {
                    subject: kept.subject.clone(),
                    body: format!("Mention {sample}."),
                },
            ] {
                let error = validate_message(&m).unwrap_err().to_string();
                assert!(
                    error.contains("Publication rejected: commit message"),
                    "{error}"
                );
            }
        }
        for body in [
            "Co-authored-by: Someone <a@example.invalid>",
            "Generated-by: tool",
            "See t3-thread://abc",
        ] {
            let m = CommitMessage {
                subject: kept.subject.clone(),
                body: body.into(),
            };
            assert!(validate_message(&m).is_err());
        }
        assert!(
            validate_message(&CommitMessage {
                subject: "changes".into(),
                body: String::new()
            })
            .is_err()
        );
    }
    fn rust_fixture(fail: &str) -> (tempfile::TempDir, Config, PathBuf, PathBuf) {
        let (temp, mut config, remote) = fixture();
        fs::write(
            config.source_repo.join("Cargo.toml"),
            "[package]\nname = \"fixture\"\nversion = \"0.1.0\"\n",
        )
        .unwrap();
        config.files.push(crate::config::Entry {
            source: "Cargo.toml".into(),
            target: "Cargo.toml".into(),
        });
        git(&config.source_repo, &["add", "."]).unwrap();
        git(&config.source_repo, &["commit", "-m", "Add Rust fixture"]).unwrap();
        let cargo = temp.path().join("cargo");
        let log = temp.path().join("calls");
        fake_program(
            &cargo,
            &format!(
                "echo \"$*\" >> '{}'\n[ \"$1\" != '{}' ]\n",
                log.display(),
                fail
            ),
        );
        (temp, config, remote, cargo)
    }
    #[test]
    fn rust_candidate_checks_run_before_push_and_noop_skips_them() {
        let (temp, config, remote, cargo) = rust_fixture("none");
        let generate = |_: &str, _: Option<&str>| Ok(message());
        execute_with_checks(&config, true, &generate, remote.to_str().unwrap(), &cargo).unwrap();
        let calls = fs::read_to_string(temp.path().join("calls")).unwrap();
        assert_eq!(
            calls.lines().collect::<Vec<_>>(),
            vec![
                "fmt --all -- --check",
                "clippy --all-targets --locked -- -D warnings",
                "test --locked"
            ]
        );
        assert!(
            !execute_with_checks(&config, true, &generate, remote.to_str().unwrap(), &cargo)
                .unwrap()
                .changed
        );
        assert_eq!(
            fs::read_to_string(temp.path().join("calls")).unwrap(),
            calls
        );
    }
    #[test]
    fn every_failed_rust_stage_blocks_generation_and_push() {
        for stage in ["fmt", "clippy", "test"] {
            let (_temp, config, remote, cargo) = rust_fixture(stage);
            let result = execute_with_checks(
                &config,
                true,
                &|_, _| panic!("Rust failure must precede generation"),
                remote.to_str().unwrap(),
                &cargo,
            );
            assert!(result.is_err());
            assert!(git(&remote, &["for-each-ref"]).unwrap().is_empty());
            assert!(
                fs::read_to_string(config.state_dir.join("candidate-rust-tests.log"))
                    .unwrap()
                    .contains(stage)
            );
        }
    }
    #[test]
    fn candidate_checks_never_inherit_the_live_opt_in() {
        // Setting the variable in this multithreaded process is unsound, so rerun this
        // test alone in a child process that inherits RUN_CLAUDE_LIVE=1.
        if std::env::var_os(LIVE_TEST_OPT_IN).is_none() {
            let status = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "export::tests::candidate_checks_never_inherit_the_live_opt_in",
                    "--test-threads=1",
                ])
                .env(LIVE_TEST_OPT_IN, "1")
                .status()
                .unwrap();
            assert!(status.success());
            return;
        }
        let (temp, config, remote, cargo) = rust_fixture("none");
        fs::write(
            config.source_repo.join("tests/test_fixture.py"),
            "import os, unittest\nclass Tests(unittest.TestCase):\n def test_offline(self): self.assertNotIn('RUN_CLAUDE_LIVE', os.environ)\n",
        )
        .unwrap();
        git(&config.source_repo, &["commit", "-am", "Check offline"]).unwrap();
        let log = temp.path().join("calls");
        fs::write(
            &cargo,
            format!(
                "#!/bin/sh\necho \"$* ${{RUN_CLAUDE_LIVE-offline}}\" >> '{}'\n",
                log.display()
            ),
        )
        .unwrap();
        execute_with_checks(
            &config,
            true,
            &|_, _| Ok(message()),
            remote.to_str().unwrap(),
            &cargo,
        )
        .unwrap_or_else(|error| {
            let tests = fs::read_to_string(config.state_dir.join("candidate-tests.log"));
            panic!("{error}: {tests:?}")
        });
        let calls = fs::read_to_string(log).unwrap();
        assert_eq!(calls.lines().count(), 3, "{calls}");
        assert!(
            calls.lines().all(|line| line.ends_with(" offline")),
            "{calls}"
        );
    }
    #[test]
    fn non_rust_candidate_never_invokes_cargo() {
        let (_temp, config, remote) = fixture();
        execute_with_checks(
            &config,
            true,
            &|_, _| Ok(message()),
            remote.to_str().unwrap(),
            Path::new("/missing-cargo"),
        )
        .unwrap();
        assert!(!config.state_dir.join("candidate-rust-tests.log").exists());
    }
    #[test]
    fn dropping_lock_releases_inherited_open_file_description() {
        let temp = tempfile::tempdir().unwrap();
        let paths = [temp.path().join("job.lock")];
        let locks = acquire(&paths).unwrap();
        let inherited = locks._files[0].try_clone().unwrap();
        assert!(acquire(&paths).is_err());
        drop(locks);
        let next = acquire(&paths).unwrap();
        drop(inherited);
        assert!(acquire(&paths).is_err());
        drop(next);
        assert!(acquire(&paths).is_ok());
    }
    #[test]
    fn shared_lock_waits_for_release_and_times_out_when_held() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("cycle.lock");
        let owner = acquire(std::slice::from_ref(&path)).unwrap();
        let file = OpenOptions::new().append(true).open(&path).unwrap();
        let error = lock_file(&file, Duration::from_millis(30), &|| false).unwrap_err();
        assert!(error.is::<LockBusy>());
        let releaser = thread::spawn(move || {
            thread::sleep(Duration::from_millis(30));
            drop(owner);
        });
        lock_file(&file, Duration::from_secs(2), &|| false).unwrap();
        releaser.join().unwrap();
        assert!(acquire(std::slice::from_ref(&path)).is_err());
        FileExt::unlock(&file).unwrap();
        assert!(acquire(&[path]).is_ok());
    }

    #[test]
    fn shared_lock_wait_checks_cancellation() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("cycle.lock");
        let _owner = acquire(std::slice::from_ref(&path)).unwrap();
        let file = OpenOptions::new().append(true).open(path).unwrap();
        let start = Instant::now();
        let error = lock_file(&file, Duration::from_secs(300), &|| {
            start.elapsed() >= Duration::from_millis(30)
        })
        .unwrap_err();
        assert_eq!(error.to_string(), "Publication cancelled");
        assert!(!error.is::<LockBusy>());
        assert!(start.elapsed() < Duration::from_secs(2));
    }

    #[test]
    fn command_deadline_covers_exited_leader_with_inherited_pipes() {
        let temp = tempfile::tempdir().unwrap();
        let pid_file = temp.path().join("pid");
        let mut cmd = Command::new("/bin/sh");
        cmd.args(["-c", "sleep 30 & echo $! > \"$1\"; exit 0", "sh"])
            .arg(&pid_file);
        let start = Instant::now();
        assert!(command(cmd, Duration::from_millis(100)).is_err());
        assert!(start.elapsed() < Duration::from_secs(3));
        let pid = fs::read_to_string(pid_file).unwrap();
        let status = Command::new("ps")
            .args(["-o", "stat=", "-p", pid.trim()])
            .output()
            .unwrap();
        let state = String::from_utf8(status.stdout).unwrap();
        assert!(
            state.trim().is_empty() || state.trim().starts_with('Z'),
            "Background process still running: {state}"
        );
    }
    fn message() -> CommitMessage {
        CommitMessage {
            subject: "Document public guidance and its regression check".into(),
            body: "Include the installable guide with an offline fixture.".into(),
        }
    }
    #[test]
    fn real_transaction_keeps_history_private_and_noop_skips_generation() {
        let (_t, c, remote) = fixture();
        let calls = AtomicUsize::new(0);
        let generate = |diff: &str, _: Option<&str>| {
            calls.fetch_add(1, Ordering::SeqCst);
            assert!(!diff.contains("Private source context"));
            assert!(!diff.contains(MANIFEST));
            Ok(message())
        };
        let first =
            execute_with_destination(&c, true, &generate, remote.to_str().unwrap()).unwrap();
        assert!(first.changed);
        assert!(
            !execute_with_destination(&c, true, &generate, remote.to_str().unwrap())
                .unwrap()
                .changed
        );
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        assert_eq!(
            text(&remote, &["rev-list", "--count", "main"]).unwrap(),
            "1"
        );
        assert!(
            !text(&remote, &["log", "--format=%B", "main"])
                .unwrap()
                .contains("Private source context")
        );
    }
    #[test]
    fn publication_continues_after_shared_cycle_lock_is_released() {
        let (temp, mut config, remote) = fixture();
        let cycle = temp.path().join("cycle.lock");
        config.cycle_lock = Some(cycle.clone());
        let owner = acquire(&[cycle]).unwrap();
        let calls = AtomicUsize::new(0);
        let result = thread::scope(|scope| {
            scope.spawn(move || {
                thread::sleep(Duration::from_millis(100));
                drop(owner);
            });
            execute_with_destination(
                &config,
                true,
                &|_, _| {
                    calls.fetch_add(1, Ordering::SeqCst);
                    Ok(message())
                },
                remote.to_str().unwrap(),
            )
            .unwrap()
        });
        assert!(result.changed);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        let commit = result.public_commit.unwrap();
        assert_eq!(text(&remote, &["rev-parse", "HEAD"]).unwrap(), commit);
        let state: Value =
            serde_json::from_slice(&fs::read(config.state_dir.join("export-state.json")).unwrap())
                .unwrap();
        assert_eq!(state["public_commit"], commit);
        assert_eq!(state["source_commit"], result.source_commit);
    }

    #[test]
    fn python_subprocess_failure_replaces_stale_success_log_and_never_pushes() {
        let (temp, mut config, remote) = fixture();
        fs::create_dir_all(&config.state_dir).unwrap();
        let log = config.state_dir.join("candidate-tests.log");
        fs::write(&log, "Old attempt: all tests passed\n").unwrap();
        config.python_path = temp.path().join("missing-private-python");
        assert!(
            execute_with_destination(
                &config,
                true,
                &|_, _| panic!("Must not generate after subprocess failure"),
                remote.to_str().unwrap()
            )
            .is_err()
        );
        assert_eq!(
            fs::read_to_string(log).unwrap(),
            "Public candidate test subprocess failed to start or complete; publication stopped.\n"
        );
        assert!(git(&remote, &["for-each-ref"]).unwrap().is_empty());
    }
    #[test]
    fn generation_and_test_failures_never_push() {
        let (_t, c, remote) = fixture();
        assert!(
            execute_with_destination(
                &c,
                true,
                &|_, _| Err("Generation unavailable".into()),
                remote.to_str().unwrap()
            )
            .is_err()
        );
        assert!(git(&remote, &["for-each-ref"]).unwrap().is_empty());
        fs::write(
            c.source_repo.join("tests/test_fixture.py"),
            "raise RuntimeError('fixture failure')\n",
        )
        .unwrap();
        git(&c.source_repo, &["add", "."]).unwrap();
        git(&c.source_repo, &["commit", "-m", "Private fixture update"]).unwrap();
        assert!(
            execute_with_destination(
                &c,
                true,
                &|_, _| panic!("Must not generate after failed check"),
                remote.to_str().unwrap()
            )
            .is_err()
        );
        assert!(git(&remote, &["for-each-ref"]).unwrap().is_empty());
    }
    #[test]
    fn scanner_rejection_regenerates_the_message_with_an_instruction() {
        let (_t, c, remote) = fixture();
        let host = ["Roy", "-Mac"].concat();
        let instructions = Mutex::new(Vec::new());
        let generate = |_: &str, instruction: Option<&str>| {
            let mut seen = instructions.lock().unwrap();
            seen.push(instruction.map(str::to_owned));
            Ok(if seen.len() < MESSAGE_ATTEMPTS {
                CommitMessage {
                    subject: format!("Document the {host} client setup"),
                    body: String::new(),
                }
            } else {
                message()
            })
        };
        assert!(
            execute_with_destination(&c, true, &generate, remote.to_str().unwrap())
                .unwrap()
                .changed
        );
        let retry = Some(SCAN_RETRY.to_owned());
        assert_eq!(*instructions.lock().unwrap(), [None, retry.clone(), retry]);
        assert_eq!(
            text(&remote, &["log", "--format=%s", "main"]).unwrap(),
            message().subject
        );
    }
    #[test]
    fn message_generation_stops_after_bounded_scanner_rejections() {
        let (_t, c, remote) = fixture();
        let calls = AtomicUsize::new(0);
        let private = |_: &str, _: Option<&str>| {
            calls.fetch_add(1, Ordering::SeqCst);
            Ok(CommitMessage {
                subject: format!("Document {}", ["/Us", "ers/alice/x"].concat()),
                body: String::new(),
            })
        };
        let Err(error) = execute_with_destination(&c, true, &private, remote.to_str().unwrap())
        else {
            panic!("A private message must not publish");
        };
        let error = error.to_string();
        assert!(
            error.contains("Publication rejected: commit message"),
            "{error}"
        );
        assert_eq!(calls.load(Ordering::SeqCst), MESSAGE_ATTEMPTS);
        assert!(git(&remote, &["for-each-ref"]).unwrap().is_empty());
        // Only scanner rejections regenerate; an uninformative message stops at once.
        calls.store(0, Ordering::SeqCst);
        let generic = |_: &str, _: Option<&str>| {
            calls.fetch_add(1, Ordering::SeqCst);
            Ok(CommitMessage {
                subject: "changes".into(),
                body: String::new(),
            })
        };
        assert!(execute_with_destination(&c, true, &generic, remote.to_str().unwrap()).is_err());
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        assert!(git(&remote, &["for-each-ref"]).unwrap().is_empty());
    }
    #[test]
    fn source_mutation_during_generation_blocks_push() {
        let (_t, c, remote) = fixture();
        assert!(
            execute_with_destination(
                &c,
                true,
                &|_, _| {
                    fs::write(c.source_repo.join("skills/guide.md"), "Concurrent edit\n")?;
                    Ok(message())
                },
                remote.to_str().unwrap()
            )
            .is_err()
        );
        assert!(git(&remote, &["for-each-ref"]).unwrap().is_empty());
    }

    #[test]
    fn imported_edit_deletion_and_enrollment_generate_public_bookkeeping_diff() {
        for case in ["edit", "delete", "enroll"] {
            let (t, mut c, remote) = fixture();
            execute_with_destination(&c, true, &|_, _| Ok(message()), remote.to_str().unwrap())
                .unwrap();
            let community = t.path().join("community");
            git(
                t.path(),
                &[
                    "clone",
                    remote.to_str().unwrap(),
                    community.to_str().unwrap(),
                ],
            )
            .unwrap();
            git(&community, &["config", "user.name", "Community"]).unwrap();
            git(
                &community,
                &["config", "user.email", "community@example.invalid"],
            )
            .unwrap();
            match case {
                "edit" => {
                    for root in [&community, &c.source_repo] {
                        fs::write(root.join("skills/guide.md"), "Community improvement\n").unwrap();
                    }
                }
                "delete" => {
                    for root in [&community, &c.source_repo] {
                        fs::remove_file(root.join("skills/guide.md")).unwrap();
                    }
                    c.files.retain(|e| e.source != "skills/guide.md");
                }
                "enroll" => {
                    for root in [&community, &c.source_repo] {
                        fs::write(root.join("skills/community.md"), "Community guide\n").unwrap();
                    }
                    c.files.push(crate::config::Entry {
                        source: "skills/community.md".into(),
                        target: "skills/community.md".into(),
                    });
                }
                _ => unreachable!(),
            }
            for root in [&community, &c.source_repo] {
                git(root, &["add", "--all"]).unwrap();
                git(root, &["commit", "-m", "Import community guidance"]).unwrap();
            }
            git(&community, &["push", "origin", "main"]).unwrap();
            let generate = |diff: &str, _: Option<&str>| {
                assert!(!diff.is_empty());
                assert!(diff.contains(MANIFEST));
                assert!(!diff.contains("Private source context"));
                Ok(CommitMessage {
                    subject: "Reconcile public guidance fingerprints after import".into(),
                    body: format!("Record the public {case} in the managed export inventory."),
                })
            };
            assert!(
                execute_with_destination(&c, true, &generate, remote.to_str().unwrap())
                    .unwrap()
                    .changed
            );
            assert!(
                !execute_with_destination(
                    &c,
                    true,
                    &|_, _| panic!("no-op must not generate"),
                    remote.to_str().unwrap()
                )
                .unwrap()
                .changed
            );
        }
    }
}
