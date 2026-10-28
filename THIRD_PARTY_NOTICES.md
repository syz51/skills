# Third-party notices

The root MIT license covers original contributions. Retained third-party text and references keep their own licenses. Preserve these notices and the licenses located inside skill directories when redistributing them.

| Source | Included material or role | License evidence |
| --- | --- | --- |
| [Anthropic skills](https://github.com/anthropics/skills/tree/41bbe19d1a1a7eaab5e7bb9050a417e5c6cffc8f/skills/frontend-design) | Frontend-design source for consolidated guidance | Retained [Apache-2.0 license](skills/frontend-ui/licenses/frontend-design.txt) |
| [Uncodixfy](https://github.com/cyxzdev/Uncodixfy/tree/de11774974165ed45a09f1a40b95d13994939992) | Source for consolidated frontend guidance | [MIT notice](licenses/uncodixfy.txt), obtained at e0e028058b5259debdd94b78147c6d6c77bf7da2; upstream SKILL.md and Uncodixfy.md were verified byte-identical to the recorded older source |
| [make-interfaces-feel-better](https://github.com/jakubkrehel/make-interfaces-feel-better/tree/27355fc332ce3dee6af7107b9224152ad3812066) | Source for consolidated polish guidance | [MIT notice](licenses/make-interfaces-feel-better.txt) |
| [antfu/skills](https://github.com/antfu/skills) | Vue, Nuxt, Nitro, Pinia, Vite, VitePress, and Vitest references | [MIT notice](licenses/antfu-skills.txt), identical at the three recorded commits listed below |
| [Vercel agent-skills](https://github.com/vercel-labs/agent-skills/blob/ba46938889d4e58635362fb8f618e1178ac3ec46/README.md#license) | Web-design-guidelines source for consolidated guidance | Recorded README explicitly declares MIT; declaration retained below |
| [Vercel skills](https://github.com/vercel-labs/skills/tree/773fb2c7bbf16781670a3520affc4abd0c6151ae/skills/find-skills) | Adapted find-skills entrypoint | Retained [MIT notice](skills/find-skills/licenses/vercel-labs-skills.txt) and [attribution](skills/find-skills/ATTRIBUTION.md) |
| [vuejs-ai/skills](https://github.com/vuejs-ai/skills) | Vue best practices, router, and testing references | Retained license in each corresponding reference directory |
| [vueuse/skills](https://github.com/vueuse/skills) | VueUse references | Retained [license](skills/frontend-ui/references/frameworks/vueuse-functions/LICENSE.md) |
| [Slidev](https://github.com/slidevjs/slidev) | Slidev references | Retained [license](skills/frontend-ui/references/frameworks/slidev/LICENSE.md) |
| [Turborepo](https://github.com/vercel/turborepo/tree/b19f6a59ccd89ef98cae5e4d8b30e89489fc00f1) | Turborepo references | [MIT notice](licenses/turborepo.txt) |
| [tsdown](https://github.com/rolldown/tsdown) | tsdown references | Retained [license](skills/frontend-ui/references/frameworks/tsdown/LICENSE.md) |

The antfu/skills license was fetched from `LICENSE.md` at `8886fe2d385d3644cbb47e46ae9cb1cf09307d3b`, `feb0f3aba6566426f12230e7fc7a10d97c1293bb`, and `5cae97ca87e0dcfb5a192cd2cbf8b83a9f769e8f`. All three license files had identical bytes. Exact source revisions for individual reference directories remain in `skills/frontend-ui/sources.json`.

The pinned Vercel agent-skills README contains the following license declaration:

> ## License
>
> MIT

The frontend source attribution also records antfu/design and its upstream inspirations. The three copied UnoCSS reference files are omitted from this initial public export because their installed snapshot could not be pinned to a verified upstream revision. This notice does not assert a license for those omitted files.

## Rewritten guidance and conceptual sources

The locally maintained content-research-writer metadata records a rewrite retaining the purpose of the [original Composio skill](https://github.com/ComposioHQ/awesome-claude-skills/tree/044d48b594f060c164f3b20fac9ea01374721bca/content-research-writer), rather than its text. Its recorded conceptual references include Anthropic doc-coauthoring, Corey Haines copy-editing, and K-Dense scientific-writing and citation-management. They are acknowledged in `skills/content-research-writer/sources.json`; attribution is not a claim that all upstream material shares this project's license.

Tauri guidance is locally authored and links to official documentation. Consolidated frontend policy is locally maintained; the source provenance and attribution files document its upstream inputs. No license notice here replaces the terms supplied with incorporated material.
