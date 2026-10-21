# 同事办公目录读写访问设计方案

## 1. 元信息

| 项 | 值 |
| --- | --- |
| 建档日期 | 2026-10-15 |
| 范围 | jiuwenswarm：`server/jiuwenmate/ports/workspace_access.py`、`spec_provider.py`、`service.py`、`models.py`、`execution_driver.py`、`agent_manager.py`、`web_adapter.py`；前端 `channels/web/frontend/src/features/jiuwenmate/` 与 jiuwenmate 文案 |
| 方案成熟度 | 已确认。只读部分（2026-10-15）：目录只建立不导入文件；Web 请求可读取整个办公目录。2026-10-20 补充：飞书私聊仅可读取当前发布版本中所选知识副本。写入部分（2026-10-15）：整个办公目录可写，另设本会话私有草稿区；只开放 Web 请求；写入不经人工确认，依赖意图记账与条件写入 |
| 计划条目 | [JM-14](../designs/jiuwenmate-development-plan.md)：同事资源选择与隔离；本文覆盖办公目录读写、资源装配及 Bash / 自动飞书 CLI 补充范围 |
| 负责人 | 常钰聪 |
| 状态 | 进行中（办公目录读写、资源装配已有实现；2026-10-20 补充 Bash 与飞书 CLI 自动准备，见第 12 节；真实飞书创建与浏览器验收未做） |
| 设计依据 | [JiuwenMate 服务端架构设计](../designs/jiuwenmate-architecture.md) 第 4.2、9.2 节；agent-core `persistent_agent/workspace/`（PA-14、PA-16）与 `execution/tool_intent_rail.py`（PA-15） |
| 关联 feature | 前置：[F_01 核心契约适配](F_01_core-contract.md)、[F_02 同事定义与配置发布](F_02_colleague-management.md)、[F_04 事件提交端口](F_04_commit-port.md)、[F_07 会话调度与执行驱动](F_07_execution-driver.md)；页面：[F_10 同事管理页面](F_10_web-colleague-management.md) |
| 验证摘要 | 文件工具历史证据见第 8 节；Bash / 自动飞书 CLI 的本轮证据与遗留见第 12.6 节 |
| Refs | 无独立 issue；需求来源为开发计划 JM-14 |

## 2. 背景、目标与范围

第 2～8 节保留最初的受控文件工具方案；第 11 节补充资源装配，第 12 节记录当前 Bash 扩展。文件工具的范围拒绝与条件写入仍有效，但不能约束新增的本地 Bash，不应将前文文件工具的隔离保证理解为所有工具的共同保证。

### 2.1 背景与问题

S1 私聊闭环要求同事能够读取用户提供的资料。此前 `spec_provider.HOST_OPEN_FEATURES` 只开放 `construction`，发布的 Spec 关闭 `enable_sys_operation`，执行端口不提供 `workspace_access`，同事没有任何文件工具。

core `9eb336b4` 已提供 PA-14：Spec 声明 `workspace_read` 且开启 `enable_sys_operation` 时，`PersistentWorkspaceReadRail` 只注册 `read_file` 与 `list_files` 两个工具。每次调用前，工具向宿主的 `workspace_access.resolve_read_scope(execution)` 取得读取范围，经 `validate_read_scope` 核对身份后，由 `resolve_read_path` 拒绝绝对路径、`..` 与越出范围的符号链接。宿主只需提供范围，不实现路径检查。

同一版本 core 也提供了 PA-16：Spec 声明 `workspace_write` 时，`PersistentWorkspaceWriteRail` 注册 `write_file` 与 `edit_file`。每次写入前，该 Rail 向宿主的 `workspace_write_access.resolve_write_scope(execution)` 取得写入范围，由 `resolve_write_path` 检查路径，并读取目标文件的内容摘要作为条件写入的前提；随后 PA-15 的 `PersistentToolIntentRail` 记账 `tool.intent` 并等待落盘，成功后才实际写入。文件在预检之后被改动时，写入返回 `version_conflict`。JiuwenSwarm 的 `HostCommitPort` 已在 `85b0e66d` 中验证：意图先落盘再产生副作用，落盘失败时副作用为零。

### 2.2 目标与关键场景

| 场景 / 触发条件 | 当前行为 | 目标行为 / 可观察变化 |
| --- | --- | --- |
| 用户在 Web 私聊中请同事查看资料 | 同事没有文件工具 | 同事用 `list_files`、`read_file` 读取自己办公目录中的文件 |
| 用户在 Web 私聊中请同事生成或修改文件 | 同事只能读取 | 同事用 `write_file`、`edit_file` 在自己办公目录中创建和修改文件；每次写入前记账 `tool.intent` |
| 两次写入针对同一文件，且文件在预检后被改动 | 不适用 | 后写入的一方得到 `version_conflict`，不覆盖他人的修改 |
| 访问其他 Session 的私有草稿 `sessions/<其他 session_id>/` | 不适用 | 读写都返回 `forbidden` |
| 模型请求办公目录之外的路径（`..`、绝对路径、指向外部的符号链接） | 不适用 | 工具返回 `forbidden`，回复中不含目录外内容与宿主路径 |
| 飞书群聊请求 | 不适用 | 读取与写入范围均为 `forbidden`，工具不访问任何文件 |
| 飞书私聊请求且当前发布版本选择了知识页面 | 之前读写范围均为 `forbidden` | 读取范围仅包含该发布版本的 `resources/knowledge/<摘要>/`；办公目录其他位置不可读，写入仍为 `forbidden` |
| 用户需要知道文件放在哪里 | 无入口 | 同事详情页“知识”分区显示办公目录的本机路径 |
| 删除同事 | 删除版本、Template 与窗口数据 | 同时删除其办公目录（含私有草稿） |

### 2.3 本次范围与非目标

- 本次包含：每个同事一个办公目录；读取与写入范围端口；`workspace_read`、`workspace_write` 开放与端口校验；Session 私有草稿区的隔离；发布时开启文件工具并补充路径说明；Web 运行时启动时重新发布；页面显示目录。2026-10-20 补充：飞书私聊可读取其配置版本选定的知识副本，不开放办公目录其他内容；群聊权限不变。
- 本次不包含：
  - 文件上传或导入：用户自行把文件放入目录（负责人确认）；
  - 飞书请求读写：S2b 再确定群聊的授权来源（负责人确认）；
  - Skill、MCP、插件装配：`SkillUseRail` 会同时注册 `read_file`、代码与命令工具，随 S2b；
  - 写入前的人工确认：PA-09 本轮不开发，写入依赖意图记账与条件写入（负责人确认）；
  - 引导模型使用私有草稿区：模型无法得知本会话的 `session_id`（见第 3 节）；
  - 删除会话时清理私有草稿：会话删除为软删除，窗口数据也保留；
  - 修改 agent-core。

## 3. 设计约束、假设与待决问题

- **已确认约束与依赖：**
  - core 每次读取或写入前都重新调用 `resolve_read_scope` / `resolve_write_scope`，宿主不缓存决定；`validate_read_scope` / `validate_write_scope` 要求 `agent_id`、`session_id`、`principal_ref`、`policy_revision` 与本次 Execution 一致。
  - 范围端口抛出异常时，core 按 `unknown` 拒绝且不返回原因。
  - `enable_sys_operation=True` 时，core 还会在全局资源中登记 shell 与文件系统操作（如 `execute_cmd`），但不作为本同事的能力挂载；同事的能力为 `list_files`、`read_file`、`write_file`、`edit_file`。
  - 写入 Rail 要求 SysOperation 的文件系统声明 `supports_conditional_write`；本机文件系统满足该条件，写入时自动创建父目录。
  - core 的 `read_file` 工具描述要求绝对路径，而路径检查拒绝绝对路径。
  - 构建 Agent 时，core 默认在后台探测模型是否支持图片输入，该调用不经过预算端口；`enable_read_image_multimodal=False` 可关闭。
  - 系统提示按同事版本发布、由各 Session 共用；core 的执行头（PA-06）只含 `request_id`、`execution_id` 与来源，不含 `session_id`。因此模型无法得知本会话私有草稿目录的名称。
- **负责人确认（2026-10-15）：** 只读部分为 Web 可读整个办公目录；写入部分为整个办公目录可写并另设本会话私有草稿区、只开放 Web 请求、写入不经人工确认。
- **负责人确认（2026-10-20）：** 飞书私聊可读取当前配置版本中明确选定的知识副本；飞书群聊和办公目录其他文件继续拒绝读取，飞书不开放写入。
- **关键假设：** Web 本地用户是全部同事的所有者，因此 Web 请求可以读写任一同事自己的办公目录。
- **待决问题：** 飞书群聊的读写授权来源（群成员、绑定者或管理员），由 S2b 确定。

## 4. 总体方案与设计取舍

### 4.1 方案概览

```mermaid
sequenceDiagram
    participant Svc as PersistentAgentService
    participant Drv as ExecutionDriver
    participant Core as core 文件工具
    participant Port as HostWorkspaceAccess
    participant Commit as HostCommitPort
    Svc->>Svc: publish：建立办公目录；Spec 开启 workspace_read、workspace_write
    Drv->>Drv: 读写端口均为本同事的 HostWorkspaceAccess
    Drv->>Drv: build_invocation 核对端口归属
    Core->>Port: 读取前 resolve_read_scope / 写入前 resolve_write_scope
    Port-->>Core: 请求范围含对应能力时 allowed，否则 forbidden
    Core->>Core: 路径检查；写入时读取原文件摘要
    Core->>Commit: 写入前记账 tool.intent 并等待落盘
    Core->>Core: 条件写入；文件已被改动时返回 version_conflict
```

### 4.2 关键决策

#### D-01 授权依据为请求的访问快照与发布版本知识范围

- 选择与依据：Request 接受时冻结 `AccessSnapshot`。启动范围与来源范围都含 `workspace_read` 时，读取整个办公目录；都含 `workspace_write` 时允许写入。飞书私聊另使用 `workspace_knowledge_read`，且只有已发布 `ConfigSnapshot` 带有知识根目录时，仅读取该 `resources/knowledge/<摘要>/`；群聊不含此权限，办公目录其他位置及所有写入仍拒绝。发布版本保存知识根目录，确保已有 Request 继续使用其接受时配置版本选定的副本。
- 实现体现：`HostWorkspaceAccess.resolve_read_scope`、`resolve_write_scope`、`resolve_feishu_access` 与 `ConfigSnapshot.knowledge_root`。

#### D-02 目录按命名空间与同事划分，路径检查交给 core

- 选择与依据：目录为 `<数据根>/workspaces/<namespace_id>/<agent_id>`，与数据库、Template、窗口数据分开。名称只允许字母、数字、`_`、`-`，避免由标识构成越界路径。宿主不重复实现路径检查，由 core 的 `resolve_read_path`、`resolve_write_path` 统一处理。
- 实现体现：`WorkspaceStore`。

#### D-03 端口由宿主构造并在调用前核对

- 选择与依据：Spec 声明 `workspace_read` 或 `workspace_write` 时，`build_invocation` 要求对应的 `ports.workspace_access` / `ports.workspace_write_access` 是本同事的 `HostWorkspaceAccess`，否则拒绝调用，避免以他人的范围或空端口执行。端口收到其他同事的 Execution 时抛出异常，core 按 `unknown` 拒绝。
- 实现体现：`spec_provider.build_invocation`、`ExecutionDriver`。

#### D-04 系统提示补充路径说明

- 选择与依据：core 的工具描述要求绝对路径，与路径检查相反，模型按描述调用会被拒绝。不修改 core 时，在发布的系统提示末尾固定追加 `WORKSPACE_PROMPT`，说明使用相对路径、修改前先读取、遇到版本冲突时重新读取，以及不读写 `sessions/`。
- 实现体现：`service.WORKSPACE_PROMPT`、`publish`。

#### D-05 Web 运行时启动时重新发布

- 选择与依据：旧版本 Spec 未开启文件能力。运行时首次启动前，`refresh_published` 重新发布没有未发布修改的同事；设置未变时 `publish` 沿用原版本，有未发布修改的同事保持原版本，避免把草稿发布出去。
- 实现体现：`PersistentAgentService.refresh_published`、`JiuwenMateWebAdapter._runtime`。

