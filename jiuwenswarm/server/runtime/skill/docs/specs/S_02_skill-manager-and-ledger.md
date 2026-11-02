# SkillManager 核心与安装账本

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_02 / skill-manager-and-ledger |
| 关联模块 | jiuwenswarm/server/runtime/skill/skill_manager.py + workspace_provider.py + enterprise_state_lock.py + skilldev/state_utils.py |
| 最近一次修订日期 | 2026-10-16 |
| 关联 feature | 暂无（初版建档） |

## 范围 / 边界

SkillManager 的**路径布局、安装账本（`skills_state.json`）、启停、事务与并发控制、实体身份模型**。安装渠道细节（SourceRegistry / 市场渠道 / 产物验证 / 下载白名单）见 [[S_03]]；企业预置同步见 [[S_04]]；RPC 路由面见 [[S_01]] P2。

## 路径布局

构造时按 `workspace_dir` 是否传入走两条路径解析：

| 传入 | 解析 | 落点 |
|---|---|---|
| `workspace_dir`（harness adapter / 企业租户） | `Workspace(root_path).get_node_path(WorkspaceNode.SKILLS)`，ImportError 时 `Path(workspace_dir)/"skills"` | `<workspace>/skills/` |
| `None`（react adapter / 单测） | 全局默认 `get_agent_skills_dir()` | 全局 agent 目录 |

固定三件套（以 skills 目录为锚）：

```
<skills_dir>/
├── skills_state.json    # 安装账本（权威 JSON，见下节）
├── _marketplace/        # git 市场仓库 clone 落点（marketplace_add / _sync_marketplace_repos）
└── <skill_name>/        # 每个已安装技能一个目录（内含 SKILL.md）
```

**生产实例一律经 `SkillWorkspaceProvider.get_or_create_manager(workspace_dir)` 获取**（per-workspace 单例，`WeakValueDictionary` + `RLock` 双检），直接 `SkillManager()` 仅限单测。`SkillWorkspaceProvider.ensure` 是 fail-closed 的目录/账本准备器：空路径拒绝、企业版 `require_valid_state=True` 时校验账本必须是 dict、不存在时**原子初始化**空账本（mkstemp + fsync + `os.replace`）、最后以 `r+` 打开证明可读写。

## 数据结构：skills_state.json

账本是**单一 JSON 文件、四个桶**（`_EMPTY_STATE`，workspace_provider.py）：

```json
{
  "marketplaces":     [...],   // git 市场仓库注册表（add/remove/toggle/last_updated）
  "installed_plugins": [...],  // 市场渠道安装记录（skillnet / clawhub / teamskillshub / builtin / web）
  "local_skills":     [...],   // 本地导入 / 手工拷入记录（import_local / _register_unmanaged_local_skills）
  "skill_configs":    {...}    // per-skill 启停：{name: {"enabled": bool}}（默认 true 兼容）
}
```

### 实体记录与身份模型

安装实体（`installed_plugins` / `local_skills` 的元素）核心字段：`name`（磁盘目录名 = 技能内部名）、`origin`（来源标识：SkillNet URL / `clawhub:{owner}/{slug}` / `teamskillshub:{asset_id}` / builtin 名）、`source_type`（企业版区分 `prebuilt` / `user`）、`version` / `verification`（checksum 等）。

身份匹配规则（改匹配逻辑前必读）：

- `record_skill_installation`：**按 name upsert**（`replace_by_name`），安装成功后落账。
- `find_skill_dir_by_identity(skill_id, version)`：企业预置按三元组反查磁盘目录（[[S_04]]）。
- `_find_installed_by_target`（工具层，[[S_06]]）：按 origin / name / slug 多形态反查防重复安装；**ClawHub 禁止按展示名匹配**（防同名误判）。
- `_find_web_uninstall_installation`（web 卸载，`SKILLS_ENTERPRISE_UNINSTALL`）：origin 精确命中 `installed_plugins` 优先；旧 skillhub 记录只在 `local_skills` 有 origin 时认领。**半安装脏数据兜底**——只有 `local_skills`、没有 `installed_plugins` 的记录（安装中途失败遗留），在 name+origin 唯一命中本地记录时**合成一条 `source_type="user"` 安装记录**继续卸载；插件行已有别的 origin，或 origin 在两本账里都不存在时，不按 name 回退。
- `normalize_local_skills`（state_utils）：盘→账本对账时，`clawhub:` origin 记录按 **slug 精确判存活**（重名技能 name 会撞），其余来源按 name 兜底。

