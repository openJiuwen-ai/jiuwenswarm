# S_09 外层集成面规约（outer-integration）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_09 / outer-integration |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/`（外层接缝：`server/runtime/agent_adapter/interface_deep.py`、`agents/harness/common/rails/*`、`server/agent_ws_server.py`、`common/e2a/gateway_normalize.py`；双仓部署边界见附录） |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无（首批 specs） |

## 范围 / 边界

### 本规约管什么

skill_turbo 模块与外层体系之间的**全部接缝**：

- **装配路径**：`interface_deep.py` 的启用开关、初始化链、`build_skill_turbo_config` 全字段、3 个外层 rail 的挂载与 priority、请求期绑定；`interface_deep.py` 之外的 16 处外层集成点。
- **ContextVar 桥**：`context_binding.py` 的 7 个通道统一 bind/reset、条件语义、新增通道规范。
- **外层 3 rail 契约**：SkillTurboPromptRail / SkillTurboDeliverySummaryRail / SkillTurboPermissionRail 的挂载条件、时序与内容约束。
- **事件桥接与 WS 边界**：AgentWebSocketServer（E2A 边界）、`send_push` 唯一推送路径与送达数语义、`gateway_normalize` 转换规则、心跳与 backpressure 现状。
- **路径与工作区规范**：`workspace_base` 唯一来源、`effective_project_dir` 与 `workspace_base` 两套语义、`get_workspace_base_path` 回退链。
- **adapter 侧 HITL 接缝点清单**：resume 入口、发卡、清理在 `interface_deep.py` 的落点（机制归 [[S_06]]，本 spec 只列位置）。
- **双仓边界与部署顺序**：技能侧仓源码 → `JIUWENSWARM_SHARED_SKILLS_DIRS` → 运行时发现注册的到达链；三仓部署顺序。
- **测试资产地图**：jiuwenclaw 侧两组测试目录 + 技能侧仓注册门禁测试。

### 本规约不管什么

| 子主题 | 归属 spec |
|---|---|
| 执行管线内部（规划 / 执行 / 回调 / 缓冲） | [[S_01]] |
| `skill_acceleration_exec` 工具入口契约与 PromptRail 指南全文 | [[S_02]] |
| 审批模型与权限语义（PermissionRail 行为细节） | [[S_03]] |
| plan_code / PlanNode 节点契约 | [[S_04]] |
| 动态包发现 / 注册 / 门面机制 | [[S_05]] |
| HITL 中断-恢复状态机内部（tcid / 断点持久化 / 重放） | [[S_06]] |
| fallback 体系与 `spawn_fallback` 协议 | [[S_07]] |
| 事件类型全集与 tool 层转发协议 | [[S_08]] |

## 不变量

任意时刻，skill_turbo 与外层的接缝必须满足：

1. **N1 七通道统一收口**：ContextVar 桥的全部 **7 个通道**（adapter / request_metadata / workspace / interactive_ask / subagent_parent / resume_answers / outer_todo）只经 `context_binding.bind_skill_turbo_context` 绑定、`reset_skill_turbo_context` 复位；禁止任何调用点散落逐通道 set/reset。
2. **N2 请求期绑定必做**：每次进入请求处理必须执行 `set_current_request_metadata` + `set_skill_turbo_request_metadata`（`interface_deep.py` 请求期绑定段）。漏绑后果可判定：executor 建会话落到随机 session_id，HITL 断点存错位置，作答无法恢复。
3. **N3 绑定条件语义与复位隔离**：bind 仅对非缺失值（None / 非 bool 视为缺失）产生 token；token 挂在 ctx 的 task-local 属性 `_jiuwenswarm_skill_turbo_bind_tokens`，不进共享 extra dict；reset 幂等（无 token 跳过）且单通道复位失败不影响其余通道。
4. **N4 推送唯一路径 + 送达数语义**：AgentServer 侧的带外推送只走 `AgentWebSocketServer.send_push`（经 `PushRegistry` 扇出 Gateway WS 连接与 `GET /api/v1/events/stream` HTTP 订阅者）；返回值为送达订阅者数，**0 = 未投递**（无订阅者 / 编码失败 / 过滤后无人匹配 / sink 全部失败），调用方必须按 0 判失败，不得把 warning 当成功。
5. **N5 workspace_base 单一 owner**：`inputs["workspace_base"]` 仅当 `request_metadata["output_dir"]` 非空时设置，除此之外**不存在任何兜底来源**。输出路径解析的 owner 是 pipeline 层（skill_code 内 workspace_init 节点），工具层不得猜值兜底。
6. **N6 两套路径语义不混用**：`effective_project_dir`（ContextVar，工具运行 cwd / 产物检测基线）与 `workspace_base`（pipeline 输出目录）是两个字段两种语义；`executor.get_workspace_base_path` 的回退链（effective_project_dir → `resolve_tenant_agent_workspace_dir()` → None）只服务产物检测基线，不得当作 pipeline 输出目录消费。
7. **N7 清单双同源**：`skill_acceleration_exec` 的 ToolCard.description 与 SkillTurboPromptRail 指南技能清单必须由**同一次** `refresh_skill_acceleration_description()` 扫描产出；两处清单不一致即为装配缺陷（矛盾指令会驱使 LLM 空跑）。
8. **N8 空清单不注入**：扫描结果为空或 `acceleration_disabled=True`（`react.disabled_tools` 含该工具）时，PromptRail 整段不注入（`remove_section`）；外部源瞬时扫描失败沿用 `_last_good_entries` 进程级兜底，**绝不闪断为"暂无"**。
9. **N9 rail priority 时序**：外层 rail priority 固定 PromptRail=8（before_model_call 注入）< StreamEventRail=80（tool_result 发射）< DeliverySummaryRail=90（交付总结）；交付总结必须晚于 tool_result 到达前端，任何 priority 调整都要保持该偏序。
10. **N10 permissions 深拷贝**：`build_skill_turbo_config` 注入的 permissions 必须深拷贝；turbo 会话对权限结构的写入不得回流污染外层 DeepAgent 审批状态。
11. **N11 共享 rail 实例约束**：fallback 子代理与父 agent **共享同一权限 rail 实例**（PermissionInterruptRail / SkillAuthorizationPermissionRail / SkillTurboPermissionRail 三类）；suspend 必须 finally restore，且 `host.get_permissions_snapshot` 回调同步临时替换（`resolve_interrupt` 首次调用会用 snapshot 刷新覆盖）。
12. **N12 暂停帧语义**：HITL 暂停收尾只发 `chat.invocation_paused`（awaiting_user_input=True, is_complete=True）终结帧；`gateway_normalize` 必须将其转为 `is_final=False` 的 in_progress 帧（前端流保持开启等待作答）；暂停中禁止合成 chat.final。
13. **N13 部署顺序**：三仓部署顺序固定 ① agent-core → ② 技能源（技能侧仓，外部 turbo 技能源）→ ③ jiuwenswarm，回滚逆序 ③ → ② → ①。③ 先于 ② = 新引擎进程扫不到外部 turbo_codes，技能加速不可用；① 未推送 = jiuwenswarm 构建拉取 agent-core 失败。
14. **N14 双仓自包含**：技能 turbo code 内不得出现 `jiuwenswarm.*` import；引擎能力只能经 `skill_turbo_runtime` 门面获取（validator 白名单 + 技能侧仓注册门禁测试双保险）。
15. **N15 心跳保活**：流式响应空闲超过 10s（`_STREAM_HEARTBEAT_INTERVAL_SECONDS`）必须发 keepalive chunk（`event_type=keepalive`、sequence=-1、agent_ref 回带），防长流水线期间 websockets ping_timeout 断连；keepalive 与主循环共用同一 ctx.sink 发送锁，两条写路径必须互斥。