#### D-06 整个办公目录可写，`sessions/` 下按 Session 隔离

- 选择与依据：用户资料与同事产出的文件须在各会话之间共享，因此写入范围为整个办公目录。`sessions/` 作为 `private_sessions_root`，本 Session 的 `sessions/<session_id>/` 作为 `current_session_root`；core 的路径检查只允许访问本会话的子目录，`sessions/` 本身与其他会话的子目录均为 `forbidden`。读取范围采用同一组根目录，因此其他会话的草稿既不可写，也不可读。`session_id` 同样只允许字母、数字、`_`、`-`，否则端口抛出异常。
- 实现体现：`HostWorkspaceAccess._session_roots`、`SESSIONS_DIR`。

#### D-07 写入不经人工确认

- 选择与依据：PA-09 本轮不开发。`write_file`、`edit_file` 均不在 PA-15 的只读名单中，每次写入前必须先把 `tool.intent` 写入 SQLite；条件写入保证并发修改可被识别。这一组合满足 JM-14-V-03“只开放无需人工确认、且已接入意图记账的写操作”。
- 实现体现：`service.publish` 写入 `workspace_write`；core 的 `PersistentToolIntentRail`。

### 4.3 替代方案与排除理由

| 候选 / 关联决策 | 关键差异 | 拒绝原因 | 重新评估条件 |
| --- | --- | --- | --- |
| R-01 / D-01 按渠道判断授权 | 不改访问快照 | 授权分散到端口内部，且与已冻结的请求事实不一致 | 无 |
| R-02 / D-06 只允许写入私有草稿区 | 共享文件只读 | 同事产出的文件无法在其他会话中继续使用；负责人选择整个目录可写 | 无 |
| R-03 / D-04 修改 core 的工具描述 | 描述与检查一致 | 本期不修改 agent-core | core 修正描述后删除提示中的路径说明 |
| R-04 / D-04 系统提示中写入草稿目录名称 | 模型可直接使用草稿区 | 系统提示由各 Session 共用，无法写入具体 `session_id`；改写请求文本会污染用户输入 | core 执行头带出 `session_id` 后 |
| R-05 / 范围 同时装配 Skill | 能力更完整 | Skill 机制会挂载代码与命令工具 | S2b |

## 5. 详细设计

### 5.1 模块责任

- `WorkspaceStore`：`path` 校验名称并返回目录；`ensure` 以私有权限建立目录；`remove` 删除目录。
- `HostWorkspaceAccess`：持有一个同事的目录；`scope` 返回 `(namespace_id, agent_id)`；`resolve_read_scope` 与 `resolve_write_scope` 按 D-01、D-06 返回 `WorkspaceReadScope` / `WorkspaceWriteScope`，`source_ref` 为 `workspace://<namespace_id>/<agent_id>`。同一实例同时作为读取端口与写入端口。
- `PersistentAgentService`：`publish` 建立目录，Spec 写入 `enabled_features=["construction", "workspace_read", "workspace_write"]`、`enable_sys_operation=True`、`enable_read_image_multimodal=False` 与追加路径说明的系统提示；`refresh_published` 按 D-05 重新发布；`delete_colleague` 删除目录；视图带 `workspace_dir`。
- `ExecutionDriver`：每次执行的端口加入本同事的 `HostWorkspaceAccess`，同时填入 `workspace_access` 与 `workspace_write_access`。
- 前端：`toColleague` 解析 `workspace_dir`；同事详情页“知识”分区显示目录与说明。

### 5.2 接口

`ColleagueView.workspace_dir`：办公目录的本机绝对路径，字符串。页面只显示该路径，不通过接口读取或上传文件。

## 6. 影响面与实施设计

| 实施项 | 文件 | 修改内容 | 验证产物 |
| --- | --- | --- | --- |
| I-01 | `ports/workspace_access.py` | `WorkspaceStore`、`HostWorkspaceAccess`、`WORKSPACE_READ`；写入部分新增 `WORKSPACE_WRITE`、`SESSIONS_DIR`、`resolve_write_scope` 与私有草稿根目录 | JM-14-V-01、V-02 |
| I-02 | `spec_provider.py` | 开放 `workspace_read`、`workspace_write`；`build_invocation` 核对两个端口 | JM-14-V-01、V-03 |
| I-03 | `service.py`、`models.py` | 发布、重新发布、删除与视图字段；写入部分更新能力列表与路径说明 | JM-14-V-01 |
| I-04 | `execution_driver.py`、`agent_manager.py` | 执行端口加入读取与写入范围 | JM-14-V-01 |
| I-05 | `web_adapter.py` | `WEB_ACCESS` 加入 `workspace_read`、`workspace_write`；运行时启动前重新发布 | JM-14-V-01 |
| I-06 | 前端 `types.ts`、`jiuwenmateApi.ts`、`pages/ColleagueProfilePage.tsx`、中英文文案 | 显示办公目录；写入部分更新说明文案 | — |
| I-07 | [`test_resource_scope.py`](../../../../../tests/unit_tests/server/jiuwenmate/test_resource_scope.py)、[`test_web_real_core.py`](../../../../../tests/integration_tests/jiuwenmate/test_web_real_core.py)、[`test_core_contract.py`](../../../../../tests/unit_tests/server/jiuwenmate/test_core_contract.py)、前端 `tests/jiuwenmateApi.test.mjs`；既有测试替身补充 `workspaces` 与端口 | 单测、真实 core 集成测试与前端解析测试 | — |

兼容核对：既有表结构不变；旧版本 Spec 仍可执行（未声明文件能力时不要求端口），Web 运行时启动后按 D-05 重新发布；`ExecutionDriver` 构造参数不变；个人版方法与事件不变。

## 8. 验证设计、执行记录与结论

### 8.1 验证设计与通过条件

