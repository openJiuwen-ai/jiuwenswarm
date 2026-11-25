---
id: B_20261124_1704_skills-uninstall-missing-name-400
title: 企业版 Skill 卸载缺 name/null 返回 HTTP 500 改为 400
type: B
author: meizi6
author_email: meizi1@huawei.com
modules:
  - agent-gateway/extensions/runtime_management_extension
  - jiuwenswarm/extensions/runtime_management_extension
  - agent-gateway/channel_manager/web
files:
  - agent-gateway/agent_gateway/extensions/runtime_management_extension/pod_http_client.py
  - agent-gateway/agent_gateway/channel_manager/web/web_http_routes.py
  - jiuwenswarm/extensions/runtime_management_extension/pod_http_client.py
  - tests/unit_tests/gateway/test_pod_rest_map.py
  - agent-gateway/tests/unit_tests/extensions/test_rest_assembly_bad_request.py
designs: []
design_note: 无独立 D；依据现有北向 POST /skills/actions/uninstall 与南向 DELETE /skills/{name} 契约，将路径占位符缺失归类为客户端入参错误。
---

# 企业版 Skill 卸载缺 name 返回 500 修复记录

## 现象与影响

企业验证对 `POST /api/v1/skills/actions/uninstall` 发送 `{}` 或 `{"name": null, ...}` 时，Gateway 返回 HTTP 500 / `INTERNAL_SERVER_ERROR`，日志含 `REST 路径缺占位符 ['name']: /skills/{name}`。预期为明确参数校验失败（HTTP 400），安装态不变。

关联问题：`ZYQ5333/jiuwenswarm#167`（企业级 18y）。

## 关联设计与当前契约

无独立设计文档。北向 mapped 路由接受 body.name；南向 `pod_rest_map` 将 `skills.uninstall` 映射为 `DELETE /skills/{name}`。`_fill_path` 在 name 缺失/空/`null` 时抛 `RestAssemblyError`。

## 根因与修复决策

- **因果**：`RestAssemblyError` 仅在路径占位符缺失时抛出；`HttpSseAgentServerClient` 只特殊消化了子类 `HttpMethodNotHandled`，其余组装异常冒泡到 Web `_unary` 通配 `except` → 500。
- **决策**：在南向 HTTP 客户端将 `RestAssemblyError` 转为 `ok=False` + `code=BAD_REQUEST`（Web `_envelope_from_res` 已映射 400）；并在 `_unary` 兜底同样映射，避免漏网冒泡。
- **备选**：改南向为 `POST /skills/actions/uninstall`（body 传 name）可避开占位符，但会改南北协议表，范围更大，本期不做。

## 回归证据与适用范围

- 候选：本地 `agent-gateway` + `jiuwenswarm` 工作区改动。
- 自动化（2026-11-24）：
  - `jiuwenswarm`：`test_pod_rest_map.py` 相关 4 项 passed（`--override-ini=addopts=`）。
  - `agent-gateway`：`test_rest_assembly_bad_request.py` 5 passed（经 jiuwenswarm venv + PYTHONPATH）。
- 未验证：真实企业 CCE 环境对 `/api/v1/skills/actions/uninstall` 的端到端复测。

## 防错模式与资料同步

路径占位符缺失属于入参错误，应在组装边界转业务错误码，不要依赖最外层 500 通配。同类 `RestAssemblyError` 均受益。

## 遗留与后续

`HTTP_METHOD_NOT_HANDLED` 仍不在 `_envelope_from_res` 状态表中（默认 500）；与本次无关，未改。
