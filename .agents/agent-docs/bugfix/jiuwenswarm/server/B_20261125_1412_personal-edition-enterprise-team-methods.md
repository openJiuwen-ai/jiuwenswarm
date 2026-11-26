---
id: B_20261125_1412_personal-edition-enterprise-team-methods
title: 个人版收到企业 team 控制面方法时落入聊天分支：服务端入口补 edition 闸门
type: B
author: mingo007
author_email: maoqiming1@huawei.com
modules:
  - jiuwenswarm/server
files:
  - jiuwenswarm/server/edition_gate.py
  - jiuwenswarm/server/front/protocol.py
  - jiuwenswarm/server/agent_ws_server.py
  - tests/unit_tests/agentserver/test_front_method_validation.py
  - tests/unit_tests/common/test_local_req_method.py
designs: []
design_note: 依据代码逐跳核实定位，无独立设计文档；修复只在服务端入口补一次 edition 判定，不改企业 handler 与协议枚举。
---

# 个人版收到企业 team 控制面方法时落入聊天分支

## 现象与影响

企业 team 控制面共 7 个方法：`team.create`、`team.get`、`team.stop`、
`team.member.join`、`team.member.leave`、`team.members.list`、`team.quota.get`。
它们在两个版本的 `ReqMethod` 枚举里都存在，但只有企业版登记 handler。

个人版进程从 WebSocket 收到这些方法时，不会得到拒绝应答，而是被当成一次聊天
请求执行。调用方拿到的是模型回复或运行时错误，看不出“本版本不提供该方法”。

这个问题此前被网关掩盖：网关维护了一份企业方法清单，在个人版部署下自己拦掉。
一旦网关改为对未识别方法不透明转发，拒绝责任就必须落在服务端。

## 关联设计与当前契约

无独立设计文档。依据的现有契约是 `front/protocol.py::unsupported_method_wire`：
显式给出但不在枚举内的方法，在入口回 `METHOD_NOT_FOUND`。本次把“在枚举内、
但本版本不提供”补成同一层的第二种拒绝。

## 根因与修复决策

**根因**：`is_enterprise()` 只在登记期生效，请求期没有任何判定。

1. `server/dispatch.py::_register_enterprise_team_handlers` 在个人版直接返回，
   7 个方法不进 `HANDLERS`。`enterprise/team/handlers.py` 内部没有 edition 校验。
2. HTTP 入口是安全的：`agent_http_routes.py::_add_declared_route` 在
   `_handler_for()` 返回 `None` 时不注册路由，个人版没有对应的 HTTP 入口。
3. WS 入口不安全：`AgentWebSocketServer.dispatch_parsed_request` 的分支链里，
   只有 `ORG_EXPERT_TEAMS_ADD` 一处调用 `try_dispatch` 查 `HANDLERS`。这 7 个方法
   没有任何分支接住，最终落到末尾的 `_handle_stream` / `_handle_unary`，
   即 `runtime.invoke(request)`。
4. 入口的 `unsupported_method_wire` 只检查“是否在枚举内”，这 7 个方法在枚举内，
   所以放行。

**修复**：

1. 新增 `server/edition_gate.py`，集中声明 `ENTERPRISE_ONLY_METHODS`，并提供
   `enterprise_only_denial(method)`：个人版返回拒绝文案，企业版或其它方法返回
   `None`。方法名取自 `ReqMethod` 成员，不手写字符串。
2. `front/protocol.py` 新增 `rejected_method_wire`，先判未知方法，再判 edition。
   个人版企业方法回 `{"code": "FORBIDDEN", "error": "method not available in
   personal edition: <method>"}`。`parse_raw_message` 改用它。
3. `agent_ws_server.py::_handle_message` 的 E2A 直传与 legacy fallback 两处改用
   `rejected_method_wire`。
4. `dispatch_parsed_request` 开头再判一次。Front 准入、supervisor、worker 以及
   程序内合成请求都直接调用它，不经过 `parse_raw_message`。

**取舍**：

- 没有在 7 个 handler 内逐个加 `is_enterprise()`。个人版根本不登记它们，判定加在
  那里永远执行不到。
