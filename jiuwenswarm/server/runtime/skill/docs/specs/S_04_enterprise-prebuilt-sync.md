# 企业预置同步与多租户技能

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_04 / enterprise-prebuilt-sync |
| 关联模块 | jiuwenswarm/server/runtime/skill/skill_prebuilt.py + skill_manager.py（install_prebuilt_from_provider / install_skill_sync / find_skill_dir_by_identity） |
| 最近一次修订日期 | 2026-10-16 |
| 关联 feature | 暂无（初版建档） |

## 范围 / 边界

企业版（`is_enterprise()`）下**管理面预置技能模板 → 租户工作区的硬同步**：`SkillPrebuiltSynchronizer` 的同步语义、双安装路径、身份保护、租户判定，以及多租户下 per-workspace 的 SkillManager 形态。通用账本 / 事务锁见 [[S_02]]；安装渠道与产物验证见 [[S_03]]。

## 数据结构

### 预置模板（管理面下发 → 本地解析）

```
企业配置 _enterprise_config.skill_prebuilt
  → parse_agent_skill_prebuilt(...)            # skill_prebuilt.py
  → AgentSkillPrebuiltConfig(items=[SkillPrebuiltItem...])   # items_with_source 过滤可安装项
```

`SkillPrebuiltItem` 一等字段：`skill_id` / `package_url` / `source_id` / `version_id` / `enabled` + 过渡期兼容 `data.*`（`data.sha256` 仅用于 url 路径完整性校验；`data.author` 管理面已下发可直接落账）。

`install_mode` 二值：**provider 路径**（`source_id + skill_id + version_id` 三元组）或 **url 路径**（合法 http(s) `package_url`）。provider 优先；**provider 路径不得回退到 package_url**。

### 常量

`SOURCE_PREBUILT = "prebuilt"` / `SOURCE_USER = "user"`——账本 `source_type` 字段的两个关键值（[[S_02]]）。

### 租户判定

`is_skill_prebuilt_tenant(agent_id, service_id)`：**仅企业版**且租户 ID 有效，且**排除** `acp/global_acp` 与 `default/default` 两组保留租户。该判定同时被 `SkillToolkit`（[[S_06]]：企业版剔除 builtin 渠道）、卸载保护（prebuilt 不可卸）、启用集计算等多处消费。

## 同步语义（SkillPrebuiltSynchronizer）

### 触发点

`interface_deep.py` `create_instance`（应用租户 workspace 覆盖之后、技能预置 sync 之前）执行 `sync`，产出 `_enabled_skills`（启用技能快照）与 `_prebuilt_skills`（预置目录快照，供动态授权 trust 判定）。热刷新路径复用 `reconcile_disk_into_ledger`（仅对账 + 重算启用集，不跑模板 sync）。

### 串行化

`_skills_dir_sync_lock_for(skills_dir)`：按 skills_dir 路径的**进程级 `asyncio.Lock`**（元锁保护字典）——同一落盘目录同时仅一个 sync 在飞。跨 Pod 一致性由 [[S_02]] N3 的账本文件锁兜底。

### sync 三步（`sync → _run_sync`）

1. 逐项 `_ensure_prebuilt_installed`（见下）
2. `_remove_prebuilt_not_in_template`：**模板里没有的预置技能硬删盘 + 删账本（不降级回 user）**——预置集是管理面意志的完整镜像
3. 重算 `enabled_skill_dirs`（`SkillManager.list_enabled_skill_names`）与 `prebuilt_skill_dirs`

### `_ensure_prebuilt_installed` 关键行为

| 行为 | 契约 |
|---|---|
| 重装判定 `_should_download_prebuilt` | 按 `source_type=="prebuilt"` + `skill_id` 匹配已装记录；provider 路径比对 `version_id`，url 路径比对 origin + 账本 `verification.checksum_sha256`。无记录 / 目录缺失 / 版本不一致 / origin 或摘要不一致 → 重装 |
| 盘→账本回填 `_adopt_existing_dir_for` | 磁盘上已就绪（`SKILL.md` 存在）的同身份目录（`find_skill_dir_by_identity`）直接采纳登记，避免重复下载 |
| 目录就绪校验 `_skill_dir_ready` | 安装后必须 `SKILL.md` 存在才算成功 |
| **用户技能身份保护** | 已装记录 `source_type=="user"` 且同名 → `skill_name_conflict` **拒绝**（预置不得吞掉用户技能身份） |
| 作者回填 | 三源：账本 → 模板 → `lookup_skill_md_author`（SKILL.md frontmatter），避免覆盖 |
| 落账 | `record_skill_installation(replace_by_name=True)`；**url 路径账本写失败时回滚删除已落盘目录** |