## 接口契约

### 4.1 装配面（`interface_deep.py`，10 个装配点）

启用开关：`config.yaml` 的 `react.skill_turbo.enabled`。所有装配点均以该开关为门禁；`react.disabled_tools` 含 `skill_acceleration_exec` 时 rail 仍建但 `acceleration_disabled=True` 不注入指南。

| # | 装配点 | 位置（`agent_adapter/interface_deep.py`） | 内容 |
|---|---|---|---|
| 1 | 初始化触发 | 11439 | `asyncio.to_thread(self._init_skill_turbo_tool)`（初始化不阻塞事件循环） |
| 2 | 工具注册 + adapter 注入 | `_init_skill_turbo_tool`（13315） | `get_skill_turbo_tools()` 注册工具卡进 ability_manager；`set_skill_turbo_adapter(self)`；`set_checkpointer` 注入 StreamEventRail |
| 3 | rail 懒建注册 | 10204-10226 | `_skill_turbo_prompt_rail` + `_skill_turbo_delivery_summary_rail` 挂载 |
| 4 | rail 构建工厂 ×2 | `_build_skill_turbo_prompt_rail`（13359）/ `_build_skill_turbo_delivery_summary_rail`（13401） | enabled 门禁 + 异常安全返回 None（rail 缺失不阻塞 adapter） |
| 5 | config 构建 | `build_skill_turbo_config`（13432-13464） | 见下方字段表 |
| 6 | fallback 委托 | `_create_skill_turbo_fallback_handler`（13466）/ `spawn_fallback`（13477） | 详见 [[S_07]] |
| 7 | 请求期绑定 | 13001-13057 | `set_current_request_metadata` + `set_skill_turbo_request_metadata`（N2） |
| 8 | resume 入口 | 请求分发检测 19598-19601 → `_try_skill_turbo_resume`（14190） | 详见 [[S_06]] |
| 9 | 暂停收尾 | 21290-21301 | `chat.invocation_paused` 终结帧发射 |
| 10 | 清理 | `_clear_pending_skill_turbo_hitl`（3785） | cancel / supplement 两路五步清理（见 4.6） |

`build_skill_turbo_config` 全字段表：

| 字段 | 来源 | 约束 |
|---|---|---|
| `tool_cards` | 外层 `ability_manager` 全量 card 列表 | turbo 内 `use_tool` 可用工具面的一部分 |
| `model_client` | 外层 `self._model` | planner 路由 + 节点 LLM 共用 |
| `fallback_handler` | `DeepAgentFallbackHandler` | 节点级 fallback 委托（[[S_07]]） |
| `card` | 外层 card | session/checkpointer 必需；隔离键 `{card.id}__skill_turbo` |
| `llm_concurrency_limit` | 固定 20 | executor 实例级 Semaphore |
| vision / audio / video / image_gen 配置 | 外层配置 | tools_loader 各组可用性条件（[[S_01]]） |
| `sys_operation` | 外层 | filesystem / bash 组装载前提 |
| `permissions` | 企业模板 body + persist_target | **深拷贝**（N10） |
| `skill_codes_dir` | **不传** | 外部 turbo 发现走 `resolve_agent_registered_skill_dirs` 标准链路；不传则内置扫描禁用 |

`interface_deep.py` 之外的 16 处外层集成点：

