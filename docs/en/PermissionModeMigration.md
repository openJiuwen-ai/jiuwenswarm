# Removing `permission_mode`: Migration Guide

[中文](../zh/PermissionMode迁移指南.md)

The `permissions.permission_mode` config field (`normal` / `strict`) has been removed from the product. It previously controlled how `severity` (without an explicit `action`) was mapped to `allow` / `ask` / `deny`.

After this change, every user rule must declare `action: allow|ask|deny` explicitly. There is no longer any "bare severity" rule that gets implicitly mapped.

## Behavior change

Old (normal mode):

| severity | action |
|----------|--------|
| LOW | allow |
| MEDIUM | allow |
| HIGH | ask |
| CRITICAL | ask |

Old (strict mode):

| severity | action |
|----------|--------|
| LOW | allow |
| MEDIUM | ask |
| HIGH | ask |
| CRITICAL | deny |

New (no mode, all paths):

| severity | action |
|----------|--------|
| LOW | allow |
| MEDIUM | allow |
| HIGH | ask |
| CRITICAL | ask |

The new mapping equals old `normal`. Users coming from `strict` mode must add an explicit `action` to keep their old behavior on rules where they previously relied on the implicit mapping.

## Rule migration

If your rules previously had only `severity` without `action`:

```yaml
# Old (relied on normal|strict implicit mapping)
rules:
  - id: r_medium
    severity: MEDIUM   # normal → allow; strict → ask
```

Migrate to:

```yaml
# New (explicit action required)
rules:
  - id: r_medium
    severity: MEDIUM
    action: allow        # explicit; for old strict behavior, change to action: ask
```

If your rules already had `action`, no change is needed (explicit action always wins).

## Config-file migration

The default `permission_mode: normal` has been removed from `resources/config.yaml`, `resources/config.team.distributed.leader.yaml`, and `resources/config.team.distributed.teammate.yaml`. Deployed instances should drop the field:

```yaml
permissions:
  enabled: false
  schema: tiered_policy
  # permission_mode is no longer supported; leftover fields are silently ignored.
  ...
```

## Agent frontmatter migration

Agent `.md` files previously accepted `permission_mode: <string>` in YAML frontmatter. This is no longer parsed. Existing frontmatter with this field is silently ignored — no error is raised.

## Errors and rollback

There is no error path. Any leftover `permission_mode` field is silently ignored. To audit, grep:

```bash
grep -rn "permission_mode" jiuwenswarm/resources/ jiuwenswarm/.jiuwenswarm/agents/ 2>/dev/null
```

Any leftover values will have no effect.
