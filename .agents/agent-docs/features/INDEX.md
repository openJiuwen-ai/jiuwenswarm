---
title: 实施与决策 F索引
author: kdgmpw
author_email: lishuqi4@huawei.com
generated: true
---

# 实施与决策 F索引

由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。

共 4 篇文档，按模块分组；跨模块文档在各相关分组展示，总数按文档去重。

## 模块导航

| 模块 | 文档数 |
|---|---|
| [docs](#module-1) | 1 |
| [jiuwenswarm/agents/harness/common](#module-2) | 1 |
| [jiuwenswarm/agents/harness/team](#module-3) | 1 |
| [jiuwenswarm/agents/swarm](#module-4) | 1 |
| [jiuwenswarm/channels/desktop/electron](#module-5) | 1 |
| [jiuwenswarm/channels/web/frontend](#module-6) | 2 |
| [jiuwenswarm/common](#module-7) | 1 |
| [jiuwenswarm/extensions/app_market](#module-8) | 2 |
| [jiuwenswarm/resources/agent/workspace/skills](#module-9) | 1 |
| [jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin](#module-10) | 2 |
| [jiuwenswarm/runtime](#module-11) | 2 |
| [jiuwenswarm/server](#module-12) | 1 |
| [jiuwenswarm/server/runtime](#module-13) | 1 |
| [jiuwenswarm/server/runtime/agent_adapter](#module-14) | 3 |
| [jiuwenswarm/server/runtime/session](#module-15) | 2 |
| [jiuwenswarm/server/runtime/skill](#module-16) | 1 |

<a id="module-1"></a>

## docs

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261104_1225_npc-team-member-sessions](F_20261104_1225_npc-team-member-sessions.md) | F | NPC 主从会话与成员咨询 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/server/runtime, jiuwenswarm/agents/harness/common, jiuwenswarm/agents/harness/team, jiuwenswarm/agents/swarm, jiuwenswarm/server, docs, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | gateway/channel_manager/web/web_connect.py, tests/unit_tests/channel/test_web_channel_personal_context_payload.py, jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/agents/swarm/assembly.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py, tests/unit_tests/agentserver/test_app_role_npc_team.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_team_helpers.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/test_session_ops_fork.py, docs/zh/AppRoleSessions.md, docs/en/AppRoleSessions.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | [D-20261104-1141-npc-team-member-sessions](../designs/active/D-20261104-1141-npc-team-member-sessions/design.md) |

<a id="module-2"></a>

## jiuwenswarm/agents/harness/common

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261104_1225_npc-team-member-sessions](F_20261104_1225_npc-team-member-sessions.md) | F | NPC 主从会话与成员咨询 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/server/runtime, jiuwenswarm/agents/harness/common, jiuwenswarm/agents/harness/team, jiuwenswarm/agents/swarm, jiuwenswarm/server, docs, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | gateway/channel_manager/web/web_connect.py, tests/unit_tests/channel/test_web_channel_personal_context_payload.py, jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/agents/swarm/assembly.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py, tests/unit_tests/agentserver/test_app_role_npc_team.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_team_helpers.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/test_session_ops_fork.py, docs/zh/AppRoleSessions.md, docs/en/AppRoleSessions.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | [D-20261104-1141-npc-team-member-sessions](../designs/active/D-20261104-1141-npc-team-member-sessions/design.md) |

<a id="module-3"></a>

## jiuwenswarm/agents/harness/team

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261104_1225_npc-team-member-sessions](F_20261104_1225_npc-team-member-sessions.md) | F | NPC 主从会话与成员咨询 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/server/runtime, jiuwenswarm/agents/harness/common, jiuwenswarm/agents/harness/team, jiuwenswarm/agents/swarm, jiuwenswarm/server, docs, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | gateway/channel_manager/web/web_connect.py, tests/unit_tests/channel/test_web_channel_personal_context_payload.py, jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/agents/swarm/assembly.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py, tests/unit_tests/agentserver/test_app_role_npc_team.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_team_helpers.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/test_session_ops_fork.py, docs/zh/AppRoleSessions.md, docs/en/AppRoleSessions.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | [D-20261104-1141-npc-team-member-sessions](../designs/active/D-20261104-1141-npc-team-member-sessions/design.md) |

<a id="module-4"></a>

## jiuwenswarm/agents/swarm

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261104_1225_npc-team-member-sessions](F_20261104_1225_npc-team-member-sessions.md) | F | NPC 主从会话与成员咨询 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/server/runtime, jiuwenswarm/agents/harness/common, jiuwenswarm/agents/harness/team, jiuwenswarm/agents/swarm, jiuwenswarm/server, docs, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | gateway/channel_manager/web/web_connect.py, tests/unit_tests/channel/test_web_channel_personal_context_payload.py, jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/agents/swarm/assembly.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py, tests/unit_tests/agentserver/test_app_role_npc_team.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_team_helpers.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/test_session_ops_fork.py, docs/zh/AppRoleSessions.md, docs/en/AppRoleSessions.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | [D-20261104-1141-npc-team-member-sessions](../designs/active/D-20261104-1141-npc-team-member-sessions/design.md) |

<a id="module-5"></a>

## jiuwenswarm/channels/desktop/electron

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_1106_app-market-containers-and-editing](app-market-implementation.md) | F | 应用市场浏览器容器与编辑会话 | default | jiuwenswarm/channels/desktop/electron, jiuwenswarm/channels/web/frontend, jiuwenswarm/extensions/app_market | jiuwenswarm/channels/desktop/electron/app_market_windows.cjs, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx | 无独立 D：用户明确授权初步本地实现，无独立设计文档。 |

<a id="module-6"></a>

## jiuwenswarm/channels/web/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_1106_app-market-containers-and-editing](app-market-implementation.md) | F | 应用市场浏览器容器与编辑会话 | default | jiuwenswarm/channels/desktop/electron, jiuwenswarm/channels/web/frontend, jiuwenswarm/extensions/app_market | jiuwenswarm/channels/desktop/electron/app_market_windows.cjs, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx | 无独立 D：用户明确授权初步本地实现，无独立设计文档。 |
| [F_20261103_1149_builtin-skill-catalog](builtin-skill-catalog.md) | F | 内置能力总目录实施记录 | kdgmpw | jiuwenswarm/resources/agent/workspace/skills, jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/channels/web/frontend | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/common/utils.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/server/runtime/agent_adapter/user_turn.py, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](../designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |

<a id="module-7"></a>

## jiuwenswarm/common

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_1149_builtin-skill-catalog](builtin-skill-catalog.md) | F | 内置能力总目录实施记录 | kdgmpw | jiuwenswarm/resources/agent/workspace/skills, jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/channels/web/frontend | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/common/utils.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/server/runtime/agent_adapter/user_turn.py, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](../designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |

<a id="module-8"></a>

## jiuwenswarm/extensions/app_market

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_1106_app-market-containers-and-editing](app-market-implementation.md) | F | 应用市场浏览器容器与编辑会话 | default | jiuwenswarm/channels/desktop/electron, jiuwenswarm/channels/web/frontend, jiuwenswarm/extensions/app_market | jiuwenswarm/channels/desktop/electron/app_market_windows.cjs, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx | 无独立 D：用户明确授权初步本地实现，无独立设计文档。 |
| [F_20261103_1501_app-role-session](app-role-session.md) | F | 保留基础系统提示的轻量 NPC 会话 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_session_create_runtime_boundary.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/agentserver/test_agent_warm_pool.py | [D-20261103-1444-app-role-session](../designs/active/D-20261103-1444-app-role-session/design.md) |

<a id="module-9"></a>

## jiuwenswarm/resources/agent/workspace/skills

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_1149_builtin-skill-catalog](builtin-skill-catalog.md) | F | 内置能力总目录实施记录 | kdgmpw | jiuwenswarm/resources/agent/workspace/skills, jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/channels/web/frontend | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/common/utils.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/server/runtime/agent_adapter/user_turn.py, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](../designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |

<a id="module-10"></a>

## jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_1501_app-role-session](app-role-session.md) | F | 保留基础系统提示的轻量 NPC 会话 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_session_create_runtime_boundary.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/agentserver/test_agent_warm_pool.py | [D-20261103-1444-app-role-session](../designs/active/D-20261103-1444-app-role-session/design.md) |
| [F_20261104_1225_npc-team-member-sessions](F_20261104_1225_npc-team-member-sessions.md) | F | NPC 主从会话与成员咨询 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/server/runtime, jiuwenswarm/agents/harness/common, jiuwenswarm/agents/harness/team, jiuwenswarm/agents/swarm, jiuwenswarm/server, docs, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | gateway/channel_manager/web/web_connect.py, tests/unit_tests/channel/test_web_channel_personal_context_payload.py, jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/agents/swarm/assembly.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py, tests/unit_tests/agentserver/test_app_role_npc_team.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_team_helpers.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/test_session_ops_fork.py, docs/zh/AppRoleSessions.md, docs/en/AppRoleSessions.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | [D-20261104-1141-npc-team-member-sessions](../designs/active/D-20261104-1141-npc-team-member-sessions/design.md) |

<a id="module-11"></a>

## jiuwenswarm/runtime

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_1501_app-role-session](app-role-session.md) | F | 保留基础系统提示的轻量 NPC 会话 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_session_create_runtime_boundary.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/agentserver/test_agent_warm_pool.py | [D-20261103-1444-app-role-session](../designs/active/D-20261103-1444-app-role-session/design.md) |
| [F_20261104_1225_npc-team-member-sessions](F_20261104_1225_npc-team-member-sessions.md) | F | NPC 主从会话与成员咨询 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/server/runtime, jiuwenswarm/agents/harness/common, jiuwenswarm/agents/harness/team, jiuwenswarm/agents/swarm, jiuwenswarm/server, docs, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | gateway/channel_manager/web/web_connect.py, tests/unit_tests/channel/test_web_channel_personal_context_payload.py, jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/agents/swarm/assembly.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py, tests/unit_tests/agentserver/test_app_role_npc_team.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_team_helpers.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/test_session_ops_fork.py, docs/zh/AppRoleSessions.md, docs/en/AppRoleSessions.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | [D-20261104-1141-npc-team-member-sessions](../designs/active/D-20261104-1141-npc-team-member-sessions/design.md) |

<a id="module-12"></a>

## jiuwenswarm/server

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261104_1225_npc-team-member-sessions](F_20261104_1225_npc-team-member-sessions.md) | F | NPC 主从会话与成员咨询 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/server/runtime, jiuwenswarm/agents/harness/common, jiuwenswarm/agents/harness/team, jiuwenswarm/agents/swarm, jiuwenswarm/server, docs, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | gateway/channel_manager/web/web_connect.py, tests/unit_tests/channel/test_web_channel_personal_context_payload.py, jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/agents/swarm/assembly.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py, tests/unit_tests/agentserver/test_app_role_npc_team.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_team_helpers.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/test_session_ops_fork.py, docs/zh/AppRoleSessions.md, docs/en/AppRoleSessions.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | [D-20261104-1141-npc-team-member-sessions](../designs/active/D-20261104-1141-npc-team-member-sessions/design.md) |

<a id="module-13"></a>

## jiuwenswarm/server/runtime

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261104_1225_npc-team-member-sessions](F_20261104_1225_npc-team-member-sessions.md) | F | NPC 主从会话与成员咨询 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/server/runtime, jiuwenswarm/agents/harness/common, jiuwenswarm/agents/harness/team, jiuwenswarm/agents/swarm, jiuwenswarm/server, docs, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | gateway/channel_manager/web/web_connect.py, tests/unit_tests/channel/test_web_channel_personal_context_payload.py, jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/agents/swarm/assembly.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py, tests/unit_tests/agentserver/test_app_role_npc_team.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_team_helpers.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/test_session_ops_fork.py, docs/zh/AppRoleSessions.md, docs/en/AppRoleSessions.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | [D-20261104-1141-npc-team-member-sessions](../designs/active/D-20261104-1141-npc-team-member-sessions/design.md) |

<a id="module-14"></a>

## jiuwenswarm/server/runtime/agent_adapter

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_1149_builtin-skill-catalog](builtin-skill-catalog.md) | F | 内置能力总目录实施记录 | kdgmpw | jiuwenswarm/resources/agent/workspace/skills, jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/channels/web/frontend | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/common/utils.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/server/runtime/agent_adapter/user_turn.py, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](../designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |
| [F_20261103_1501_app-role-session](app-role-session.md) | F | 保留基础系统提示的轻量 NPC 会话 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_session_create_runtime_boundary.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/agentserver/test_agent_warm_pool.py | [D-20261103-1444-app-role-session](../designs/active/D-20261103-1444-app-role-session/design.md) |
| [F_20261104_1225_npc-team-member-sessions](F_20261104_1225_npc-team-member-sessions.md) | F | NPC 主从会话与成员咨询 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/server/runtime, jiuwenswarm/agents/harness/common, jiuwenswarm/agents/harness/team, jiuwenswarm/agents/swarm, jiuwenswarm/server, docs, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | gateway/channel_manager/web/web_connect.py, tests/unit_tests/channel/test_web_channel_personal_context_payload.py, jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/agents/swarm/assembly.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py, tests/unit_tests/agentserver/test_app_role_npc_team.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_team_helpers.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/test_session_ops_fork.py, docs/zh/AppRoleSessions.md, docs/en/AppRoleSessions.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | [D-20261104-1141-npc-team-member-sessions](../designs/active/D-20261104-1141-npc-team-member-sessions/design.md) |

<a id="module-15"></a>

## jiuwenswarm/server/runtime/session

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_1501_app-role-session](app-role-session.md) | F | 保留基础系统提示的轻量 NPC 会话 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_session_create_runtime_boundary.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/agentserver/test_agent_warm_pool.py | [D-20261103-1444-app-role-session](../designs/active/D-20261103-1444-app-role-session/design.md) |
| [F_20261104_1225_npc-team-member-sessions](F_20261104_1225_npc-team-member-sessions.md) | F | NPC 主从会话与成员咨询 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/server/runtime, jiuwenswarm/agents/harness/common, jiuwenswarm/agents/harness/team, jiuwenswarm/agents/swarm, jiuwenswarm/server, docs, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | gateway/channel_manager/web/web_connect.py, tests/unit_tests/channel/test_web_channel_personal_context_payload.py, jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/agents/swarm/assembly.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py, tests/unit_tests/agentserver/test_app_role_npc_team.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_team_helpers.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/test_session_ops_fork.py, docs/zh/AppRoleSessions.md, docs/en/AppRoleSessions.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | [D-20261104-1141-npc-team-member-sessions](../designs/active/D-20261104-1141-npc-team-member-sessions/design.md) |

<a id="module-16"></a>

## jiuwenswarm/server/runtime/skill

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_1149_builtin-skill-catalog](builtin-skill-catalog.md) | F | 内置能力总目录实施记录 | kdgmpw | jiuwenswarm/resources/agent/workspace/skills, jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/channels/web/frontend | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/common/utils.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/server/runtime/agent_adapter/user_turn.py, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](../designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |
