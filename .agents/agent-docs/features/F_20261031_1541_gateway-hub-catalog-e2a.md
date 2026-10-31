---
id: F_20261031_1541_gateway-hub-catalog-e2a
title: 企业管理 Hub 目录查询经 E2A 交由 Server 执行
type: F
author: ruiqiang2
modules:
  - gateway/enterprise
  - jiuwenswarm/server
  - jiuwenswarm/extensions/runtime_management_extension
files:
  - gateway/enterprise/config/manager_config/routers/application_config_routers.py
  - gateway/enterprise/plane.py
  - jiuwenswarm/server/handlers/hub_catalog.py
  - jiuwenswarm/server/dispatch.py
  - jiuwenswarm/server/http_invoke.py
  - jiuwenswarm/common/schema/message.py
  - jiuwenswarm/extensions/runtime_management_extension/runtime_routed_client.py
  - jiuwenswarm/extensions/runtime_management_extension/pod_rest_map.py
  - tests/unit_tests/gateway/test_hub_catalog_e2a.py
designs: []
design_note: 用户确认的 Gateway 解耦任务 4.5，采用现有无会话 E2A 管理通路，无独立设计编号。
author_email: renruiqiang@huawei.com
---

# Hub 管理目录经 E2A 查询

基线为 develop `60eb1355e`；分支 `gateway-dep-4.5`，保留已完成的 4.1 修改。
本次替代“Gateway 直接调用 Server 目录函数”的实现，未移动或复制 marketplace。

## 实际实现和边界

- 新方法 `hub.equipment.catalog` 对应 Server `GET /api/v1/hub/equipment/catalog`，在 HTTP 与 WS 处理表注册，Server 返回现有目录查询结果。
- Gateway 从装配的 WebChannel 获取当前客户端并发 E2A；企业 runtime 模式走既有默认 Server HTTP 地址，不调用 session route/touch。Server HTTP 调用跳过用户 Tenant/Agent Manager 初始化，因为它读取进程级 Hub 目录。
- Gateway 原有管理链路鉴权及固定隐藏包过滤保留，返回结构保持兼容。Server 不可用时返回空 items 和错误，不做本地兜底。
- 新增了 Server 可用性前提；Hub 地址和缓存现在由目标 Server 决定。缓存冷启动、异步刷新、分页和现有错误语义不变。
- 新方法沿用 common/schema/message.py 的现有临时注册机制；未修改外部协议仓库，协议 pin 包含该成员后需清理 shim。
- 用户侧专家包列表、白名单执行判断和 Hub 安装流程不变。

## 验证证据

CPython 3.14.8，当前 checkout `.venv`；补装项目声明的 runtime foundation `915eab39`，未修改依赖声明或锁文件。

1. `JIUWENSWARM_DATA_DIR=/tmp/session45-test-data .venv/bin/python -m pytest tests/unit_tests/gateway/test_hub_catalog_e2a.py tests/unit_tests/gateway/test_package_allowlist_catalog.py tests/unit_tests/server/runtime/test_equipment_hub_catalog.py -o addopts='' --asyncio-mode=auto -q`：11 passed。
2. 同一环境运行 `tests/unit_tests/gateway/test_pod_rest_map.py tests/unit_tests/server/test_http_server.py`：40 passed。

新增用例通过 ASGI 传输连接真实 Gateway 路由、E2A runtime 客户端、HTTP 映射、Server 路由和 handler；仅 Hub 查询用替身。覆盖成功、Hub 错误、缺失/离线/断连 Server、固定隐藏包过滤，以及无会话路由、无 Agent 执行依赖。

## 交付与遗留

已同步 docs/zh 与 docs/en 的 E2A 查询契约和默认 Server 部署配置。未进行真实 Hub 与跨机器部署验证；生产需提供可达且 Hub 配置正确的 Server。未创建 PR。