| 文件 | 位置 | 用途 |
|---|---|---|
| `agent_adapter/interface_code.py` | 1422 | code agent 请求也执行 `set_skill_turbo_request_metadata` 绑定 |
| `agents/harness/common/rails/stream_event_rail.py` | 313-315 / 360-382 / 905-920 / 1182-1222 / 1241-1320 | context_binding 桥接、ContextVar 绑定与复位、HITL TIE 改写、请求级 cwd |
| `agents/harness/common/rails/task_execution_rail.py` | 93/128/166/241、870/992/1052、1342-1367 | 产物检测 / workspace 基线共享逻辑；`set_skill_turbo_request_metadata` |
| `agents/harness/common/rails/interrupt/interrupt_helpers.py` | 821-839 | `build_permission_rail` 按 enabled 换 `SkillTurboPermissionRail` 类 |
| `agents/harness/common/rails/llm_retry_notify_rail.py` | 30/43 | SkillTurbo PPT page gather 并发共享 rail 实例，防抢占 reset |
| `agents/harness/common/rails/skill_protocol_prompt_rail.py` | 8 | 文档引用：加速通道指南由 SkillTurboPromptRail 注入 |
| `agents/harness/code/rails/coding_artifact_post_process_rail.py` | 26/140/189 | 与 SkillTurboArtifactRail 共享产物后处理 |
| `agents/harness/common/tools/subagent_compat.py` | 50 | 从 SkillTurbo chunk 提取 reasoning 的兼容 |
| `server/runtime/session/session_history.py` | 179-181 | `chat.llm_usage`（SkillTurbo 原始事件）resume 时改写为 usage_metadata |
| `server/runtime/session/permission_interrupt_cancel.py` | 42 | 混合中断（ask-user / SkillTurbo / 未知 schema）处理 |
| `server/sandbox_config_rpc.py` | 14/94 | 沙箱关闭时清 Runner.resource_mgr 残留 sysop，防 `_resolve_sys_operation` 兜底命中 |
| `server/utils/stream_utils.py` | 63/392 | stream_source_id 提取；payload.content vs output 协议兼容 |
| `common/e2a/gateway_normalize.py` | 455-458 | E2A 网关归一化 stream_source_id |
| `agent_adapter/interface_flash.py` | 212 | 行为约束：skill 安装不落盘则 skill_turbo PPT 直接失败 |
| `agent_adapter/interface.py` | 3583 | 行为约束：SkillTurbo write_stream 任务列表推流确认 |
| `common/utils.py` | 2379 / 2474 / 616 | `resolve_agent_registered_skill_dirs` / `resolve_tenant_agent_workspace_dir` / `get_user_workspace_dir` |

### 4.2 ContextVar 桥（`context_binding.py`，7 个通道）

`bind_skill_turbo_context(ctx, *, adapter, request_metadata, parent_session, resume_answers, outer_todo_active)` 由 `stream_event_rail.before_tool_call` 调用，统一绑定 7 个通道：

| # | 通道 | setter | 来源 | 消费方 |
|---|---|---|---|---|
| 1 | adapter | `set_current_skill_turbo_adapter` | interface_deep before_tool_call 绑定 | skill_turbo 工具体内取 DeepAdapter |
| 2 | request_metadata | `set_current_request_metadata` | interface_deep 请求期绑定 | 工具体 inputs 组装（session_id/request_id/channel_id/user_id/output_dir/language） |
| 3 | workspace | `set_effective_request_workspace_dir`（经 `extract_effective_project_dir`，复用权威实现单一词源） | request_metadata | executor `_setup_execution_context` 设 cwd、`get_workspace_base_path` 产物检测基线 |
| 4 | interactive_ask | `set_interactive_ask` | request_metadata | SkillTurboAskUserRail 非引导模式跳过 preview 卡（[[S_06]]） |
| 5 | subagent_parent | 解包 `._parent` 绑 DeepAgent 主会话本体 | parent_session | 子代理上下文中回主会话路由 |
| 6 | resume_answers | `set_skill_turbo_resume_answers` | interface_deep resume 路径 / StreamEventRail bind | 工具体 resume 入口 A（[[S_06]]） |
| 7 | outer_todo | `set_skill_turbo_outer_todo_active`（仅 bool） | interface_deep | 工具体内部 task 事件抑制判定 |

- **条件语义**：值缺失（None / 非 bool）不绑、不产生 token（N3）。
- **token 存放**：ctx 的 task-local 属性 `_jiuwenswarm_skill_turbo_bind_tokens`，随 ctx 生命周期，不进共享 extra dict。
- **复位**：`reset_skill_turbo_context(ctx)` 在 `after_tool_call` / `after_invoke` / `on_model_exception` 三处触发，循环 pop token 经 `_RESETTERS` 分发；幂等 + 单通道失败不影响其余。
- **新增通道规范**：`bind_skill_turbo_context` 加一段绑定 + `_RESETTERS` 加一行复位，两处必须同一改动落地。
- **为什么统一收口**：复位有三个触发点且含异常路径，散落的逐通道 reset 在任一异常路径都会产生遗漏窗口（绑定残留 → 串请求）；统一入口 + token 列表使绑定/复位天然对称。
- 全部延迟 import（函数内 import），避免模块级循环依赖。

### 4.3 外层 3 rail 契约

**SkillTurboPromptRail**（priority=8，`rails/skill_prompt_rail.py`，指南全文归 [[S_02]]）

- 六条规则要点：① 默认优先——意图匹配时第一个工具调用必须是 `skill_acceleration_exec`（唯一例外是用户显式要求的、加速通道不会自行处理的前置操作；搜索/读文件不算例外）；② 已误触 `skill_tool` 加载技能正文后仍须立即调用加速通道；③ 加速成功后禁止用 skill_tool 重复同一任务，失败/未处理必须转 skill_tool 标准流程；④ **暂停不是失败**——返回"技能加速已暂停"/弹补充问题卡片/含 `__skill_turbo_hitl__` 标记时必须停止后续工具调用等待用户回答，禁止改走标准流程从头重跑；⑤ 选区/编辑已有 PPT 排除（region_edit 关键词，只支持从零生成）；⑥ tool_result 提示"检测到上一轮留下的已完成产物"时不调用。
- **清单动态化双同源**：`before_model_call` 每次模型调用前调 `refresh_skill_acceleration_description()`（`skill_turbo_tools.py:181-213`）——同一次扫描同时重建 ToolCard.description 与指南技能清单（N7）。
- **缓存与兜底**：60s TTL `_entries_cache`（key=根目录集合）+ `_last_good_entries` 进程级兜底（N8）。
- **注入方式**：`system_prompt_builder.add_section(PromptSection(name="skill_turbo_guide", priority=PromptPriority.SKILL_PROTOCOL))`；uninit / disabled / 空清单时 `remove_section`。

**SkillTurboDeliverySummaryRail**（priority=90，`rails/delivery_summary_rail.py`）

- **时序**：在 `skill_acceleration_exec` 的 after_tool_call 中、晚于 StreamEventRail(priority=80) 发出的 tool_result 之后，以 `write_stream(llm_output)` 流式发出 PPT 交付总结（N9）。
- **通道**：消费 `_pending_ppt_delivery_summary` ContextVar（tool 层 `_wrap_skill_turbo_result` 成功分支挂载）；`take_pending_ppt_delivery_summary()` 取走并清空；骨架文本由 PPT 流水线 P10 节点写入 artifact_holder，通道内确定性产出、不调 LLM。
- **标记校验**：`startswith(DELIVERY_SUMMARY_START)`（标记常量经动态包 `skill_turbo_codes_ppt.ppt.delivery_summary` 引用；ppt 技能不在场时返回空串短路，不误发）。
- **防御**：HITL/审批中断（tool_result 或 exception 含 TIE）→ `clear_pending_ppt_delivery_summary()` 不发；session None → 清 pending 跳过；emit 异常仅 debug 日志。

**SkillTurboPermissionRail**（`rails/permission_rail.py`，行为归 [[S_03]]）

- 装配：`interrupt_helpers.build_permission_rail` 在 enabled=true 时**替换外层 DeepAgent 权限 rail 的类**（`SkillAuthorizationPermissionRail` 子类）。
- 仅覆盖 `_build_message`：`tool_name == "skill_acceleration_exec"` 时返回统一审批卡（`SKILL_TURBO_APPROVAL_DESCRIPTION` 一次性授权语义 + `SKILL_TURBO_APPROVAL_TOOLS` 11 项工具清单：bash/read_file/write_file/list_dir/glob/web_search/fetch_webpage/image_ocr/visual_question_answering/generate_image/send_file_to_user）；文案必须含"需要授权"供 Relay 权限桥识别。
- 维护约定：清单仅展示用、无行为耦合，但须覆盖 turbo 节点实际可用工具类别（含网络组）；新增 turbo 工具类别须同步此表（回归测试 `test_skill_turbo_approval_tools.py`）。

### 4.4 事件桥接与 WS 边界

- **AgentWebSocketServer**（`server/agent_ws_server.py:324`，`ws://127.0.0.1:{agentPort}`，默认 ping_interval=30.0 / ping_timeout=300.0）：引擎进程与上层应用之间的 E2A WebSocket 边界。
- **send_push**（`agent_ws_server.py:1532`）：经 `PushRegistry` 扇出给 Gateway WS 连接与 HTTP SSE 订阅者——**推送唯一路径**；返回送达数，0=未投递（N4）。
- **gateway_normalize 转换规则**（`common/e2a/gateway_normalize.py`）：

| 入站帧 | E2A 归一化结果 |
|---|---|
| `chat.error` + is_complete | E2A final **FAILED**（response_kind=e2a_error） |
| `awaiting_user_input=true`（chat.invocation_paused） | `is_final=False` 的 in_progress 帧，`event_type=chat.invocation_paused`（客户端不得当任务完成） |
| 正常 complete | E2A_COMPLETE **SUCCEEDED** |

- **事件端到端流向**（事件类型与转发协议归 [[S_08]]）：executor chunk → 工具层过滤/映射/剥 id → `parent_session.write_stream` → 外层 DeepAgent 主循环 → AgentResponseChunk → AgentWebSocketServer E2A wire → Gateway（PushRegistry + normalize）→ 上层前端（气泡 / taskProgress 槽位 / taskStack segment）。`task.update` 另走 `send_push` 带外旁路直达（全量快照幂等，后到覆盖先到）。
- **心跳机制**（`server/handlers/_default.py`）：`_STREAM_HEARTBEAT_INTERVAL_SECONDS = 10.0`；`_heartbeat_loop`（737-781）空闲超时发 keepalive chunk（payload `{"event_type": "keepalive"}`、is_complete=False、sequence=-1、agent_ref 回带防跨 mode 错路由）；与主循环共用 ctx.sink 同一发送锁；Gateway 侧 message_handler 跳过 keepalive 不投递 IM 通道。
- **WS backpressure 现状记录**：websockets legacy `serve` 必须传 `max_queue=None`（`agent_ws_server.py:496`）——legacy 协议默认有界接收队列，被不读数据的对端填满后帧处理（含 ping/pong）阻塞，长流水线大量 delta 下可能触发 ping_timeout 断连。`ImportError` 回退的新 API `websockets.serve` 分支（498-508）**未传 max_queue**（现状记录；改 WS 层须两分支同步）。

