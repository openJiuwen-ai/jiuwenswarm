---
name: skill-compass
description: Run static evaluation for one workspace skill. Score SKILL.md structure, trigger quality, security, functional value, comparative value, and mandatory spec compliance, then save snapshot reports under evals/static/.
---

# Skill Compass

Evaluate one skill at a time, without modifying it, and write a static quality report to `<workspace>/evals/static/`.

This skill owns only D1-D6 scoring, verdict calculation, suggestions, and report writing. The caller decides when to run it.

## Inputs

- `<workspace>`: The current SkillDev workspace.
- `<skillPath>`: Path to the target `SKILL.md`; default to `<workspace>/skill/<skill-name>/SKILL.md`.
- `<packagedPath>`: Optional packaged artifact under `<workspace>/output/`; include it in report metadata when present.
- `{baseDir}`: The directory containing this `skill-compass/SKILL.md`.

If only a packaged `.zip` or `.skill` file is provided, prefer the source skill under `<workspace>/skill/` when it exists. If source is missing, inspect the package safely without extracting outside the workspace.

When substituting Windows paths into `node -e` commands, replace backslashes with forward slashes or JSON-escape the path string so JavaScript does not treat `\` as an escape.

## Dimensions

| ID | Dimension   | Weight | Purpose |
|----|-------------|--------|---------|
| D1 | Structure   | 10%    | Frontmatter validity, markdown format, declarations |
| D2 | Trigger     | 15%    | Activation quality, rejection accuracy, discoverability |
| D3 | Security    | 20%    | Gate dimension: secrets, injection, permissions, exfiltration |
| D4 | Functional  | 30%    | Core quality, edge cases, output stability, error handling |
| D5 | Comparative | 15%    | Value over direct prompting, with vs without skill |
| D6 | Compliance  | 10%    | Mandatory skill-name format, description, and body limits |

## D6 Mandatory Constraints

Score D6 against these exact constraints:

- `skill-name`: 1-64 characters, only `[a-z0-9-]`, must not start/end with `-`, must not contain consecutive `--`, and must exactly match the parent directory name.
- `description`: Chinese/CJK text must be <=256 characters and <=300 tokens; English/non-CJK text must be <=512 characters and <=300 tokens.
- Body: `SKILL.md` body after frontmatter must be <=500 lines and <=5000 tokens.

Any D6 violation means `compliance.pass = false` and the final verdict is `FAIL`, even if the weighted score is otherwise high.

## Procedure

Skill Compass requires Node.js. Before running the evaluation, verify that a usable Node.js installation is available on the current system; if it is not, install Node.js before continuing.

1. Load the target `SKILL.md`. If the file does not exist, stop with `Error: File not found: <skillPath>`.
2. Parse YAML frontmatter. If malformed, continue with D1 frontmatter score 0 and record the parse error.
3. Run local pre-analysis:
   ```bash
   node -e "const {BasicValidator} = require('{baseDir}/lib/basic-validator.js'); const r = new BasicValidator().validateBasics('{skillPath}'); console.log(JSON.stringify(r, null, 2));"
   ```
4. Evaluate D1 Structure with the local validator first:
   ```bash
   node -e "const {StructureValidator} = require('{baseDir}/lib/structure-validator.js'); const r = new StructureValidator().validate('{skillPath}'); console.log(JSON.stringify(r, null, 2));"
   ```
   For borderline local scores 5-7, supplement with `{baseDir}/prompts/d1-structure.md`.
5. Evaluate D2 Trigger:
   ```bash
   node -e "const {TriggerValidator} = require('{baseDir}/lib/trigger-validator.js'); const r = new TriggerValidator().validate('{skillPath}'); console.log(JSON.stringify(r, null, 2));"
   ```
   For complex cases, supplement with `{baseDir}/prompts/d2-trigger.md`.
6. Evaluate D3 Security with local + LLM supplementation:
   ```bash
   node -e "const {SecurityValidator} = require('{baseDir}/lib/security-validator.js'); const r = new SecurityValidator().validate('{skillPath}'); console.log(JSON.stringify(r, null, 2));"
   ```
   Read `{baseDir}/prompts/d3-security.md` for LLM supplementation. Merge findings, dedupe by `(location, check_type)`, keep the highest severity, then mechanically map findings to score using `{baseDir}/shared/scoring.md`.
7. Evaluate D4 Functional using `{baseDir}/prompts/d4-functional.md`.
8. Evaluate D5 Comparative using `{baseDir}/shared/llm-capability-baseline.md` and `{baseDir}/prompts/d5-comparative.md`; mechanically map delta to score using `{baseDir}/shared/scoring.md`.
9. Evaluate D6 Compliance with the local validator:
   ```bash
   node -e "const {ComplianceValidator} = require('{baseDir}/lib/compliance-validator.js'); const r = new ComplianceValidator().validate('{skillPath}'); console.log(JSON.stringify(r, null, 2));"
   ```
   If the local validator cannot run, read `{baseDir}/prompts/d6-compliance.md` and apply the same checks manually. Do not supplement D6 with uniqueness analysis.
10. Aggregate scores:
    ```text
    D1_contribution = D1 * 0.10 * 10
    D2_contribution = D2 * 0.15 * 10
    D3_contribution = D3 * 0.20 * 10
    D4_contribution = D4 * 0.30 * 10
    D5_contribution = D5 * 0.15 * 10
    D6_contribution = D6 * 0.10 * 10
    sum_contributions = D1_contribution + D2_contribution + D3_contribution + D4_contribution + D5_contribution + D6_contribution
    overall_score = round_half_up(sum_contributions)
    ```
    `round_half_up` means round to the nearest integer, with `.5` rounded upward; for example, `round_half_up(80.5) = 81`.
11. Apply verdict rules in order:
    - `FAIL`: `D3.pass == false`, or `D6.pass == false`, or `overall_score < 50`
    - `CAUTION`: D3 has any High-severity finding, or `50 <= overall_score < 70`
    - `PASS`: `overall_score >= 70`, `D3.pass == true`, `D6.pass == true`, and no D3 High findings
12. Identify the weakest dimension. On ties, use priority:
    `security > compliance > functional > trigger > structure > comparative`.
13. Generate at most 5 improvement suggestions for dimensions scoring below 8. Suggestions belong only in `static_report.md`; do not save them in `static_report.json`.
14. Before saving JSON or Markdown, recompute every contribution, `sum_contributions`, and `overall_score` from the final D1-D6 scores using the exact formula in step 10. If any drafted contribution, total, or `overall_score` differs from the recomputed value, replace it and use the recomputed value for verdict rules and both reports. Do not save inconsistent scores. Before saving `static_report.md`, verify that both `{Impact summary}` and every `{suggestion text}` entry use the same language as the latest user request; if either does not, discard and regenerate those placeholder values before writing the file.
15. Save both outputs:
    - JSON: `<workspace>/evals/static/static_report.json`
    - Markdown summary: `<workspace>/evals/static/static_report.md`
    Create `<workspace>/evals/static/` if needed and overwrite the two report files on every run. If `static_report.json` or `static_report.md` already exists, do not open, read, parse, or reuse its content; delete all original file content first, then write only the new complete report data from the current run. Do not append, merge, patch, preserve previous fields or sections, or read existing reports to create before/after comparisons.
    Treat every run as a fresh snapshot, including runs after the target skill was optimized from a previous static report.
    Step 15 is not complete until Step 16 has been executed. After writing the report files, immediately continue to Step 16; do not stop, present results, or hand off before Step 16 validation succeeds.
16. After saving `static_report.json`, immediately validate that the file is parseable JSON and contains no `reviewed` field anywhere. If parsing fails for any reason, discard the invalid JSON draft, regenerate `static_report.json` from the schema and final scores, overwrite the file, and validate it again before continuing. If a `reviewed` field is present, remove it, overwrite `static_report.json`, and validate the cleaned file before continuing. Do not present or use an invalid JSON file.

## JSON Output

### Schema Contract

`static_report.json` is the machine-readable source of truth for the evaluation result. It must conform to `{baseDir}/schemas/eval-result.json` exactly, using the final recomputed scores and verdict from the evaluation flow above.

Include these required result areas:

- `skill_name`, `skill_path`, `skill_type`
- `scores.structure`, `scores.trigger`, `scores.security`, `scores.functional`, `scores.comparative`, `scores.compliance`: each value must be only the final integer score from 0 to 10
- `overall_score`, `verdict`, `weakest_dimension`
- `metadata`: include `evaluated_at`, `evaluator: "skill-compass"`, and `packaged_path` when available

Each field must use the type and value constraints declared in the schema. Under `scores`, save only the six dimension score numbers; each score value must be an integer, not an object, array, string, or diagnostic record. `verdict` and `weakest_dimension` must use the allowed enum values, and `metadata.evaluator` must be exactly `"skill-compass"`.

The JSON output must contain only fields allowed by `{baseDir}/schemas/eval-result.json`. Do not add `suggestions` or comparison fields such as `previous_score`, `before_score`, `after_score`, `delta`, or `improvement` anywhere in the JSON.

### Strict JSON Serialization

`static_report.json` must be saved as strict parseable JSON on disk, not as a Markdown example or a loose JavaScript/Python object. The adapter reads this file with a JSON parser, so any invalid character, missing comma, trailing comma, or unescaped quote makes the report unusable.

- The file must contain one complete JSON object only. Do not wrap it in Markdown fences, explanatory text, comments, or partial snippets.
- Use strict JSON syntax: double-quoted property names, double-quoted string values, lowercase `true`/`false`/`null`, commas between all object properties and array items, and no trailing commas.
- Escape every double quote, backslash, and control character inside string values according to JSON rules. When human-readable text needs quoted phrases, prefer single quotes inside the string or escape double quotes as `\"`.
- Do not use Python/JavaScript-only syntax such as `True`, `False`, `None`, `undefined`, single-quoted strings, comments, or unquoted keys.
- Before writing the final file, mentally parse each object and array boundary to ensure every `{`, `}`, `[`, `]`, `:`, and `,` is in a valid position.

