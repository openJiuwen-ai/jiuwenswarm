---
id: F_20261031_1139_gateway-session-e2a
title: Web 会话管理统一经 E2A 交由 Server 执行
type: F
author: ruiqiang2
modules:
  - gateway/channel_manager/web
files:
  - gateway/channel_manager/web/app_web_handlers.py
  - tests/unit_tests/gateway/test_app_web_handlers.py
designs: []
design_note: 用户确认的 Gateway 解耦任务 4.1，沿用 develop 个人版现有链路，无独立设计编号。
author_email: renruiqiang@huawei.com
---

# Web 会话管理统一经 E2A

## 实施与决策

基线为 develop `eabe143db`，实施分支为 `gateway-dep-4.1-session`。
本次执行已确认的 4.1 修正：企业版与个人版的 Web `session.list`、`session.rename`、`session.pin` 均使用现有 `proxy_unary_request` 转发，由目标 Server 访问会话数据。
删除企业版 legacy 共享目录且 Server 未就绪时的本地读写分支，以及这三个处理函数对 Server 内部实现的直接导入。不注入兼容模块。
Server 不可用时沿用 E2A 的 `SERVICE_UNAVAILABLE`，不再提供 Gateway 离线管理本地会话的能力；Server 端的列表投影、重命名和置顶实现不变。

此次修正替代前期“可选兼容模块保留本地管理”的建议。范围仅为上述三个 Web 入口，未扩大到其他渠道或其他业务的回退清理。
当前契约同步至 [中文说明](../../../docs/zh/项目与会话管理.md) 与 [英文说明](../../../docs/en/Project-Session-Management.md)。

## 验证

- 回归用例：覆盖个人版/企业版、legacy/remote/缺失客户端的不可用响应（18 个组合），以及两版三个方法的 E2A 请求身份、参数和 Server 成功响应（6 个组合），以及企业版 Server 业务错误和断连（6 个组合）；保留已有 session.list 转发测试，删除跳过占位和不再收集的旧回退测试体。
- 实际尝试：`/Users/richard/Documents/learn/ideal/.venv/bin/python -m pytest tests/unit_tests/gateway/test_app_web_handlers.py -k 'session_management or session_list_forwards' -o addopts='' -q`。加载 conftest 时因缺少 `portalocker` 失败，尚未运行这些 pytest 用例。当前 checkout 无 `.venv`。
- 补充验证：以 Python 标准库从当前源码提取三个 handler 及真实 proxy 函数执行；协议规范化与传输由替身提供。成功、legacy 离线、remote 离线、无客户端、Server 业务失败与断连共 18 个场景通过。该结果仅证明隔离后的处理及代理控制流，不代表真实协议、完整注册或持久化链路已通过。

## 补齐环境后的回归验证（2026-10-31）

- 已建立当前 checkout 独立 `.venv`（CPython 3.14.8），执行 `uv sync --frozen`，并补充 pyproject 已声明但 uv.lock 未收录的 gateway-protocol（6edf9e36）、minio、asyncpg；portalocker 使用锁定的 3.2.0。项目依赖声明与锁文件未修改。
- 命令：`JIUWENSWARM_DATA_DIR=/tmp/session41-test-data .venv/bin/python -m pytest tests/unit_tests/gateway/test_app_web_handlers.py -k 'session_management or session_list_forwards' -o addopts='' --asyncio-mode=auto -q`。
- 结果：31 passed, 262 deselected。真实 handler 注册及 E2A 代理代码运行，传输客户端使用测试替身；覆盖个人版/企业版转发、缺失或离线客户端、业务错误和断连，无本地会话读写回退。此前依赖缺失造成的测试阻塞已解除。

## 遗留与交接

尚未启动真实 Server 做端到端会话管理回归。本次交付包括代码、回归用例和中英文文档，未创建 PR；不将单元回归标为真实部署验收。
