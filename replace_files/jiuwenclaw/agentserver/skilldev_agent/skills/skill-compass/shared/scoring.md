# Scoring Rules

> Single source of truth for dimension names, weights, formula, verdicts, and security gate.
> Referenced by all command and prompt files. Do NOT duplicate this information elsewhere.

## Canonical Dimension Table

| ID | Name        | JSON Key      | Weight |
|----|-------------|---------------|--------|
| D1 | Structure   | structure     | 10%    |
| D2 | Trigger     | trigger       | 15%    |
| D3 | Security    | security      | 20%    |
| D4 | Functional  | functional    | 30%    |
| D5 | Comparative | comparative   | 15%    |
| D6 | Compliance  | compliance    | 10%    |

## Scoring Formula

```
overall_score = round((D1 × 0.10 + D2 × 0.15 + D3 × 0.20 + D4 × 0.30 + D5 × 0.15 + D6 × 0.10) × 10)
```

Result is an integer 0-100.

## Verdict Rules

Evaluate in order. The FIRST matching rule wins (FAIL > CAUTION > PASS):

| Priority | Verdict | Condition |
|----------|---------|-----------|
| 1 | FAIL    | D3 pass == false (any Critical finding), OR D6 pass == false (mandatory compliance violation), OR overall_score < 50 |
| 2 | CAUTION | D3 has any High-severity finding (even if D3 pass == true and score >= 70) |
| 3 | CAUTION | 50 <= overall_score < 70 |
| 4 | PASS    | overall_score >= 70 AND D3 pass == true AND D6 pass == true AND no D3 High findings |

> **Rationale for Priority 2**: A skill with a known High-severity security issue
> should not receive a clean PASS verdict, even if its overall score is high.
> CAUTION signals that the skill works well but has security concerns the user should review.

## Security Gate

D3 is a **gate dimension**. If ANY finding has severity `"critical"`, set `D3.pass = false` and `verdict = "FAIL"` regardless of overall_score. This is non-negotiable.

## D3 Findings-to-Score Mapping

D3 score is computed mechanically from the merged findings list (local validator + LLM supplementation). Start at 10, apply deductions:

| Finding Severity | Deduction per Finding |
|------------------|-----------------------|
| critical         | → score = 0, pass = false (gate) |
| high             | -4 |
| medium           | -1 |
| low              | -0.5 |

`D3_score = max(0, 10 - sum(deductions))`. Round to nearest integer.

No judgment needed. The findings determine the score mechanically — same pattern as D5 Delta-to-Score.

## Compliance Gate

D6 is a mandatory compliance dimension. If any D6 check fails, set
`D6.pass = false` and `verdict = "FAIL"` regardless of overall_score.

## Improvement Priority Order

When multiple dimensions tie for lowest score, prioritize:
security > compliance > functional > trigger > structure > comparative

## D5 Delta-to-Score Mapping

| Delta Range        | Score |
|--------------------|-------|
| delta >= 0.45      | 10    |
| 0.40 <= delta < 0.45 | 9  |
| 0.35 <= delta < 0.40 | 8  |
| 0.30 <= delta < 0.35 | 7  |
| 0.25 <= delta < 0.30 | 6  |
| 0.20 <= delta < 0.25 | 5  |
| 0.15 <= delta < 0.20 | 4  |
| 0.10 <= delta < 0.15 | 3  |
| 0.05 <= delta < 0.10 | 2  |
| delta < 0.05       | 1    |

No judgment needed within ranges. The delta value determines the score mechanically.

## Unified Dimension Output Contract

Every dimension prompt outputs this JSON structure:

```json
{
  "dimension": "D{N}",
  "dimension_name": "{json_key}",
  "score": 0,
  "max": 10,
  "details": "...",
  "sub_scores": {},
  "issues": [],
  "metadata": {}
}
```