| 验证 ID | 场景 | 验证入口 | 预期结果 | 层级 |
| --- | --- | --- | --- | --- |
| JM-14-V-01（只读部分） | Web 请求读取本同事目录；越界与无授权请求被拒绝 | `test_resource_scope.py` | 相对路径解析到本同事目录；缺少任一范围时为 `forbidden`；`..`、绝对路径、Windows 盘符、外链符号链接为 `forbidden`；其他同事的 Execution 抛出异常；非法名称被拒绝；发布建立目录、删除移除目录；重新发布跳过有未发布修改的同事；缺少或错配端口时拒绝调用 | 单测（真实 SQLite、真实 core 路径检查） |
| JM-14-V-01（执行部分） | 真实 core 执行中调用 `read_file` | `test_web_real_core.py::test_a_colleague_reads_only_its_office_directory` | 读取目录内文件时回复含文件内容；请求 `../other/secret.md` 时回复不含他人文件内容与宿主路径 | 集成（真实 core `9eb336b4`，模型为替身） |
| JM-14-V-02（Session 隔离） | 本会话访问自己的草稿；访问 `sessions/` 与其他会话的草稿 | `test_resource_scope.py::test_a_session_reaches_its_own_drafts_only` 等 | 本会话子目录可读可写；`sessions/`、其他会话子目录及其文件读写均为 `forbidden`；非法 `session_id` 抛出异常 | 单测（真实 core 路径检查） |
| JM-14-V-02（冲突识别） | 文件在预检后被改动 | agent-core PA-16 条件写入测试 | 返回 `version_conflict`，不覆盖 | 由 core 组件测试覆盖；宿主只提供本机条件写入文件系统 |
| JM-14-V-03 | 真实 core 执行中写入、修改、越界写入 | `test_web_real_core.py::test_a_colleague_writes_and_edits_shared_files_but_not_other_sessions_drafts` | `write_file` 创建、`edit_file` 修改共享文件，结果为“定稿”；写入其他会话草稿返回 `workspace path is not writable` 且文件不存在；SQLite 中按顺序记录 `write_file`、`edit_file` 两条 `tool.intent`，目标均为该文件；回复不含宿主路径 | 集成（真实 core `9eb336b4`、真实 `HostCommitPort` 与 SQLite，模型为替身） |
| JM-14-V-03（写前屏障） | 意图落盘成功与失败 | `test_web_real_core.py` 的 PA-15 两项用例（`85b0e66d`） | 意图先落盘再产生副作用；落盘失败时副作用为零 | 集成 |

JM-14-V-01 的 Skill / MCP / 插件部分不在本次范围；V-03 中“清理一个实例不卸载其他实例的工具”随 Skill 装配验证。

### 8.2 实际执行与证据

只读部分：2026-10-15 实现，提交 `9d5b69e8`。

| 验证 ID | 命令 | 结果 | 覆盖内容 |
| --- | --- | --- | --- |
| JM-14-V-01 | `pytest tests/unit_tests/server/jiuwenmate/test_resource_scope.py tests/integration_tests/jiuwenmate/test_web_real_core.py` | 25 passed | 见 8.1 |
| — | `pytest tests/unit_tests/server/jiuwenmate tests/integration_tests/jiuwenmate tests/unit_tests/gateway/test_persistent_group_fanout.py` | 274 passed | JM-01～JM-13 不受影响 |
| — | `pytest tests/unit_tests/server tests/unit_tests/gateway tests/unit_tests/channel tests/integration_tests/jiuwenmate` | 2829 passed，1 skipped | AgentServer、Gateway 与 Web 渠道既有行为不变 |
| — | `npm run test:jiuwenmate-api`、`test:jiuwenmate-presentation`、`tests/i18nLocales.test.mjs` | 17、5、5 passed | 新增目录字段解析 |

写入部分：2026-10-15，在 `fork/huenrui_jiuwenswarm` 工作区执行，基于 `0ba83f08`，core `9eb336b4`，未提交。

| 验证 ID | 命令 | 结果 | 覆盖内容 |
| --- | --- | --- | --- |
| JM-14-V-02、V-03 | `pytest tests/unit_tests/server/jiuwenmate` | 271 passed | 写入范围、Session 隔离、端口核对与发布能力 |
| JM-14-V-03 | `pytest tests/integration_tests/jiuwenmate` | 12 passed | 真实 core 写入、修改、越界写入与写前屏障 |
| — | `pytest tests/unit_tests/server tests/unit_tests/gateway tests/unit_tests/channel tests/integration_tests/jiuwenmate` | 2867 passed，1 skipped | AgentServer、Gateway 与 Web 渠道既有行为不变 |
| — | `ruff check`（改动的文件） | 本次改动无新增问题；`execution_driver.py` 未使用的导入与 `test_web_real_core.py` 导入顺序为既有问题 | — |

### 8.3 验证结论

只读与写入的宿主组件验证通过：Web 请求可以读写本同事办公目录，每次写入前意图已落盘；其他会话的私有草稿与越界路径被拒绝。飞书群聊仍拒绝文件访问；飞书私聊的已选知识副本范围由 2026-10-20 补充的回归测试验证。真实飞书模型读取、浏览器验收与联合验收未执行。

## 9. 风险与已知遗留

### 9.1 设计风险

| 风险 / 触发条件 | 影响 | 已有应对措施 | 核实方式 |
| --- | --- | --- | --- |
| 模型按 core 工具描述传入绝对路径 | 读写被拒绝 | 系统提示说明使用相对路径 | 真实模型冒烟 |
| core 全局登记 shell 与文件系统操作 | 本地 Bash 具有服务账号权限，文件工具授权不能约束它 | 新增 Bash 的提示词约定及非隔离风险见第 12 节，不再以“只有四个文件工具”作为保证 | core 升级及实际命令执行时复核 |
| 写入不经人工确认 | 模型可能覆盖共享文件 | 意图记账可追溯；条件写入防止覆盖他人的并发修改；任务失败不自动回滚 | PA-09 开放后评估 |

### 9.2 已知遗留

