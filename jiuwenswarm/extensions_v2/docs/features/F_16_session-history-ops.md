# 会话历史读写与追加前扩展

| 项 | 值 |
| --- | --- |
| 编号 | F_16 / session-history-ops |
| 归档日期 | 2026-10-27 |
| 关联计划 | EXTV2-16 / Server PR-04 |
| 主责契约 | [S_06](../specs/S_06_session-history-ops.md) |

## 1. 背景与范围

在 cwd/root/model/repo 的会话绑定基础上，增加 `ops.session.messages(limit=50, cursor=None)`、`ops.session.append(message)` 和追加前事件 `session.append`。用户能在当前会话读取历史、审核或改写待追加正文，并在得到真实写入回执后读回结果。追加只影响产品会话历史，不把内容送入 Agent 执行或注入模型上下文。

本特性也接入 Worker 中既有 `history.append_record` RPC，以及 `run_history_io(append_history_record, ...)` 的主会话可等待写入边界。同步后台 / 子 Agent / Team 专用历史不改造，不用落盘后的通知冒充写入前 Hook。Front 与 Supervisor 复用原路由，EventBus 仍只由 Worker 持有。

## 2. 方案与取舍

- D-01：复用现有历史 JSONL、游标快照、生命周期锁和原子文件操作，不增加第二份消息存储。查询复用历史恢复的可见记录判据。
- D-02：`messages` 是只读能力，不创建同名事件。`append` 方法通过当前 Worker Bus 触发 `session.append`，默认 CORE 才执行一次实际写入。
- D-03：可信调用封装保存目标会话、原记录身份、来源及幂等键；事件只开放正文改写。CORE 重新校验身份与正文，用户不能通过改写 Event 改变目标、角色或包归属。
- D-04：Hook 期间不持历史文件锁；CORE 在线程中先排空原写入队列，再持文件锁和生命周期锁完成去重与写入。返回前文件已关闭，写失败不返回成功；不承诺断电级 fsync 耐久性。
- D-05：扩展追加要求用户提供稳定 id；同一会话、包与 id 的原请求一致时返回 duplicate，原内容不同时报冲突。去重标记与正文在同一历史记录，避免另一个索引文件的提交窗口；首版查重扫描历史，属于已知成本。
- D-06：追加 Hook 中再次调用 append 明确拒绝，避免递归事件链。重复调用 next 不重复提交。USER 抛异常沿用 Core 的故障隔离；显式拒绝使用 `SessionAppendRejection`。
- D-07：取消发生在 Hook 阶段可以不写入；进入文件提交后沿用 `run_history_io` 的屏蔽与排空语义，完成写入后再传播取消。调用方应以同一 id 重试确认结果。

- D-08：Hook 前预检并捕获会话生命周期 generation，提交锁内再次核对；Hook 等待期间删除、恢复等代次变化不能写入新一代会话。平台已有的展示时间戳字段保持原格式，只校验用于平台事实的数值时间戳。
- D-09：沿用当前 Core 的 USER 故障隔离：非法改写或异常可能使该 Handler 被跳过，并用其进入前的有效输入继续，不能将其当作拒绝手段。显式拒绝必须在 next 前返回有效的 `SessionAppendRejection`；下游已落盘后不能再改写成功事实。

## 3. 分工与入口

| 位置 | 责任 |
| --- | --- |
| `handler_ops/session_ops.py` | 在已有 ops.build BUILTIN 中加入 messages / append，沿用当前 Handler 的 SessionContext 和包身份 |
| `event_types/session_history.py` | 事件输入、拒绝 / 回执协议、可信调用封装与递归保护 |
| `event_handlers/session_history.py` | 追加 CORE，校验后委托历史业务服务 |
| `server/runtime/session/session_history_ops.py` | 参数、会话与生命周期校验、分页投影、安全错误与写入服务 |
| `server/runtime/session/session_history.py` | 复用记录构造、主历史存储、提交与幂等判定 |
| `history_io.py` / `agent_ws_server.py` | 将既有可等待主会话写入和追加 RPC 接入事件；未装配 V2 时保留原路径 |

## 4. 用户接入

用户安装示例 ZIP，订阅已有 `context.usage.updated`：调用 append 写一条审计说明，再用 messages 读回。其 `session.append` Handler 只给扩展追加的正文加标记，允许其它平台记录继续。用户不创建 Bus、不注册平台 Ops 或 CORE，不修改平台 Adapter。

```python
import hashlib

receipt = await ops.session.append({
    "id": "audit-" + hashlib.sha256(event.event_input.request_id.encode("utf-8")).hexdigest(),
    "role": "assistant",
    "content": "本回合已完成审计",
})
page = await ops.session.messages(limit=10)
```

## 5. 验证设计与执行记录

### 5.1 单元与集成回归

2026-10-27，Windows / Python 3.11.15；使用本分支源码与 agent-core `rsi_native_agent_os` 的 `ead700d7d18a247968d64de54918cf5d201a207a`。Core 未修改。

定向合跑 **345 项通过、1 项跳过**；新增 `test_session_history_ops.py` 的 **48 项全部通过**。跳过项是原历史路径测试中的符号链接用例，原因是当前 Windows 未获得创建 symlink 的权限。

