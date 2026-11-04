---
id: F_20261103_0652_client-tool-adapter-p1
title: 客户端工具调用适配层 P1：detached 客户端工具
type: F
author: chickeyton
modules:
  - jiuwenswarm/runtime
  - jiuwenswarm/server
  - jiuwenswarm/agents/harness/common/rails
  - jiuwenswarm/agents/harness/common/tools
  - gateway/channel_manager/web
files:
  - jiuwenswarm/runtime/client_tool_adapter.py
  - jiuwenswarm/runtime/service.py
  - jiuwenswarm/agents/harness/common/tools/detached_client_tool.py
  - jiuwenswarm/agents/harness/common/rails/client_tool_guard_rail.py
  - jiuwenswarm/agents/harness/common/prompt/priority_registry.py
  - jiuwenswarm/common/schema/message.py
  - jiuwenswarm/server/handlers/client_tools.py
  - jiuwenswarm/server/handlers/chat.py
  - jiuwenswarm/server/dispatch.py
  - jiuwenswarm/server/agent_ws_server.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_code.py
  - jiuwenswarm/server/runtime/session/session_archive.py
  - jiuwenswarm/resources/config.yaml
  - gateway/common/cancel_sources.py
  - gateway/message_handler/message_handler.py
  - gateway/channel_manager/web/app_web_handlers.py
  - gateway/channel_manager/web/web_connect.py
  - gateway/channel_manager/tui/tui_connect.py
  - tests/unit_tests/runtime/test_client_tool_adapter.py
  - tests/unit_tests/agentserver/test_detached_client_tools.py
  - tests/unit_tests/agentserver/test_client_tool_result_delivery.py
  - tests/unit_tests/server/test_client_tool_handlers.py
  - tests/unit_tests/gateway/test_message_handler_stream_cancel.py
  - tests/unit_tests/agentserver/permissions/test_deep_required_composition.py
designs: [D-20261103-0410-client-tool-adapter]
author_email: ngton2014@gmail.com
---

# 客户端工具调用适配层 P1：detached 客户端工具

## 实施范围

实现 D-20261103-0410-client-tool-adapter §8 的 P1：统一协议、调用账本、detached 模式、超时/取消/断开、防重入 rail、结果投递。现有 blocking（中断）模式未改动，留给 P2。

## 实现要点

- **账本与状态机** `jiuwenswarm/runtime/client_tool_adapter.py`：`ClientToolAdapter` 管理各会话的工具声明和调用。调用只能从 `pending` 迁出一次，先到者胜：`completed`、`error`、`timeout`、`cancelled`、`client_gone`。迁移过程中没有 await，在单事件循环下天然原子。
  - 状态保存在 `<sessions>/<sid>/client_tools.json`，经 `get_agent_sessions_dir()` 取路径，原子写入。
  - 重启后重新挂上截止时间，已过期的调用直接判为 `timeout`。
- **detached 工具** `DetachedClientTool`：Agent 看到的名字是 `client__<name>`。它是普通服务端工具，不使用 `execution="client"`，所以 core 不会挂起 turn。调用时登记账本、推送 `chat.client_tool_dispatch`，并立即返回“已受理”。
  - call_id 取自 tool_call.id，通过 `accepts_tool_callback_context` 获取。
  - card 显式声明 `ToolExposure.DIRECT`（同 `process_cli/host_tools.py` 的 `HostCallbackTool`）。不声明时，渐进式工具发现会把它放到 `tool_search` 后面，模型要先搜索才能调用。
- **防重入** `ClientToolGuardRail`：priority 110，先于权限队列（100）执行。agent 和 code 两套 rail 列表都挂载了它，TUI 会话走的是 code 模式。
  - 与仍在运行的调用，或已失败（timeout/cancelled/client_gone）的调用同指纹时，用 `_skip_tool_calls` 跳过；拒绝文案不含任何客户端内容。
  - `on_user_message` 收到非结果信封的输入时解除失败调用的封锁。rail 看到的是 `UserTurn.render` 渲染后的文本（引导语加 JSON 包装），`is_result_envelope` 先取出其中的 `content` 再判断。
  - `before_model_call` 注入 `client_tools` 提示段（新增 `SystemPromptPriority.CLIENT_TOOLS = 79`）。
- **协议**：
  - 新增方法 `client_tool.register / result / cancel / list_pending`，分别登记在 WS 判断链和 `dispatch.HANDLERS`（HTTP）中。
  - 新增事件 `chat.client_tool_dispatch`、`chat.client_tool_settled`、`chat.client_tool_delivery`。以下位置已同步加入：
    - Web 的两份转发白名单；
    - TUI 的两份转发白名单（`CLI_FORWARD_*`）；
    - Web 全量事件集合。
  - 鉴权：`session_id` 先按 `gateway/common/protocol_ids.py` 校验（存储层再兜底一次），再按跨会话消息的归属规则校验请求者；会话不存在或不属于请求者都返回 `CLIENT_TOOL_SESSION_NOT_FOUND`。
