# S_04 · fork / rewind / restore 会话操作与上下文恢复

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_04 / session-ops-and-context-restore |
| 关联模块 | `jiuwenswarm/agents/harness/common/session_ops_service.py`（物理位置在 agents/harness/common/，业务归 Agent 运行时域）；协作方：`server/runtime/session/session_history.py`、`session_metadata.py`（会话与配置子模块）、`server/utils/diff_service.py` |
| 最近一次修订日期 | 2026-10-21 |
| 关联 feature | 暂无 |

## 范围 / 边界

### 本规约管什么

`session_ops_service.py` 全部 12 个纯函数：fork_session / rewind_session /
compact_partial_session / list_session_turns / get_last_turn_info /
restore_session_files / redo_session_files / warmup_session_context /
rewind_session_context / resolve_live_agent_session / copy_session_state /
copy_session_context。核心是**三条数据轴的对齐**：磁盘 history.jsonl（对话）、
checkpointer（DeepAgentState / HITL 状态）、file_ops 日志 + 工作区文件（代码改动）。

### 本规约不管什么

| 子主题 | 归属 |
|---|---|
| history.jsonl / metadata.json 读写原语与写队列 | `session/session_history.py`、`session_metadata.py` |
| DiffService 的 file_ops 扫描与 discard 标记 | `server/utils/diff_service.py` |
| adapter 侧在哪个时点调用 warmup_session_context | `agent_adapter/AGENTS.md` |

## 不变量

- **N1 soft-only 原则**：`rewind_session` 与 `compact_partial_session` 对
  file_ops 只做 `soft=True` 截断——**只回退对话、不动工作区文件**。硬删快照会让
  文件永久失去 /rewind 选 code 的回滚能力却仍报告成功。工作区文件回退唯一入口
  是 `restore_session_files`（显式调用）。
- **N2 project_dir 先解析后截断**：`truncate_file_ops` 依赖的 project_dir 必须
  在 `update_session_metadata` **之前**解析——metadata.json 是非原子原地覆写且
  走后台线程，之后解析会撞半截 JSON → 静默 None → 清理无声失效
  （rewind_session L220 注释，历史 bug，见 tests/unit_tests/test_rewind_project_dir_race.py）。
- **N3 截断后元数据同步**：每次 history 截断（rewind / compact）必须
  `update_session_metadata(set_message_count=remaining)`。
- **N4 warmup 有界且从 user 边界开始**：`_WARMUP_HISTORY_MAX_RECORDS = 10_000`；
  超限截断后的保留尾巴若找不到 user 边界则**整段丢弃**（防孤儿 tool result 播种
  上下文）。`history_before_request_id` 指定时只恢复该请求之前的记录。
- **N5 事件流重建状态机**（`_build_context_messages_from_history`）：history.jsonl
  存的是**流式事件不是干净消息**。每个 LLM call 边界 = 新 reasoning chunk 在
  tool_calls 收集后到达时 flush 出 AssistantMessage(reasoning + tool_calls)；无
  matching tool_call 的 tool_result 一律跳过（ask_user 会预发一条空 result）；
  后处理过滤未闭合 tool_calls（对齐 claude-code filterUnresolvedToolUses——历史
  截断在轮中间 / 崩溃时会制造 AssistantMessage 带 tool_calls 但无 ToolMessage，
  直接喂 API 会报错）。chat.delta / usage / tool_update / ask_user_question /
  compact_boundary 全部 skip。
- **N6 rewind 重建的 live-session 语义**：`resolve_live_agent_session` 匹配
  `_interaction_session` / `_loop_session` 的 live session 时**原地重建后用
  `commit()` 而非 `post_run()`**——post_run 会关流并标记结束，而下一轮 chat 必须
  继续用同一个 Session 对象（写新 session 只更新 checkpointer，下轮读到的仍是
  旧内存快照——这个陷阱同样咬 `DeepAgentState.plan_mode`）。无 live session 才
  建临时 session 并 pre_run/post_run。
- **N7 重建前必须清残留**：`_wipe_session_runtime_state` 清 context /
  `_SESSION_STATE_KEY` / `INTERRUPTION_KEY` / `INTERRUPT_AUTO_CONFIRM_KEY` 与
  `_hitl_handler` 内存态——**INTERRUPTION_KEY 残留 = 旧卡应答被复活的根源**。
  空 history 的 rewind 也要走清空重建（`_apply_rewound_context` 空消息分支）。