- 没有改成“未登记即拒绝”的通用兜底。`dispatch_parsed_request` 的分支链与
  `HANDLERS` 是两套并存的分发表，很多合法方法只在分支链里，通用兜底会误伤。
- 本地 team 方法（`team.list`、`team.delete`、`team.snapshot` 等）两个版本共有，
  不在清单内。

## 回归证据与适用范围

`tests/unit_tests/agentserver/test_front_method_validation.py`：
**30 passed**。

- `test_personal_edition_blocks_enterprise_method_before_dispatch` 直接调用
  `AgentWebSocketServer.dispatch_parsed_request`，确认个人版 `team.create`
  在入口返回 `FORBIDDEN`。
- 其余用例覆盖 `parse_raw_message` / `rejected_method_wire` 路径。

`tests/unit_tests/common/test_local_req_method.py` 与上面的文件一起运行：
**623 passed**。

- `test_all_local_methods_roundtrip_through_front_and_worker` 对枚举全部成员
  调 `parse_raw_message` 并断言得到 `AgentRequest`。闸门加上后，7 个企业方法
  在个人版下得到的是 `FORBIDDEN` 应答，这 7 条在 CI 上失败。现在这 7 个方法
  在企业版（`JIUWENSWARM_EDITION=enterprise`）下做往返断言，个人版的拒绝由
  `test_personal_edition_rejects_enterprise_team_methods` 覆盖。闸门行为没有改。
- 改用例之前，同一条命令的结果是 616 passed、7 failed，失败的正是这 7 条。

运行环境与仓库的标准环境有出入，结果应以 CI 为准：

- Windows，Python 3.14.0，没有用 `uv sync` 建环境。
- `openjiuwen` 和 `openjiuwen-e2a` 取 `pyproject.toml` 锁定的提交
  （`31171fb`、`10ad4a2`），以源码目录经 `PYTHONPATH` 导入，没有安装。
- 第三方依赖只装了导入链用到的部分，从 PyPI 取最新版。`tiktoken`
  （0.14.0，锁定 0.12.0）和 `pdfplumber`（0.11.11，锁定 0.11.10）与
  `uv.lock` 不一致。
- 跳过了 `conftest.py` 和 `pytest.ini`（缺部分依赖和 `--cov` 插件参数）。

这份记录的上一版写的是“29 passed，1 failed”：当时本机导入不了
`openjiuwen`，上面那条用例没能运行。改用锁定的 agent-core 提交后可以
运行并通过。

已执行：

- 4 个改动文件 `py_compile` 通过。
- 经 `PYTHONPATH` 真实导入 `parse_raw_message` 运行，结果如下：
  - 个人版：7 个企业方法全部返回 `FORBIDDEN`，应答保留 `request_id`。
  - 个人版：`team.list`、`team.delete`、`team.snapshot`、`chat.send` 正常解析。
  - 未知方法仍返回 `METHOD_NOT_FOUND`；未带方法的内部请求仍放行。
  - 企业版（`JIUWENSWARM_EDITION=enterprise`）：`team.create` 正常解析。

未验证：

- `_handle_message` 的两处改动（E2A 直传、legacy fallback）没有用例直接覆盖，
  也没有运行过。
- 未做端到端联调（真实 WS 连接、supervisor/worker 多进程）。

重验条件：在装好依赖的环境执行
`pytest tests/unit_tests/agentserver/test_front_method_validation.py`。

## 防错模式与资料同步

在枚举内声明、但只在部分版本登记 handler 的方法，必须同时加入
`edition_gate.ENTERPRISE_ONLY_METHODS`。测试
`test_enterprise_only_methods_are_declared_in_the_enum` 保证清单不脱离枚举。

## 遗留与后续

- 企业版下这 7 个方法走 WS 时同样没有分支接住（见根因第 3 条），目前依赖网关把
  它们映射到 HTTP 路由。这是既有行为，本次未改。
- 企业专家 HTTP 方法（`_install_enterprise_expert_handlers`）也是企业版才登记，
  本次没有核对它们在个人版 WS 入口的表现。
- 网关侧三份重复的企业方法清单在本修复合入后可以删除，由 agent-gateway 单独处理。
