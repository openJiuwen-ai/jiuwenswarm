---
description: session 会话与配置模块说明书——给修改/检视本模块代码的 AI 助手与工程师：13 实现文件地图、7 份 spec 索引、会话队列 / 历史持久化 / 元数据 / 项目注册 / Git / diff 订阅 / KV 亲和的本地铁律、与 agent_adapter / gateway / openjiuwen 的接缝、测试与提交约束。
---

# session

会话与配置域：session 的**生命周期与任务队列**、**对话历史持久化**、**会话元
数据**、**项目注册表**、**项目 Git 集成**、**diff 订阅推送**与**KV 缓存亲和**
七个主题（13 个实现 + `__init__`，共 11,575 行）。改这里的代码前先读
[../AGENTS.md](../AGENTS.md) 的域铁律索引与本文引用的 spec。本文 `[[S_NN]]`
一律指本模块 [docs/specs/](docs/specs/) 下的 spec。

## 模块地图（13 个实现文件，按 spec 分组）

### 会话生命周期 —— [[S_01]]

| 文件 | 行数 | 职责 |
|---|---|---|
| [session_manager.py](session_manager.py) | 498 | `SessionManager`：每 session 一条任务队列与一个 processor 协程；LIFO 入队、优雅关闭、一次性 session 哨兵回收 |
| [work_mode.py](work_mode.py) | 179 | `session.create` 请求的 work_mode / project_id / project_dir 归一化与严格校验（只解析，不反查注册表） |
| [session_rename.py](session_rename.py) | 69 | `session.rename` 三语义（查询 / 设置 / 清除） |

### 历史持久化 —— [[S_02]]

| 文件 | 行数 | 职责 |
|---|---|---|
| [session_history.py](session_history.py) | 1701 | `history.jsonl` 新格式双轨、三层写入（事件循环线程只入队）、双缓冲合并、一致性读取点、进程退出收尾 |

### 会话元数据 —— [[S_03]]

| 文件 | 行数 | 职责 |
|---|---|---|
| [session_metadata.py](session_metadata.py) | 2041 | `metadata.json` 异步写队列（满退化同步）、first-write-wins 字段集、pin 字段保护、多租户写根 |

### 项目注册表 —— [[S_04]]

| 文件 | 行数 | 职责 |
|---|---|---|
| [project_store.py](project_store.py) | 1121 | `projects.json`：跨进程文件锁、原子写、mode-aware 路径规范化、session↔project 绑定规则、create_or_restore 原子三分支 |

### 项目 Git —— [[S_05]]

| 文件 | 行数 | 职责 |
|---|---|---|
| [project_git.py](project_git.py) | 2209 | 子进程 git 封装：init / commit / branch / worktree、超时分层、快照持久化、transient 中间态二分；`get_project_git_service()` 单例 |

### Diff 订阅与状态 —— [[S_06]]

| 文件 | 行数 | 职责 |
|---|---|---|
| [git_diff_watcher.py](git_diff_watcher.py) | 1310 | `GitDiffWatcherRegistry`：三索引 + 每 project 单轮询任务、三层 fingerprint 独立去重、首次快照播种、错误三分类 |
| [git_diff_status.py](git_diff_status.py) | 1058 | `DiffStatusService`：项目级 / turn 级 diff 查询、no-git fallback 与 transient 错误语义、dataclass 族与事件 payload helper |

### KV 缓存亲和 —— [[S_07]]

| 文件 | 行数 | 职责 |
|---|---|---|
| [kv_cache_affinity_lifecycle.py](kv_cache_affinity_lifecycle.py) | 418 | 动作生命周期：总开关 / provider 白名单 / 模型解析链；同步 evict（先等后台尾巴）、后台 offload / prefetch（会话内串行） |
| [kv_cache_product_hooks.py](kv_cache_product_hooks.py) | 250 | 产品钩子：删除会话时 preserving delete、会话切换事实解析（纯函数）与切换信号发送 |

### 权限中断（规约归 harness 中断交互域 specs）

