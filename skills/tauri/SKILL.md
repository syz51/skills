---
name: tauri
description: Build, configure, and troubleshoot Tauri desktop and mobile applications, including frontend and Rust integration. Use alongside frontend-ui; ordinary web-only work does not need this skill. Also use to review feedback to improve this skill.
---

# Tauri

For saved guidance provenance, use `uv run --script ~/.local/share/codex-skills/repository/scripts/read_guidance.py <file>` when available, one file per invocation with at least 8000 output tokens. Follow `next_start` with `--start <offset>`; use the returned `content` as guidance. Otherwise read each file separately.

Read `frontend-ui` first for shared frontend policy. Inspect the current manifests, lockfiles, Tauri configuration, capabilities, and relevant code before applying version-specific documentation.

## Preserve the runtime boundary

- Bundle a static frontend for Tauri. Keep SSR, server routes, credentials, and other server-only work in the server application or an explicitly designed backend; importing shared UI does not make server code available inside the native app.
- Keep portable UI and application logic independent of Tauri APIs. Put native integrations in the native application; add a shared abstraction only when a real cross-platform caller needs it.
- Preserve the shared Rust library entry point for mobile. A mobile-compatible entry point does not establish that iOS/Android projects, SDKs, signing, or device behavior are configured.
- Grant capabilities and CSP sources for the actual feature and target. Check plugin platform support; a desktop permission or plugin may not apply to mobile. Do not broaden production permissions or CSP to solve a development-only failure.

## Verify the affected path

Run the relevant frontend and Rust checks. Treat a Vite build, Rust compilation, application packaging, and an observed native launch as separate evidence. For native changes, exercise the affected behavior in the native app when feasible; report target, mode, and verification limits. Do not infer mobile readiness from a desktop build or a browser rendering the same UI.

Use official, version-matched [Tauri documentation](https://v2.tauri.app/) for current APIs and platform prerequisites. Keep new lessons narrow and evidence-supported; keep project-specific setup in the application repository rather than in this shared skill.

## Learn from real use

After meaningful authorized work, capture a concrete correction, guidance gap or verified technique using [Feedback capture and review](references/feedback.md). Skip routine notes and speculative rules. Keep observations separate from policy; improve this shared skill through the evidence-backed maintenance workflow when authorized. Explicit user requests to evolve the skill permit narrow source edits, followed by validation and a review record; they do not authorize changing installed releases or bypassing publication checks.