## Markdown Summary

Use this exact shape as the complete `static_report.md` output. Do not add any content outside this template:

```markdown
## Skill Compass Report: {skill_name}

Score: {overall_score}/100 | Verdict: {verdict}

### Dimension Scores

  D1 Structure     {score}/10  {bar}
  D2 Trigger       {score}/10  {bar}
  D3 Security      {score}/10  {bar}  {pass ? "PASS" : "GATE FAIL"}
  D4 Functional    {score}/10  {bar}
  D5 Comparative   {score}/10  {bar}
  D6 Compliance    {score}/10  {bar}  {pass ? "PASS" : "GATE FAIL"}

### Weighted Breakdown

  D1 Structure     {D1} x 10% x 10 = {D1_contribution}
  D2 Trigger       {D2} x 15% x 10 = {D2_contribution}
  D3 Security      {D3} x 20% x 10 = {D3_contribution}
  D4 Functional    {D4} x 30% x 10 = {D4_contribution}
  D5 Comparative   {D5} x 15% x 10 = {D5_contribution}
  D6 Compliance    {D6} x 10% x 10 = {D6_contribution}

  Total: round_half_up({sum_contributions}) = {overall_score}/100

### Weakest: {Dimension Name} ({score}/10)

{Impact summary}

### Improvement Suggestions

1. **{Dimension Name}** ({score}/10): {suggestion text}
```