| 验证范围 | 已通过的结果 |
| --- | --- |
| 生产装配和真实 Registry 用户包 | `ops.build` 提供两项 Ops；USER 在已有 `ui.ready` 等宿主绑定事件中可直接调用；无需 cwd / model / 用量事件 |
| 追加处理链 | 用户改写、显式拒绝、默认 CORE 真正落盘后读回；重复 next、伪造成功、提交后改回拒绝不能伪造提交事实 |
| 隔离与幂等 | 同 session + 包 + 原请求 id 去重；不同内容冲突；跨会话和跨包隔离；对外 record.id 分包命名且保留原 extension_message_id |
| 分页 | 最新在前、稳定游标、后追加排除、跨会话游标拒绝、缺失历史与非法参数 |
| 原平台路径 | 可等待主历史写入和 `history.append_record` RPC 的改写、拒绝与重试；同步写路径保持原行为 |
| 失败与生命周期 | 部分写失败回滚、回滚失败结果未知、Hook 取消无写入、提交取消排空、Hook 内查询无锁等待、递归拒绝、generation 变化拒绝 |
| 安装与兼容 | 用户包卸载后平台默认 CORE 仍工作、重载后已有 id 去重；旧格式边界；平台字符串展示时间戳与混合 JSON 键兼容 |
| 既有行为 | V2、原历史分页 / 恢复 / 文件写入 / 空消息过滤、异步 I/O、流拆分、payload、路径安全、子 Agent 路由及原生流结束回归 |

实际命令（仓库根，设置 PYTHONPATH 为本 Swarm 与对应 Core 源码目录）：

```powershell
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2 `
  tests/unit_tests/test_session_history_cursor.py tests/unit_tests/test_session_history_jsonl_read.py `
  tests/unit_tests/test_session_history_atomic_write.py tests/unit_tests/test_session_history_paths.py `
  tests/unit_tests/test_session_history_empty_filter.py tests/unit_tests/test_compact_history_records.py `
  tests/unit_tests/server/test_history_service.py tests/unit_tests/server/test_history_todo_restore.py `
  tests/unit_tests/agentserver/test_history_io_offload.py tests/unit_tests/agentserver/test_history_append_record.py `
  tests/unit_tests/agentserver/test_history_get_stream_split.py tests/unit_tests/agentserver/test_history_tool_result_restore.py `
  tests/unit_tests/agentserver/test_history_payload_limits.py tests/unit_tests/agentserver/test_session_history_path_safety.py `
  tests/unit_tests/agentserver/test_subagent_history_routing.py tests/unit_tests/agentserver/test_split_history_record_for_stream.py `
  tests/unit_tests/agentserver/test_deep_adapter_stream_end_final.py --asyncio-mode=auto -o log_cli=false
```

### 5.2 真实 Front → Supervisor → Worker 验证

启动隔离数据目录的真实服务，安装实际示例 `session_history_audit` 和独立验收包两个 ZIP，创建普通 Git 仓库与 linked worktree 中的两个真实项目 / 会话，通过现有 AgentServer WebSocket 发起两次 Qwen3.8-Flash 回合。未替换模型、Ops、Adapter 或手工构造 SessionContext。

- 两个会话均得到实际模型回复和有效用量；USER Handler 在真实 Worker（PID 32612，Front PID 3336）执行。cwd / root / model / repo 四项旧能力同时读取并核对通过。
- 两项新 Ops 完成改写后追加、`persisted` 实际回执、相同 id 的 `duplicate`、不同原内容的 `APPEND_ID_CONFLICT`、显式拒绝无记录、旧快照不含后追加、新快照包含后追加。
- 两个 `session.append` USER Handler 的改写均进入实际持久化正文；现有 `history.get` 读回正文，不暴露内部去重标记。仓库内示例包的 `visible=True` 日志已核对。
- 在发模型请求前，原有 `history.append_record` RPC 的改写、重试去重、拒绝及历史读回均通过；实际回合内也观测到 `source=history` 的可等待原生写入。三种 source 都进入 Worker Bus。
- 两个扩展包卸载成功；Worker 已退出，监听端口 65095 已释放。

本地证据目录为 `D:\work_space\.codex-temp\server-history-ops-20261027`：`regression.xml`、`validation-summary.json`；真实执行目录 `real-104004` 中有 `result.json`、`cleanup.json`、用户 Handler 回执和 WebSocket 历史记录。这些包含运行数据的文件不提交到仓库。

第一次真实运行发现平台展示时间戳可为字符串，而新增校验误用了展示字段；已改为校验平台原始时间事实，补上回归用例后重新完成上述全量定向和真实服务验证。

本次未覆盖 Gateway / 浏览器展示、远端 CI、非 Windows 实机、断电恢复和大历史性能压测。历史可读回不代表已主动推送 UI。

## 6. 限制

只提供主会话产品历史，不发送聊天请求、不驱动 Agent，不实现 Team / 子 Agent 扩展。游标沿用现有 JSONL 模式与旧 JSON 自动迁移；显式启用旧 JSON 写入模式时，分页查询沿用原服务的不支持错误。历史重写 / 截断可能令游标失效，调用方重新开始查询。完整记录内的幂等标记可供进程重启后去重，但不承诺对断电或已有损坏文件自动修复。
