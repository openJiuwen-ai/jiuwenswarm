# S_02 · 会话历史持久化

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_02 / history-persistence |
| 关联模块 | `jiuwenswarm/server/runtime/session/session_history.py`（1,701 行，含内联 tool_stats 模块） |
| 最近一次修订日期 | 2026-10-22 |
| 关联 feature | 暂无 |

## 范围 / 边界

### 本规约管什么

单个 session 对话历史的完整落盘链路：文件格式（JSONL / legacy JSON 双轨）与
格式引导迁移、目录解析安全、**双缓冲写入架构**（普通缓冲层 + 暂留层）、
三个后台线程（两个 writer + 一个 flusher）、同步读取接口（含 team /
member 过滤视图）、截断、compact 边界记录、进程退出收尾，以及内联的
tool_stats 增量统计模块。

### 本规约不管什么

| 子主题 | 归属 |
|---|---|
| append 时顺带更新的 metadata 写链（message_count / 标题 / delivery_context） | [[S_03]] |
| 截断之后的上下文重建状态机 | runtime/docs 的 session-ops spec（`agents/harness/common/session_ops_service.py`） |
| subagent 会话的业务语义 | subagent 模块文档（本文只管其历史的独立落盘通道） |
| 消费方之一 `server/utils/diff_service.py` 的 file_ops 扫描 | server 层文档 |

## 不变量

- **N1 双格式双轨 + 环境变量切换**：新格式 `history.jsonl`（每行一条 JSON），
  legacy 格式 `history.json`（JSON 数组）。`use_legacy_history_json()`（:464）
  读 `JIUWENSWARM_USE_LEGACY_HISTORY_JSON`，**默认 legacy**。读写路径分离：
  `get_write_history_path` / `get_read_history_path`（:475/:482）——读取侧
  对两种格式都认（`_history_file_is_json_array` 首字符探测，:555）；首次写
  时 `_ensure_jsonl_bootstrap` / `_ensure_legacy_json_bootstrap`（:662/:676）
  做格式引导，legacy 数组文件可被 `_rewrite_json_array_as_jsonl`（:654）
  整体重写迁移。
- **N2 目录纵深防御在 mkdir 之前**：`resolve_session_dir`（:419）先
  `resolve()` + `relative_to()` 确认路径仍在 sessions 根内，**再** mkdir
  （:443-445 注释）——顺序反了会在根外先越界建目录、事后才检测到。
- **N3 JSONL 读取用 `split("\n")` 不用 `splitlines()`**（:587）：`splitlines`
  会把 U+2028（LINE SEPARATOR）当行界切开合法 JSON；带行号报错日志。
  读取侧对 user 角色内容做 `collapse_file_content_blocks`（:604）——写入侧
  展开的 file-content 块折叠回 `@path` 引用（对称变换，`_FILE_CONTENT_BLOCK_RE`，:39）。
- **N4 三层写入架构**：事件循环线程**只入队**（`_batch_write_items`，:851），
  `session-history-writer` daemon 线程（:987）单线程落盘——写盘 IO 不阻塞
  asyncio 循环。`_WRITE_QUEUE(maxsize=20000)`（:23）；**队列满时调用线程
  退化同步直写**（:854 注释），宁可慢不可丢。subagent 历史走独立的
  `_SUBAGENT_WRITE_QUEUE` + `subagent-history-writer` 线程（:947），与主队列
  互不挤占。
- **N5 双缓冲合并**：`BUFFERABLE_EVENT_TYPES`（:104，delta / reasoning /
  tool_update / tool_calls.delta）在内存合并后批写：
  - **普通缓冲层** `_session_buffer`：同类型同 request 合并（`_MERGE` 分发表
    :361）；`chat.tool_update` 单独走 per-call 字典 `_session_tool_update_buffer`
    （每个 call_id 合并成一条）；触发落盘条件 = `BUFFER_FLUSH_INTERVAL=5.0`
    定时（flusher 线程）或 `BUFFER_MAX_SIZE=100`（delta_count / call 数达到）。
  - **暂留层** `_session_pending`：`PENDING_EVENT_TYPE = "chat.tool_calls.delta"`
    （:111）暂留不写——**若后续 `chat.tool_call` 命中其中某个 call id，整个
    暂留被丢弃**（该 delta 流将被完整 tool_call 记录取代，写出去就是冗余）；
    未命中则暂留期间到达的其他事件进 `pending.pending_queue` 随队缓冲；
    超时（`PENDING_MAX_SECONDS=2.0`）由 flusher 落盘（`_periodic_flush` 条件 B）。
  - 非 BUFFERABLE 事件（chat.final / chat.file 等）类型切换时先冲刷旧缓冲再直写。
