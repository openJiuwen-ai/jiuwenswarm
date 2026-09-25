# 移除 `permission_mode` 配置项：迁移指南

[English](../en/PermissionModeMigration.md)

`permissions.permission_mode`（`normal` / `strict`）配置项已从产品中移除。该字段原用于控制"无显式 action 时，severity 如何映射到 allow/ask/deny"。

迁移后，所有用户规则必须显式声明 `action: allow|ask|deny`，不再有"裸 severity"规则被静默映射的歧义。

## 行为变化

旧版（normal 模式）：

| severity | 动作 |
|----------|------|
| LOW | allow |
| MEDIUM | allow |
| HIGH | ask |
| CRITICAL | ask |

旧版（strict 模式）：

| severity | 动作 |
|----------|------|
| LOW | allow |
| MEDIUM | ask |
| HIGH | ask |
| CRITICAL | deny |

新版（无 mode，所有模式统一）：

| severity | 动作 |
|----------|------|
| LOW | allow |
| MEDIUM | allow |
| HIGH | ask |
| CRITICAL | ask |

新版与旧版 normal 模式行为一致。strict 模式用户必须把对应规则**显式**加上 `action` 才能保留旧行为。

## 用户规则迁移

如果你之前的规则只写了 `severity` 没有 `action`：

```yaml
# 旧版（依赖 normal|strict 隐式映射）
rules:
  - id: r_medium
    severity: MEDIUM   # 在 normal 下 → allow；在 strict 下 → ask
```

迁移为：

```yaml
# 新版（必须显式 action）
rules:
  - id: r_medium
    severity: MEDIUM
    action: allow        # 显式选择行为；如要旧版 strict 行为，改为 action: ask
```

如果你之前的规则已经写了 `action`，无需修改（显式 action 始终优先）。

## 配置文件迁移

`resources/config.yaml`、`resources/config.team.distributed.leader.yaml`、`resources/config.team.distributed.teammate.yaml` 已删除 `permission_mode: normal` 默认值。已部署实例需要：

```yaml
permissions:
  enabled: false
  schema: tiered_policy
  # 不再支持 permission_mode 字段；遗留字段将被静默忽略
  ...
```

## Agent frontmatter 迁移

agent `.md` 文件的 frontmatter 之前支持 `permission_mode: <string>` 字段，现已移除。已有 frontmatter 中的该字段会被解析器忽略，不会报错。

## 报错与回滚

无报错路径。`permission_mode` 字段残留会被静默忽略。如需审计，可 grep：

```bash
grep -rn "permission_mode" jiuwenswarm/resources/ jiuwenswarm/.jiuwenswarm/agents/ 2>/dev/null
```

任何残留值都不会生效。
