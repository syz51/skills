---
name: frontend-ui
description: Design, build, polish, and review frontend/web interfaces and their tooling. Also use for React, Next.js, TanStack Start/Router/Query, Vue, Nuxt, Nitro, Pinia, Vue Router, VueUse, Vite, VitePress, Vitest, Slidev, Turborepo, and tsdown implementation or configuration, and for reviewing feedback to improve this skill. Follow the brief and existing project; load only the relevant supporting references.
---

# Frontend and UI

For shell reads of this skill or its references, use `uv run --script ~/.local/share/codex-skills/repository/scripts/read_guidance.py <file>` when available, with one file per invocation and an output budget of at least 8000 tokens. The returned `content` is the guidance; follow `next_start` with `--start <offset>` when more is needed. This preserves the path, exact-content hash, and complete/excerpt status in the saved session. If the helper is unavailable, read each file separately with `cat` or a clearly bounded `sed -n` excerpt.

## Start from the task

- Identify the audience, primary task, requested change, and relevant screens or states. Inspect the existing design system, components, assets, package versions, and project instructions before choosing an approach.
- Follow explicit user requirements and established product conventions. The defaults below fill gaps; examples and conventions in the supporting references or plugin design guides do not override them. Preserve accessibility, security, and the actual runtime/tool contracts.
- For a small change, work within the existing design. For a new interface or substantial redesign, choose a coherent direction for hierarchy, layout, typography, color, and interaction before implementation. Explain consequential choices briefly; produce wireframes, variants, or a formal design plan when useful or requested.
- Ask about conflicts that materially change the result. Reuse known preferences and make disclosed assumptions for reversible details. Keep reviews read-only unless changes are requested.

## Design for the purpose

- Follow the brief, reference, and existing design. Favor clear, restrained product and tooling interfaces; allow expressive marketing, editorial, and portfolio work when it serves the subject.
- Put the main task where people can find and use it. Dashboards and tools need their working surface first. Marketing pages may open with a distinctive headline, image, demonstration, or interaction.
- Make hierarchy reflect the content. Use headings, groups, dividers, cards, navigation, badges, and labels when they communicate structure or function. Avoid decorative filler, repeated pitches, fake metrics, and charts without useful data.
- Choose a few deliberate visual ideas and apply them consistently. Expressive work still needs quiet supporting structure. Avoid applying the same template to unrelated briefs.
- Reuse project tokens. If none exist, define a small coherent set for surfaces, text, borders, accents, spacing, typography, radii, and elevation. Choose values for the content and context; palette lists and numerical examples are starting points.
- Support the themes required by the project or brief. Preserve existing theme behavior; add another theme when useful or requested. Check every supported theme, including images, controls, focus, and overlays.

## Typography and copy

- Preserve established typography unless changing it is part of the task. System fonts are valid. Choose custom fonts deliberately when they improve the brief, with appropriate fallbacks and loading behavior.
- Use a clear type hierarchy, readable line lengths, comfortable line height, and sizes suited to the surface. Keep text legible on narrow screens and with zoom or text enlargement.
- Use monospace for content that benefits from it, such as code and technical identifiers. Use tabular numerals when comparing or updating quantities needs stable alignment; do not force them onto ordinary prose or every numeric string.
- Use plain, specific language from the user's perspective. Name an action consistently throughout the flow. Default to sentence case when no product convention exists. Preserve proper names, source quotations, and locale-appropriate punctuation.
- Explain errors and the next useful action. Provide meaningful empty, loading, success, and disabled states where the flow needs them. Distinguish sample content from verified facts and real service behavior.
- Truncate only when necessary. Keep the complete value available through an accessible disclosure, detail view, wrapping, or copy action appropriate to the task; hover-only help is insufficient for essential information.

## Layout, surfaces, and assets

- Use content-driven layout with consistent alignment and spacing. Reflow for narrow screens rather than shrinking everything. Handle long labels, localization, empty content, and overflow.
- Distinguish structure from elevation. Borders and dividers define boundaries; shadows can communicate depth on overlays and raised surfaces. Choose contrast, radius, and shadow strength within the product's system.
- Align nested rounded shapes and asymmetric icons optically. Keep icon family, weight, size, and placement consistent. Avoid adding decoration that weakens hierarchy or usability.
- Reuse suitable supplied or existing assets. Choose code-native geometry for controls, diagrams, and existing vector systems; use image search or image generation for appropriate imagery. Preserve provenance and verify factual subjects rather than inventing sources.
- Give images intrinsic dimensions or a stable aspect ratio, meaningful alternative text when informative, and suitable responsive sizing. Verify asset loading and copy project-bound assets into the project.
- See [Polish details](references/polish.md) for targeted refinements. Use them when they improve the interface.

