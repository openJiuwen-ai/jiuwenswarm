---
id: B_20261125_1658_unhandled-known-method-no-chat-fallback
title: 枚举内未接 handler 的方法落入聊天分支：runtime 两个入口补通用兜底
type: B
author: mingo007
author_email: maoqiming1@huawei.com
modules:
  - jiuwenswarm/server
files:
  - jiuwenswarm/server/runtime/agent_adapter/method_guard.py
  - jiuwenswarm/server/runtime/agent_adapter/interface.py
  - tests/unit_tests/agentserver/test_chat_turn_method_guard.py
designs: []
design_note: 静态审计全部枚举成员的认领层，确认只有 4 个聊天入口方法允许落入 chat 执行器；兜底不维护 175 方法白名单，而是挂在两个入口的收口点上。
---

# 枚举内未接 handler 的方法落入聊天分支

## 现象与影响

`ReqMethod` 目前有 566 个成员，但没有任何一处集中声明"哪些方法有 handler"。
`_process_message` / `_process_message_stream` 用一串 `if` 加若干
`_handle_*_request` 分流，末尾没有命中分支的请求直接当成一次用户聊天执行。

后果是：枚举里声明了、专属路由忘了接的方法，调用方拿到的是模型回复或运行时
错误，而不是"方法不存在"。这既掩盖了漏接 handler，也会在网关改为不透明转发后
把新方法的错误暴露成莫名其妙的聊天内容。

## 关联设计与当前契约

入口层已有明确契约：显式给出但不在枚举内的方法回
`{"code": "METHOD_NOT_FOUND", "error": "unknown method: <method>"}`
（`front/protocol.py::unsupported_method_wire`）。本次把"在枚举内、但没有任何
一层认领"补成同一语义，只是判定点从协议解析挪到 runtime 的两个收口点。

## 根因与修复决策

**审计结论**（脚本静态遍历架构，不进仓库）：

- 566 个枚举成员中，391 个在 runtime 之前的 WS 分支链被认领。
- 175 个会到达 runtime 入口，其中 121 个由无状态 handler 接住：
  skills 70、packages 24（其中 8 个 agent_groups 方法由同一个 handler 按
  route 表分发）、skilldev 7、plugins 6、session assets 4、assets 2、
  其它显式分支 2、聊天入口 3。这里的"聊天入口 3"按非流式入口统计；
  `chat.user_answer` 在非流式入口归"其它显式分支"，在流式入口没有专属分支，
  同样靠聊天链路续跑，所以白名单是 4 个方法。
- 剩余 54 个没有任何一层认领。逐个 grep 后，除 7 个企业 team 方法外，
  其余在服务端没有 Python 调用点（`files.*`、`mode.*`、`channel.*`、
  `updater.*`、`heartbeat.*`、`commands.list`、`tts.synthesize`、`ssh.relay`、
  `harness.packages.*` 等），Web 前端仍可能发出但服务端没有对应 handler。
  `video.conversation.append`、`jiuwenmate.*` 一类由 HTTP 或扩展通道处理，
  不经过这条链路。

**修复**：

1. 新增 `method_guard.py`，只声明 4 个允许落入 chat 执行器的方法：
   `chat.send`、`chat.resume`、`chat.user_answer`、`command.goal`。
   `chat.user_answer` 必须在列：非流式入口有专属分支，流式入口没有，
   带 `Accept: text/event-stream` 的应答会走到流式聊天链路续跑，口径与
   `jiuwenswarm.runtime.request.CHAT_TURN_METHODS` 一致。`chat.cancel`、
   `chat.swarmflow_reply` 只在非流式入口有专属分支，流式下没有调用方，
   不在此列。比较一律取 wire value（`getattr(method, "value", method)`），
   为后续 `e2a.Message.req_method` 支持 `str` 做准备。
2. 在 `_process_message` 的 `_ensure_adapter` 之前、所有无状态 handler 之后
   插入判定，未命中返回 `METHOD_NOT_FOUND`。
