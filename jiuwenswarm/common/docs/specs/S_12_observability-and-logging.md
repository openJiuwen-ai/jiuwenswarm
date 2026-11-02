# 审计与日志（观测体系）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_12 / observability-and-logging |
| 关联模块 | jiuwenswarm/common/{audit_emit, audit_gate, audit_net, log_context, log_preview, openjiuwen_logging, debug_dump, openjiuwen_rail_compat, stage_timer}.py |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **审计打点链路与日志体系装配**：audit 三件（emit / gate / net）、请求级日志
会话绑定、用户内容预览脱敏、SDK 日志引导、USR1 活体快照、SDK 版本兼容、热路径分段
计时。日志的**全局装配**（setup_logger / 源头脱敏）在 utils.py，归 [[S_01]] 的 utils
职责表；OTEL / telemetry runtime 本身不在 common（在 telemetry 包）。

## 审计三件的数据流

```
业务代码 → audit_emit.emit_audit*
  → 两道闸：企业版 + audit_gate.is_audit_config_enabled（默认 False，fail-closed 起步）
  → 字段归一（UA/EVT/UID/session_id/request_id 提升为顶层入参，防泄漏进 attributes）
  → audit_net.enrich_extra_with_hop_ips（补 SRCIP/DSTIP）
  → telemetry.audit.audit_claw_log（优先）
  → 失败回退 openjiuwen_runtime.foundation.log_audit
  → 任何一层失败仅 warning（整体 fail-open，[[S_01]] 铁律 6）
```

## 不变量

- **N1 审计 fail-open**：`emit_audit*` 任何异常仅记 warning 不抛；个人版直接 no-op。
  打点弄坏请求路径是事故，丢一条审计是可接受的损失。
- **N2 闸门默认 False**：`audit_gate` 默认关闭（空库 / DELETE 后为关）；由 gateway 侧
  `apply_audit_log_config_payload` 在加载库审计配置时切换——与 OTEL / telemetry
  runtime 解耦的模块级布尔。
- **N3 敏感字段顶层化**：UA / EVT / UID / session_id / request_id 必须是
  `audit_claw_log` 的**顶层入参**，不得塞 attributes（下游按顶层字段建索引）；caller
  栈回溯跳过三组封装帧（`capture_audit_caller`）；EVT → success=False 的固定映射。
- **N4 网络字段采集 fail-open**：`get_local_ip` 优先 env `POD_IP`，否则 UDP connect
  8.8.8.8 探测默认路由；进程级缓存；一切失败返回占位 `-`；仅当打点已带 DSTIP 时自动
  补 SRCIP=本端。
- **N5 日志会话绑定利用 ContextVar 复制语义**：`bind_log_session` 后 create_task 拷贝
  上下文——父级后续 bind 不影响已运行子任务；空值 = 解绑；未设显示 `<nosid>`；bind
  时联动 openjiuwen trace id 并保留 previous 成对恢复。
- **N6 用户内容预览有界**：`preview_text` 默认 200 字符；开关优先级 env
  `JIUWENSWARM_LOG_PREVIEW_USER_CONTENT` > config `logging.preview_user_content`；关闭时
  仅输出 `<N chars omitted>`；超限截断 `...(+N chars)`；**配置读取失败 fail-open 返回
  True**。
- **N7 SDK 日志引导时序**：`bootstrap_openjiuwen_logging` 必须在 workspace ready 后、
  导入会触发 openjiuwen logger 的模块**之前**调用；跨版本兼容（有 `set_log_path` 用之，
  否则 snapshot + configure 降级）；无 logging.yaml 时全员 INFO；热更 apply 清除
  per-logger 覆盖使托管级别唯一生效。日志钉住 `<root>/agent/.logs/openjiuwen`，避免落
  cwd 相对 `./logs`。
- **N8 USR1 快照信号安全**：`install_async_dump_handler` 处理器只读、永不抛；用 gc 全堆
  扫描代替 `all_tasks()`（后者看不到他线程 loop 且需运行中 loop）；Windows 无 SIGUSR1
  时装 no-op（`dump_async_state` 仍可直调）；输出
  `logs/async_dump/<svc>_<pid>_<ts>.txt`。
- **N9 SDK 兼容按签名静默丢弃**：`filter_unsupported_kwargs` 用 `inspect.signature`
  过滤（目标已有 `**kwargs` 则不过滤）；对 4 个 Skill*Rail 类做幂等 `__init__` 包装
  （`_jiuwenswarm_kwargs_compat` 标记防重复）；`attach_output` 的 `steal` 参数按 SDK
  支持性丢弃。
- **N10 计时器单次使用**：`StageTimer` 每次 pass 新建、非线程安全；`monotonic` 绑定
  模块属性便于测试注入；`render(slowest_first=True)` 单行 `name=ms` 格式是日志解析
  契约。

## 接口契约

| 模块 | 关键符号 | 语义要点 |
|---|---|---|
| `audit_emit.py` | `emit_audit` / `emit_audit_ua` / `emit_audit_evt` / `capture_audit_caller` / `AuditTimer` | N1 / N3 |
| `audit_gate.py` | `is_audit_config_enabled` / `set_audit_config_enabled` | N2 |
| `audit_net.py` | `get_local_ip` / `resolve_peer_ip` / `enrich_extra_with_hop_ips` / `clear_local_ip_cache` | N4 |
| `log_context.py` | `log_session_id` / `bind_log_session` / `unbind_log_session` / `current_log_session_id` / `NO_SESSION_ID` | N5 |
| `log_preview.py` | `preview_text` / `DEFAULT_PREVIEW_MAX_CHARS = 200` | N6 |
| `openjiuwen_logging.py` | `bootstrap_openjiuwen_logging` / `apply_openjiuwen_log_level` | N7 |
| `debug_dump.py` | `dump_async_state` / `install_async_dump_handler` | N8 |
| `openjiuwen_rail_compat.py` | `filter_unsupported_kwargs` / `call_attach_output` / `install_evolution_rail_kwargs_compat` | N9 |
| `stage_timer.py` | `StageTimer`（mark / total_ms / render） | N10 |

## 执行路径 / 消费者

- audit_emit 消费（16 处）：`gateway/channel_manager/web`、`message_handler`、
  `routing/http_agent_client`、`interface_deep`、`sandbox_lifecycle`、agents rails。
- log_context：`utils`、`message_handler`、`server/pipeline`、`interface_deep`。
- log_preview：`server/pipeline`、`interface_deep`、`team_helpers`、`team_manager`。
- debug_dump：`app.py`、`app_gateway`、`app_agentserver`、`app_web`（各入口一次）。
- rail_compat：`app_agentserver`、`interface_deep`。
- stage_timer：`interface_deep`（热路径）。

## 修改检查单

- 改审计字段结构 → N3 顶层化清单核对 + telemetry 侧索引同步 + 16 处消费点 grep。
- 改预览开关 / 长度 → env > config > 默认 True 三序回归（含读取失败路径）。
- 改 SDK 引导时序 → 导入顺序矩阵（bootstrap 先于 openjiuwen logger 触发模块）回归。
- 升级 openjiuwen SDK → N9 签名过滤回归 + 4 个 Skill*Rail 包装点确认。
- 改 dump 输出路径 / 格式 → 采集侧（日志平台）解析同步。

## 与其它 spec 的关系

- [[S_01]]：utils 的全局日志装配（setup_logger / install_source_record_masking）是
  本体系的底座；debug_dump / openjiuwen_logging 的装配点登记。
- [[S_05]]：ws_diagnostics 的诊断输出同属"日志解析契约"精神，但归传输域。