| 遗留项 | 性质 | 影响与现有处置 | 后续入口 |
| --- | --- | --- | --- |
| Skill、MCP、插件装配 | 已实现发布装配 | 见第 11 节。未安装的名称会拒绝发布 | 真实模型执行验收 |
| 员工知识选择 | 由 JM-22 承接 | 已发布个人上下文页面在发布时复制到办公目录，见 [F_22](F_22_employee-knowledge.md) | 真实模型读取验收 |
| 模型无法使用私有草稿区 | core 限制 | 隔离已生效，但模型不知道本会话的 `session_id`；系统提示要求不读写 `sessions/` | core 执行头带出 `session_id`（PA-06） |
| 删除会话时保留私有草稿 | 未实施 | 会话为软删除，草稿随会话保留；删除同事时一并删除 | 会话硬删除时评估 |
| 飞书群聊读写 | 未实施 | 读取与写入均为 `forbidden` | 仍未开放 |
| 飞书私聊所选知识读取 | 2026-10-20 实施 | 仅当前版本知识副本为 `allowed`；兼容从既有已发布 Spec 提示恢复范围；其他路径与写入为 `forbidden` | JM-22 |
| 文件导入 | 未实施 | 用户在本机放置文件 | 待评估 |
| 飞书私聊读取知识副本 | 2026-10-20 实施 | 使用独立 `workspace_knowledge_read` 能力和发布版本范围；兼容旧发布快照及已接受的私聊请求；不加入通用 `workspace_read` | 真实飞书模型联调 |

## 10. 设计演进与修订

- 2026-10-15：建档并实现最小只读配置子范围（提交 `9d5b69e8`）。
- 2026-10-15：增加办公目录写入子范围：开放 `workspace_write`，同一端口提供写入范围，`sessions/` 下按 Session 隔离；写入不经人工确认；补充真实 core 写入集成测试。
- 2026-10-16：增加 Skill / MCP / 插件发布装配，见第 11 节。

## 11. Skill、MCP 与插件装配

发布时读取 JM-02 已经保存的技能、连接器和插件名称。技能和技能市场安装记录仍向技能库与 `skills_state.json` 查询；插件包向 `plugins/plugin_packages` 下已安装的包读取其自带技能、工具和护栏。市场安装的插件在列表里用资产编号作为 `id`，包目录名在 `packageName`；员工保存包目录名。若已经保存了资产编号，发布按同目录 `hub_state.json` 的安装记录找回包目录。发布过程不安装、不卸载、不改写这些目录。MCP 仍向连接器状态查询。

- 已安装的技能和插件所含技能被复制到该同事办公目录的 `resources/skills/<内容摘要>/`。连接器附带的操作说明单独复制到 `resources/connector-skills/<内容摘要>/`，不进入该员工的技能列表。复制只包含普通文件，不跟随指向技能目录之外的符号链接。内容变化会生成新的摘要目录，已发布版本仍指向原来的副本。
- Spec 写入 `skills` 和一条 `core.skill_use` Rail。当前 Rail 的 `include_tools` 为假，因此该 Rail 不注册 bash 与 code。进入技能列表的说明由该 Rail 注入；系统提示补充相对路径，并要求回答“有哪些技能”时只报已装配技能。第 12 节的 Bash 由独立宿主 Provider 装配，已在当前工作区实施；用户选择先用提示词约束访问范围，不引入沙箱。真实飞书验收尚未完成。
- 带连接地址的连接器冻结为该同事自己的 `McpServerConfig`。`server_id` 含同事标识和配置摘要，凭据只从该连接器的凭据库写入 Spec，不读取、不修改进程环境变量。缺少凭据时发布失败，不把环境变量里的同名值当作凭据。
- 插件包里的工具和护栏按 `harness.tool.file`、`harness.rail.file` 写入 Spec，路径必须是该包内的普通文件。只有技能的插件不写工具。技能、工具、护栏都没有的包拒绝发布。
- 名称未安装，或同一技能名指向两个目录时，发布失败。删除同事只删除其办公目录，不卸载技能库或其他同事的副本。
- 已发布的个人上下文页面由 JM-22 复制到办公目录。core 的 `enabled_features` 没有技能开关，因此不新增宿主特性名。创建页不再提供示例知识名称；未发布的页面名称会拒绝发布。

验证：`tests/unit_tests/server/jiuwenmate/test_resource_assembly.py` 7 项通过。同事服务、Web 适配与办公目录范围测试在补上已安装技能夹具后通过（2026-10-16，`--no-cov`）。未做真实模型执行和浏览器验收。

## 12. Bash 与飞书 CLI 自动装配（2026-10-20，方案已确认）

### 12.1 已确认目标与现状

用户要求开放 Bash，绑定飞书渠道后自动准备 lark-cli 和配套 Skill，不另做一套受限飞书命令工具。用户先要求仅访问当前 Agent 的工作空间；在说明提示词不提供强隔离后，明确选择“先用系统提示词约束”。因此当前目标是本地 Bash、默认 Agent 工作目录和提示词行为约定，不引入沙箱，也不再等待 Linux / WSL / Jiuwenbox 部署。

本节记录 JM-14 的补充设计：用户已确认方案与实施计划，代码已在当前工作区实施，未提交。实际执行证据与尚未验证范围见第 12.6 节；JM-14 整项仍为进行中。本轮不改 agent-core、个人版默认行为或总体架构文档。

本次只读检查确认：

- 新版已有资源发布、独立 Invocation、PA-15 写前记账和 core Bash 工具；现有工具扩展入口可接入，不需要把飞书业务写进 agent-core。
- core 的 `LocalWorkConfig.restrict_to_sandbox` 说明的是文件操作检查，不能据此认定任意 Shell 子进程已被操作系统隔离。cwd 和提示词都不能阻止命令或脚本访问范围之外的文件。
- 本机为 Windows，当前没有 `sandbox` 配置。本次不安装 Docker / WSL、不启动 Jiuwenbox，也不把命令入口是否存在当作 Bash 已能正常运行的证据。Shell 可执行性仍需实际测试。

### 12.2 方案与取舍

采用“core Bash 工具 + 本次执行绑定的本地 Shell + 自动飞书 Skill / CLI”方式。Persistent Agent 发布配置时声明 Bash；绑定飞书后自动增加该应用的 CLI 准备和配套 Skill，不要求用户再手动选择一次连接器。未绑定飞书的 Agent 可以使用 Bash，但不借用个人版飞书认证。普通问答与飞书连接不因可选 CLI 准备失败而失效。