### 启停语义（state_utils 纯函数，`skilldev/state_utils.py`）

| 函数 | 语义 |
|---|---|
| `get/set_skill_enabled` | 读写 `skill_configs[name].enabled`；缺省 true（向后兼容） |
| `list_disabled_skills` | 账本中所有 `enabled=false` 的名字（**不管是否还安装着**） |
| `list_execution_disabled_skills` | disabled ∩ 已注册（`installed_plugins ∪ local_skills`）——**已安装且被禁用**，喂给 `SkillUseRail.disabled_skills` |
| `remove_skill_config` | 卸载时整条删除（防重装沿用旧禁用态） |
| `load_execution_disabled_skills` | 直接读盘版，供拿不到 manager 实例的消费方（swarm providers / evolution / symphony extension） |

**注意双语义**：`list_disabled_skills`（账本全量禁用名）与 `list_execution_disabled_skills`（已安装禁用名）不同——执行侧名单只关心磁盘上真实存在且禁用的技能。

## 不变量

- **N1（账本原子写）**：`_save_state` 一律临时文件 + `os.replace` + fsync；**禁止直接 `write_text` 覆盖账本**（skilldev 的 `store.py` 是反例，见 [[S_07]] 风险清单）。
- **N2（事务三层语义）**：账本变更必须经 `@_state_transactional` 装饰器或 `state_transaction()` 上下文。非企业或非持久化实例（`persist_skills_state=False`）直接 yield 不加锁；企业版在事务内**最外层重读一次权威 JSON**（`_load_state`），内层修改不丢弃。
- **N3（企业锁双层结构）**：`enterprise_skill_state_lock(state_file)` = 进程内 per-file `RLock`（normcase 路径键控，弱引用缓存）+ 跨进程 `portalocker.Lock`（`{state_file}.lock`，timeout 30s）；嵌套进入由 thread-local 深度计数短路（只有最外层取文件锁）。
- **N4（手工拷入自动登记）**：构造时 `_register_unmanaged_local_skills()` 把手工放入 skills 目录、未走安装流程的技能补登记到 `local_skills`（可展示/卸载/禁用）；**必须先于企业 builtin 清理执行**，否则手工技能与历史 builtin 共用目录名时会被误删。
- **N5（企业版无 builtin）**：`is_enterprise()` 时构造即 `_remove_enterprise_builtin_skills()`——仅清理旧版本登记的 builtin 记录，prebuilt / user / 已补登记 local **一律保留**。
- **N6（目录变更必落账）**：任何写 skills 目录的操作（安装提交 / 删除 / 预置同步 / builtin 清理）必须在同一事务内更新账本对应桶；只动盘不动账 = 下次对账时状态漂移。

## 接口契约

### skills.* RPC 路由（server/runtime/agent_adapter/interface.py `_SKILL_ROUTES`）

按域分组（handler 均为 `SkillManager.handle_skills_*` 方法）：

| 域 | ReqMethod → handler（节选） |
|---|---|
| 查询 / 启停 | `SKILLS_LIST` / `SKILLS_INSTALLED` / `SKILLS_GET` / `SKILLS_TOGGLE` / `SKILLS_ENTERPRISE_LIST` |
| 通用安装 | `SKILLS_INSTALL`（builtin 名分发）/ `SKILLS_UNINSTALL` / `SKILLS_IMPORT_LOCAL` / `SKILLS_ENTERPRISE_INSTALL→handle_skills_web_install` / `SKILLS_ENTERPRISE_UNINSTALL→handle_skills_web_uninstall` |
| marketplace（git） | `SKILLS_MARKETPLACE_LIST / ADD / REMOVE / TOGGLE` |
| SkillNet | `SKILLS_SKILLNET_SEARCH / INSTALL / INSTALL_STATUS / EVALUATE` + `SKILLS_ONLINE_SEARCH`（聚合） |
| ClawHub | `SKILLS_CLAWHUB_GET_TOKEN / SET_TOKEN / SEARCH / DOWNLOAD` |
| TeamSkillsHub | `SKILLS_TEAMSKILLS_HUB_INFO / INIT / VALIDATE / PACK / SEARCH / INSTALL / PUBLISH / DELETE` |
| Source SPI | `SKILLS_SOURCE_PROVIDERS / SOURCE_SEARCH / SOURCE_INSTALL`（企业版有镜像 `SKILLS_ENTERPRISE_SOURCE_*`，路由到同一 handler） |
| 更新 | `SKILLS_UPDATES_CHECK` / `SKILLS_UPDATE` |
| 检索 | `SKILLS_RETRIEVAL_STATUS / INDEX_BUILD / INDEX_CANCEL / SEARCH / TREE`（桥接 symphony，见 [[S_06]]） |
| Skill Graph | `SKILLS_GRAPH_BUILD / STATUS / GET / CANCEL`（handler `handle_skills_graph_*`，委托 `jiuwenswarm/symphony/service.py` 的 `get_swarm_symphony_service()`——技能图谱的后台构建 / 状态 / 获取 / 取消（保留检查点）；实现归 symphony 域，[[S_01]] 边界） |
| 演进 | `SKILLS_EVOLUTION_STATUS / GET / SAVE`（archives / rollback / rebuild 由 DeepAdapter 处理，**不经 SkillManager**） |