- **结果投递**（Q4）：AgentServer `_deliver_client_tool_result` 以 `chat.send` + `input_mode=follow_up` 投递结果信封。
  - 会话有执行在跑时，由该执行在本轮结束后接收。
  - 会话空闲时开新 turn，事件经 `send_push` 推给客户端。
  - 会话有待答交互（ask_user 等）时先等待，避免 follow-up 取代挂起。
  - 结果轮可靠的关联方式是 `request_id = client-tool-result-<call_id>`。`client_tool_result` 标记也会带上，但 Gateway 精简 `chat.delta`、`chat.final` 时会把它丢掉。
  - 投递请求带 `source=client_tool_result`，输入因此是 internal 来源。客户端的输出不会被权限 root intent 当作用户亲自写的内容，也不算用户发起的 turn。
- **失败不投递**（R4）：只有 `completed`/`error` 生成信封；`error` 作为工具错误投递（Q2）；客户端上报 `timeout/cancelled/client_gone` 按终结失败处理。
- **取消与断开**：
  - `chat.cancel` 的 intent=cancel（用户停止，前端的“暂停”也映射到 cancel）会取消会话内所有挂起调用；supplement 不影响挂起调用。
  - Gateway 在每次新 `chat.send` 前，会对仍在流式的同会话发 intent=cancel 的 interrupt（`_cancel_stream_tasks_for_channel`）。这个 interrupt 带取消来源 `new_message`（`gateway/common/cancel_sources.py`），挂起调用不受影响：用户还在对话里。
  - Gateway 因客户端断开发出的 cancel（来源 `client_disconnect`）会启动宽限计时，到期判为 `client_gone`；宽限期内 `register` 或 `list_pending` 视为客户端已回来。只有 TUI 通道会发这个信号，而且 Gateway 先等 60 秒才发，见偏差 6。
  - 会话归档或删除时取消挂起调用并删除状态文件（`SessionArchiveService.session`）。
- **投递结果告知**：`chat.client_tool_settled.accepted` 只表示宿主接受了结果；投递完成或失败后再发 `chat.client_tool_delivery{delivered}`。以下情况算投递失败：会话模式已不支持客户端工具、待答问题超过 `delivery_wait_s` 仍未回答、runtime 抛异常。调用记录里的 `delivery` 字段（pending/delivered/failed）会持久化；重启时仍为 pending 的调用改记为 failed，并告知客户端。
- 配置项 `client_tools.default_timeout_s`（120）、`disconnect_grace_s`（30）、`delivery_wait_s`（600）已写入 `resources/config.yaml`；值非法时回退到默认值并记警告，不会让服务启动失败。

## 与设计的偏差

1. **结果投递不走 `SessionMessageService`**：该邮箱要求跨会话（拒绝自投递），且会把输入包装成“来自其他会话的消息”。改用 runtime 已有的 `follow_up` 输入，同样满足“排到当前 turn 之后”。代价是投递为至多一次：服务在结果到达与投递之间重启时，结果会丢失；重启后客户端会收到 `delivered: false`。
5. **detached 请求事件改名**（检视后修订）：不再复用 `chat.client_tool_request`，改为 `chat.client_tool_dispatch`，以免已有的 blocking 客户端误按中断方式答复。
2. **结果中的图片暂不传给模型**：信封只标注“N image(s) not shown”，需要接入 `media_items` 后补齐。
3. **Python SDK 推迟到 P2**：现有 `sdks/` 是一次性子进程 SDK，不经过 WS，没有承载客户端工具事件的通道，需要先确定 SDK 形态。
4. **客户端身份**：AgentServer 只看到 Gateway 的单条 WS，客户端身份完全依赖请求里的 `client_id`；断开信号没有 client_id，会作用于会话内所有挂起调用。
6. **Web 通道没有断开信号**（E2E 发现）：Web 客户端断开时 Gateway 不通知 AgentServer（只有 TUI 会通知），所以 Web 上的挂起调用要等到各自超时才结束。结束后仍然不重试、不投递。TUI 从断开到判为 `client_gone` 约需 65 秒：Gateway 先等 60 秒，再加 `disconnect_grace_s`。

## 验证

