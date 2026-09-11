# Config layers and upgrades

For developers adding or changing config keys. End-user panel fields: [Configuration](Configuration.md). **Chinese doc is the source of truth:** [配置分层与升级](../zh/配置分层与升级.md).

Live config is a single user-root `config.yaml`. Repo / installer `jiuwenswarm/resources/` is a **copy source**; `get_config()` does not read the package. `.env` is not split.

There is no live `config.user.yaml`. A leftover overlay from production is folded (current allowlist leaves only) into `config.yaml` on startup, then deleted.

## Restart vs upgrade

Stamp: `config/.template.sha256` stores **packaged** template hashes (`config.yaml` + `builtin_rules.yaml`), **not** whether the user yaml still equals the template. Runtime writes to the user yaml (sandbox, HITL, desktop pipes, `last_*`) do **not** count as an upgrade on the next start. That mis-detect was the old production rule (overwrite whenever user bytes differed from the template).

Do **not** use the installer / app version as the copy2 signal. The question is “has this packaged template been ingested?”, not “is the exe new?”. Code-only bumps should not overwrite yaml; a template change with a forgotten version bump would never ingest. Log app version if needed; copy2 still follows template hashes.

Upgrade (`copy2`) only when: stamp missing; stamp ≠ current packaged hashes; user yaml missing; packaged `builtin_rules.yaml` exists but the user copy does not; `prepare_workspace(overwrite=True)` / `init -f`.

**Restart:** do not copy2; fold leftover overlay if present. Desktop waits for the stamp then re-patches xiaoyi / GaussPD / permission knobs if placeholders are missing. While the Security Center route stays hidden, desktop still forces `sandbox.enabled` false after allowlist restore; drop that call when the UI ships.

**Upgrade:** snapshot keep-set + allowlist → whole-file copy2 → restore both → write stamp → drop overlay → desktop waits for the stamp then re-patches pipes. Missing stamp + existing user yaml is one upgrade (first run of this scheme). `init -f` still restores the allowlist; to lock a knob, remove it from the list.

Desktop Gateway sets `JIUWENSWARM_SKIP_SYSTEM_FILE_SYNC=1` (no copy2). Ship desktop and framework **in the same package**.

## Allowlist

`_SCALAR_PATHS` + `LIST_PATHS` in `jiuwenswarm/common/config_split.py`. Purpose: on upgrade copy2, write back XiaoYi PC/phone **clicked** values that **differ from the current packaged template**. Not “this key is frozen forever”.

Now: `sandbox.enabled`; HITL `approval_overrides` / `file_guard.paths` (upsert by id / path onto the new template list). Not on the list: permission-profile knobs (desktop re-applies after copy2), xiaoyi / GaussPD / `auto_memory`, Web rules, keep-set keys.

Snapshot: scalars kept only if present and ≠ current package template; leftover overlay allowlist leaves win as-is; lists keep extras vs template.

**Force a new value for an allowlisted key**

1. Remove the path from the list → next upgrade copy2 does not restore it (template lock). A restart with an unchanged stamp keeps the old yaml until a real upgrade.
2. Leave it listed but the user value already equals the new template → snapshot skips it.
3. Leave the list unchanged and edit non-listed template keys → copy2 overwrites those anyway.

**Add to the allowlist:** XiaoYi PC/phone click only, key ↔ click; add to `_SCALAR_PATHS` or `LIST_PATHS`; write `config.yaml` (`update_config` or desktop); do not pre-write the template default; tests in `test_config_split_layers.py`; ship with desktop. Existing installs lose the old value on **this** upgrade if the key was not listed before.

**Remove from the allowlist:** drop the path, ship together. Next upgrade follows the template. Example: stop letting users own sandbox → delete `sandbox.enabled` from `_SCALAR_PATHS` and set the template default.

Pipes / GaussPD stay in desktop post-copy2 patches. `last_*` / `push_id` go in the keep-set, not the allowlist.

Write APIs all target `config.yaml`. Desktop `configFile()` is the sandbox + pipe file.