### 4.5 路径与工作区规范

- **workspace_base 唯一来源**：`request_metadata["output_dir"]`（格式 `.../files/user_id/chat_id/output`）。`skill_turbo_tools.py:972-981` 仅当其非空时设置 `inputs["workspace_base"]`，否则不设置、无任何 agent workspace 兜底（N5）。pipeline（skill_code 的 workspace_init 节点）在其下建 `generate-{timestamp}` 时间戳子目录，防同 session 连续任务产物串扰。
- **路径解析唯一 owner 原则**：输出路径的解析优先级只能有一个 owner（pipeline 层）；工具层不得猜值兜底——兜底会把产物写到抢占性目录，路径跑偏。
- **两套语义区分**（N6）：`effective_project_dir`（ContextVar `get_effective_request_workspace_dir`）= 工具运行 cwd / 产物检测基线；`workspace_base`（inputs）= pipeline 输出目录。executor `_setup_execution_context` 据前者 `init_cwd/set_cwd`。
- **get_workspace_base_path 回退链**（`executor.py:1570-1584`，仅产物检测用）：`effective_project_dir` → `resolve_tenant_agent_workspace_dir()`（`<tenant_root>/agent/jiuuwenclaw_workspace`）→ None。
- `chat_id`（= routing.group_id）随 inputs 注入，供 pipeline 重建输出路径。

### 4.6 adapter 侧 HITL 接缝点清单（机制归 [[S_06]]，此处只列落点）

| 接缝点 | 位置（`interface_deep.py`） | 职责 |
|---|---|---|
| `_try_skill_turbo_resume` | 14190-14313（分发检测 19598-19601：`answers` 非空 + `source == "ask_user_interrupt"`） | 作答路由到 turbo resume：终态守卫 → 摘活卡 → load_resume_ctx → in_flight 去重 → 委托 resume stream |
| `_make_skill_turbo_resume_stream` | 14357-14562 | 新建 `SkillTurbo(config)` 执行 `resume_stream`；正常完成 / 二次中断 / NotHandled 三分支收尾 |
| `_emit_skill_turbo_hitl_chunks` | 14612-14692 | resume 内二次中断发卡四件套（tool_call(pending_approval) / tool_update / ask_user_question / invocation_paused），request_id 用 `_derive_outer_call_id` 剥 `#{n}` 后缀 |
| `_dedupe_ask_user_card` | 21339-21403 | 卡片去重 / 挂起表复用重发原卡 / `{id}#{n}` 序号 |
| `_register_hitl_card_instance` | 21405-21439 | 卡片活性登记（有界 1024/512）+ 死卡守卫数据源 |
| `_clear_pending_skill_turbo_hitl` | 3785-3871（cancel/supplement 两路调用：15716-15717 / 15818-15841） | 五步清理：校验 loop session → 弹悬挂 tool_call → 清 INTERRUPTION_KEY → 隔离键清 resume_ctx → 保留 node_artifacts 强制落盘 |
| `chat.invocation_paused` 终结帧 | 21290-21301 | 暂停收尾（awaiting_user_input=True, is_complete=True）；暂停中跳过合成 chat.final |
| `_rewrite_skill_turbo_usage_chunk` | 14032（配合 `session_history.py:179-181`） | resume 时 `chat.llm_usage` → usage_metadata 改写 |
| `_skill_turbo_answers_to_confirm_payload` | 14577-14609 | 作答 → 结构化 answers / ConfirmPayload 转换 |
| `_deep_agent_has_skill_turbo_interrupt` | 14167-14188 | 嵌套中断归属判定（deferred to DeepAgent） |

## 数据结构

### 请求期绑定字段（`request_metadata`，ContextVar `_current_request_metadata`）

request_id / channel_id / session_id / user_id / output_dir / language / routing.group_id（→ chat_id）。并发会话下这些字段**只能**走 ContextVar，禁止挂 adapter 实例属性（adapter 为长生命周期对象，实例属性会被并发请求覆盖——见边界 case B6）。

### tool 层模块级 ContextVar（`skill_turbo_tools.py`）

`_current_skill_turbo_adapter` / `_current_request_metadata` / `_skill_turbo_hitl_tic`（TIE 旧路径兜底，主信号是工具返回值 `__skill_turbo_hitl__` 标记）/ `_skill_turbo_resume_answers` / `_skill_turbo_outer_todo_active` / `_pending_ppt_delivery_summary`（DeliverySummaryRail 消费）。绑定/复位经 4.2 的统一桥（tool 层自有的在 stream_event_rail 复位点 token 复位 + clear）。

### adapter 实例内存态（不持久化，重启即失）

`_ask_user_card_seq`（同 base 卡片序号）、`_hitl_card_instances` / `_hitl_base_live_instance` / `_hitl_base_live_questions` / `_hitl_dead_card_ids`（卡片活性注册表，有界 1024/512）。重启后死卡守卫 fail-open，仅剩相位守卫兜底。

### tool_info.json 导出（`environment._export_tool_info`）

路径 = `config["tool_info_export_path"]` 或 `<get_user_workspace_dir()>/tool_info.json`；内容去重（类级 `_tool_info_last_export`）+ 原子写（tmp + `os.replace`）+ 失败仅 WARNING。消费方：CodeGen（skill-turbo-converter）read_file 查询工具 schema ground truth。

