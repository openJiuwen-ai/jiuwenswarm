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

### Chunked Write Protection

A single `write_file` call encodes its `content` as a JSON string — every newline and quote is escaped, inflating the payload. When the document is large, the model's `max_output_tokens` runs out mid-string, producing a truncated JSON (`Unterminated string ...`) that the harness rejects — the file is never written.

**Hard rule**: a single `write_file` content MUST NOT exceed **3000 characters**. When the document is longer, split it:

1. First `write_file` call: write the front matter + early sections (≤3000 chars), creating the file.
2. Follow-up `read_file` (required before edit) + `edit_file` calls: append each remaining section, one per call, each ≤3000 chars. Use `edit_file` with `old_string=""` and `new_string="<next section>"` to append, or re-read and use a section-anchored `old_string`/`new_string` pair.
3. Never put an entire mermaid diagram or a large table inside a single `edit_file` `new_string` if it would exceed 3000 chars — split the section further.

This keeps every tool call's JSON well within the output budget and guarantees the file is actually written.

</instruct>

<constraint>

- DO NOT generate any document without loading the template first.
- ALWAYS ensure consistency across template sections; no conflicts or omissions in the requirements content.
- NEVER include design decisions or implementation details in the requirements specification.
- NEVER copy comments (e.g. `<!-- xxx: xxx -->`) into the generated document as body content. Strip all comments from the final output.

</constraint>
