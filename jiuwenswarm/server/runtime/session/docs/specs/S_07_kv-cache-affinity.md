# S_07 · Ascend KV 缓存亲和（生命周期 + 产品钩子）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_07 / kv-cache-affinity |
| 关联模块 | `jiuwenswarm/server/runtime/session/kv_cache_affinity_lifecycle.py`（418 行）<br>`jiuwenswarm/server/runtime/session/kv_cache_product_hooks.py`（250 行） |
| 最近一次修订日期 | 2026-10-22 |
| 关联 feature | 暂无 |

## 范围 / 边界

### 本规约管什么

- 会话级 KV 缓存动作协议：`evict` / `offload` / `prefetch` 三类动作的
  同步执行（`run_session_kv_cache_lifecycle`）与后台调度
  （`dispatch_session_kv_cache_lifecycle`）。
- 总开关与门槛判定：`is_kv_cache_affinity_enabled`（配置门）、
  provider 白名单（仅 `AscendAffinity`）、模型能力探测。
- 后台任务的注册 / 排序 / 回收纪律（`_BACKGROUND_ACTIONS` /
  `_BACKGROUND_TAILS` 与 `cancel_pending_*`）。
- 产品侧钩子的三张契约：`evict_plan_session`（删除前逐出）、
  `resolve_session_switch_context`（切换事实解析）、
  `dispatch_session_switch_signals`（切换后信号发送）、
  `cancel_pending_tasks`（三级注册表清理）。

### 本规约不管什么

| 子主题 | 归属 |
|---|---|
| `evict_kvc` / `offload_kvc` / `prefetch_kvc` 的**实现**（重试 / 退避 / 截止时间所有权） | `openjiuwen.core.foundation.kv_cache`（agent-core 仓） |
| Plan 侧与 Team 侧的信号注册表本体 | `openjiuwen.core.foundation.kv_cache` / `openjiuwen.agent_teams.kv_cache.kv_cache_lifecycle` |
| Team 会话的 prefetch 编排（team_manager.prefetch_session_kv_cache 的内部逻辑） | `agents/harness/team/`（team 域） |
| `is_team_params` 判定与 `build_reasoning_model_request_kwargs` | `jiuwenswarm.common` / `server/utils` |
| 钩子的**调用时机**（谁在删除 / 切换 / 关闭时调它们） | 各调用方（handlers/session.py、agent_ws_server.py、gateway 两连接层） |

### 关键背景：best-effort 语义贯穿全部

KV 缓存亲和是**纯增益功能**：所有动作失败只记 warning 返回
`failed` / `False`，**绝不抛出、绝不阻塞、绝不回滚产品动作**——
`evict_plan_session` 的 docstring 明言 "preserving delete"（逐出失败也照删）。
默认关闭（`enable_kv_cache_affinity=False`），且两文件互相 import 均为
**函数内延迟导入**（容忍对方不可用）。

## 不变量

- **N1 三类动作 / 四种结果**：`KVAction = evict | offload | prefetch`
  （:19）；`KVCacheLifecycleResult.status ∈ {ok, failed, skipped,
  scheduled}`（frozen dataclass，附 `ok` / `failed` / `scheduled`
  便捷属性）。skipped = 条件不满足（未启用 / 无模型 / 模型不支持），
  failed = 已尝试但失败（含异常与 provider 拒绝）。
- **N2 总开关**（:46-51）：`config.react.kv_cache_affinity_config.
  enable_kv_cache_affinity`，缺省 False。所有入口（run / dispatch /
  hooks 的 gate）都先查此开关——关则 `skipped`，不产生任何下游调用。
- **N3 provider 白名单**：模型 `model_client_config.client_provider`
  非空时必须等于 `AscendAffinity`（大小写不敏感，:71-72 / :190-199），
  否则记 warning 返回 `failed`（**不是 skipped**——配置了启用却指错
  provider 属于部署错误，应当显式暴露）。provider 为空放行（兼容不
  上报 provider 的自定义模型）。
- **N4 模型解析链**（:88-166）：优先活体——
  `agent._adapter._model` → `agent.get_instance().deep_config.model` →
  `...react_agent._llm`；全失败则用配置**重建默认模型**
  （`_build_default_model`：`get_default_models` 的 `is_default` 项，
  无标记取第一项，再退到 `models.default` / `react` 节点；缺
  `client_provider` 补 "OpenAI"，request kwargs 经
  `build_reasoning_model_request_kwargs`）。能力探测
  `_model_supports_kv_cache_affinity`（:54-61）：优先
  `supports_kv_cache_affinity()` 方法，否则要求 `evict_kvc` /
  `offload_kvc` / `prefetch_kvc` 三个方法齐全——不满足 → skipped。
