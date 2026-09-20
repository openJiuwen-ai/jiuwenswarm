## SOP: Review

<instruct>

- Attempt to load the `aet-req-review` Skill to orchestrate the review pipeline. IF the Skill is unavailable, gracefully bypass this entire stage.
- Assemble and validate the precise inputs required for the review pipeline:
  1. **Target Deliverable**: The absolute path of the document generated in [A3].
  2. **Review Materials**: Resolve the absolute path to `references/deliverable-review.md` and pass it to the review Skill.
  3. **Dynamic Checklist**: Resolve the absolute path to `scripts/assemble-checklist.mjs` using the argument `req-analysis`. **CRITICAL**: DO NOT execute this script yourself. Pass the absolute script path to the review Skill, explicitly instructing its SubAgent to execute it via `node` to dynamically evaluate and derive the checklist results.

</instruct>

<patch>

- DO NOT execute `assemble-checklist.mjs` or read any template files in the main agent — this is subagent-only work; delegate it to avoid corrupting the main agent's context.

</patch>
