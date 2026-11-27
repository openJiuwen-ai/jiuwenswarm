# Skill Specification (Single Source of Truth)

This document defines the canonical rules for a valid skill package. The validation scripts use this file as the authoritative rule set.

## Directory structure

```text
<skill-name>/
├── SKILL.md       required — YAML frontmatter + body instructions
├── scripts/       optional — deterministic or repeated operations
├── references/    optional — load-on-demand domain docs, schemas, API details
└── assets/        optional — templates, icons, fonts used in outputs
```

## Frontmatter constraints

The YAML frontmatter (between `---` markers) defines the skill metadata.

### Allowed keys

Only these top-level keys are permitted: `name`, `description`, `license`, `allowed-tools`, `metadata`, `compatibility`. No duplicate keys.

### `name` (required)

- Machine-readable identifier, not a display title.
- Pattern: `^[a-z0-9-]+$` (lowercase letters, digits, hyphens only).
- Must not start or end with `-`, must not contain `--`.
- Maximum 64 characters.
- Must exactly match the skill directory name.
- Chinese or non-ASCII names are never allowed — use `description` for localized display.

### `description` (required)

- Non-empty string; no angle brackets (`<` or `>`).
- This is the **only triggering mechanism** — all "when to use" guidance goes here.
- Character limits (MUST):
  - Contains CJK: ≤ 512 characters.
  - Otherwise: ≤ 1024 characters.
- Style: imperative, slightly pushy. Focus on user intent and when to trigger.

### `metadata` (optional)

External dependencies declared here:
- `metadata.tools`: list of `{bundleName, toolName}` for function tools.
- `metadata.agents`: agent tool references.
- `metadata.clis`: CLI tool references.

## Body constraints

The body is everything after the closing `---` of the frontmatter.

- Must not be empty.
- Maximum 500 lines.
- Keep under ~300 lines when possible; bulk material goes to `references/`.

## Static security rules

Checked on `SKILL.md` and all files under `scripts/`:

### Dangerous command patterns (in scripts/ only)

- `rm -rf /` (forced recursive root deletion)
- `chmod 777` (world-writable permissions)
- `curl ... | bash` (piped remote shell execution)
- `eval(` (dynamic eval execution)

### Hardcoded credentials (in SKILL.md and scripts/)

- Patterns like `api_key = "..."`, `secret = "..."`, `token = "..."`, `password = "..."`
- Strings matching `sk-[A-Za-z0-9_-]{16,}`

### Path traversal

No relative path component in the skill package may contain `..`.
