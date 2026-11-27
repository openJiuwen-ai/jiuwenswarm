# D6: Compliance Evaluation

> **Dimension:** D6 - Compliance | **JSON Key:** `compliance` | **Weight:** 10%

Evaluate mandatory platform constraints for the target `SKILL.md`. This is a gate dimension: any violation sets `pass = false` and forces the final verdict to `FAIL`.

## Constraints

- `skill-name`: 1-64 characters, only `[a-z0-9-]`, must not start/end with `-`, must not contain consecutive `--`, and must exactly match the parent directory name.
- `description`: Chinese/CJK text must be <=256 characters and <=300 tokens; English/non-CJK text must be <=512 characters and <=300 tokens.
- Body: content after frontmatter must be <=500 lines and <=5000 tokens.

## Scoring

Use local validator output as authoritative when available:

```bash
node -e "const {ComplianceValidator} = require('{baseDir}/lib/compliance-validator.js'); const r = new ComplianceValidator().validate('{skillPath}'); console.log(JSON.stringify(r, null, 2));"
```

If local validation cannot run, inspect the file directly and apply the same constraints.

Suggested scoring:

- 10: all mandatory constraints pass.
- 7-9: only measurement uncertainty exists, with no clear violation.
- 1-6: one or more constraints fail, with partial credit for unaffected sections.
- 0: missing/unparseable frontmatter, missing required fields, or body is empty.

## Output

Return this JSON object:

```json
{
  "dimension": "D6",
  "dimension_name": "compliance",
  "score": 10,
  "max": 10,
  "pass": true,
  "details": "Mandatory skill-name, description, and body constraints pass.",
  "sub_scores": {
    "name": 10,
    "description": 10,
    "body": 10
  },
  "issues": [],
  "tools_used": ["local"],
  "metadata": {
    "description_language": "cjk",
    "description_chars": 120,
    "description_tokens": 80,
    "body_lines": 180,
    "body_tokens": 2200
  }
}
```
