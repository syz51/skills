use codex_skills::{
    CommitMessage,
    config::{Config, relative},
    export::{Files, Imported, git, imported, manifest, prepare, scan, snapshot, validate_message},
};
use std::{fs, path::Path};
fn g(p: &Path, args: &[&str]) -> String {
    String::from_utf8(git(p, args).unwrap())
        .unwrap()
        .trim()
        .to_owned()
}
fn init(p: &Path) {
    fs::create_dir_all(p).unwrap();
    g(p, &["init", "--initial-branch=main"]);
    g(p, &["config", "user.name", "Test"]);
    g(p, &["config", "user.email", "test@example.invalid"]);
}
fn files() -> Files {
    [("skills/guide.md".into(), b"Public guide\n".to_vec())].into()
}
fn commit(p: &Path) {
    g(p, &["add", "--all"]);
    g(p, &["commit", "-m", "Public test change"]);
}
fn remote_ref(p: &Path) {
    g(p, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
}
#[test]
fn independent_root_and_manifest_recovery() {
    let t = tempfile::tempdir().unwrap();
    init(t.path());
    assert!(prepare(t.path(), &files(), None, &Imported::new()).unwrap());
    commit(t.path());
    let previous = g(t.path(), &["rev-parse", "HEAD"]);
    remote_ref(t.path());
    assert!(!prepare(t.path(), &files(), Some(&previous), &Imported::new()).unwrap());
    assert!(!prepare(t.path(), &files(), None, &Imported::new()).unwrap());
    assert_eq!(g(t.path(), &["rev-list", "--count", "HEAD"]), "1");
}

#[test]
fn managed_directory_can_be_replaced_by_a_file() {
    let t = tempfile::tempdir().unwrap();
    let p = t.path();
    init(p);
    prepare(p, &files(), None, &Imported::new()).unwrap();
    commit(p);
    let previous = g(p, &["rev-parse", "HEAD"]);
    remote_ref(p);
    let desired = [("skills".into(), b"Replacement file\n".to_vec())].into();
    assert!(prepare(p, &desired, Some(&previous), &Imported::new()).unwrap());
    assert_eq!(fs::read(p.join("skills")).unwrap(), b"Replacement file\n");
}

#[test]
fn imported_deletion_does_not_follow_unowned_parent_symlink() {
    let t = tempfile::tempdir().unwrap();
    let p = t.path().join("repository");
    init(&p);
    prepare(&p, &files(), None, &Imported::new()).unwrap();
    commit(&p);
    let previous = g(&p, &["rev-parse", "HEAD"]);
    let victim = t.path().join("victim");
    fs::create_dir(&victim).unwrap();
    fs::write(victim.join("guide.md"), "Preserve external material\n").unwrap();
    fs::remove_file(p.join("skills/guide.md")).unwrap();
    fs::remove_dir(p.join("skills")).unwrap();
    std::os::unix::fs::symlink(&victim, p.join("skills")).unwrap();
    commit(&p);
    remote_ref(&p);
    assert!(prepare(&p, &Files::new(), Some(&previous), &Imported::new()).unwrap());
    assert_eq!(
        fs::read_to_string(victim.join("guide.md")).unwrap(),
        "Preserve external material\n"
    );
    assert!(p.join("skills").is_symlink());
}

#[test]
fn git_ignore_and_encoding_cannot_change_the_validated_index() {
    for policy in ["ignore", "encoding"] {
        let t = tempfile::tempdir().unwrap();
        let p = t.path();
        init(p);
        prepare(p, &files(), None, &Imported::new()).unwrap();
        commit(p);
        let previous = g(p, &["rev-parse", "HEAD"]);
        let mut desired = files();
        if policy == "ignore" {
            fs::write(p.join(".gitignore"), "skills/new.md\n").unwrap();
            desired.insert("skills/new.md".into(), b"Enrolled guide\n".to_vec());
        } else {
            fs::write(
                p.join(".gitattributes"),
                "*.md working-tree-encoding=ISO-8859-1\n",
            )
            .unwrap();
            desired.insert(
                "skills/guide.md".into(),
                "Public café\n".as_bytes().to_vec(),
            );
        }
        commit(p);
        let before = g(p, &["rev-parse", "HEAD"]);
        remote_ref(p);
        assert!(prepare(p, &desired, Some(&previous), &Imported::new()).is_err());
        assert_eq!(g(p, &["rev-parse", "HEAD"]), before);
    }
}
#[test]
fn divergence_requires_import_and_preserves_public_owned_files() {
    let t = tempfile::tempdir().unwrap();
    let p = t.path();
    init(p);
    prepare(p, &files(), None, &Imported::new()).unwrap();
    commit(p);
    let old = g(p, &["rev-parse", "HEAD"]);
    fs::write(p.join("skills/guide.md"), "Community fix\n").unwrap();
    fs::write(p.join("README.md"), "Community docs\n").unwrap();
    commit(p);
    remote_ref(p);
    assert!(prepare(p, &files(), Some(&old), &Imported::new()).is_err());
    let mut desired = files();
    desired.insert("skills/guide.md".into(), b"Community fix\n".to_vec());
    assert!(prepare(p, &desired, Some(&old), &Imported::new()).unwrap());
    assert_eq!(
        fs::read_to_string(p.join("README.md")).unwrap(),
        "Community docs\n"
    );
}
fn guide(text: &str) -> Files {
    [("skills/guide.md".into(), text.as_bytes().to_vec())].into()
}
/// Publishes `base`, then commits `public` as a public edit. Returns the trusted export and
/// the public commit.
fn public_edit(p: &Path, base: &str, public: &str) -> (String, String) {
    init(p);
    prepare(p, &guide(base), None, &Imported::new()).unwrap();
    commit(p);
    let old = g(p, &["rev-parse", "HEAD"]);
    fs::write(p.join("skills/guide.md"), public).unwrap();
    commit(p);
    remote_ref(p);
    (old, g(p, &["rev-parse", "HEAD"]))
}
fn import_error(public: &str) -> String {
    format!(
        "Public managed file changed; import it, then record its public commits in \
         public/imported-commits: skills/guide.md ({})",
        &public[..12]
    )
}
#[test]
fn contained_public_edit_needs_no_record() {
    let t = tempfile::tempdir().unwrap();
    let p = t.path();
    let lines = |first: &str, last: &str| format!("{first}\nk1\nk2\nk3\nk4\nk5\n{last}\n");
    let (old, public) = public_edit(p, &lines("intro", "outro"), &lines("intro", "public fix"));
    // The private source took the public fix on top of its own newer change.
    let both = guide(&lines("private intro", "public fix"));
    assert!(prepare(p, &both, Some(&old), &Imported::new()).unwrap());
    assert_eq!(g(p, &["rev-parse", "HEAD"]), public);
    assert_eq!(
        fs::read(p.join("skills/guide.md")).unwrap(),
        both["skills/guide.md"]
    );
    // Without the public fix, or with a conflicting edit of its lines, the export would lose
    // it, so it has to be imported and recorded.
    for desired in [
        lines("private intro", "outro"),
        lines("intro", "private outro"),
    ] {
        g(p, &["reset", "--hard", "HEAD"]);
        let error = prepare(p, &guide(&desired), Some(&old), &Imported::new()).unwrap_err();
        assert_eq!(error.to_string(), import_error(&public));
    }
}
#[test]
fn recorded_import_covers_edits_a_merge_cannot_prove() {
    // A private addition next to the imported public one conflicts, as does an imported edit
    // the private source revised.
    for (base, public, export) in [("x\n", "x\ny\n", "x\ny\nz\n"), ("a\n", "b\n", "c\n")] {
        let t = tempfile::tempdir().unwrap();
        let p = t.path();
        let (old, commit) = public_edit(p, base, public);
        let error = prepare(p, &guide(export), Some(&old), &Imported::new()).unwrap_err();
        assert_eq!(error.to_string(), import_error(&commit));
        g(p, &["reset", "--hard", "HEAD"]);
        // Recording an unrelated commit is not enough.
        let other: Imported = ["0".repeat(40)].into();
        assert!(prepare(p, &guide(export), Some(&old), &other).is_err());
        g(p, &["reset", "--hard", "HEAD"]);
        assert!(prepare(p, &guide(export), Some(&old), &[commit].into()).unwrap());
        assert_eq!(
            fs::read_to_string(p.join("skills/guide.md")).unwrap(),
            export
        );
    }
}
#[test]
fn recorded_import_covers_a_public_mode_change() {
    let t = tempfile::tempdir().unwrap();
    let p = t.path();
    init(p);
    prepare(p, &guide("same\n"), None, &Imported::new()).unwrap();
    commit(p);
    let old = g(p, &["rev-parse", "HEAD"]);
    g(p, &["update-index", "--chmod=+x", "skills/guide.md"]);
    g(p, &["commit", "-m", "Make the guide executable"]);
    remote_ref(p);
    let public = g(p, &["rev-parse", "HEAD"]);
    let error = prepare(p, &guide("same\n"), Some(&old), &Imported::new()).unwrap_err();
    assert_eq!(error.to_string(), import_error(&public));
    g(p, &["reset", "--hard", "HEAD"]);
    assert!(prepare(p, &guide("same\n"), Some(&old), &[public].into()).unwrap());
}
#[test]
fn recorded_import_covers_a_public_edit_git_cannot_merge() {
    let t = tempfile::tempdir().unwrap();
    let p = t.path();
    let (old, public) = public_edit(p, "text\n", "binary\0\n");
    let error = prepare(p, &guide("text v2\n"), Some(&old), &Imported::new()).unwrap_err();
    assert_eq!(
        error.to_string(),
        "Could not compare the public edit of skills/guide.md: Git operation failed: merge-file"
    );
    g(p, &["reset", "--hard", "HEAD"]);
    assert!(prepare(p, &guide("text v2\n"), Some(&old), &[public].into()).unwrap());
}
#[test]
fn imported_commits_are_read_from_the_source_revision() {
    let t = tempfile::tempdir().unwrap();
    let p = t.path();
    init(p);
    fs::write(p.join("README.md"), "Source\n").unwrap();
    commit(p);
    assert!(imported(p, "HEAD").unwrap().is_empty());
    let id = "a".repeat(40);
    fs::create_dir(p.join("public")).unwrap();
    fs::write(
        p.join("public/imported-commits"),
        format!("# Imported public fixes\n{id}  # CI fix\n\n"),
    )
    .unwrap();
    commit(p);
    assert_eq!(imported(p, "HEAD").unwrap(), [id].into());
    fs::write(p.join("public/imported-commits"), "abc123\n").unwrap();
    commit(p);
    assert_eq!(
        imported(p, "HEAD").unwrap_err().to_string(),
        "Invalid public/imported-commits entry: abc123"
    );
}
#[test]
fn deletion_is_restricted_and_tampering_rejected() {
    let t = tempfile::tempdir().unwrap();
    let p = t.path();
    init(p);
    prepare(p, &files(), None, &Imported::new()).unwrap();
    commit(p);
    let old = g(p, &["rev-parse", "HEAD"]);
    fs::write(p.join("unmanaged.txt"), "Keep\n").unwrap();
    commit(p);
    remote_ref(p);
    prepare(p, &Files::new(), Some(&old), &Imported::new()).unwrap();
    assert!(!p.join("skills/guide.md").exists());
    assert!(p.join("unmanaged.txt").exists());
    g(p, &["reset", "--hard", "HEAD"]);
    fs::write(p.join(".public-export.json"), "{}\n").unwrap();
    commit(p);
    remote_ref(p);
    assert!(prepare(p, &files(), Some(&old), &Imported::new()).is_err());
}
#[test]
fn input_and_message_boundaries() {
    for p in ["../x", ".GIT/config", "skills//x", "skills/../x"] {
        assert!(relative(p).is_err())
    }
    assert!(scan("public.txt", format!("{}{}", "roy", "-mac").as_bytes()).is_err());
    assert!(scan("public.txt", b"Use FAKE_TOKEN in examples.").is_ok());
    assert!(
        validate_message(&CommitMessage {
            subject: "Document the host-a reference role".into(),
            body: format!("Path {}{}alice/file is private.", "/Us", "ers/")
        })
        .is_err()
    );
    assert!(
        validate_message(&CommitMessage {
            subject: "Update public skills and evolution tools".into(),
            body: String::new()
        })
        .is_err()
    );
    assert!(
        validate_message(&CommitMessage {
            subject: "Fix monitor lock-file isolation on Linux".into(),
            body: "Mock the host-specific service query in the lock test.".into()
        })
        .is_ok()
    );
}
/// The real exporter over the committed inventory at HEAD, so a marker in an exported file
/// fails `cargo test` before the daily export rejects it. The public repository and the
/// candidate validation directory have no private configuration or Git checkout and skip.
#[test]
fn committed_inventory_passes_the_publication_scanner() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let inventory = root.join("public/export.json");
    if !inventory.is_file() || !root.join(".git").exists() {
        eprintln!("skipped: needs the private export configuration and source checkout");
        return;
    }
    // The inventory and the file contents both come from HEAD.
    let mut value: serde_json::Value =
        serde_json::from_slice(&git(root, &["show", "HEAD:public/export.json"]).unwrap()).unwrap();
    value["source_repo"] = root.to_str().unwrap().into();
    let config = Config::from_value(value).unwrap();
    let files = snapshot(&config, "HEAD").unwrap();
    assert_eq!(files.len(), config.files.len());
}
#[test]
fn manifest_matches_existing_python_encoding() {
    let value = String::from_utf8(manifest(&files()).unwrap()).unwrap();
    assert!(value.starts_with("{\n  \"files\": {"));
    assert!(value.ends_with("  \"schema_version\": 1\n}\n"));
}
#[test]
fn config_rejects_private_sources_and_unknown_keys() {
    let t = tempfile::tempdir().unwrap();
    let base = serde_json::json!({"schema_version":1,"source_repo":t.path(),"state_dir":t.path(),"target_url":"https://github.com/example/skills.git","author_name":"Test","author_email":"test@example.invalid","files":[{"source":"skills/guide.md","target":"skills/guide.md"}]});
    assert!(Config::from_value(base.clone()).is_ok());
    let mut bad = base.clone();
    bad["files"][0]["source"] = "feedback/private.md".into();
    assert!(Config::from_value(bad).is_err());
    let mut bad = base;
    bad["surprise"] = true.into();
    assert!(Config::from_value(bad).is_err());
}

