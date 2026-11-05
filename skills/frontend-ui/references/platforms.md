# Platforms and styling

The shared rules govern design and scope. Keep platform-specific workflows and runtime contracts in their installed skill or tool documentation. Do not merge runtime permissions, service APIs, hosting internals, or host-only CSS helpers into universal UI advice.

## UnoCSS

Use the bundled [token combinations](unocss/core-tokens-and-combinations.md), [class utility guidance](unocss/best-practices-class-utilities-over-attributify.md), and [Floating Vue setup](unocss/features-floating-vue-overrides.md) when the project uses UnoCSS:

- Reuse semantic shortcuts and existing token families; define named overlay layers when the project needs them.
- Prefer class-based utilities for new examples. Preserve established Attributify code unless changing it is necessary or requested.
- Keep generated icon and status classes statically discoverable; use the project's extraction or safelist mechanism when necessary.
- Reuse existing tooltip/popover setup and compatible Floating Vue overrides rather than adding that library.
- Avoid scattering raw values where project tokens exist, and check how modifiers interact with shortcuts.

Do not install UnoCSS to satisfy this guide. The old dual-theme mandate, dials, palette and punctuation bans, mandatory monospace, and fixed animation recipes are superseded by the choices in the shared policy.

## Hosted pages and inline visuals

Use the acting host's page and visual tools when it provides them (for example, claude.ai Artifacts with the `artifact-design` skill), and follow their design, capability, preview, and publishing contracts. Keep host styling inside host surfaces; an ordinary web project uses its own components and styles, and host-only utilities are not a production frontend design system.

- Show a coherent first version with the requested product, real visual direction, and main affordance visible. Complete the requested behavior and relevant responsive/accessibility checks before reporting completion.
- A sparse brief permits coherent design decisions and sample content. It does not authorize speculative features, invented factual claims, new external integrations, or misleading success states. Add capabilities because the requested experience needs them.
- Keep source facts grounded, controls accessible, layouts responsive, and motion restrained with reduced-motion support.
- Product mockups may preserve the represented product's design. Do not invent states to fill controls or suggest that mockup interactions reach a live service.
- Preview and save locally by default. Publish or share only when the user's request authorizes it, and confirm the outcome through the host's status tools.

## Images

Claude Code has no built-in image generation. Use supplied or existing assets, code-native vectors, or an image tool the host provides. Preserve provenance and transparency, verify any text in the image, and save final project assets into the workspace.

## Browser verification

- Verify your own UI changes with the `headless-browser` skill: check wide and narrow widths, supported themes, and console errors.
- Use the in-app browser when the user asks for it, and the user's own browser only when the task depends on its state.
- Act and verify from observed state, preserve permissions, and never fabricate browser results.
