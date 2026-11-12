---
id: S_02_app-role-npc-team
title: app_role 的团队 NPC 与轻量咨询契约
type: S
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/runtime
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/server/runtime/session
  - jiuwenswarm/server/runtime
  - jiuwenswarm/agents/harness/common
  - jiuwenswarm/agents/harness/team
  - jiuwenswarm/agents/swarm
  - gateway/channel_manager/web
files:
  - gateway/channel_manager/web/web_connect.py
  - jiuwenswarm/runtime/app_role.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py
  - jiuwenswarm/runtime/session_provisioner.py
  - jiuwenswarm/server/runtime/session/session_metadata.py
  - jiuwenswarm/agents/harness/common/session_ops_service.py
  - jiuwenswarm/server/runtime/agent_manager.py
  - jiuwenswarm/agents/harness/team/team_manager.py
  - jiuwenswarm/agents/swarm/assembly.py
  - jiuwenswarm/server/runtime/agent_adapter/team_helpers.py
---

# app_role 的团队 NPC 与轻量咨询契约

`session.create` 在 `execution_profile:"app_role"` 上支持创建时固定的 `npc_team`（主从成员名册）。缺省时行为与既有轻量角色一致：`model.stream` 收到 `tools=None`，无工具轮。

## 能力边界

不支持宿主工具注入。创建或聊天请求中的非空 `tools`（包括 `web_search`）明确拒绝，创建校验在 claim 前以 `BAD_REQUEST` 返回；不读取搜索配置、不构造搜索工具。已有 metadata 的 `role_tools` 不参与模型声明或执行，fork 不复制该字段。装备类参数（skills/mcp/plugin_names/agent_template_name/agent_group_name/moa_enabled）维持拒绝。

## 原生团队 NPC

`execution_profile:"app_role"`、`mode:"team"` 和合法名册共同启用原生 Team 模式。创建结果回显按项目工作模式确定的 `team.work.normal` 或 `team.code.normal`，之后固定。成员名保留 `leader` 和框架保留标识。创建仅保存 Team 会话配置，首次发消息时由 framework bootstrap 装配固定成员，使用 scheduled dispatch 和进程内原生成员流。成员使用各自的 Team 会话上下文及 Team 内独立工作区，装配只保留 Team 协作能力，关闭项目工具、技能与团队记忆。

应用只提交普通玩家文本到 Team 会话。宿主将内容作为 leader 的普通文本输入，玩家文本中的成员寻址和斜杠命令不参与路由。leader 按角色分配发言任务，自己的最终文本独立发出。每个已发言的角色产生独立 `chat.final`，带 `member_name`、`display_name` 和完整 `content`。应用只展示这些发言，按 `turn_request_id` 关联回合，在 `chat.processing_status` 的 `is_processing:false` 时读取 `npc_missing_members` 检查角色是否齐全。

原生 Team 会话沿用 Team 删除生命周期。分叉保留角色定义，分叉后的 Team 独立装配成员。原生 Team 不写独立轻量会话的 `npc_member_sessions` 绑定。

## 旧版轻量名册与咨询

`mode:"agent"` 携名册时走兼容的轻量咨询路径。leader 只声明固定名册中的 `ask_<name>` 咨询工具，普通角色和成员的模型请求使用 `tools=None`。收到未声明的宿主工具调用时明确报错。

`npc_team.members` 为 1–5 个成员，每项含 `name`（匹配 `^[a-z][a-z0-9_]{1,31}$`、不重复）、`role_instructions`（1–12000 字符）及可选 `title`/`desc`（desc 缺省取 role_instructions 前 200 字符）。名册创建后固定，运行时绑定存于宿主内部字段 `npc_member_sessions`，不由请求参数表达。

每个成员由 provisioner 在 leader 创建事务内级联创建一个标准 app_role 会话，继承同 `project_id`/`work_mode`/`channel_id`/`user_id`，`persist_session:false`，metadata 携带 `npc_member:{leader_session_id, name}`。初始化失败清理已写目录。成员不进入预热池；leader 的 create_token 幂等身份包含名册定义。

`AppRoleMemberExecutor` 校验成员 profile 与所属 leader/name；适配器选择锁内强制刷新元数据、复核绑定并通过 lifecycle guard 后，才将自包含问题交给成员自己的 `AppRoleAdapter`，并通过 `append_history_record` 将 user 问题和 `chat.final` 答案写入成员历史。成员无工具、不递归咨询；leader 工具轮上限 5。工具中间消息只在当轮内存，不写 leader 持久化历史。leader 流产生 `npc.member` started/finished 事件，含成员名、会话 ID、回答摘要与用量。Gateway 完整透传这些结构化字段，并补齐主请求的 request_id。

## 旧版轻量会话生命周期与 fork

删除 leader 成功后 best-effort 级联删除成员，失败不回滚 leader 删除。fork leader 复制名册定义并创建全新成员会话（空历史）；fork 单个成员剥离 `npc_member` 成为独立轻量 NPC。`npc_team`/`npc_member`/`npc_member_sessions` 为 metadata 首绑定字段，陈旧快照不得覆盖。

不进入产品 team 体系：不写 `TEAM_CANONICAL_MODES`、不物化 `.agent_teams/`、不出现在团队看板。成员在同项目会话列表展示，可查看咨询历史并单独对话；人工消息进入后续咨询上下文。

成员单独删除后咨询返回明确错误且不重建；leader 可依据错误结果继续回答。