### 双安装路径

- **provider**：`SkillManager.install_prebuilt_from_provider(source_id, skill_id, version_id)`——经 SourceRegistry 取 provider（[[S_03]]），**无 url 回退**。
- **url**：`SkillManager.install_skill_sync(package_url, True, None, sha256, protected_source_types={user})`——`protected_source_types` 保证 url 路径同样不吞用户技能。

## 多租户形态

```
TenantAgentPool（企业多租户）
  → per-tenant TenantContext → TenantAgentPool 内 per-tenant AgentManager
  → 每个租户 workspace 独立：SkillWorkspaceProvider.ensure(require_valid_state=True)
                             → get_or_create_manager（per-workspace 单例，[[S_01]] P1）
  → 企业版无 workspace 直接 raise（server/runtime/agent_adapter/interface.py 构造期 fail-fast）
```

- 企业版构造 SkillManager 时 `require_valid_state=True`（账本必须已是合法 dict，[[S_02]]）。
- 企业版不提供 builtin：构造期 `_remove_enterprise_builtin_skills` 仅清旧版 builtin 记录，prebuilt / user / local 全保留（[[S_02]] N5）。
- 企业版对 `skills.enterprise.install / uninstall`（web 装卸）有专属 RPC 面（[[S_02]] 路由表）；`SkillToolkit` 的企业形态剔除 builtin 渠道 + `prebuilt_not_removable` 卸载保护（[[S_06]]）。

## 不变量

- **N1（模板是完整镜像）**：预置集 = 管理面模板的完整投影——模板移除即硬删（不降级、不残留）；不要给"模板外预置"留存活路径。
- **N2（用户技能不可侵犯）**：预置安装与同步在任何路径下不得覆盖 / 吞并 `source_type=="user"` 的已装记录（`skill_name_conflict` + `protected_source_types` 双保险）。
- **N3（prebuilt 不可卸 / 不可由用户启停）**：卸载与启停入口对 `source_type=="prebuilt"` 记录一律拒绝；管理面更新模板是预置技能变更的唯一合法通道。
- **N4（provider 无回退）**：provider 三元组路径失败即失败，不得静默降级到 `package_url`（两者信任模型不同：provider 带 HMAC 验签，url 仅 sha256）。
- **N5（同目录单飞）**：同一 skills_dir 的 sync 经进程级锁串行；跨 Pod 由账本文件锁兜底。两层锁都不可省。

## 修改检查单

- 改模板解析（新增字段 / data.* 迁移）→ 核对 `parse_agent_skill_prebuilt` 的过渡期兼容 + `_ensure_prebuilt_installed` 的作者回填三源。
- 改删除语义 → 必须保 N1（硬删）与 N2（user 保护）同时成立；`_remove_prebuilt_not_in_template` 只删 `prebuilt` 记录。
- 改安装路径 → provider/url 双路径的验证强度差异（HMAC vs sha256）不可拉平（N4）。
- 改租户判定 → 核对 `acp/global_acp`、`default/default` 保留租户排除逻辑与所有消费点（toolkits / interface_deep 授权 / 卸载保护）。
- 企业多 Pod 场景改动 → 验证 sync 并发（不同 Pod 同时 sync 同一租户）走账本文件锁仲裁。

## 与其它 spec 的关系

- 依赖 [[S_02]]（账本 / 锁 / 身份）与 [[S_03]]（provider 安装 / url 下载验证）。
- 授权侧：`_prebuilt_skills` 快照喂给 `SkillAuthorizationRail` 的 trust 判定（prebuilt 目录 → `SkillTrustLevel.BUILTIN`，见 [[S_05]]）。