另有 `_PLUGIN_ROUTES`（`handle_plugins_*`）走同一 manager。

**新增 RPC 的三处同步**：`ReqMethod` 枚举（common/schema/message.py）+ `_SKILL_ROUTES` + `SkillManager.handle_skills_*`。

### 同步查询面（供 rails / tools 直接调用）

`get_local_skills` / `get_installed_plugins` / `list_skill_installations`（安装账本 DTO）/ `get_skill_meta(name)` / `is_builtin_skill(name)` / `list_enabled_skill_names()` / `find_skill_dir_by_identity` / `lookup_skill_md_author` / `remove_skill_directory(name)`。

### 安装后的 Agent 刷新

server/runtime/agent_adapter/interface.py 在 `SKILLS_INSTALL / SKILLS_UNINSTALL / SKILLS_SKILLNET_INSTALL` 成功后触发 Agent 重载；SkillNet 异步安装完成时经 `set_skillnet_install_complete_hook` 回调（见 [[S_03]]）。

## 执行路径全景

### P1：安装提交（通用骨架，渠道差异见 [[S_03]]）

```
渠道 handler（如 handle_skills_clawhub_download）
  → 下载 + 校验（白名单断言 → sha256 → HMAC，见 [[S_03]]）
  → 安全解压到 <skills_dir>/<name>/（zip-slip 防护）
  → _commit_*_entity / _add_local_skill（同一事务内）
  → record_skill_installation / _add_installed_plugin（落账）
  → 失败回滚：remove_skill_directory + remove_skill_installation（部分渠道，如企业 web install）
```

### P2：卸载

```
handle_skills_uninstall → _handle_skills_uninstall_sync
  → 内置技能（真正运行于 builtin 目录，is_builtin_skill）拒绝
  → 企业版：仅 source_type ∈ {prebuilt, user} 可操作，prebuilt 不可卸（见 [[S_04]]）
  → 按内部名/展示名大小写不敏感匹配；实际卸载用磁盘规范名（防大小写删不到目录）
  → remove_skill_directory + _remove_installed_plugin/_remove_local_skill + remove_skill_config（同一事务）
```

### P3：盘→账本对账

```
构造时：_register_unmanaged_local_skills（手工技能补登记，先于企业 builtin 清理）
读账本时：_load_state → _normalize_state → normalize_local_skills（clawhub 按 slug 判存活）
查询时：get_local_skills / _scan_local_skills / _scan_builtin_skills / _scan_marketplace_skills（四路扫描）
```

## 修改检查单

- 改账本桶结构 / 字段 → 同步 `state_utils.py` 对账函数 + `_normalize_state` + 本 spec 数据结构节。
- 改安装提交路径 → 确认"落盘 + 落账"在同一事务（N6）；企业版确认走 `_state_transactional`。
- 改实体身份匹配 → 核对 `_find_installed_by_target` / `normalize_local_skills` / `record_skill_installation` 三处的一致性；重名场景（ClawHub slug）必须按 origin 而非 name。
- 改卸载语义 → 核对 builtin 拒卸 / 企业 prebuilt 不可卸 / `remove_skill_config` 清理三条都保留。
- 改 `_save_state` / 锁实现 → 跑 `tests/unit_tests/agentserver/test_skill_manager_enterprise_state.py` + `test_skill_manager_enable_disable.py` + 企业多 Pod 场景验证（锁超时 30s）。

## 与其它 spec 的关系

- 实例化链与路由全景见 [[S_01]] P1/P2；渠道与安全见 [[S_03]]；企业预置与多租户见 [[S_04]]。
- `state_utils.py` 物理上位于 `skilldev/` 包内但**与流水线无关**（历史归置），被 SkillManager 反向引用以避免循环依赖——见 [[S_07]] 的边界说明。