#[test]
fn unicode_identity_prevents_inventory_collisions() {
    let t = tempfile::tempdir().unwrap();
    for (a, b) in [("Straße.md", "STRASSE.md"), ("café.md", "cafe\u{301}.md")] {
        let value = serde_json::json!({"schema_version":1,"source_repo":t.path(),"state_dir":t.path(),"target_url":"https://github.com/example/skills.git","author_name":"Test","author_email":"test@example.invalid","files":[{"source":"skills/a.md","target":a},{"source":"skills/b.md","target":b}]});
        assert!(Config::from_value(value).is_err());
    }
}
#[test]
fn public_aliases_are_rejected_without_overwrite() {
    for (existing, target) in [
        ("README.md", "readme.md"),
        ("Docs/guide.md", "docs/new.md"),
        ("Straße.md", "STRASSE.md"),
        ("café.md", "cafe\u{301}.md"),
    ] {
        let t = tempfile::tempdir().unwrap();
        let p = t.path();
        init(p);
        prepare(p, &files(), None, &Imported::new()).unwrap();
        commit(p);
        let old = g(p, &["rev-parse", "HEAD"]);
        let public = p.join(existing);
        fs::create_dir_all(public.parent().unwrap()).unwrap();
        fs::write(&public, "Public-owned material\n").unwrap();
        commit(p);
        let before = g(p, &["rev-parse", "HEAD"]);
        remote_ref(p);
        let mut desired = files();
        desired.insert(target.into(), b"Candidate material\n".to_vec());
        assert!(prepare(p, &desired, Some(&old), &Imported::new()).is_err());
        assert_eq!(
            fs::read_to_string(&public).unwrap(),
            "Public-owned material\n"
        );
        assert_eq!(g(p, &["rev-parse", "HEAD"]), before);
        assert!(!prepare(p, &files(), Some(&old), &Imported::new()).unwrap());
    }
}