```mermaid
flowchart LR
    A[发布 Persistent Agent] --> V[配置包含 Bash 与工作空间约定]
    B[保存已有 app_id 与 Agent 绑定] --> P[后台准备 CLI、独立认证和 Skill]
    P --> V
    V --> E[新 Execution 取得可信 Agent 与 Session 身份]
    E --> I[PA-15 持久化命令调用意图]
    I --> S[本地 Bash 执行命令或 lark-cli]
    S --> R[文档 ID、链接与结果交还模型]
    R --> D[正常终态回复沿 JM-09 投递]
```

替代方案与取舍：暂缓真实沙箱，原因是用户选择先以提示词约束推进；这不是对等安全方案。未来需要“技术上只能访问工作空间”时再接实际隔离环境。不采用进程全局 `os.environ` 切换认证，因为它会影响并发执行与个人版；不改为独立 `feishu_create_document` 工具，因为用户已选择 Bash 加 CLI 的使用方式。

### 12.3 提示词约定与实际安全边界

给模型的系统提示明确以下行为约定：

1. 文件、脚本、输入和产物仅使用当前 Agent 办公目录；优先相对路径，不通过绝对路径、`..`、符号链接或其他程序访问其他 Agent、个人版目录、宿主配置、数据库和群公共消息库。
2. `sessions/` 下仅使用本次 Session 的草稿，不读写其他会话。共享资料可在当前 Agent 的会话之间复用。
3. 不读取、显示或复制认证配置、Secret、Token；不运行 CLI 的用户登录、切换应用、重置配置等身份管理命令。只使用宿主已经准备的当前 Agent 的飞书应用身份。
4. 优先使用原有 `read_file/write_file/edit_file` 完成文件操作；Bash 用于必要的脚本与 CLI。修改已有文件前先读取；不要用 Shell 绕过工具报出的授权拒绝或版本冲突，不自行安装全局依赖。
5. 创建文档与授予用户访问权分别核实，返回链接不等于用户已经能打开。不能把创建超时解释为未创建并再次提交。

以上是行为约定，不是强制安全边界。本地 Bash 具有运行服务账号的实际权限：模型、脚本及子进程仍可能越界读取或修改文件、接触其他应用认证。飞书群聊原有文件工具的拒绝策略和私聊知识副本限制不能约束 Bash；本方案不承诺抵御提示词注入或恶意命令。不得在页面、文档或测试报告中把它描述为“只能访问工作空间”或“Session 已被 Bash 隔离”。

执行默认 cwd 使用当前 Agent 办公目录；环境、身份和默认目录按 Invocation 绑定，不修改进程全局 cwd 或 `os.environ`。保留现有命令工具的安全检查，不把全局安全模式改成放行所有命令。后台命令只表示已启动，不能当作已完成；超时、停止、换绑与后台任务的生命周期必须在执行侧明确处理。

Bash 的本地文件变更不经过 `write_file/edit_file` 的 PA-16 条件写入；现有文件工具仍保持原保证，不能把条件写入保证扩展到任意命令。Bash 每次调用均按非只读工具进入 PA-15；这只保证调用前意图已提交，不提供跨系统事务或命令级恰好一次执行。

### 12.4 自动准备、绑定与发布

