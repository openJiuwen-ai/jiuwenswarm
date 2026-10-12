---
name: runtime-config-check
description: Diagnose JiuwenSwarm model configuration files and Windows dotenv encoding/path problems with a read-only, secret-safe offline check. Use when initial model calls fail or configuration seems ignored; does not test API credentials or network availability.
---

# Runtime Config Check

Use this Skill for an explicitly specified JiuwenSwarm dotenv file. Resolve
`scripts/check_env.py` relative to this file; do not hard-code installation paths.
Ask for the file path only when the active instance's path is not known.

Run the helper with the project's Python environment:

```text
python <skill_root>/scripts/check_env.py --dotenv <config-file>
```

Explain the reported problem categories without showing config values. The
helper is offline and read-only: a pass does not prove API authentication,
provider/model availability, correct effective environment, or running services.
Read `docs/zh/WindowsDotenvTroubleshooting.md` in the source repository for the
framework's BOM handling and Windows path examples when available.

Do not dump the file or environment, transmit keys, change configuration, kill
processes, or install dependencies merely to diagnose. Offer a minimal fix;
apply configuration changes only when the user requests them. Distinguish the
dotenv file's contents from shell overrides and the active instance path.
