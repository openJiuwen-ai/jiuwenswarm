# S_01 · 会话管理器与任务队列

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_01 / session-manager-and-task-queue |
| 关联模块 | `jiuwenswarm/server/runtime/session/session_manager.py`（498 行）；协作：`work_mode.py`（179 行）、`session_rename.py`（69 行）、`jiuwenswarm/telemetry/session.py` |
| 最近一次修订日期 | 2026-10-22 |
| 关联 feature | 暂无 |

## 范围 / 边界

### 本规约管什么

- `SessionManager`：每个 session_id 一条任务队列与一个 processor 协程的全部
  生命周期——入队（submit_task / submit_and_wait）、执行环、优雅关闭
  （close_session / close_all_sessions）、一次性 session 的哨兵回收。
- `work_mode.py`：`session.create` 请求的 work_mode / project_id / project_dir
  参数归一化（`resolve_session_work_mode_params`）与严格校验
  （`resolve_request_work_mode`）。本模块**只做参数解析**，不反查项目注册表。
- `session_rename.py`：session.rename 指令的三语义实现。

### 本规约不管什么

| 子主题 | 归属 |
|---|---|
| rename 背后的 metadata 读写原语与写队列 | [[S_03]] |
| 归一化之后的 Project 存在性 / work_mode 一致性校验 | [[S_04]] |
| history / 持久化 | [[S_02]] |
| processor 上跑的 agent 主流程 | `agent_adapter/AGENTS.md`（interface.py 持有 SessionManager 实例） |

## 不变量

- **N1 每 session 一个 processor + 一个 PriorityQueue**：`ensure_session_processor`
  在 processor 缺失或已 done 时**重建队列并把优先级归零**（session_manager.py:239-240）。
  队列元素是四元组 `(priority, task_func, task_ctx, result_future)`；`task_func
  is None` 是哨兵语义（:257-260）。
- **N2 LIFO——新任务优先**：每次入队 `_session_priorities[session_id] -= 1`
  （:349/:386），PriorityQueue 取最小值 → 后提交的任务排在先提交的之前执行。
  已在执行的任务**不被打断**（抢占由 interrupt 机制另行处理，不走本队列）。
- **N3 一次性 session 哨兵回收**：session_id 前缀 `heartbeat` / `cron` /
  `diagnosis`（`_is_oneshot_session`，:62）永不复用。`submit_and_wait` 的
  finally 投递哨兵 `(1_000_000_000, None, None, None)`（:405）——**大正数
  优先级确保排在所有已入队任务之后**，processor 收到即 break，走 finally 清理
  回收队列 / 字典条目，防 processor 协程永久泄漏。
- **N4 ContextVar 快照传代**：submit 时 `contextvars.copy_context()`（:353/:390），
  processor 用 `asyncio.create_task(task_func(), context=task_ctx)`（:266）——
  任务继承**提交者**的上下文（workspace / cwd / project_root），而不是 processor
  协程自身可能陈旧的上下文。
- **N5 代际隔离**：processor finally 只在映射仍属于本代时清理
  （`self._session_queues.get(session_id) is queue` 且 processor 一致，:321-326）。
  close_session 允许同一 session_id 在旧 processor 退出前重连成新一代——
  新一代先建新映射，旧 processor 退出时发现映射已不属于自己，不误删。
- **N6 空闲引用清空**：每轮任务收尾后显式置空
  `item = task_func = task_ctx = result_future = task = None`（:302-305）——
  queue.get() 的下一次 await 会保留当前协程帧，不清空则空闲 session 持有上轮
  任务的对象图（内存泄漏源）。
- **N7 close_session 优雅关闭**：顺序固定（:150-218）——pop 全部映射 →
  标记当前任务 cancelled（telemetry）→ cancel 其 result_future → **排空队列**
  并 cancel 所有 pending future → cancel processor（若非 current_task）→
  `_track_closing_task` 挂 done_callback → `asyncio.wait(timeout=wait_timeout)`。
  默认 `wait_timeout=5.0`，超时只 warning **不抛错**。返回值 `had_runtime`
  表示本 session 此前是否持有运行时状态。
- **N8 work_mode 严格校验**：`resolve_request_work_mode`（work_mode.py:60）对
  非法值返回 `(None, "BAD_REQUEST")`，**公开接口不得静默回落**；未传 / None /
  空串 → 按通道默认（tui→`code`，其他→`work`）。`resolve_session_work_mode_params`
  **只信服务端传入的 channel_id，不信任 `params.channel_id`**（:116-117）——
  防 Web 客户端伪装 TUI 通道获得 code 模式会话（功能面越权）。
- **N9 默认项目冲突检测**：`project_id="default"` 与显式 `work_mode≠work` 矛盾 →
  BAD_REQUEST；`"default_code"` 与 `work_mode≠code` 矛盾同理（:143-164）。
  `has_explicit_work_mode` 标识请求是否显式传 work_mode，真实 project 命中后的
  一致性校验由调用方（binding）完成。
- **N10 rename 三语义与截断**：`title=None` 查询、空串（strip 后）清除
  （`clear_title=True`）、非空设置；标题一律 `strip()[:200]`
  （`_RENAME_TITLE_MAX_LEN=200`，session_rename.py:11）——防御异常 / 恶意输入
  污染 metadata.json。设置前无 metadata 则先 init（channel_id 默认 `"tui"`）。

## 接口契约

`SessionManager` 不是全局单例——由 `JiuWenSwarm` 门面实例化并持有
（agent_adapter/interface.py:1195），一个 adapter 实例一个 manager。