- **N6 事件分发规则**（`_route_event`，:1087）：暂留层存在时优先处理暂留
  （同为 delta → 合并进 pending.item；命中 tool_call → pop + 冲 pending_queue +
  本条；其余 → 进 pending_queue）。**不同 sessions_root 的批次严禁合并同写**
  （:1162-1170 显式分流两次 `_batch_write_items`）——多租户写串文件的事故防线。
- **N7 append 主入口的跳过与剥离**（`append_history_record`，:1293）：
  - ephemeral heartbeat session 整体跳过（`_is_ephemeral_heartbeat_session`，:132）；
  - 空 shell assistant 记录跳过（`_has_persistable_assistant_payload`，:146）；
  - **skill env 必须剥离**：`_strip_skill_env_from_history_item`（:1363）——注入
    bash tool_args.env 的 skill_envs 属运行期凭据，不得持久化；
  - extra 序列化过 `_serialize_value_with_flag`（:211）——非 JSON 安全值被
    sanitize 且记 debug 日志（fail-open，不丢整条记录）；
  - subagent 记录（带 `subagent_id`）分流独立队列且**不得覆盖父 session 的
    mode**（:1401-1404 注释）。
- **N8 append 顺带更新 metadata**（:1388-1419）：`increment_message_count=True`；
  user 消息传 `user_content`（自动标题原料）并刷新 `last_user_message_at`
  （用消息时间戳，比请求到达时刻精确）；user 时 `set_session_delivery_context`。
  该段 try/except 包裹——**metadata 失败不回滚 history 写入**（两类持久化
  解耦）。契约细则归 [[S_03]]。
- **N9 一致性读取点**：任何"读 history 必须看到最新"的路径先调
  `flush_session_history`（:1266）——冲本 session 缓冲 + `_WRITE_QUEUE.join()`。
  整文件重写类操作（`write_history_records` :690 / `truncate_history_records`
  :1474）必须**先排空写队列、再在 `_FILE_LOCK` 内重写**——绕开队列直接改文件
  会与后台 writer 竞争造成丢写。
- **N10 进程退出收尾**（`shutdown`，:1207，`atexit.register` :1242）：置
  `_SHUTDOWN_DONE` 防重入 → 停 flusher（join 2s）→ `_periodic_flush` →
  `_force_flush_all_pending`（忽略超时冲掉全部暂留）+
  `_flush_subagent_history_writes(timeout=5.0)` → 排空 `_WRITE_QUEUE`
  （deadline 5s，10ms 轮询）→ 末次复扫。所有步骤失败只 warning 不抛。
- **N11 tool_stats 增量扫描**：`tool_call` / `tool_result` 事件落盘后
  `schedule_tool_stats_update`（:1691）**防抖 5s**（重复调用只刷新 deadline）
  由 `tool-stats-scanner` daemon 线程（:1686）执行 `_tool_stats_scan_once`
  （:1586）：从 state 文件（`tool_stats.json`）持久化的 **offset** 增量扫描、
  结果按 tool 汇总（成功 / 失败计数）后写回。TOCTOU 注释（:1600）：offset
  加载与文件读取之间若发生 truncate / 整文件重写，offset 可能指向新文件中部——
  扫描对非法起点做重置处理。孤儿 result（无匹配 open call）/ 空结果跳过。

## 接口契约

| 函数 | 语义 | 主要调用方 |
|---|---|---|
| `append_history_record(*, session_id, request_id, channel_id, role, content, timestamp, event_type, extra, channel_metadata, mode, sessions_root, task_id, subagent_id)` | 唯一追加入口（keyword-only） | agent_adapter 流式事件侧 |
| `append_compact_history_records`（:1422） | compact 边界 + 可选摘要（摘要 timestamp+0.001 保序在边界之后） | compact 流程 |
| `load_history_records` / `read_session_history_records`（:618/:826） | 同步读取（N3 折叠变换在读取侧） | 会话列表 / warmup / session_ops |
| `read_team_history_records`（:741） | team 视图过滤 | team 快照刷新 |
| `read_member_history_records(session_id, member_name)`（:808） | 单成员视图（`_is_member_relevant` 按 member_name 匹配） | team 成员上下文 |
| `write_history_records` / `truncate_history_records` | 整文件重写 / 按 cut_index 截断（N9 先冲队列） | fork / rewind（session_ops） |
| `flush_session_history`（:1266） | 读前一致性点（N9） | rewind / fork / 列表读取等 |
| `resolve_session_dir`（:419） | 目录解析 + 纵深防御（N2） | metadata / 本模块 |
| `resolve_subagent_history_path`（:379） | subagent 历史子路径（同样 relative_to 防御，:396） | subagent 流转发 |
| `shutdown()`（:1207） | atexit 收尾（N10） | 进程退出自动触发 |
| `schedule_tool_stats_update` | 防抖调度统计扫描（N11） | append 内部 |