- **N8 fork 的状态移植规则**：`copy_session_state` 读源 DeepAgentState →
  deepcopy → `iteration=0`、`stop_condition_state=None`、`pending_follow_ups=[]`、
  生成新 plan_slug 并复制 plan 文件（旧 slug 的文件绝不能被两个 session 共写）。
- **N9 redo 空结果不是成功**：`redo_session_files` 的 `get_files_to_redo` 返回空
  （file_ops 缺失 / 未打 discarded 标记）时返回的是"空成功"结构，调用方必须判空
  并返回 `REDO_HISTORY_MISSING`，不能误清 discarded 状态。redo 与 restore 对称：
  restore 写回 old_content + 删 agent 新建文件；redo 写回 new_content + 重删。
- **N10 各函数分工边界**：warmup_session_context **不改写 history、不清 Session
  state、不强写 checkpointer**（消息持久化由 history.jsonl 承担；agent 状态已由
  checkpointer pre_run 恢复）；rewind_session_context 是"已截断后的重建"，
  history 破坏性动作全在 rewind_session。

## 接口契约

调用方（生产接缝，改动时保持签名兼容）：

| 函数 | 调用方 |
|---|---|
| `fork_session` | `server/handlers/bootstrap.py`（session.fork；配 copy_session_context / copy_session_state） |
| `rewind_session` + `rewind_session_context` | `server/handlers/session.py`、`gateway/message_handler`（/rewind 受控通道）、`gateway/channel_manager/tui/tui_connect.py` |
| `restore_session_files` / `redo_session_files` | `handlers/session.py`、`tui_connect.py`、`web/git_ws_handler.py`（discard/redo turn changes） |
| `compact_partial_session` | `handlers/session.py`、`tui_connect.py` |
| `warmup_session_context` | `agent_adapter/interface_deep.py`（start_interaction 之后） |
| `get_last_turn_info` | `git_ws_handler.py`（discard 本轮修改） |

compact 落盘结构：`context.compact_boundary` + `context.rewind_summary`（必写）+
可选 `context.compact_summary`（llm_summary，transcript_only=True）；三者
`compact_metadata.trigger="manual_rewind"` 同源。

## 数据结构

- history 记录的 `forked_from`（fork 时逐条回填 `{session_id, original_id}`）与
  metadata 的 `forked_from`（顶层 session id）、`project_id/project_dir/channel_metadata`
  继承。
- `_NON_USER_AUTHORED_TAGS`：list_session_turns 过滤系统注入消息的 7 类 tag
  （local-command-stdout/stderr、bash-stdout/stderr、task-notification、tick、
  teammate-message）。
- fork 标题：`"(Branch)"` / `"name (Branch)"` / `"(Branch N)"`，基于全量
  existing_titles 去重（limit=500）。

## 执行路径

- **P1 rewind**：flush → load → 定位 turn（user_positions[turn_index-1]）→
  解析 project_dir（N2）→ truncate_history_records → update metadata（N3）→
  soft truncate file_ops（N1）→ 调用方择机 `rewind_session_context`（清 + 重建 +
  commit/post_run）。
- **P2 fork**：flush + load → 逐条 forked_from → 写目标 history → 读源 metadata →
  算唯一标题 → 写目标 metadata → 调用方配 copy_session_state/context。
- **P3 warmup**：内存 context 存在则短路 → 磁盘 history → N4 限界 → N5 状态机
  重建消息 → live/临时 session → create_context。
- **P4 restore/redo**：DiffService 求文件集 → 逐文件 write/delete（异常逐条收集
  不中断）。

## 修改检查单

- 改 rewind / compact → 对照 N1/N2/N3 三连；跑
  `tests/unit_tests/test_rewind_project_dir_race.py` 与 `test_compact_partial.py`。
- 改 `_build_context_messages_from_history` → 跑 `test_session_ops_warmup.py`；
  新增 history 事件类型时明确归入 N5 的 skip / 处理清单。
- 改 HITL 清理（N7）→ 同查 adapter 的 stale 卡拒绝路径（agent_adapter 侧）。
- 改 fork 状态移植（N8）→ 跑 `test_session_ops_state_copy.py` /
  `test_session_ops_fork.py`；DeepAgentState 加字段时评估是否需要 fork 重置。
