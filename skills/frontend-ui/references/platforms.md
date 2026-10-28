# Platforms and styling

The shared rules govern design and scope. Keep platform-specific workflows and runtime contracts in their installed skill or tool documentation. Do not merge runtime permissions, service APIs, hosting internals, or host-only CSS helpers into universal UI advice.

## UnoCSS

Use the project's installed UnoCSS version and its [official documentation](https://unocss.dev/). Reuse existing tokens, shortcuts, extraction rules, theme behavior, and Floating Vue integration. Preserve established Attributify usage unless the requested change requires otherwise.

Three copied UnoCSS reference snapshots are omitted from this public edition pending source-provenance review.

## Sites

Resolve Sites from the acting host's available skill catalog (currently `sites:sites`) and follow that installed entrypoint and its relevant references. Preserve its environment, registration, preview, connector, authentication, storage, and deployment contracts.

- Reuse the existing project and audience. On a new standard starter, use matching installed accessible primitives; preserve existing or template-derived stacks.
- Show a coherent first preview when supported, with the requested product, real visual direction, and main affordance visible. Complete the requested behavior and relevant responsive/accessibility checks before reporting completion.
- A sparse marketing brief permits coherent design decisions and sample content. It does not authorize speculative features, invented factual claims, new external integrations, WebMCP tools, or misleading success states. Add capabilities because the requested experience needs them.
- Respect the platform's image workflow and native asset restrictions. Preserve existing social previews; generate social cards or deployment thumbnails only when requested.
- Preview and save locally by default. Follow the installed Sites publishing workflow when the user's request authorizes deployment, including already-authorized follow-up edits. Keep the intended audience and verify successful deployment through native status tools.
- Perform relevant visual and interaction verification during authoring. A deployment-status check confirms publication, not the interface's correctness.
- Follow the installed Sites preview and troubleshooting references for the actual managed or local environment. Preserve their recovery limits and report material verification limitations.
- Follow the installed Sites MCP reference for requested remotely connected MCP tools. Preserve authorization and hosting boundaries; do not add an MCP endpoint merely because the Site can support one.

The local-first delivery policy is the user's chosen policy, replacing Sites' automatic publication default in this consolidation.

## Inline Visualize artifacts

Keep the currently installed `visualize:visualize` skill and its host references for conversational interactive visuals and mockups.

- Follow its fragment format, host palette and typography, CSP/network limits, icon system, widgets, and interaction contracts. These are specific to the embedded renderer.
- Product mockups may use the skill's product-scoped styling exception. Preserve the represented product's design rather than applying host styling inside the mockup.
- Use provided carousel, Tweak controls, tabs, tooltips, and calendar contracts when relevant. Avoid reimplementing host machinery, inventing states to fill controls, or suggesting that snapshot interactions reach a live service.
- Keep source facts grounded, controls accessible, layouts responsive, and motion restrained with reduced-motion support.
- An ordinary web project should use its own components and styles; host-only utilities are not a production frontend design system.

## Image generation

Use the built-in `imagegen` skill for raster assets and bitmap mockups. Use the built-in tool by default; follow current tool arguments and edit-target requirements. Preserve editable native vectors when that is the appropriate format.

Inspect requested images, preserve relevant invariants and transparency, verify text, and save final project assets into the workspace. Tool/API fallback and credentials remain in that workflow; they are not general UI prerequisites.

## Browser and desktop verification

- Prefer the in-app Browser for local frontend inspection when available; use the user's specified browser when requested.
- Chrome is useful when the task depends on the user's existing browser state. Desktop Computer Use is for native applications or tasks requiring that surface. Prefer dedicated APIs and tools when appropriate.
- Load and follow the selected automation runtime's current documentation. Use observed state for actions and verification, preserve permissions, and do not fabricate browser results.
- Browser, Chrome, Computer Use, and Unified Computer Use supply automation capabilities. Keep them separate from aesthetic rules; these runtime plugins remain installed.