## 数据结构

- 记录字段：`id`（`f"{request_id}:{role}"`）/ `role`（仅 assistant/user 两值，
  :1316 归一）/ `request_id` / `channel_id` / `timestamp` / `content` /
  `event_type`（仅 assistant）/ `session_id` / 可选 `task_id` / `subagent_id` /
  `mode` / extra 展开字段。
- `_PendingState`（:250，dataclass）：`item`（暂留的 delta 合并体）/ `request_id` /
  `pending_queue`（OrderedDict）/ `start_time`（monotonic）/ `sessions_root`。
- 普通缓冲层五张按 sid 索引的字典：`_session_buffer` / `_session_buffer_type` /
  `_session_buffer_request_id` / `_session_buffer_root` /
  `_session_tool_update_buffer`（per-call）。**同类型同 request 才合并**，
  request_id 变化即冲刷（`_flush_on_request_switch`，append 内 :1374）。
- tool_stats 状态文件三键：`offset` / `open_calls`（未闭合 tool_call）/ `stats`。
- 线程清单（全 daemon）：`session-history-writer` / `subagent-history-writer` /
  `session-history-flusher`（`BUFFER_FLUSH_INTERVAL` 周期）/ `tool-stats-scanner`。

## 执行路径

- **P1 追加**：append → 跳过判定（N7）→ 构造记录 → skill env 剥离 →
  subagent 分流（独立队列）→ request 切换冲刷 → `_route_event` 三路分发
  （N5/N6）→（tool_call/tool_result）防抖调度统计（N11）→ metadata 顺带更新（N8）。
  缓冲路径异常 → **降级直写**（:1376-1378）。
- **P2 后台落盘**：writer 线程从 `_WRITE_QUEUE` 取批次 → `_FILE_LOCK` 下
  JSONL append（`_append_record_jsonl`）或 legacy 重写；flusher 线程周期执行
  `_periodic_flush`（普通缓冲全量冲 + 暂留超时冲）。
- **P3 读取**：`resolve_session_dir`（N2）→ 读路径判定（新 / legacy 格式）→
  解析（N3 split 规则 + 折叠）→ 过滤视图（team / member）。
- **P4 重写 / 截断**：`flush_session_history` 排空 → `_FILE_LOCK` 内整文件写 /
  按 cut_index 前缀保留重写 → （调用方）同步 metadata message_count。
- **P5 退出**：N10 顺序。

## 修改检查单

- 改事件类型集合（BUFFERABLE / NORMAL_BUFFER / PENDING）→ 同步核对 `_MERGE`
  分发表有对应合并函数；新增暂留候选类型必须定义"命中即丢弃"的判据。
- 改队列容量 / 常量（20000 / 100 / 5.0 / 2.0）→ 点名数字是调优契约，改前评估
  内存上限与可见性延迟的取舍；`shutdown` 的排空 deadline 与 `_SUBAGENT_SHUTDOWN_
  FLUSH_TIMEOUT_S` 需同步审视。
- 改记录字段（id 公式 / role 归一）→ 读取侧（session_ops 的重建状态机）与
  [[S_03]] metadata 的 message_count 语义联动核查。
- 改 sessions_root 相关分流（N6 两段写）→ 多租户写串文件是事故红线，改动必须
  保持不同 root 批次分离。
- 改 tool_stats（offset 语义 / state 结构）→ 旧 state 文件的兼容读取；TOCTOU
  重置分支必须有对应测试。
- 改 `_strip_skill_env_*` → 与 skill 模块的 env 注入点对齐（skill/AGENTS.md），
  剥离漏项 = 凭据落盘事故。

## 与其它 spec 的关系

- [[S_01]]：processor 上跑的任务产生本文落盘的事件流。
- [[S_03]]：append 顺带的 metadata 更新（N8）落到写队列侧契约；heartbeat
  session 前缀语义两边共用。
- [[S_04]]：subagent 历史目录归属 session 目录树（`subagent_history_dir_name`）。
- runtime/docs session-ops spec：fork / rewind 消费本文的重写 / 截断原语，
  截断后重建状态机的契约在那边。
