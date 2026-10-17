## SOP: Verification

<instruct>

- Confirm the absolute path of `references/feasibility-checklist.md` in this skill, but DO NOT read it.
- Confirm the absolute path of the Requirement Analysis Document Path, but DO NOT read it.
- When delegating verification to a Subagent, use the prompt template shown in `<example />`.
- Fix any design elements that do not comply with the design specifications. Perform the review and revision only once.

</instruct>

<example>

```text
## Architecture Overview
- **Design approach**: [Briefly describe the design approach and responsibility assignment]
- **Design Decisions**: <!-- Max 3 key decisions -->

## Module Design (per module)
### Module A <!-- Max 3 key modules -->
- **Responsibilities**: [describe in two or three sentences]
- **Core Approach**: [describe the core design approach in two or three sentences]
- **Assumptions & Constraints**: [1,2,...]
- **Risks & Mitigations**: [1,2,...]

## Additional Notes

---

- Requirement Analysis Document Path: <path>
- Checklist: <path>

Please start evaluation based on the checklist.
```

</example>

<constraint>

- NEVER proceed to document generation before verification is complete — the feasibility of the draft has not been assessed at this point (Effort=High).
- NEVER skip this step because there will be a subsequent review — feasibility verification and review are independent (Effort=High).
- DO NOT READ the checklist or the requirement analysis document yourself; Your role is to coordinate the verification process, not to perform it directly.

</constraint>

<patch>

- ALWAYS delegate verification to Subagent when delegation tools are provided — only retrieve the verification results and revise accordingly. Self‑verification is of limited effectiveness; only perform direct verification when delegation is impossible.

</patch>

<condition>

- IF Effort is Low or Medium, THEN skip this step (verification not required).
- IF Effort is High, THEN perform feasibility verification (mandatory).

</condition>