- 新增测试 64 个，全部通过（含检视后补的路径穿越、会话归属、投递告知、配置容错用例）：
  - `tests/unit_tests/runtime/test_client_tool_adapter.py`：42 个
  - `tests/unit_tests/agentserver/test_detached_client_tools.py`：8 个
  - `tests/unit_tests/server/test_client_tool_handlers.py`：9 个
  - `tests/unit_tests/agentserver/test_client_tool_result_delivery.py`：5 个
- 回归范围：凡引用了本次改动的 prompt 优先级、dispatch、chat.cancel、Gateway 白名单、归档、ReqMethod 注册、client_tool、会话工具注册、rail 构建等代码的 unit 测试文件，共 1958 个用例（Windows）。
  - 首轮有 9 个失败。其中 8 个是本次引入的：用 `__new__` 构造的 adapter 缺少新属性，`_update_session_tools` 报 `AttributeError`；已改为 `getattr` 容错，复跑通过。
  - 剩余 1 个 `test_auto_harness_channel_context.py::test_runtime_config_syncs_channel_and_task_workspace` 是 Windows 路径分隔符断言（`\` 与 `/`）。该用例打桩了 `_update_session_tools`，不经过本次代码，与本次改动无关。
- 测试环境副作用：`~/.jiuwenswarm` 不存在时，配置回落到仓库内的 `resources/config.yaml`；个别用例会在真实家目录写入 `config/.file_download_secret`、在 `resources/` 写入 `.env` 和 `*.lock`。之后再导入时，会因 `~/.jiuwenswarm/config/config.yaml` 缺失而失败。这是既有的测试隔离问题，本次未修。
- **端到端测试**（2026-11-03，测试服务器，Linux）：
  - 链路：真实的 Gateway、AgentServer 和 runtime worker，模型换成脚本化的 OpenAI 兼容服务，由 WS 脚本扮演客户端。
  - 用例：
    - s1 正常调用；
    - s2 等待期间继续对话，重复调用被拒；
    - s3 超时；
    - s4 取消与用户停止；
    - s5 断开与重连。
  - s3、s4 中，模型被设计成会自己重试未完成的调用，用来验证 rail 能挡住对超时、取消调用的自动重试。
  - 结果：
    - Web：s1–s4 共 43/43 通过；s5 未通过，原因见偏差 6。
    - TUI：s1–s4 共 43/43 通过；s5 共 10/10 通过。
  - E2E 首轮发现并已修复的问题（每项都补了回归单测）：
    1. 客户端工具被放在 `tool_search` 后面，改为声明 DIRECT。
    2. Gateway 在新消息顶替旧 turn 时发的 interrupt 会取消挂起调用，改为用 `new_message` 来源区分。
    3. rail 把以 host JSON 包装送进来的结果信封当成用户输入，解除了重试封锁；`is_result_envelope` 改为先解包。
    4. 投递的结果被当作 `external_user_authored`，权限 root intent 会把客户端输出当成用户本人意图；投递时加上 `source`，改为 internal 来源。
    5. TUI 不转发 `client_tool.*`，已补白名单。
    6. code 模式（TUI 会话）没有挂载 guard rail，已补上。
- **完整 unit 测试集**（`tests/unit_tests`，Linux）：
  - 结果：20749 通过，29 失败，123 跳过。
  - 处理 29 个失败的方法：在改动版本和干净的基线 worktree（`105bad92`）上各重跑一遍。
    - 26 个在基线上同样失败，或只在基线上失败。
    - 余下 3 个（2 个诊断、1 个归档恢复）再在两边各跑 6 轮，基线上也都失败过，属于 CPFS 共享盘导致的既有不稳定用例。
  - P1 首次检视时的回归集按关键字筛选，漏掉了 `test_deep_required_composition.py`：它固定了 agent 模式的 rail 顺序。本次已在这个测试里加上 `_client_tool_guard_rail`。
- 未做真实前端联调：Web 前端尚未接入 `client_tool.*`（P2）。

## 遗留

- P2：blocking 模式补齐（宿主超时终结、不可重试文案、bind 补发）、`client_tool.result` 统一 blocking 入口、TS SDK 与 Web 前端、Python SDK 形态。
- 结果中的图片投递；投递的持久化与重启后重投。
- Web 通道断开时通知 AgentServer（偏差 6）。可以选择：Web 也按 TUI 的方式延迟发 disconnect cancel，或者只为挂起的客户端工具调用单独发一个信号。
- 如果客户端需要用 `client_tool_result` 标记来认出结果轮，要么让 Gateway 精简 `chat.delta`、`chat.final` 时保留它，要么在协议里写明以 `request_id` 为准。
- 新增 S 文档，记录 `client_tool.*` 协议和状态机不变量（等前端接入、协议稳定后再写）。
