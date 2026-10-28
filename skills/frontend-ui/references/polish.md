# Polish details

Apply these selectively within the brief and existing design system. Numerical recipes from the source skills are examples, not universal requirements.

## Typography and alignment

- Use a small, coherent type scale. Balance short headings when it improves wrapping; check body line breaks and avoid awkward orphans without inserting brittle manual breaks.
- Preserve font metrics and control widths during loading and state changes. Use tabular numerals for timers, counters, aligned quantitative tables, or other changing quantities that would otherwise shift.
- Check actual glyphs and screenshots before optical nudges. Prefer correcting the icon's bounds or using the existing component alignment to scattered offsets.
- Treat font smoothing as a tested platform adjustment, not an unconditional requirement.

## Surfaces

- Preserve borders that communicate structure, selection, focus, or state. Use elevation when the surface sits above other content. A shadow is not a replacement for every boundary.
- For nested rounded rectangles, consider the relationship between outer radius, inner radius, and inset. The outer radius being approximately the inner radius plus the inset is a useful starting point; inspect the rendered result.
- Separate adjacent surfaces without excessive boxes. Check seams, border doubling, clipping, and overlay stacking.
- Add a subtle image outline when the image edge disappears into its surroundings. Neutral black or white at low opacity may work; verify the actual image, theme, and background.

## Motion and icons

- Give actions clear feedback. Color, opacity, or a subtle press response may suffice; scaling is optional and should not shift surrounding layout or conflict with reduced motion.
- Use interruptible transitions for reversible state changes. Reserve staged sequences for an intentional infrequent moment. Do not animate every item or replay entrances on ordinary updates.
- Keep icon changes stable in size and alignment. A simple cross-fade can clarify a state change; scale and blur are optional, with no fixed required values.
- Test motion at normal speed and inspect it more closely when timing or transition defects are suspected. Preserve state and focus when animation is interrupted or disabled.
- Reuse the installed icon system. Distinguish decorative icons from labeled controls and state indicators. Keep stroke weight coherent at the rendered size.

## Performance and input

- Keep the main action available while content loads. Reserve space to avoid layout shifts and prevent feedback from obscuring controls.
- Prefer inexpensive animated properties. Avoid animating shadows or filters across large lists; use a simpler treatment or opacity on a separate layer when useful.
- Add `will-change` only for a measured rendering issue, on the relevant element and property, for the required duration.
- Use comfortable targets for the actual input context. Invisible hit-area expansion must preserve expected targeting and spacing; fix the layout when neighboring targets would overlap.
- Use efficient scrolling and rendering for demonstrated data volume. Preserve semantic structure, focus, and assistive-technology access when virtualizing.
