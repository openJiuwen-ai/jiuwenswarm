## SOP: Load Template

<instruct>

**Assemble the template**: Run `scripts/assemble-template.mjs req-analysis` (emphasizing running via `bash`, not reading with `read`). Execute the Node script with argument `req-analysis` to output the assembled template.

</instruct>

<constraint>

- DO NOT read template files (e.g. `xxx-template.md`) directly.
- DO NOT generate any document without first loading and interpreting the assembled template.

</constraint>

<patch>

- Distinguish: `assemble-template.mjs` (generation template) vs. `assemble-checklist.mjs` (review checklist).

</patch>

<condition>

- IF there are unresolved items or conflicts in the template interpretation, THEN ask the user first, and only generate after confirmation.

</condition>
