---
name: skill-verifier
description: Run the verification gate on a skill under <workspace>/skill/. Returns a structured JSON summary with per-stage PASS/FAIL details. Provides three entry points — validate (local spec check only), safety_scan (remote risk scan for a given URL), and gate (full best-effort pipeline). Use whenever a skill needs spec validation, packaging, uploading, or risk scanning. Never creates or edits skill content — only checks and packages.
---

# Skill Verifier

Pure verification gate for skills. Does not create, edit, or interact — only checks, packages, uploads, and scans.

## Three entry points

### 1. `validate` — local spec check (cheap, fast)

```bash
cd "<skill-verifier-dir>" && python3 -m scripts.validate <workspace>
```

Checks SKILL.md frontmatter (name, description, allowed keys), body length, and static security. Returns `Validation passed.` or `Validation failed:` with details. 

### 2. `safety_scan` — remote risk scan for a known URL

```bash
cd "<skill-verifier-dir>" && python3 -m scripts.safety_scan <skill-name> <url>
```

Submits a URL to the platform risk-scanning service and polls for a result. Returns `Safety scan passed.` or `Safety scan failed:` with details. Use this when the skill package URL is already available (e.g. from an uploaded import package), saving one upload round-trip.

### 3. `gate` — full best-effort pipeline

```bash
cd "<skill-verifier-dir>" && python3 -m scripts.gate <workspace>
```

Orchestrates the complete verification pipeline in best-effort mode:

```
validate (local)
  └─ result recorded (PASS or FAIL with details)
package (zip with dependency references, excludes *.bak-*)
  └─ runs regardless of validate result
  └─ FAIL → upload and safety_scan marked as "skipped"
upload (to OBS, returns CDN URL)
  └─ runs only if package succeeded
  └─ FAIL → safety_scan marked as "skipped"
safety_scan (remote, uses uploaded URL)
  └─ runs only if upload succeeded
  └─ result recorded (PASS or FAIL with details)
```

Outputs a structured JSON summary after `---GATE_RESULT_JSON---`. On full success, also prints the packaged file path and uploaded URL. On any failure, lists the failed stages. The gate does not block delivery — it reports results for the user to review.

## Spec rules (single source of truth)

The full specification for SKILL.md structure, frontmatter constraints, and description limits is in `references/skill_spec.md`. The validation scripts use this file as the canonical rule set.

## Hard rules

1. Never create or modify skill content — this is a read-only gate.
2. Never interact with the user — return structured PASS/FAIL output only.
3. Never skip `validate` before `package` in the gate pipeline.
4. Continue to next stage on failure where possible; mark dependent stages as skipped when their prerequisite output is unavailable.