| 文件 | 行数 | 职责 |
|---|---|---|
| [permission_interrupt_cancel.py](permission_interrupt_cancel.py) | 551 | 权限请求中断的取消路径；与 `agent_adapter` 的 HITL 中断状态机协同 |
| [permission_response_ledger.py](permission_response_ledger.py) | 170 | 权限响应台账（去重 / 追溯） |

这两件属于**中断交互主题**，不在 S_01~S_07 范围内；其设计规约归
`agents/harness/docs/specs/` 的中断交互域——台账与 Team landed 判定见
[[S_18]]（harness 域 S_18，中断相位与恢复守卫），取消结算三级入口与
`{id}#{n}` 序号代际收尾见 [[S_19]]（harness 域 S_19，取消结算与收尾）。
跨域引用写全路径消歧（与
[../../../agents/harness/docs/AGENTS.md](../../../agents/harness/docs/AGENTS.md)
的归档域表对照）。

## 架构铁律（改代码前必读）

以下从各 spec 不变量提炼，完整可判定表述见对应 spec：

1. **每 session 一个 processor + 一个 PriorityQueue，LIFO 新任务优先**
   （[[S_01]] N1/N2）：入队自减优先级；processor 缺失或已 done 时重建队列并把
   优先级归零。任务执行携带 submit 时的 ContextVar 快照（N4）。
2. **历史写入三层架构——事件循环线程只入队，绝不直接写盘**（[[S_02]] N4）：
   事件循环线程入队 → 后台写线程组 batch → 落盘线程 fsync。"读 history 必须看到
   最新"的路径先触发一致性读取点（N9）。JSONL 读取用 `split("\n")` 不用
   `splitlines()`（N3，`\x0b` 等会撕裂行）。
3. **metadata 异步队列 + 满退化同步 + 原子落盘**（[[S_03]] N1）：队列满时退化
   为同步写以保证不丢；缓存仅由 `_enqueue_write` 维护（N2），绕过队列直写 =
   缓存与磁盘不一致。多租户缓存 key 必须含 root（N4）。
4. **`projects.json` 的一切写走 `_mutate`——文件锁内重读磁盘再变更**
   （[[S_04]] N5）：锁内重读是跨进程正确性的根基；写走 tmp + fsync + replace
   （N3），读容错损坏即空表（N4）。
5. **git 子进程纪律**（[[S_05]] N1/N2/N12）：命令白名单式封装、写操作超时分层
   默认 10s、环境变量钳制；commit message 经 stdin `-F -` 注入；`force=True`
   用 `--force-with-lease` 而非裸 `--force`。
6. **diff watcher 三索引 + 每 project 单轮询**（[[S_06]] N1）：昂贵 git 子进程
   不随 watcher 数量线性复制；无 watcher 的 project 不启动轮询。首次快照由
   handler 计算后 Registry 播种 fingerprint（N3），防首轮"空串→非空"冗余推送。
7. **diff 查询错误语义三态**（[[S_06]] N11）：no-git fallback（正常返回
   is_git=False）、transient（current=None 但成功返回）、其余异常**向上抛**
   （防客户端误以为订阅成功拿不到内容）。
8. **KV 亲和 best-effort——任何失败不得阻断产品动作**（[[S_07]] N1）：
   删除 / 切换 / 收尾路径里 KV 动作失败仅记录，"preserving delete"；但 provider
   白名单不匹配返回 **failed 非 skipped**（部署错误要显式暴露，N3）。
9. **KV 动作同步 / 后台二分**（[[S_07]] N6/N7/N8）：evict 必须同步（删除路径
   要确认结果）且先等本会话后台尾巴；offload / prefetch 才允许后台，同会话内
   串行，任务持强引用防 GC。
10. **多租户写根显式传递**（[[S_03]] N5）：写路径的 sessions root 由入队线程
    捕获或显式传入，**不回退**全局默认——回退会把企业租户会话写进个人目录。

## 与其它模块的接缝

- **agent_adapter**（[../agent_adapter/AGENTS.md](../agent_adapter/AGENTS.md)）：
  `interface.py` 持有 `SessionManager` 实例（processor 上跑 agent 主流程）；
  请求前 `warmup_session_context`；哪一层负责清 Session / 重建 context 见
  runtime 域 `[[runtime/S_04_session-ops-and-context-restore]]` N10。
