## SOP: Generation

<guideline>

- Keep it concise without sacrificing quality, and never exceed actual needs (see Effort).

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

- **Clarify what to include**
  - `<!-- condition: -->` → gate on whether to generate the section at all:
  - e.g. `<!-- condition: Low=Skip, Medium=AsNeeded, High=Generate -->` → gate on Effort level; Low always skips, Medium generate only when necessary for clarity, High generate always.
  - When the template has no annotation on a section, generate by default.
- **Secondary exploration**: When the template still has information gaps, perform additional exploration and design revisions until the content is complete and reasonable.
- Generate the document to the designated location
- Output a document summary for the user to review quickly

### Chunked Write Protection

A single `write_file` call encodes its `content` as a JSON string — every newline and quote is escaped, inflating the payload. Design documents (with mermaid diagrams, module detail tables, sequence diagrams) are especially prone to truncation: when the model's `max_output_tokens` runs out mid-string, the harness receives a truncated JSON (`Unterminated string ...`) and rejects the call — the file is never written.

**Hard rule**: a single `write_file` content MUST NOT exceed **3000 characters**. When the document is longer, split it:

1. First `write_file` call: write the front matter + early sections (≤3000 chars), creating the file.
2. Follow-up `read_file` (required before edit) + `edit_file` calls: append each remaining section, one per call, each ≤3000 chars. Use `edit_file` with `old_string=""` and `new_string="<next section>"` to append, or re-read and use a section-anchored `old_string`/`new_string` pair.
3. Never put an entire mermaid diagram or a large table inside a single `edit_file` `new_string` if it would exceed 3000 chars — split the section further (e.g. write the diagram code block in one call, the surrounding text in another).

This keeps every tool call's JSON well within the output budget and guarantees the file is actually written.

</instruct>

<constraint>

- DO NOT generate any document without loading template.
- ALWAYS ensure consistency across template sections; no conflicts or omissions in the design content.
- NEVER copy comments (e.g. `<!-- xxx: xxx -->`) into the generated document as body content. Strip all comments from the final output.

</constraint>