Bar format: 10 chars wide, filled in proportion to score. Use ASCII bars such as `[#######---]`.
Weighted breakdown consistency is mandatory: each displayed contribution must equal its displayed dimension score times its weight times 10, `{sum_contributions}` must equal the sum of the six displayed contributions, and `{overall_score}` must equal `round_half_up({sum_contributions})`.
Use this Markdown template exactly for every run, including after optimization. Fill placeholders only; do not append content before, between, or after the template sections. Do not add extra sections, comparison tables, score-delta summaries, or phrases like "优化完成", "主要改进内容", "文件统计", "预期评估提升", "提升了多少", "improved by", "before/after", or "delta".

The Markdown output may contain only these headings:

- `## Skill Compass Report: {skill_name}`
- `### Dimension Scores`
- `### Weighted Breakdown`
- `### Weakest: {Dimension Name} ({score}/10)`
- `### Improvement Suggestions`

Mandatory language rule for placeholders: `{Impact summary}` and each `{suggestion text}` must match the language of the current user request. Use Chinese for Chinese requests and English for English requests. This applies to every explanatory sentence, quoted phrase, replacement wording, example trigger description, and proposed `description` snippet inside those placeholders.

## Rules

1. Do not modify the target skill.
2. Do not ask interactive follow-up questions during evaluation.
3. Evaluate only one skill per run. If multiple are provided, evaluate the first and note that only one was evaluated.
4. Always write the report files under `<workspace>/evals/static/`, creating the directory if needed.
5. Keep JSON field names and fixed Markdown template labels/headings in English. Free-text Markdown content, especially `{Impact summary}` and every `{suggestion text}`, must match the current user request language.
6. Score consistency is mandatory in every output. If any D1-D6 score changes after drafting, immediately recompute the weighted breakdown contributions, weighted total, `overall_score`, and verdict before presenting the result.
7. Static reports are snapshot reports, not comparison reports. Even after optimization, regenerate JSON and Markdown from the original schema/template and do not include improvement amounts or before/after commentary.
8. If a drafted Markdown report does not match the template above, discard it and regenerate `static_report.md` before saving.