## 执行路径全景

### P1 启用与装配（进程初始化期）

`config.react.skill_turbo.enabled` 读取 → `interface_deep._init_skill_turbo_tool`（11439 `asyncio.to_thread` 触发）：注册 `skill_acceleration_exec` 工具卡 → `set_skill_turbo_adapter(self)` → StreamEventRail `set_checkpointer` → rail 懒建（10204-10226，两个工厂异常安全）→ `interrupt_helpers.build_permission_rail` 按 enabled 换 `SkillTurboPermissionRail` 类。

### P2 请求到达与请求期绑定

请求入站 → `interface_deep._update_runtime_config` → `set_current_request_metadata` + `set_skill_turbo_request_metadata`（StreamEventRail / TaskExecutionRail 重绑工具执行上下文）→ 外层 DeepAgent ReAct 主循环（PromptRail 已按清单状态注入/移除指南段）。

### P3 加速工具调用期

外层 LLM 决策调用 `skill_acceleration_exec` → `StreamEventRail.before_tool_call` → `bind_skill_turbo_context`（7 通道）→ 工具体 `skill_turbo(query)`：读 ContextVar 组装 inputs（`workspace_base` 按 N5 条件设置）→ `adapter.build_skill_turbo_config()` → `SkillTurbo(config)` **每请求新建实例** → `run_stream` / `resume_stream`。

### P4 事件回流主路（FIFO）

executor chunk → 工具层 skip/映射/剥 id（[[S_08]]）→ `parent_session.write_stream(OutputSchema)` → 外层 DeepAgent 主循环 → AgentResponseChunk → AgentWebSocketServer E2A wire → Gateway normalize → 上层前端。空闲 10s 由心跳循环补 keepalive（N15）。

### P5 带外推送旁路

`task.update` 全量快照 → `_push_task_event_out_of_band` → `AgentWebSocketServer.send_push`（PushRegistry 扇出）→ 返回送达数（0=未投递）。`task.start`/`task.complete` 留 FIFO 与 chat.* 同序，禁止带外。

### P6 HITL 发卡与暂停收口（adapter 侧）

AbortError 透传到工具层 → 返回 `__skill_turbo_hitl__` 标记 dict → `StreamEventRail.after_tool_call` 改写 TIE → harness `build_interrupt_state` → `__interaction__` → `convert_interactions_to_ask_user_question` 发卡（经 `_dedupe_ask_user_card`）→ `hitl_pending_stream` → 流末 `chat.invocation_paused` 终结帧 → gateway_normalize 转 `is_final=False` in_progress（N12）。

### P7 resume 恢复入口（adapter 侧）

作答入站（`source == "ask_user_interrupt"`）→ 请求分发处检测（19598-19601）→ `_try_skill_turbo_resume`（守卫/去重/断点加载）→ `_make_skill_turbo_resume_stream` → `SkillTurbo.resume_stream`（跳过 planner 全树重放，[[S_06]]）。

### P8 cancel / supplement 清理

两路触发（非 interaction 15716-15717 / interaction 15818-15841）→ **先终止执行、后清状态**（顺序反了会被退出路径的断点落盘覆盖）→ `_clear_pending_skill_turbo_hitl` 五步清理（见 4.6 表）。

### P9 双仓部署与技能到达链

技能侧仓源码（`<shared_skills_root>/{skill}/turbo/turbo_codes/{skill_name}/`）→ 按三仓顺序部署（N13）→ 部署侧注入 `JIUWENSWARM_SHARED_SKILLS_DIRS` env → `resolve_agent_registered_skill_dirs`（请求级绑定 > 共享目录 env > workspace agent skills 目录）→ `environment._scan_external_turbo_skills` 发现 → `ensure_turbo_package` 注册动态包（[[S_05]]）→ planner 可路由。部署验证：② 后确认 turbo_codes/ppt/ 含 meta.json + ppt_gen_root.py；③ 重启后日志出现 `[TurboPackageLoader] bound skill_turbo_codes_ppt`。

## 异常分支与边界 case