3. 在 `_process_message_stream` 的无状态 handler 循环之后插入同样的判定，
   产出一个 `is_complete=True` 的错误 chunk。流式 chunk 没有 `ok` 字段，
   下游只按 `event_type` 识别终止错误，所以 payload 带
   `event_type: "chat.error"`（`method_not_found_stream_payload`），
   与同函数内其它终止错误 chunk 一致；非流式仍用入口层契约的两字段形状。
4. 顺带把流式入口漏掉的两个无状态 handler（session assets、assets）
   补进循环，与非流式入口保持一致。否则同一方法在两条通道上一条正常、
   一条被判为未实现。

**取舍**：

- 没有把 175 个"会到 runtime"的方法写成白名单。白名单会随枚举增长再次失配，
  等于把耦合从网关搬到 runtime。
- 没有在 WS 分支链尾部做通用判定。`dispatch_parsed_request` 的分支链与
  `dispatch.HANDLERS` 是两套并存的分发表，在那一层兜底会误伤只在分支链里
  处理的方法。
- 兜底默认拒绝而不是默认放行。漏接 handler 属于编程错误，明确报错优于静默
  执行成聊天。

## 回归证据与适用范围

- `test_chat_turn_method_guard.py`：23 passed。覆盖白名单、白名单包含
  runtime 的 `CHAT_TURN_METHODS`、两种 payload 形状（流式带
  `event_type: chat.error` 且属于 `TERMINAL_ERROR_EVENT_TYPES`）、
  回包保留 `request_id/channel_id/metadata`、AST 断言兜底排在全部无状态
  handler 之后且在其后第一处 `_ensure_adapter` 之前、两条入口查询同一组
  handler、用 `object.__new__(JiuWenSwarm)` 真实执行两个入口断言返回
  `METHOD_NOT_FOUND`，以及流式 `chat.user_answer` 越过兜底到达 chat 执行器。
- `test_chat_turn_method_guard.py` + `test_front_method_validation.py` +
  `common/test_local_req_method.py`：**633 passed**。
- `test_agentserver_modes.py` + `test_agentserver_boundary_characterization.py`：
  **124 passed, 2 failed**。两条失败均为本机缺 `openjiuwen_runtime`
  （`jiuwenswarm/enterprise/team/runtime_storage.py:16`），与本次改动无关。

运行环境与仓库的标准环境有出入，结果应以 CI 为准：

- Windows，Python 3.14.0，没有用 `uv sync` 建环境。
- `openjiuwen`、`openjiuwen-e2a` 取锁定的提交经 `PYTHONPATH` 导入，没有安装。
- `a2ui` 包在本机缺失且内网镜像不可用，用一个只提供导入符号的本地临时 stub
  让 `interface.py` 可导入。stub 不在仓库内，不影响 CI。
- `pytest` 调用跳过 `addopts`（含 `--cov`，本机缺插件）。

未验证：没有做端到端联调，也没有逐个方法构造真实 WS 请求。54 个无认领方法中
是否有客户端实际发送，靠静态引用判断，没有运行时采样。

重验条件：在装好依赖的环境执行
`pytest tests/unit_tests/agentserver/test_chat_turn_method_guard.py`。

## 防错模式与资料同步

- 新增方法时若只加枚举不加 handler，请求会得到 `METHOD_NOT_FOUND`，
  不会再变成一次聊天。
- 新增无状态 handler 时，两条入口都要登记；用例
  `test_stream_and_unary_consult_the_same_stateless_handlers` 会失败提醒。
- 若确有新的方法需要落入 chat 执行器，必须显式加入
  `CHAT_TURN_METHOD_VALUES`；用例
  `test_chat_turn_allowlist_is_exactly_the_chat_entries` 会锁定该清单。

## 遗留与后续

- 企业 team 方法在个人版由 `edition_gate` 在入口拒绝；企业版经 WS 仍没有
  handler，本次从"落聊天"变成明确 `METHOD_NOT_FOUND`。是否给它们接 WS 路由
  另议，当前依赖网关映射到 HTTP。
- 54 个死枚举方法没有清理。它们要么该删除，要么该补 handler，建议随枚举
  治理单独处理。
- 8 个 agent_groups 方法在 `_handle_package_catalog_request` 内按 route 表
  分发，两条入口共用，未受影响。
- 网关侧仍需改为不透明转发，并复用本块的判定语义，由 agent-gateway 单独处理。