- **N5 动作调用契约**（:205-224）：动态取 `model.{action}_kvc`
  （不可调用 → skipped）；调用统一
  `action_fn(target="session", session_id, parent_session_id or
  session_id, timeout=action_timeout)`。timeout 经
  `resolve_kvc_action_timeout(action, "session", timeout)`
  （agent-core 侧解析）——**整个动作的截止时间（含重试 / 退避）归
  AscendAffinityModelClient 所有**，产品生命周期代码只选择并转发
  共享预算，超时所有权不歧义（:221-223 注释）。返回值按 truthiness
  判定成败；一切异常捕获后 → failed（**run 永不抛出**）。
- **N6 后台调度只限 offload / prefetch**：`dispatch_session_kv_cache_
  lifecycle` 的 action 参数类型为 `Literal["offload", "prefetch"]`
  （:256）——evict 必须同步等待（删除路径需要确认结果），不进后台。
- **N7 后台任务的会话内串行**（:251-329）：`_BACKGROUND_TAILS`
  按 session_id 记录最近一次后台任务；新任务先
  `await asyncio.shield(previous)`（gather + return_exceptions）再
  执行——同会话的后台信号**有序**，跨会话并行。任务名
  `kvc-{action}[{session_id}]`；`_BACKGROUND_ACTIONS` 集合持强引用
  防 GC；done_callback 回收注册表并消化结果（failed → warning、
  CancelledError → debug、异常 → warning）。dispatch 同步返回
  `scheduled`。
- **N8 evict 对后台尾巴的等待**（:209-212）：同步 evict 前先
  `await asyncio.shield(previous)`（排除自身）——保证 evict 观察到
  该会话所有先前后台动作完成后的状态。
- **N9 三级注册表清理**（`cancel_pending_tasks` :27-63）：按
  best-effort 依次清理 **root**（本模块
  `cancel_pending_kv_cache_lifecycle_tasks`）、**Plan**
  （`openjiuwen...cancel_pending_session_kv_cache_signals`）、
  **Team**（`openjiuwen.agent_teams...cancel_pending_signal_tasks`）。
  每个注册表 import 失败只 warning 不阻断（对应仓版本缺失时降级），
  每个清理调用独立 try 包裹。
- **N10 切换事实解析是纯函数**（`resolve_session_switch_context`
  :108-181）：只读不写——params（请求携带的 mode / team /
  previous_mode）与 session metadata（`session_metadata.
  get_session_metadata`）两源合成 `SessionSwitchContext`
  （target_is_team / previous_is_team / resolved_mode /
  affinity_enabled）。previous 为 `"new"` 或等于 target 时**不解析**
  previous metadata（视为无前会话）；gate 查询失败 →
  affinity_enabled=False（KVC 跳过，切换本身继续）。team 判定 =
  params 优先、metadata 兜底（`is_team_params`）；resolved_mode =
  team 则 "team"，否则 metadata.mode → params.mode → "agent.plan"。
- **N11 切换信号发送在产品侧处理完成之后**（
  `dispatch_session_switch_signals` :184-250）：语义为「产品 owner
  已完成切换，KVC 是可选后处理」。规则：真实 previous 且非 team →
  dispatch offload（不 await）；target 有历史（`history_exists`）时
  ——team 走 `team_manager.prefetch_session_kv_cache`（**await**，
  team 域自己管理其信号），非 team → dispatch prefetch。Plan 模型
  活体取 `agent_manager.get_agent_nowait(channel_id)`，失败回退
  配置模型。整体 try 包裹：信号失败仅 warning（"continuing"）。

## 接口契约

### kv_cache_affinity_lifecycle.py（会话级动作层）

| 函数 | 契约要点 |
|---|---|
| `is_kv_cache_affinity_enabled(config=None)` | 读 `react.kv_cache_affinity_config.enable_kv_cache_affinity`；config 参数允许注入测试配置 |
| `run_session_kv_cache_lifecycle(action, *, session_id, parent_session_id=None, agent=None, config=None, timeout=None)` | 同步执行一个动作。skip 链：空 session_id → 未启用 → 无模型 → 不支持 → 无 action_fn；异常 / provider 不符 / 返回 falsy → failed；成功 → ok。evict 先等后台尾巴（N8） |
| `dispatch_session_kv_cache_lifecycle(action, *, ...)` | action 仅 offload / prefetch（N6）。空 session / 未启用 → skipped；启用 → 建后台任务返回 scheduled（会话内串行，N7） |
| `prefetch_session_kv_cache` / `offload_session_kv_cache` / `evict_session_kv_cache` | 对应动作的同步便捷包装 |
| `dispatch_prefetch_session_kv_cache` / `dispatch_offload_session_kv_cache` | 对应动作的后台便捷包装（evict 无此形态） |
| `resolve_kv_cache_affinity_model(*, agent=None, config=None)` | 模型解析链（N4）——活体优先、配置重建兜底 |
| `cancel_pending_kv_cache_lifecycle_tasks()` | 取消并回收本模块全部后台任务（root 级清理，N9 的第一级） |