| # | 场景 | 现行为 / 约束 | 测试覆盖 |
|---|---|---|---|
| B1 | 外部技能目录扫描失败 | 60s TTL 缓存 + `_last_good_entries` 进程级兜底，沿用上次成功清单，绝不闪断"暂无"；ToolCard 并发最后写入生效（per-user sidecar 内目录一致，无实际冲突） | test_description_refresh（last-good） |
| B2 | adapter 长生命周期内存态 | HITL 卡片注册表有界 1024/512 防泄漏；`_ask_user_card_seq` 等不持久化，重启后死卡守卫 fail-open、序号回卷，仅剩相位守卫兜底 | 部分：test_hitl_superseded_invalidation（活卡/死卡守卫）；有界上限无 |
| B3 | tool_info.json 多实例写同一默认路径 | 类级去重 + 原子写；不同 agent 实例注册不同工具集时最后写者胜（CodeGen 可能读到别家 schema） | test_environment_export_tool_info |
| B4 | 对端心跳不识别 | Gateway 侧 message_handler 跳过 keepalive 不投递 IM；对端版本过旧时需确认其 keepalive 帧过滤行为，否则保活失效退化为依赖 ping/pong | 无（gateway 侧） |
| B5 | 跨请求 workspace 切换 | 每请求新建 SkillTurbo/Environment/Executor；`_setup_execution_context` 重置计数器与执行 ContextVar；ArtifactRail 工作区基线随实例重建，不跨请求残留 | test_context_binding_channels（通道复位契约） |
| B6 | 并发会话 metadata 覆盖 | request 级字段一律走 ContextVar；send_file_to_user 等携带 session 路由的工具按全局 tool_id 从 Runner.resource_mgr 现取实例，并发下"后注册者覆盖"是已知行为，必须经请求级 ContextVar 解析路由 | test_context_binding_channels（条件语义/复位隔离） |
| B7 | WS backpressure | legacy serve 必须 `max_queue=None`；有界队列被不读的对端填满后 ping/pong 阻塞 → ping_timeout 断连；新 API 回退分支未传 max_queue（现状记录，两分支须同步） | test_ws_keepalive_backpressure（agentserver） |
| B8 | skills 目录变化 rebind | 同名动态包不同目录 → `ensure_turbo_package` WARNING rebind + 清陈旧子模块；per-user sidecar 单进程安全；多租户共享进程下 rebind 会导致先注册租户 import 解析到后注册租户代码（[[S_05]]） | test_turbo_package_loader（rebind 用例） |
| B9 | 类级缓存无淘汰 | `_scan_cache` / `_external_scan_cache` / `_tool_info_last_export` 只增不减；skills_dir 变更后旧 key 永久驻留（长驻进程内存缓慢增长面） | 无 |
| B10 | 沙箱关闭残留 | `sandbox_config_rpc` 关沙箱时清 Runner.resource_mgr sysop 残留，防 `environment._resolve_sys_operation` 兜底命中已关闭实例 | 无 |
| B11 | parent_session 为 None | task 事件 drop 并告警（任务列表到不了前端）；resume 侧 session_id 缺失仅告警继续（checkpointer key 可能 miss） | 无专项 |
| B12 | rail 构建失败 | 两个 rail 工厂异常安全返回 None，不阻塞 adapter 初始化（缺指南注入/交付总结但加速仍可运行） | 无 |
| B13 | send_push 0 送达 | 无订阅者 / 编码失败 / 过滤无匹配 / sink 全败 → warning + return 0；调用方必须以返回值判定成败（N4） | 部分：test_skill_turbo_tools（OOB 旁路）；0 送达判定无 |
| B14 | ③ 先于 ② 部署 | 新引擎进程扫不到外部 turbo_codes → 清单为空 → PromptRail 不注入 + planner 无匹配 → `SkillTurboNotHandled` 降级（用户表现为"加速不生效"而非报错；只能靠部署顺序与验证日志预防） | 无（部署顺序预防） |
| B15 | gateway 帧误判 | `chat.error` + is_complete 会被 normalize 为 E2A final FAILED、前端清 activeRequestIdRef 终结本轮——这正是降级路径禁止发 chat.error 的边界（[[S_07]]/[[S_08]]），改事件桥时须逐帧推演 | 部分：test_exit_path_frames（E10/E11 禁发 chat.error）；gateway 帧归 gateway 侧 |

## 修改检查单

1. **改 adapter 集成**（interface_deep.py 任意 skill_turbo 装配点）→ 必查 7 通道 bind/reset 对称性：新增通道时 `bind_skill_turbo_context` 加一段 + `_RESETTERS` 加一行；三处复位触发点（after_tool_call / after_invoke / on_model_exception）全覆盖。
2. **改 WS server** → 必查：心跳（10s keepalive + sequence=-1 + agent_ref 回带 + 发送锁互斥）→ backpressure（legacy `max_queue=None` 与新 API 分支同步）→ `send_push` 返回值的所有消费方（0=失败）。
3. **改路径解析** → 必查唯一 owner 原则：`workspace_base` 只出自 `metadata["output_dir"]`；不得新增 agent workspace 兜底；`effective_project_dir` 与 `workspace_base` 语义不得互换。
4. **改 rail 挂载** → 必查 priority 时序（8 < 80 < 90）+ enabled 门禁 + `react.disabled_tools` 含工具时 rail 仍建但 `acceleration_disabled=True` 不注入。
5. **改 build_skill_turbo_config** → 必查 permissions 深拷贝 + 不传 `skill_codes_dir`（外部 turbo 走标准发现链路）+ tool_cards 全量语义。
6. **改事件桥 / gateway_normalize** → 必查三分支逐帧推演：chat.error+is_complete→FAILED / awaiting_user_input→in_progress(is_final=False) / complete→SUCCEEDED。
7. **改部署脚本 / 发布流程** → 必查三仓顺序 ①agent-core→②技能侧仓→③jiuwenswarm + 回滚逆序 + 部署验证（turbo_codes/ppt/ 含 meta.json+ppt_gen_root.py；重启日志 `[TurboPackageLoader] bound`）。
8. **新增请求级状态** → 必查 before_invoke/bind 时重置 + 异常路径复位不残留（复用 token 机制，不自建 reset）。
9. **改 ToolCard description / 技能清单逻辑** → 必查双同源（同一次 `refresh_skill_acceleration_description()` 产出两处）+ 空清单不注入 + `_last_good_entries` 兜底。
10. **改技能侧仓 turbo code** → 必跑注册门禁测试：`python -m unittest discover -s <shared_skills_root>/pptx-craft/turbo/tests -p 'test_turbo_registration_gate.py'`——`compileall` 只能发现语法错误，validator 安全策略拒绝（注册期整技能静默跳过）只能靠注册门禁发现。
11. **改 spawn_fallback / 权限 rail 交互** → 必查共享 rail 实例 suspend / finally restore + `get_permissions_snapshot` 回调临时替换（[[S_07]]）。
12. **改 HITL 收尾帧**（chat.invocation_paused）→ 必查 gateway_normalize `is_final=False` 分支 + 暂停中禁 chat.final（[[S_06]]）。
13. **改产物检测 / 水印管线** → 必查与 TaskExecutionRail 共享逻辑（detect_artifact_paths_safe / WorkspaceBaselineState / filter_unhooked / fire_artifact_hook 等）不产生第二份实现。
14. **改接口层请求分发** → 必查 `interface_code.py:1422` 的 metadata 绑定与 interface_deep 对齐（code agent 请求同样绑定 `set_skill_turbo_request_metadata`）。