## Interaction and accessibility

- Use semantic HTML and established accessible components. Links navigate; buttons perform actions. Label controls, give icon-only actions accessible names, and preserve meaningful heading and landmark structure.
- Make the whole relevant flow work with keyboard, touch, and pointer input. Keep focus visible and unobscured; manage and restore focus in overlays, support expected dismissal, and avoid traps.
- Associate form labels, instructions, validation, and errors with their inputs. Use appropriate input types and autocomplete; preserve entered data through recoverable failures. Confirm consequential destructive actions where the flow requires it.
- Maintain sufficient text and control contrast. Do not rely on color, hover, animation, or icons alone for meaning. Size and space targets for the input context and applicable accessibility requirements; expanded hit areas must not overlap or conceal other controls.
- Use restrained motion when it explains an action, change, or spatial relationship. Keep repeated interactions responsive, allow interruption, and avoid unnecessary first-render or staggered entrances.
- Respect reduced-motion preferences for all relevant movement, including hover and press effects. Preserve the same information and usable state without motion.
- Prefer CSS or existing animation tools for simple effects. Specify transitioned properties; favor transform and opacity for movement, and measure expensive effects when necessary. Do not add a motion dependency or public component option for an isolated flourish.

## Implementation and verification

- Reuse the project's framework, styling approach, primitives, icons, utilities, state model, and conventions. Use bun for new JavaScript tooling unless project requirements or an existing lockfile require another tool; use uv for Python tooling.
- Implement the complete requested behavior with the smallest maintainable change. Create components, composables, types files, shared constants, libraries, and options when responsibility, reuse, or complexity justifies them. Avoid quotas and speculative extraction.
- Keep state minimal and ownership explicit. Preserve validation, error handling, cleanup, and data-flow contracts. Avoid unsafe HTML, exposed secrets, accidental duplicate submissions, and treating simulated behavior as a completed external action.
- Check the changed user path and relevant loading, empty, error, and success states. For live interfaces, follow relevant user and external updates through delivery of current content; completed background work alone does not establish freshness. Visually inspect representative wide and narrow layouts, supported themes, focus, touch behavior, and reduced motion when affected. Compare against the requested reference or established design.
- Run the smallest meaningful tests and required project checks. Measure suspected performance problems before adding optimization; reserve virtualization, memoization, preloading, and persistent hints for demonstrated needs.
- Report the outcome, material findings or changes, verification, and limitations concisely. For reviews, rank actionable findings and cite precise locations; consolidate repeated issues without hiding affected locations. Do not invent findings or rejected alternatives to fill a report template.
- Preview and save locally by default. Publish when the user's request authorizes deployment, preserve the intended audience, and confirm the actual deployment outcome.

## Learn from real use

- After meaningful use during authorized execution, save a brief feedback note when a user correction, instruction problem, missing guidance, or verified successful technique offers a reusable lesson. Follow [Feedback capture and review](references/feedback.md); skip routine work with no concrete lesson. For read-only requests, mention material lessons in the response without writing notes unless saving feedback is requested.
- Keep observations separate from policy. Do not revise this skill during ordinary task delivery or load accumulated notes as instructions. When the user requests a feedback review or skill improvement, use the same guide to assess evidence, propose narrow changes, and verify their effect.

## Load only the relevant specialization

- React, Next.js, TanStack Start/Router/Query, Vue, Nuxt, Pinia, VueUse, Vite, Vitest, VitePress, Slidev, Nitro, Turborepo, and tsdown: [Frameworks and tooling](references/frameworks.md).
- UnoCSS, Sites, inline Visualize artifacts, image generation, and browser verification: [Platforms and styling](references/platforms.md).
- The former standalone frontend/framework skills are supporting references inside this skill. Their examples describe APIs, not mandatory scaffolding or visual policy. Keep plugin runtime/deployment documentation in its maintained plugin, resolve the current installed version, and preserve actual runtime/tool contracts. Use package-version-matched documentation; do not migrate a project merely to match a reference snapshot.
