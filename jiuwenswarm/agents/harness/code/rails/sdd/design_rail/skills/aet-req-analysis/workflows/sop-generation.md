## SOP: Generation

<guideline>

- Keep it concise without sacrificing quality, and never exceed actual needs (see Effort).
- More content is not necessarily better — never exceed actual requirements.

## Unsourced Quantitative Values — Universal Placeholder Convention

- **Hard rule**: ALL quantitative values across the document, NEVER state a specific quantitative value as a fact unless it has a source (user input, named benchmark/report, codebase evidence, or authoritative spec).
- **When no source exists**: the value's only legitimate form is a placeholder token. Do NOT emit a bare number.
- **Token syntax** (wrap in backticks so it renders distinctly in markdown):

```markdown
`[[PH:<id> | rec:<recommended value, optional> | why:<one-line reason, optional>]]`
```

- **Aggregation hook**: downstream review may regex-scan `\[\[PH:([^\]|]+)` to enumerate unfilled placeholders.

</guideline>

<instruct>

### Clarify What to Include

- `<!-- condition: -->` comments indicate content requirements.
- `<!-- condition: Low=Skip, Medium=AsNeeded, High=Generate -->` → gate on Effort level.
- When the template has no annotation on a section, generate by default.

### Secondary Exploration

When the template still has information gaps, perform additional exploration and revision until the content is complete and reasonable.

### Generation

1. Fill the assembled template with the clarified requirements from the elicitation phase.
2. Generate the document to the designated location (the skill caller will specify the output path).
3. Output a document summary for the user to review quickly.

</instruct>

<constraint>

- DO NOT generate any document without loading the template first.
- ALWAYS ensure consistency across template sections; no conflicts or omissions in the requirements content.
- NEVER include design decisions or implementation details in the requirements specification.
- NEVER copy comments (e.g. `<!-- xxx: xxx -->`) into the generated document as body content. Strip all comments from the final output.

</constraint>