## 测试资产地图

- `tests/unit_tests/agentserver/`：skill_turbo 源码迁至 `server/runtime/skill_turbo/` 后测试保留在此路径（迁移遗留布局；新测试应落 `server/runtime/skill_turbo/` 目录）。6 个 `test_skill_turbo_*` 文件：`test_skill_turbo_tools.py`（工具面）、`test_skill_turbo_resume_usage.py`（resume usage 改写）、`test_skill_turbo_prompt_disabled.py`（enabled/disabled 门禁）、`test_skill_turbo_interrupt_recovery.py`（中断/取消恢复）、`test_skill_turbo_hitl_tie_rewrite_repro.py`（TIE 重写）、`test_skill_turbo_hitl_resume_react.py`（HITL resume ReAct 全链）；另有 `test_spawn_fallback_suspend_and_interrupt.py`（fallback 权限挂起 + interrupt 报错）。
- `tests/unit_tests/server/runtime/skill_turbo/`：模块级测试 29 个 `test_*.py` + 1 个夹具 `_ppt_gate_fixture.py`（共 30 个 .py，pytest 收集 245 个用例），含 fallback 交付门禁（`test_fallback_deliverable_gate.py`，配上述夹具）、fallback 回写（`test_fallback_context_writeback.py`）、审批清单（`test_skill_turbo_approval_tools.py`）、PPT 交付总结（`test_ppt_delivery_summary.py`）、门面同一性（`test_runtime_facade.py`）、外部 turbo 端到端（`test_external_turbo_end_to_end.py`）等。
- 技能侧仓注册门禁：`<shared_skills_root>/pptx-craft/turbo/tests/test_turbo_registration_gate.py`——改 turbo code 后必跑（用运行时实际 `PlanCodeValidator.for_skill_code` 校验整个 turbo_codes/ppt/；compileall 不能发现安全策略拒绝）。

## 与其它 spec 的关系

| Spec | 关系 |
|---|---|
| [[S_01]] execution-pipeline | 本 spec 只划接缝（config 构建 / 请求绑定 / 事件出入）；规划与执行管线内部归 S_01 |
| [[S_02]] skill-routing-and-registration | 工具入口契约与 PromptRail 指南全文归 S_02；本 spec 管 rail 的挂载 / priority / 双同源 / 空清单约束 |
| [[S_03]] security-model | PermissionRail 审批模型归 S_03；本 spec 只记录装配点（`interrupt_helpers.build_permission_rail` 换类）与审批卡清单维护约定 |
| [[S_04]] node-contract | plan_code / PlanNode 契约归 S_04；本 spec 不重复 |
| [[S_05]] dynamic-package-and-facade | 动态包发现 / 注册 / 门面归 S_05；本 spec 引用其到达链（JIUWENSWARM_SHARED_SKILLS_DIRS → 发现 → 注册）与部署验证日志 |
| [[S_06]] hitl-interrupt-resume | resume / 发卡 / 清理完整状态机归 S_06；本 spec 只列 adapter 侧 10 个接缝点位置（4.6 表） |
| [[S_07]] fallback-and-degradation | spawn_fallback 与降级协议归 S_07；本 spec 管其与共享 rail 实例的 suspend/restore 约束（N11） |
| [[S_08]] event-stream-protocol | 事件类型全集与 tool 层转发协议归 S_08；本 spec 只管 WS 边界 / gateway_normalize / 心跳 / send_push |
| TURBO_CODE_SPEC.md（技能侧仓） | 技能侧 turbo code 规范（目录结构 / import 规则 / §11 部署顺序 / §9 增量同步规程）；本 spec 的部署顺序与门禁要求直接引用之，改动需两文档同步 |

## 附录：角色与当前部署形态对照（非正典契约）

本模块文档（AGENTS.md / docs/AGENTS.md / S_01–S_09）一律以**角色名**指代外部协作方，避免把通用引擎绑定到单一产品部署。下表仅描述当前主要部署形态，**不构成通用契约**；去品牌化或多产品复用时，以正文的角色定义与配置键（`JIUWENSWARM_SHARED_SKILLS_DIRS` / `config.channels[channel].send_file_allowed`）为准：

| 角色（正典，文档用语） | 当前部署形态（非正典） |
|---|---|
| 技能侧仓（skills sidecar，外部 turbo 技能源） | relay-claw 仓 `office-claw-skills/`（含 vendored 引擎副本） |
| 共享技能根目录 `<shared_skills_root>` | `office-claw-skills/`（由部署侧注入 `JIUWENSWARM_SHARED_SKILLS_DIRS` env） |
| 上层前端 / 上层应用（事件消费、作答路由、终稿拼接） | relay-claw / OfficeClaw 前端链路（FrameQueue / pptTurboSummary / computeFinalTextDelta 等行为锚点位于该链路） |
| 上层 sidecar（事件帧生命周期管理） | relay-claw sidecar（FrameQueue 提前关闭风险源自该层） |
| `channel == "officeclaw"` | `tools_loader.py` 历史兼容旁路（待删除）；正典装载条件为 `config.channels[channel].send_file_allowed`（[[S_02]]） |