### kv_cache_product_hooks.py（产品编排层）

| 函数 | 契约要点 |
|---|---|
| `evict_plan_session(*, session_id, agent=None, agent_manager=None, channel_id=None)` | 永久删除非 team 会话前的 best-effort 逐出；agent 可为 None（此时经 agent_manager.get_agent_nowait 取活体）。返回 bool；**任何失败不阻断删除** |
| `resolve_session_switch_context(*, target_session_id, previous_session_id, params)` | 纯函数（N10），返回 `SessionSwitchContext` |
| `dispatch_session_switch_signals(*, context, agent_manager, channel_id, team_manager, target_session_id, previous_session_id, reason)` | 产品侧切换完成后的信号发送（N11）；不抛出 |
| `cancel_pending_tasks()` | 三级注册表清理（N9）；每级独立容错 |

### 调用方分布（实测 grep）

| 调用方 | 用途 |
|---|---|
| `server/handlers/session.py` :505 | 删除会话路径：`evict_plan_session`（先逐出再删） |
| `server/agent_ws_server.py` :1233 / :1572 | 运行时收尾 `cancel_pending_tasks`；会话切换 `resolve_session_switch_context` + `dispatch_session_switch_signals` |
| `gateway/channel_manager/web/app_web_handlers.py` :4254 | Web 删除：`evict_session_kv_cache` |
| `gateway/channel_manager/tui/tui_connect.py` :1636 | TUI 删除：`evict_session_kv_cache` |
| `agents/harness/team/kv_cache_hooks.py` :23 | Team 域复用 gate：`is_kv_cache_affinity_enabled` |

## 数据结构

| 结构 | 字段 / 取值 |
|---|---|
| `KVCacheLifecycleResult` | frozen；status ∈ ok / failed / skipped / scheduled |
| `SessionSwitchContext` | frozen；target_is_team / previous_is_team / resolved_mode / affinity_enabled |
| `_BACKGROUND_ACTIONS` | `set[Task]`——后台任务强引用（防 GC + 全量取消用） |
| `_BACKGROUND_TAILS` | `dict[session_id, Task]`——每会话最近后台任务（串行链） |

## 执行路径

- **P1 会话删除（Gateway 两连接层）**：Web / TUI 删除 handler →
  `evict_session_kv_cache(session_id, parent_session_id=session_id)` →
  gate / provider / 模型解析 → 同步 `evict_kvc`（先等后台尾巴）→
  结果仅影响日志，删除流程继续。
- **P2 会话删除（AgentServer handlers/session.py）**：
  `evict_plan_session`（活体 agent 优先）→ 同上，返回 bool 供调用方
  记录，**不改变删除结果**。
- **P3 会话切换（agent_ws_server）**：`resolve_session_switch_context`
  （纯解析）→ 产品 owner 完成切换 → `dispatch_session_switch_signals`：
  offload 前会话（非 team 且非 "new"）后台化；prefetch 目标会话
  （有历史时；team 走 team_manager await 版）。
- **P4 运行时收尾**：`cancel_pending_tasks` → root / Plan / Team 三级
  注册表逐个取消（import 缺失降级 warning）。

## 修改检查单

改动本两文件前自问：

1. 是否破坏「best-effort、永不抛出」语义——任何新路径让 KVC 失败
   阻断删除 / 切换 / 收尾，都是回归（关键背景 + N9 / N11）？
2. evict 是否被改成后台 dispatch——删除路径依赖同步确认（N6）？
3. 后台任务是否仍持强引用且 done_callback 完整回收——漏
   `_BACKGROUND_ACTIONS.add` 会遭 GC 静默消失，漏回收会泄漏条目
   （N7）？
4. 新增动作类型是否同步更新 provider 白名单与能力探测的三方法
   集合（N3 / N4）？
5. 是否把超时控制下沉到了产品侧——截止时间所有权归
   AscendAffinityModelClient（N5），产品只转发共享预算？
6. 切换事实解析是否保持了纯函数性——`resolve_session_switch_
   context` 内引入任何写操作都会污染调用方时序（N10）？

## 与其它 spec 的关系

- `openjiuwen.core.foundation.kv_cache`：动作实现、
  `resolve_kvc_action_timeout`、Plan 侧信号注册表与清理
  （agent-core 仓，实现不在本模块）。
- [[S_03]]：`resolve_session_switch_context` 读 session metadata
  （mode / team 判定的兜底源）。
- [[S_02]]：`history_exists`（prefetch 的前置条件——有历史才值得
  预取）。
- `agents/harness/team/kv_cache_hooks.py`：Team 域对 gate 的复用；
  team_manager.prefetch 的编排归 team 域。
- [[S_01]]：`agent_manager.get_agent_nowait` 的队列语义（活体模型
  解析的上游）。
