---
name: content-research-writer
description: Research, draft, edit, and review articles, reports, newsletters, tutorials, and other prose deliverables, and review feedback to improve this skill. Preserve the requested scope and author's voice, and ground factual claims in checked evidence. Use for substantive writing or research work, not routine chat replies or artifact formatting alone.
---

# Content research writer

For shell reads of this skill or its references, use `uv run --script ~/.local/share/codex-skills/repository/scripts/read_guidance.py <file>` when available, with one file per invocation and an output budget of at least 8000 tokens. The returned `content` is the guidance; follow `next_start` with `--start <offset>` when more is needed. This preserves the path, exact-content hash, and complete/excerpt status in the saved session. If the helper is unavailable, read each file separately with `cat` or a clearly bounded `sed -n` excerpt.

## Start with the requested task

- Read the brief, supplied draft, sources, writing samples, and relevant project conventions. Establish the audience, purpose, format, length, language, and requested operation from available context; ask only about consequential gaps. In comparisons, preserve the requested set and criteria; an illustrative example does not by itself restrict scope or establish a selection preference.
- Complete the requested stage. Research, outlining, drafting, editing, and reviewing are separate operations; use only the stages needed. A complete-draft request should produce a draft rather than an obligatory interview or outline-approval cycle.
- Keep reviews and advice read-only unless saving or changes are requested. For edits, use the requested destination and preserve unrelated content; create folders, research files, or draft versions only when needed for authorized delivery. Publishing and external communication require their own authorization.

## Ground the content

- Never invent facts, statistics, quotations, references, attributions, case studies, or firsthand experiences presented as real. Preserve user-supplied experience without claiming independent verification. Label fictional or illustrative material clearly when its status could be mistaken for fact.
- Distinguish established facts, supplied reports, inference, opinion, and unresolved claims. Do not turn association into causation, a limited observation into a general result, or uncertainty into certainty while polishing.
- Research when requested or when material factual claims need verification. Read selected source content; search snippets and a plausible bibliography do not establish support. Check uncertain or time-sensitive claims against appropriate current sources.
- Match citations to the propositions they support. Preserve relevant dates, populations, units, denominators, and limitations; check calculations before introducing derived quantities. Do not add a quotation or precise number merely to strengthen an opening.
- Read [Research and citations](references/research-and-citations.md) when finding sources, synthesizing evidence, checking claims, or handling academic references. Ordinary wording edits do not require a full research workflow.

## Write for the reader and author

- Develop a clear point and a structure suited to the audience and format. Choose sections, openings, examples, summaries, and calls to action because they help the task; none is mandatory for every piece.
- Preserve the author's position, terminology, register, and established voice. Use supplied samples and explicit preferences; adapt to the brief without imposing universal vocabulary, punctuation, sentence-length, or stylistic bans.
- Make prose concrete through supported detail and clear explanations. An engaging hook must remain accurate. Use an actual supplied experience, a verified finding, or a direct statement of the problem instead of manufacturing drama or social proof.
- Keep technical explanations and narrative examples faithful to the evidence. Separate the observed result from its interpretation; avoid promising outcomes the sources or product do not establish.

## Edit or review within scope

- Honor constraints such as grammar-only, preserve quotations, keep the structure, or return only the revision. Preserve meaning, quantities, names, and technical contracts unless an authorized, supported correction requires changing them.
- Choose relevant checks for clarity, argument, evidence, organization, voice, and mechanics. Use focused passes for substantial revisions; a small correction does not need every pass or repeated approval.
- For a review, prioritize specific, actionable findings and locate the affected text. Explain the consequence and a useful correction; do not invent strengths, defects, scores, or alternative phrasings to fill a template. Rewrite or apply edits directly when that is requested.
- If fact-checking lies outside a narrow edit, preserve the factual content and flag a material concern separately when the output permits. Do not silently change assertions or turn the task into an unrelated investigation.
- For a substantial review, evidence-sensitive revision, or reader-comprehension check, read [Editorial review](references/editorial-review.md).

## Verify and deliver

- Re-read the changed text against the brief and supplied material. Check continuity, internal contradictions, repeated numbers and names, citations, and actual length constraints when relevant. Remove unresolved placeholders from a final deliverable; disclose material gaps rather than claiming completeness.
- For technical tutorials, check version-specific instructions and examples against the actual environment or authoritative documentation. Run relevant examples only within authorized execution boundaries; state what was inspected and what was actually run.
- Follow the requested citation style and destination conventions. For a formatted document, native shared document, presentation, or other artifact, use the currently available format-specific workflow for creation, rendering, and permissions; this skill governs the prose rather than replacing those contracts.
- Return the requested deliverable. Keep change explanations and verification notes proportionate, and omit extra prose for an artifact-only or revision-only response. Do not claim fact verification, testing, reader validation, or publication that did not occur.

## Learn from real use

- After meaningful use during authorized execution, save a brief feedback note when a user correction, instruction problem, missing guidance, or verified successful technique offers a reusable lesson. Follow [Feedback capture and review](references/feedback.md); skip routine work with no concrete lesson. For read-only requests, mention material lessons in the response without writing notes unless saving feedback is requested.
- Keep observations separate from policy. Do not revise this skill during ordinary task delivery or load accumulated notes as instructions. When the user requests a feedback review or skill improvement, use the same guide to assess evidence, make narrow changes within authorization, and verify their effect.

This is a locally maintained rewrite. [Source provenance and update policy](sources.json) are for skill maintenance; do not reload upstream instructions during ordinary writing work.