| 函数 | 语义 | 生产调用方 |
|---|---|---|
| `submit_task(session_id, task_func)` | 入队，不等结果 | agent_adapter/interface.py（会话主流程） |
| `submit_and_wait(session_id, task_func)` | 入队 + Future 等结果；一次性 session 结束后投哨兵 | agent_adapter/interface.py:3044 |
| `close_session(session_id, wait_timeout=5.0)` | 释放本代 processor，返回 had_runtime | agent_adapter/interface.py:4431 |
| `close_all_sessions()` | 并发关闭全部 | 进程退出路径 |
| `get_current_task` / `has_active_processor` / `has_session_runtime` / `get_session_queues` | 状态查询（后者供工作状态判定） | gateway / handlers |
| `resolve_request_work_mode(params, channel_id)` | 严格校验，非法 → BAD_REQUEST | gateway/cron/controller.py:329、server/handlers/_default.py:525、app_web_handlers.py:4731、project_store.py:633 |
| `resolve_session_work_mode_params(params, channel_id)` | session.create 三元组归一化 | server/handlers/bootstrap.py:118、gateway/app_gateway.py:215（主路径 / fallback 共用，保持一致） |
| `default_work_mode_for_channel(channel_id)` | tui→code，其他→work | session_metadata.py（work_mode 兜底） |
| `infer_legacy_project_work_mode(raw_project)` | 旧记录兜底，非法回退 work | project_store.py:135/:271 |
| `apply_session_rename(params, connection_session_id, init_channel_id="tui")` | 三语义，返回 `(ok, payload, err, code)` | handlers/session.py:315、tui_connect.py:2043、app_web_handlers.py:3967（本地模式）；**remote 模式**标题走 `channels/web/history_store/db_actor.py:477`，语义须与本函数对齐 |

`work_mode.py` 重导出 `common.work_mode` 的常量（`DEFAULT_WEB_WORK_MODE="work"` /
`DEFAULT_TUI_WORK_MODE="code"` / `DEFAULT_PROJECT_ID_WORK="default"` /
`DEFAULT_PROJECT_ID_CODE="default_code"` 等），外部调用方保持单一 import 来源。

## 数据结构

| 状态 | 类型 | 谁写谁读 |
|---|---|---|
| `_session_queues` | `dict[session_id, PriorityQueue]` | processor 重建写 / close_session 清 |
| `_session_processors` | `dict[session_id, Task]` | ensure_session_processor 建 / close_session pop |
| `_session_priorities` | `dict[session_id, int]`（从 0 单调递减） | submit 写 |
| `_session_tasks` | `dict[session_id, Task \| None]` | processor 循环写（执行前置 task、结束置 None，:269/:300） |
| `_task_result_futures` | `dict[Task, Future]` | submit_and_wait 建 / close_session cancel |
| `_closing_tasks` | `dict[session_id, set[Task]]` | `_track_closing_task` 加、done_callback 自动移除 |
| `_telemetry_sessions` | set | `_session_created` 写 / close_session discard |

telemetry 钩子（`_session_created` / `_task_started` / `_task_finished` /
`_mark_task_cancelled`）委托 `jiuwenswarm.telemetry.session`，任务状态四值
`idle / error / cancelled / idle`（成功）在 processor 循环内判定。

## 执行路径

- **P1 提交**：`ensure_session_processor` → priority 递减 → `copy_context` →
  `queue.put((priority, task_func, ctx, future))`。submit_and_wait 额外包一层
  `wrapped_task`（异常转投 Future）并在 finally 判定一次性 session 投哨兵。
- **P2 processor 循环**（`process_session_queue`，:242-330）：哨兵或代际失配
  break → create_task(context=ctx) → await（CancelledError / Exception 均置
  task_state 后 re-raise）→ task_done → 引用清空（N6）→ finally 代际清理（N5）。
- **P3 关闭**：见 N7 顺序。
- **P4 work_mode 归一化**：`has_explicit_work_mode` 判定 → `resolve_request_work_mode`
  → 默认项目分支（冲突检测 N9 + project_dir 透传）或真实 project_id 三元组透传。
- **P5 rename**：get_session_metadata →（设置分支先 init）→ update(title) /
  update(clear_title) → get 回读拼 payload（含 previous_title）。

## 修改检查单

- 改队列元素结构（四元组）→ 连带改 processor 解包点（:257）与 close_session
  排空解包点（:181）。
- 改一次性 session 前缀（:62）→ 核对 [[S_03]] 的内部会话目录前缀
  （`heartbeat_` / `__prewarm__` / `diagnosis_` 等）语义是否需同步。
- 改 close_session 的 cancel / wait 顺序 → 重跑会话关闭相关单测；`had_runtime`
  的判定覆盖六个字典，新增状态字典时补进 `has_session_runtime` 与
  `close_all_sessions` 的并集。
- 改 work_mode 校验 → 四个调用方逐一确认；`common/work_mode` 的
  `SUPPORTED_WORK_MODES` 变更属于跨层契约（见 common/AGENTS.md 的配置 spec）。
- 改 rename → 本地三调用方 + db_actor remote 路径语义对齐确认；200 上限与
  [[S_03]] `_auto_title` 的 50 字符截断是**两处独立上限**，勿混。

## 与其它 spec 的关系

- [[S_02]]：processor 上任务产出的 history 事件走 S_02 的双缓冲写链。
- [[S_03]]：rename / work_mode 兜底最终落到 metadata 写队列；title 上限规则分立。
- [[S_04]]：`resolve_session_work_mode_params` 的产出交给 binding 做存在性与
  一致性校验——两者拼起来才是 session.create 的完整参数链。