- **gateway**：`channel_manager/web/git_ws_handler.py` 是 [[S_06]] 接口的主要
  消费方（订阅生命周期全家）；`app_gateway.py` 装配
  `web_channel.git_watcher_registry` + `set_channel`（供 `_mark_git_watcher_dirty`
  唤醒）。server 层的 `handlers/session.py` / `handlers/bootstrap.py` 消费本模块
  接口但规约不归本模块。
- **`server/utils/diff_service.py`**：diff 计算本体（[[S_06]] 只转换不改写其
  输出，N14）。
- **openjiuwen（agent-core 仓）**：[[S_07]] 的动作实现 / 超时所有权 /
  `resolve_kvc_action_timeout` 在 `openjiuwen.core.foundation.kv_cache`；
  [[S_05]] 的结构化错误（`GitOperationError`）与 openjiuwen 基础错误体系对齐。
- **`jiuwenswarm/telemetry/session.py`**：[[S_01]] 的会话遥测协作方。

## 测试

- 单测两处：
  - `tests/unit_tests/server/`：`test_git_diff_watcher.py`（S_06 订阅 / 推送 /
    错误分类）、`test_turn_diff_history.py`（S_06 查询 / 历史回放）、
    `test_history_todo_restore.py`（S_02）、`test_project_git_safe_directory.py`
    （S_05）、`test_agent_history_gitignore.py`。
  - `tests/unit_tests/agentserver/`：`test_session_manager_lifecycle.py`（S_01）、
    `test_session_history_path_safety.py`（S_02）、`test_tenant_sessions_write.py`
    （S_03 多租户）、`test_resolved_project_dir.py`（S_04）、
    `test_session_git_snapshot.py`（S_05）、`test_deep_adapter_session_teardown.py`
    （S_01 清理联动）等。

## 设计文档归档与双向同步（本模块强制）

本模块设计文档**全部**落在 `docs/` 下，命名与结构规约见 [`docs/AGENTS.md`](docs/AGENTS.md)。
把 **`session/` 代码 + `docs/specs/` + 本 AGENTS.md** 当作一个一致性单元维护——本模块
写入链横跨三层结构与多线程协作（事件循环线程只入队）、订阅生命周期与 KV 动作时序
跨模块生效，"只改一层不改另两层"是数据不一致的头号来源，specs 的执行路径全景与
修改检查单就是为根治此问题而设。

**前置铁律——文档先是设计的输入，才是提交的产出。** 动手改任何代码**之前**，先读相关
`docs/specs/S_NN_*`（现有契约 / 执行路径 / 修改检查单），拿它们校准方案；方案定了**当场**同步
修订对应 spec。先码后档、拖到提交前才补文档是把流程做反了。

三条强制约束，提交时必查：

1. **特性更新归档 feature 且三连续提交**：`docs/features/` 新增 `F_NN_<slug>.md`；特性代码
   （`feat(session)`）、测试（`test(session)`）、文档（`docs(session)`）拆成三个
   紧邻提交、同一 PR 落地。bugfix 触及 spec 描述的路径 / 不变量时归档
   `docs/bugfix/B_NN_<slug>.md`（复现场景 + 根因 + 修复口径），代码、测试、文档合并为**一个**
   `fix(session)` 提交——三连续提交的唯一例外（与 skill / skill_turbo 模块约定一致）。
2. **规约变动必更新 specs**：契约 / 执行路径 / 不变量 / 持久化结构 / 订阅协议变化时，同步修订
   对应 `S_NN`（元信息"最近一次修订日期"填当天）；新规约主题 = 新 spec 文件。
3. **读到不一致当场修文档**：本文件 / specs 里与代码不符的接口名、枚举值、路径、点名数量的
   结构表述（如"三层写入"、"三索引"、"错误三分类"），先 grep 代码、以代码为准刷新文档，
   同一次改动落地；不把过时表述当新约束执行。