- 沿用已有 `persistent_agent_id ↔ app_id`，不新增绑定 ID、绑定表或让模型选择应用。准备 CLI 时由宿主读取当前唯一有效绑定，在办公目录外生成私有 `config/app-secret`，由 CLI 官方支持的 `appSecret: {"source":"file","id":"<私有文件绝对路径>"}` 引用。Secret 不进入 Spec、Skill、普通办公文件、命令参数、子进程环境或模型输入。
- 每个 Agent 的认证单独保存在 `.jiuwenswarm/jiuwenmate/runtime/<namespace_id>/<agent_id>/lark-cli/config/`；原子创建私有文件（POSIX 为 0600，Windows 使用当前用户目录的 ACL）。每次 Shell 调用指定自己的 `LARKSUITE_CLI_CONFIG_DIR` 和 `LARKSUITE_CLI_DATA_DIR`，清除继承的飞书认证环境；配置默认身份及严格身份均为 `bot`，不导入用户登录。**不调用 `config init`**，因为其 Secret 保存会按 app_id 写入操作系统共享凭据库，单独配置目录并不能隔离该副作用。依据为 CLI v1.0.94 的 [SecretRef 解析](https://github.com/larksuite/cli/blob/v1.0.94/internal/core/secret_resolve.go) 与 [配置结构](https://github.com/larksuite/cli/blob/v1.0.94/internal/core/config.go)。这避免正常调用串号，但无沙箱时仍不能阻止 Bash 主动读取其他目录。
- 复用现有连接器包 / Skill 安装能力与固定版本包清单，Persistent 所需 CLI 使用独立安装前缀和启动器，不升级或替换个人版全局二进制。安装及私有配置写入属于宿主准备步骤，不放进无副作用的工具构造 Provider，不在每次模型请求时重新安装。重复准备仍核验实际二进制版本；匹配的私有认证文件不重复改写。
- 保存绑定或渠道配置后自动通知受影响的新旧 Agent；准备途中再次换绑会重新核对，不丢失后一次通知。重启时扫描已发布同事和有效绑定，包括已解绑、有未发布草稿的同事，移除过时能力声明并保留草稿。相同资源准备幂等；失败保存脱敏状态，允许修复后重试，不声称渠道绑定失败或 CLI 已就绪。
- Bash 和飞书 Skill 的声明进入配置版本链，已接受的 Request 继续使用原发布快照；认证等运行句柄不写入 Spec。CLI 就绪后自动刷新飞书能力，不顺带发布同事的其他未发布设置；存在草稿时从已发布版本派生能力更新并保留草稿状态。
- 换绑、解绑时后续调用必须重新核对当前绑定；旧的 CLI 身份不得继续用于新调用。旧进程、后台命令及不确定的在途创建须分别处理，不能宣称解绑已经撤销外部文档。
- 创建超时或回执缺失时记录结果未知，不自动重复创建；成功时保留文档 ID / 链接，权限授予失败不能伪装成文档完全失败再重建。

### 12.5 修改范围与实施前置

| 位置 | 拟修改内容 |
| --- | --- |
| `gateway/channel_manager/web/app_web_handlers.py` 的飞书绑定入口与 AgentServer 内部交接 | 保存绑定后触发当前 Agent 的准备，不把安装失败混同为配置未保存 |
| `server/jiuwenmate/` 的资源发布与新增 CLI 运行装配模块 | 按当前绑定准备私有认证、安装状态和飞书 Skill，形成新的能力发布版本 |
| `resource_assembly.py`、`service.py` | 保持已有资源选择与草稿语义；追加 Bash 声明及自动飞书说明，消除“禁止所有命令”的过时提示，不全局启用 Skill 的兜底工具集 |
| `execution_driver.py`、`spec_provider.py` 与宿主工具 Provider | 从可信 Execution 获取 namespace / Agent / Session，创建独立 Bash 实例，注入默认 cwd 和子进程 CLI 环境；文件工具继续使用原有 HostWorkspaceAccess，不顺带开放 PowerShell / code |
| 新增宿主 Bash / CLI 运行装配模块 | 复用 core Bash，封装 Invocation 专属的 Shell 环境；准备独立 CLI 安装、认证和脱敏状态，不改变个人版通用 CLI 驱动默认值 |
| 本文、开发计划及必要 spec / 架构说明 | 方案确认后同步已确认的新边界；架构文件变更先按 docs/AGENTS.md 获得确认 |

实施不再依赖沙箱部署。代码使用 Hub 公共安装器把 `feishu` 包安装到 JiuwenMate 的 `packages/`，按包清单的固定版本将 `@larksuite/cli` 安装到私有 `cli/` 前缀；不执行包中任意安装脚本，不改个人版全局二进制。缺少 Node.js/npm、包源或应用配置时保留脱敏失败状态。中文实施计划已确认：`docs/superpowers/plans/2026-10-20-jm14-bash-feishu-cli.md`。

本轮实现入口：

- `lark_cli_runtime.FeishuCliRuntime`：读取已有唯一绑定，准备独立认证、固定版本 CLI 和 Skill；跨进程安装锁与 Agent 准备锁防重；换绑或 Secret 变化立即使旧就绪状态失效。删除同事时清理该同事认证，不卸载共享包。
- `bash_runtime`：注册 `jiuwenmate.bash` Provider，模型工具名为 `bash`；按 Invocation 注入默认办公目录与子进程环境。保留 core 命令检查和非幂等属性；不启用 Skill 的 Bash/code 兜底工具集。core 在 Provider 之后才创建默认 SysOperation 时，适配器独立创建本次 Shell；显式父配置则复制，不修改原对象。
- `PersistentAgentService.refresh_feishu_capability` 与 `PersistentAgentRepository.save_capability_version`：从已发布 Spec 派生自动资源，用当前版本和下一版本号做事务 CAS，不改用户草稿或其发布标记。相同资源复用版本；正常发布也带上已就绪的自动资源。
- Gateway 保存飞书绑定或配置后向受影响的新旧 Agent 发送内部 `jiuwenmate.feishu.prepare`，仅传 `{ "agent_id": "..." }`。AgentServer 交给 Web / 飞书入口共用的 Manager 后台准备，短请求只确认排队，不等同安装成功。准备失败不回滚已保存绑定；重启核对已发布同事，后续调用重新核对当前身份。
- 所选插件工具与 Bash 在同一宿主 Provider 内逐一构造，检查实际模型工具名，拒绝动态插件与 `bash` 重名；不静默覆盖实例或重复构造插件。原插件声明仍保存在该 Provider 的内部参数中。
- 命令输出保留在本次返回值中，超长内容截断为 20000 字符；不沿用 core Bash 的跨 Agent 临时输出文件。需要完整输出时由命令显式重定向到当前办公目录。后台进程随执行收尾清理，不把“启动成功”当作命令完成。

### 12.6 验证与遗留

以下为验证目标；实际结果单独记录，不表示已通过真实飞书验收：

- JM-14-V-01：真实构造的工具清单包含 Bash，提示词包含办公目录、草稿、凭据和禁止绕过授权约定，默认 cwd 为当前 Agent；原文件工具的授权检查不变。不把模型遵守提示词当作越界攻击已被阻止。
- JM-14-V-02：并发 Invocation 的默认目录、CLI 环境与身份不串用；宿主环境和 cwd 前后不变。原文件工具仍拒绝其他 Session 的草稿，但不声称 Bash 具有同等强制限制。
- JM-14-V-03：意图提交失败则无命令副作用；CLI 诊断脱敏，实例清理不卸载其他 Agent 的工具；超时或未知创建结果不自动重跑。原有个人版命令、卡片和附件测试用于兼容性检查，不修改其逻辑。
- 两个 Agent 并发创建文档分别使用自己的应用；绑定准备、换绑、解绑、重启恢复不串用身份，不改变个人版环境与认证。
- 真实飞书创建后核对文档正文、ID、链接以及请求者的实际访问权；Web 与飞书终态投递分别验证，不向群里泄露命令过程或凭据。

2026-10-20 本地证据（当前未提交代码、Python 3.12、Windows、实际 agent-core 本地依赖；模型、市场下载和认证初始化使用替身）：

- `test_lark_cli_runtime.py`：9 项通过，验证私有前缀、stdin、重复准备、Secret 轮换、解绑、歧义、换绑竞态、环境与脱敏；真实受控进程验证中文路径 `.cmd`、stdin 和取消后进程停止。
- `test_bash_runtime.py` 与 `test_bash_feishu_cli_real_core.py`：9 项通过。真实 core 工具在默认办公目录执行命令，检查并发目录 / 环境、流式调用、超时只执行一次、后台清理及原默认允许列表不变；SQLite 意图提交失败时无进程副作用，模拟 CLI 返回链接可从 Web 会话历史查回。
- `test_cli_capability_publication.py`：4 项通过，真实 SQLite 验证草稿保留、旧版本不变、资源幂等、CAS 以及正常发布保留自动资源。
- `test_feishu_cli_production_wiring.py`：保存绑定到实际 E2A 与 AgentServer 分发已验证；安装和认证使用替身。覆盖启动恢复、未绑定渠道、准备失败不撤销绑定；新增删除认证回归随最终组合命令验证。
- 调度、恢复、定时任务与停止控制 4 个既有测试文件：57 项通过。

命令前缀为 `uv run --no-sync python -u -m pytest --no-cov -q -o log_cli=false --tb=short`，追加上述 `tests/unit_tests/server/jiuwenmate/` 和 `tests/integration_tests/jiuwenmate/` 中同名文件即可复现。上述为 2026-10-20 历史证据，stdin 初始化方案已由下一节的设计修订替换，不能作为当前凭据隔离的证明；2026-10-21 结果另行记录。

2026-10-21 本轮证据（Swarm `24d695909a` 加未提交改动，Windows / Python 3.12、本地 agent-core）：

- 新增 5 个测试文件共 **41 passed**（46.37 秒）：`test_lark_cli_runtime.py` 12 项、`test_bash_runtime.py` 10 项、`test_cli_capability_publication.py` 5 项、`test_feishu_cli_production_wiring.py` 11 项、`test_bash_feishu_cli_real_core.py` 3 项。
- 独立审查后的凭据隔离、缓存版本核验、换绑重跑、启动解绑清理、新旧 Agent 通知均先观察对应测试失败，再修复并重跑；同应用 Secret 轮换与个人版渠道不触发 Persistent 准备也有覆盖。
- 真实 core 集成穿过模型工具调用、PA-15 / HostCommitPort / SQLite、Bash 子进程、终态回复与 Web 历史；受控 CLI 检查私有 SecretRef、bot 身份和中文路径启动器。外部市场、npm 安装、模型与飞书 API 仍为替身。

新增用例复现命令（仓库根目录）：

```powershell
uv run --no-sync python -u -m pytest --no-cov -q -o log_cli=false --tb=short tests/unit_tests/server/jiuwenmate/test_lark_cli_runtime.py tests/unit_tests/server/jiuwenmate/test_bash_runtime.py tests/unit_tests/server/jiuwenmate/test_cli_capability_publication.py tests/unit_tests/server/jiuwenmate/test_feishu_cli_production_wiring.py tests/integration_tests/jiuwenmate/test_bash_feishu_cli_real_core.py
```

同日完整相关回归 **686 passed、11 failed**（697 项，121.18 秒），没有将失败计为通过。命令为：

```powershell
uv run --no-sync python -u -m pytest --no-cov -q -o log_cli=false --tb=short tests/unit_tests/server/jiuwenmate tests/integration_tests/jiuwenmate tests/unit_tests/agents/test_bash_tool_safety.py tests/unit_tests/agents/test_shell_process_registry.py tests/unit_tests/gateway/test_feishu_streaming_card.py tests/unit_tests/channel/test_feishu_upload.py tests/unit_tests/gateway/test_feishu_websocket_status.py tests/unit_tests/gateway/test_feishu_im_adapter.py tests/unit_tests/gateway/test_app_web_handlers.py
```

失败与本次收尾前的回归相同，分类如下；本轮不放宽原文件工具授权或顺带改写这些既有测试：

| 未通过范围 | 数量 | 本机证据与原因 |
| --- | --- | --- |
| 同事服务 / 窗口私库的 POSIX 权限断言 | 2 | Windows 文件模式与预期 0600 / 0700 不一致；不是凭据泄露断言失败 |
| 资源副本 / 知识目录 / 办公目录的符号链接用例 | 4 | 创建链接报 Windows 1314，需要相应系统权限 |
| 未发布知识页的发布拒绝用例 | 1 | 既有服务清理无效选择，测试仍索引已移除的草稿选择，报 IndexError |
| 原文件工具的两项真实 core 集成 | 2 | 测试替换的 `publish_spec_with_test_tool` 未接受既有 `knowledge_root` 关键字；本轮 Bash 的真实意图落盘成功 / 失败链另有通过用例 |
| 个人版 Shell 注册表的两项进程清理用例 | 2 | Windows 没有测试直接启动的 Unix `sleep` 可执行文件；本轮另有跨实际 Windows 进程的超时 / 后台清理测试 |

个人版 Bash 安全检查、飞书卡片、附件、连接状态、IM 适配和 Web 配置用例已纳入上述回归，未修改其产品逻辑。`git diff --check` 通过；本地 `pyproject.toml`、`uv.lock` 的内容摘要未变，agent-core 未新增产品代码改动。以上是相关回归，不是整个项目全量测试或真实渠道验收。

未验证：真实 Hub 下载和 npm 安装、当前实际应用认证 / 创建文档、请求者可打开文档以及真实飞书终态投递。没有替用户安装真实账号认证、创建外部文档或重启服务。强隔离有意暂缓，提示词测试不能证明防越界。既有 Windows 符号链接权限、POSIX 文件模式断言和旧测试夹具不兼容等失败不计为通过；详见最终回归记录。

### 12.7 设计演进

2026-10-20：最初讨论真实沙箱，以技术手段保证只能访问工作空间；说明该方案需要隔离运行环境后，用户选择先以系统提示词约束。因此原草稿的沙箱、仅挂载当前工作空间和强 Session 隔离设计不再作为本轮方案。保留本地 Bash 的实际权限风险，后续需要强隔离时重新设计与验收，不能用提示词测试替代。

2026-10-21：一次独立只读审查指出共享凭据库副作用、准备中换绑通知丢失、配置变更与解绑恢复遗漏、二进制就绪核验不足及旧发布版本未补 Bash 的问题。核实后采用官方文件 SecretRef，不再执行共享凭据初始化；补齐新旧 Agent 通知、准备重跑、启动核对、版本检查及保留草稿的旧版本升级。取舍是额外维护办公目录外的私有凭据文件并在解绑 / 删除时清理，而不是引入宿主 Token 服务或修改个人版 CLI 驱动。
