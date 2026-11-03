---
id: F_20261103_1501_app-role-session
title: 保留基础系统提示的轻量 NPC 会话
type: F
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/runtime
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/server/runtime/session
  - jiuwenswarm/extensions/app_market
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin
files:
  - jiuwenswarm/runtime/app_role.py
  - jiuwenswarm/runtime/session_provisioner.py
  - jiuwenswarm/server/agent_ws_server.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/server/runtime/agent_adapter/interface.py
  - jiuwenswarm/server/runtime/agent_manager.py
  - jiuwenswarm/server/runtime/session/session_metadata.py
  - jiuwenswarm/agents/harness/common/session_ops_service.py
  - tests/unit_tests/agentserver/test_app_role_adapter.py
  - tests/unit_tests/agentserver/test_session_create_runtime_boundary.py
  - tests/unit_tests/runtime/test_session_create_provisioner.py
  - tests/unit_tests/agentserver/test_agent_warm_pool.py
designs:
  - D-20261103-1444-app-role-session
---

# 保留基础系统提示的轻量 NPC 会话

## 范围与决定

在 `codex/builtin-skill-catalog` 的 HEAD `079f09d91` 上创建 `codex/npc-minimal-prompts`。沿用本次此前未提交的项目归属修复，未提交或推送。

按用户要求保留基础系统提示。新增 `execution_profile: app_role` 和 `role_instructions`，与项目的 work_mode 分离。基础规则说明应用角色助手身份、玩家输入的数据边界、能力边界及格式遵循；应用人格接在其后。后续只发送当前业务输入，历史仅恢复自身完整 user/assistant 对话。

当前契约见 `S_01_app-session-project`，设计确认和阶段见关联 D。本记录承载实施证据，不将实施状态回填设计正文。

## 实施与取舍

- Runtime 校验并落盘执行配置，普通会话不新增持久化字段。只给轻量创建响应增加 profile 标识，保持普通响应字段集合。
- 角色绕过普通 prewarm；创建幂等签名包含角色规则，冲突拒绝。元数据写盘保护固定配置，fork 复制 profile、规则及 work_mode。
- Deep/Code 路由器按服务端 session metadata 选择独立 `AppRoleAdapter`，不依据聊天请求临时选择 profile。该 adapter 直接复用 core Model 接口及既有模型选择，不构建通用 DeepAgent。普通模型调用之外不创建工具和项目上下文 rails。
- Facade 对角色使用原始文本并关闭云记忆入口，避免 user envelope 或插件记忆额外注入。角色仍沿通用流式和历史持久化封装返回 delta/final/usage/error。
- 角色 adapter 每轮读取自己的历史，保留最近完整对话对，忽略失败或取消请求和非对话事件；输入按 48000 字符上限裁剪，不做额外摘要。该上限是应用级保护，不是精确模型 token 预算。
- 取消关闭模型流并传播取消；暂停/继续、工具、MCP、技能、插件、team、cron、永续记忆和显式增强执行被拒绝。热更新不能补挂普通 rails。

未采用“创建通用 Agent 后删 prompt”的方式，因为扩展和 reload 可能重新注入内容。选用专用 adapter，复用现有模型/流式/历史基础设施；没有改动 core 依赖。实现与设计一致，装配扩展点落实到直接 Model 调用。

## 本机应用接入

仓库外 `D:/project/temp/living-island/scripts/app_server.py` 创建 NPC 时发送 `app_role` 和角色系统规则；`_compose` 只返回场景/玩家 JSON，不再将角色规则写进首条 user 消息。来源项目仍通过来源会话查询。检查创建响应 profile，旧宿主不支持时拒绝降级并尝试清理误建会话，失败保留待清理记录。

本机应用改动不属于本仓 Git 差异。未重启运行中的宿主或应用，也未删除用户的已有 NPC；新代码需要部署并重启宿主后，通过应用正常的新会话流程启用。应用原有启动清理会删除旧 NPC，因此不能承诺保留旧会话进行在线迁移。

## 验证证据

Windows，项目 `.venv` Python 3.12.13，2026-11-03，工作区候选：

```text
python -m pytest tests/unit_tests/agentserver/test_app_role_adapter.py tests/unit_tests/runtime/test_session_create_provisioner.py tests/unit_tests/agentserver/test_session_create_runtime_boundary.py tests/unit_tests/agentserver/test_agent_warm_pool.py -q --no-cov
60 passed in 6.74s
```

覆盖最终 `model.stream(messages=..., tools=None)` 的可控模型边界，断言基础系统规则、人格、历史与当前输入存在；项目/记忆哨兵和工具定义不进入请求。检查同角色复用、设备/规则变更拒绝、意外工具调用拒绝、取消后模型流关闭、root 恢复选择轻量 adapter、fork/元数据固定配置、输入裁剪、创建参数穿透 Server → Runtime、非法配置在 claim 前拒绝、普通创建和预热行为回归。

随后补充旧快照缺少 profile/规则的真实写盘场景，定向重验 fork/元数据保护用例，1 passed；创建调用改用显式的 profile/规则参数后，定向重验普通创建与轻量创建，2 passed。其余行为未再变化，不重复全套测试。

曾有两次测试失败：取消用例引用不存在的 ReqMethod 常量（测试构造错误），以及普通创建响应增加了 profile 字段（真实兼容问题）。分别修正测试构造、让响应字段仅用于轻量会话后得到上述全通过结果，保留这一定位事实。

另用本机应用的真实 `IslandAI.ensure_session` 配合可控 Gateway 客户端，验证创建请求带来源项目、code 模式、app_role 和阿海人格；重复调用复用同一 session；聊天 JSON 不再包含人格，验证通过。未调用真实模型、未进行升级后的真实 WebSocket/UI 联调、未测供应商 token 降幅。未运行全量 UT、格式化或静态检查。

## 交接与限制

- 需要实际部署和重启宿主后再启用本机应用；现有进程不会热加载这些 Python 改动。
- 仍需真实 Gateway/UI 和供应商验证；当前模型边界证据来自可控模型，不将其说成线上真实请求。
- 角色没有工具，也不支持暂停/继续或在线变更人格。普通 code/work 会话保持原入口。
- 未提交、推送或创建 PR；等待后续检视和交付确认，设计保留 active。
