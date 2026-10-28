use crate::{
    CommitMessage, Result,
    config::{Config, identity, relative},
};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::os::unix::{fs::PermissionsExt, process::CommandExt};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

pub const MANIFEST: &str = ".public-export.json";
pub type Files = BTreeMap<String, Vec<u8>>;
pub struct Locks {
    _files: Vec<File>,
}
pub fn acquire(paths: &[PathBuf]) -> Result<Locks> {
    let mut files = Vec::new();
    for p in paths {
        let f = OpenOptions::new().create(true).append(true).open(p)?;
        f.try_lock_exclusive()
            .map_err(|_| "Publication deferred: job lock held")?;
        files.push(f);
    }
    Ok(Locks { _files: files })
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
    format!("{:x}", Sha256::digest(data))
}
fn command(mut cmd: Command, timeout: Duration) -> Result<(bool, Vec<u8>, Vec<u8>)> {
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
                    let _ = Command::new("/bin/kill")
                        .args(["-KILL", &format!("-{}", child.id())])
                        .stdout(Stdio::null())
                        .stderr(Stdio::null())
                        .status();
                    let _ = child.kill();
                    let _ = child.wait();
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
            let _ = Command::new("/bin/kill")
                .args(["-KILL", &format!("-{}", child.id())])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
            let _ = child.kill();
            let _ = child.wait();
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
        status.ok_or("Missing child status")?.success(),
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
    let (ok, out, _) = command(c, Duration::from_secs(120))?;
    if !ok {
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
    let markers = [
        "/Us".to_owned() + "ers/",
        "roy".to_owned() + "-mac",
        "sunmacbook".to_owned() + "-air",
        "syz51/".to_owned() + "codex-skills",
    ];
    let secret = regex::Regex::new(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-(?:proj-)?[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16})\b",
    )?;
    if markers.iter().any(|m| s.contains(m)) || secret.is_match(s) {
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
pub fn prepare(repo: &Path, files: &Files, previous: Option<&str>) -> Result<bool> {
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
                if current.get(p) != trusted.get(p)
                    && !((!current.contains_key(p) && !files.contains_key(p))
                        || files
                            .get(p)
                            .is_some_and(|b| matches(repo, "HEAD", p, b, &current)))
                {
                    return Err("Public managed files changed; import first".into());
                }
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
pub fn execute(
    c: &Config,
    publish: bool,
    generate: &dyn Fn(&str) -> Result<CommitMessage>,
) -> Result<ExportResult> {
    execute_with_destination(c, publish, generate, &c.target_url)
}
fn execute_with_destination(
    c: &Config,
    publish: bool,
    generate: &dyn Fn(&str) -> Result<CommitMessage>,
    destination: &str,
) -> Result<ExportResult> {
    fs::create_dir_all(&c.state_dir)?;
    let mut paths = vec![c.state_dir.join("export.lock")];
    if let Some(p) = &c.cycle_lock {
        paths.push(p.clone())
    }
    let _locks = acquire(&paths)?;
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
    if let Some(s) = &state {
        if s.target_url != c.target_url
            || !regex::Regex::new(r"^[0-9a-f]{40,64}$")?.is_match(&s.public_commit)
        {
            return Err("Publication state mismatch".into());
        }
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
        let validation = tmp.path().join("validation");
        fs::create_dir(&validation)?;
        for (p, b) in &files {
            let target = validation.join(p);
            fs::create_dir_all(target.parent().ok_or("Parent missing")?)?;
            fs::write(target, b)?;
        }
        let runner = "import os,sys,unittest;sys.path.insert(0,os.getcwd());suite=unittest.defaultTestLoader.discover('tests');count=suite.countTestCases();print('Discovered tests:',count);sys.exit(0 if count and unittest.TextTestRunner().run(suite).wasSuccessful() else 1)";
        let mut cmd = Command::new(&c.python_path);
        cmd.args(["-I", "-c", runner])
            .current_dir(&validation)
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("UV_OFFLINE", "1");
        let (ok, mut out, err) = command(cmd, Duration::from_secs(600))?;
        out.extend(err);
        fs::write(
            c.state_dir.join("candidate-tests.log"),
            &out[out.len().saturating_sub(65536)..],
        )?;
        if !ok {
            return Err("Public candidate tests failed".into());
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
        let message = generate(std::str::from_utf8(&diff)?)?;
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
        validate_message(&message)?;
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
pub fn validate_message(m: &CommitMessage) -> Result<()> {
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
    scan("commit message", all.as_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
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
        let c=Config::from_value(json!({"schema_version":1,"source_repo":source,"state_dir":t.path().join("state"),"author_name":"Test","author_email":"test@example.invalid","target_url":"https://github.com/example/skills.git","files":[{"source":"skills/guide.md","target":"skills/guide.md"},{"source":"tests/test_fixture.py","target":"tests/test_fixture.py"}]})).unwrap();
        (t, c, remote)
    }
    #[test]
    fn command_deadline_covers_exited_leader_with_inherited_pipes() {
        let mut cmd = Command::new("/bin/sh");
        cmd.args(["-c", "sleep 30 & exit 0"]);
        let start = Instant::now();
        assert!(command(cmd, Duration::from_millis(100)).is_err());
        assert!(start.elapsed() < Duration::from_secs(3));
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
        let generate = |diff: &str| {
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
    fn generation_and_test_failures_never_push() {
        let (_t, c, remote) = fixture();
        assert!(
            execute_with_destination(
                &c,
                true,
                &|_| Err("Generation unavailable".into()),
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
                &|_| panic!("Must not generate after failed check"),
                remote.to_str().unwrap()
            )
            .is_err()
        );
        assert!(git(&remote, &["for-each-ref"]).unwrap().is_empty());
    }
    #[test]
    fn source_mutation_during_generation_blocks_push() {
        let (_t, c, remote) = fixture();
        assert!(
            execute_with_destination(
                &c,
                true,
                &|_| {
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
            execute_with_destination(&c, true, &|_| Ok(message()), remote.to_str().unwrap())
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
            let generate = |diff: &str| {
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
                    &|_| panic!("no-op must not generate"),
                    remote.to_str().unwrap()
                )
                .unwrap()
                .changed
            );
        }
    }
}
