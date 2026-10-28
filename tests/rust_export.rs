use codex_skills::{
    CommitMessage,
    config::{Config, relative},
    export::{Files, git, manifest, prepare, scan, validate_message},
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
    assert!(prepare(t.path(), &files(), None).unwrap());
    commit(t.path());
    let previous = g(t.path(), &["rev-parse", "HEAD"]);
    remote_ref(t.path());
    assert!(!prepare(t.path(), &files(), Some(&previous)).unwrap());
    assert!(!prepare(t.path(), &files(), None).unwrap());
    assert_eq!(g(t.path(), &["rev-list", "--count", "HEAD"]), "1");
}

#[test]
fn managed_directory_can_be_replaced_by_a_file() {
    let t = tempfile::tempdir().unwrap();
    let p = t.path();
    init(p);
    prepare(p, &files(), None).unwrap();
    commit(p);
    let previous = g(p, &["rev-parse", "HEAD"]);
    remote_ref(p);
    let desired = [("skills".into(), b"Replacement file\n".to_vec())].into();
    assert!(prepare(p, &desired, Some(&previous)).unwrap());
    assert_eq!(fs::read(p.join("skills")).unwrap(), b"Replacement file\n");
}

#[test]
fn imported_deletion_does_not_follow_unowned_parent_symlink() {
    let t = tempfile::tempdir().unwrap();
    let p = t.path().join("repository");
    init(&p);
    prepare(&p, &files(), None).unwrap();
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
    assert!(prepare(&p, &Files::new(), Some(&previous)).unwrap());
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
        prepare(p, &files(), None).unwrap();
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
        assert!(prepare(p, &desired, Some(&previous)).is_err());
        assert_eq!(g(p, &["rev-parse", "HEAD"]), before);
    }
}
#[test]
fn divergence_requires_import_and_preserves_public_owned_files() {
    let t = tempfile::tempdir().unwrap();
    let p = t.path();
    init(p);
    prepare(p, &files(), None).unwrap();
    commit(p);
    let old = g(p, &["rev-parse", "HEAD"]);
    fs::write(p.join("skills/guide.md"), "Community fix\n").unwrap();
    fs::write(p.join("README.md"), "Community docs\n").unwrap();
    commit(p);
    remote_ref(p);
    assert!(prepare(p, &files(), Some(&old)).is_err());
    let mut desired = files();
    desired.insert("skills/guide.md".into(), b"Community fix\n".to_vec());
    assert!(prepare(p, &desired, Some(&old)).unwrap());
    assert_eq!(
        fs::read_to_string(p.join("README.md")).unwrap(),
        "Community docs\n"
    );
}
#[test]
fn deletion_is_restricted_and_tampering_rejected() {
    let t = tempfile::tempdir().unwrap();
    let p = t.path();
    init(p);
    prepare(p, &files(), None).unwrap();
    commit(p);
    let old = g(p, &["rev-parse", "HEAD"]);
    fs::write(p.join("unmanaged.txt"), "Keep\n").unwrap();
    commit(p);
    remote_ref(p);
    prepare(p, &Files::new(), Some(&old)).unwrap();
    assert!(!p.join("skills/guide.md").exists());
    assert!(p.join("unmanaged.txt").exists());
    g(p, &["reset", "--hard", "HEAD"]);
    fs::write(p.join(".public-export.json"), "{}\n").unwrap();
    commit(p);
    remote_ref(p);
    assert!(prepare(p, &files(), Some(&old)).is_err());
}
#[test]
fn input_and_message_boundaries() {
    for p in ["../x", ".GIT/config", "skills//x", "skills/../x"] {
        assert!(relative(p).is_err())
    }
    assert!(scan("public.txt", format!("{}{}", "roy", "-mac").as_bytes()).is_err());
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
        prepare(p, &files(), None).unwrap();
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
        assert!(prepare(p, &desired, Some(&old)).is_err());
        assert_eq!(
            fs::read_to_string(&public).unwrap(),
            "Public-owned material\n"
        );
        assert_eq!(g(p, &["rev-parse", "HEAD"]), before);
        assert!(!prepare(p, &files(), Some(&old)).unwrap());
    }
}
