---
title: D/F/B/S 文档总索引
author: kdgmpw
author_email: lishuqi4@huawei.com
generated: true
---

# D/F/B/S 文档总索引

由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。

共 33 篇文档，按模块分组；跨模块文档在各相关分组展示，总数按文档去重。

## 模块导航

| 模块 | 文档数 |
|---|---|
| [agent_teams](#module-1) | 1 |
| [agent_teams/workflow](#module-2) | 3 |
| [jiuwenswarm](#module-3) | 3 |
| [jiuwenswarm/agents/harness/common](#module-4) | 1 |
| [jiuwenswarm/channels/desktop/electron](#module-5) | 2 |
| [jiuwenswarm/channels/web/frontend](#module-6) | 5 |
| [jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel](#module-7) | 2 |
| [jiuwenswarm/common](#module-8) | 4 |
| [jiuwenswarm/extensions/app_market](#module-9) | 8 |
| [jiuwenswarm/gateway/channel_manager/web](#module-10) | 1 |
| [jiuwenswarm/observability](#module-11) | 1 |
| [jiuwenswarm/resources/agent/workspace/skills](#module-12) | 4 |
| [jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin](#module-13) | 4 |
| [jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder](#module-14) | 1 |
| [jiuwenswarm/runtime](#module-15) | 2 |
| [jiuwenswarm/server/runtime/agent_adapter](#module-16) | 3 |
| [jiuwenswarm/server/runtime/session](#module-17) | 1 |
| [jiuwenswarm/server/runtime/skill](#module-18) | 4 |
| [未分类](#module-19) | 12 |

<a id="module-1"></a>

## agent_teams

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0029](designs/proposals/SDD-0029-team-session-runtime-integration/design.md) | D | Team 模式接入统一 Session Runtime | @zhanglongmin | agent_teams | — | — |

<a id="module-2"></a>

## agent_teams/workflow

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0009](designs/archive/SDD-0009-jiuwen-tui-swarmflow-human-session-adaptation/design.md) | D | jiuwen TUI 适配 SwarmFlow 三算子（human / human_session / agent_session） | @chenchunzhou | agent_teams/workflow, jiuwenswarm | — | — |
| [SDD-0010](designs/archive/SDD-0010-jiuwen-tui-nested-workflow-and-budget-observability/design.md) | D | jiuwen TUI 嵌套 Workflow 与 Budget 可观测性 | @chenchunzhou | agent_teams/workflow, jiuwenswarm | — | — |
| [SDD-0028](designs/archive/SDD-0028-swarmflow-verify-fork-observability/design.md) | D | swarmflow verify / fork 可观测与前端呈现 | @xiajing | agent_teams/workflow, jiuwenswarm | — | — |

<a id="module-3"></a>

## jiuwenswarm

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0009](designs/archive/SDD-0009-jiuwen-tui-swarmflow-human-session-adaptation/design.md) | D | jiuwen TUI 适配 SwarmFlow 三算子（human / human_session / agent_session） | @chenchunzhou | agent_teams/workflow, jiuwenswarm | — | — |
| [SDD-0010](designs/archive/SDD-0010-jiuwen-tui-nested-workflow-and-budget-observability/design.md) | D | jiuwen TUI 嵌套 Workflow 与 Budget 可观测性 | @chenchunzhou | agent_teams/workflow, jiuwenswarm | — | — |
| [SDD-0028](designs/archive/SDD-0028-swarmflow-verify-fork-observability/design.md) | D | swarmflow verify / fork 可观测与前端呈现 | @xiajing | agent_teams/workflow, jiuwenswarm | — | — |

<a id="module-4"></a>

## jiuwenswarm/agents/harness/common

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [D-20261103-1123-builtin-skill-catalog](designs/active/D-20261103-1123-builtin-skill-catalog/design.md) | D | 内置 Skill 总目录与按需加载设计方案 | kdgmpw | jiuwenswarm/resources/agent/workspace/skills, jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/agents/harness/common | — | — |

<a id="module-5"></a>

## jiuwenswarm/channels/desktop/electron

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_1106_app-market-electron](features/F_20261103_1106_app-market-electron.md) | F | 应用市场分端浏览器容器 | default | jiuwenswarm/channels/desktop/electron, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel | jiuwenswarm/channels/desktop/electron/app_market_windows.cjs, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx | 无独立 D：用户明确授权初步本地实现，无独立设计文档。 |
| [S_01_app-market-windows](specs/jiuwenswarm/channels/desktop/S_01_app-market-windows.md) | S | 应用市场分端展示契约 | default | jiuwenswarm/channels/desktop/electron, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel | jiuwenswarm/channels/desktop/electron/app_market_windows.cjs | — |

<a id="module-6"></a>

## jiuwenswarm/channels/web/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0026](designs/proposals/SDD-0026-trajectory-observability-web-ui/design.md) | D | 轨迹观测前端：单 Agent、Subagent 与 Team 多 Agent 轨迹 UI | @alan_cheng | jiuwenswarm/channels/web/frontend | — | — |
| [F_20261103_1149_builtin-skill-catalog](features/F_20261103_1149_builtin-skill-catalog.md) | F | 内置能力总目录实施记录 | kdgmpw | jiuwenswarm/resources/agent/workspace/skills, jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/channels/web/frontend | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/common/utils.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/server/runtime/agent_adapter/user_turn.py, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |
| [B_20261105_0941_app-stop-graceful-and-page-close](bugfix/jiuwenswarm/extensions/app_market/B_20261105_0941_app-stop-graceful-and-page-close.md) | B | 应用市场停止强杀跳过应用清理与页面关闭无感知 | kdgmpw | jiuwenswarm/extensions/app_market, jiuwenswarm/channels/web/frontend | jiuwenswarm/extensions/app_market/app_market/static_server.py, jiuwenswarm/extensions/app_market/app_market/status_manager.py, jiuwenswarm/extensions/app_market/app_market/market_service.py, jiuwenswarm/extensions/app_market/app_market/process_supervisor.py, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx | 无独立 D：修复依据既有契约（app-builder SKILL.md 的"应用负责优雅退出和会话清理、市场只终止进程"与扩展 README 的运行描述），无独立 D；新增契约已固化为 specs/jiuwenswarm/extensions/app_market/S_02_app-stop-and-page-lifecycle.md。 |
| [B_20261105_1500_start-completion-steals-view](bugfix/jiuwenswarm/extensions/app_market/B_20261105_1500_start-completion-steals-view.md) | B | 应用启动完成抢走用户后来切换的页面 | kdgmpw | jiuwenswarm/channels/web/frontend | jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx, jiuwenswarm/channels/web/frontend/tests/appMarketPanel.test.mjs | 无独立 D：依据 S_01_app-market-windows 与 S_02_app-stop-and-page-lifecycle 的“按稳定 app_id 复用页面、失败/停止不误关其他页面”契约；本次修复补充用户意图排序，无独立 D。 |
| [S_02_app-stop-and-page-lifecycle](specs/jiuwenswarm/extensions/app_market/S_02_app-stop-and-page-lifecycle.md) | S | 应用停止协议与页面联动 | kdgmpw | jiuwenswarm/extensions/app_market, jiuwenswarm/channels/web/frontend | jiuwenswarm/extensions/app_market/app_market/static_server.py, jiuwenswarm/extensions/app_market/app_market/status_manager.py, jiuwenswarm/extensions/app_market/app_market/market_service.py, jiuwenswarm/extensions/app_market/app_market/process_supervisor.py, jiuwenswarm/extensions/app_market/manager.py, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/appMarketViews.ts, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | — |

<a id="module-7"></a>

## jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_1106_app-market-electron](features/F_20261103_1106_app-market-electron.md) | F | 应用市场分端浏览器容器 | default | jiuwenswarm/channels/desktop/electron, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel | jiuwenswarm/channels/desktop/electron/app_market_windows.cjs, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx | 无独立 D：用户明确授权初步本地实现，无独立设计文档。 |
| [S_01_app-market-windows](specs/jiuwenswarm/channels/desktop/S_01_app-market-windows.md) | S | 应用市场分端展示契约 | default | jiuwenswarm/channels/desktop/electron, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel | jiuwenswarm/channels/desktop/electron/app_market_windows.cjs | — |

<a id="module-8"></a>

## jiuwenswarm/common

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [D-20261103-1123-builtin-skill-catalog](designs/active/D-20261103-1123-builtin-skill-catalog/design.md) | D | 内置 Skill 总目录与按需加载设计方案 | kdgmpw | jiuwenswarm/resources/agent/workspace/skills, jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/agents/harness/common | — | — |
| [F_20261103_1149_builtin-skill-catalog](features/F_20261103_1149_builtin-skill-catalog.md) | F | 内置能力总目录实施记录 | kdgmpw | jiuwenswarm/resources/agent/workspace/skills, jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/channels/web/frontend | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/common/utils.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/server/runtime/agent_adapter/user_turn.py, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |
| [B_20261103_1636_builtin-catalog-upgrade](bugfix/jiuwenswarm/server/runtime/skill/B_20261103_1636_builtin-catalog-upgrade.md) | B | 旧内置技能未归一与应用市场入口不明显 | kdgmpw | jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/resources/agent/workspace/skills | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/legacy_versions.json, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/SKILL.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/skill_display.md, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |
| [S_01_builtin-catalog](specs/jiuwenswarm/server/runtime/skill/S_01_builtin-catalog.md) | S | 内置能力目录规约 | kdgmpw | jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/resources/agent/workspace/skills | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/common/utils.py, jiuwenswarm/server/runtime/skill/skill_manager.py | — |

<a id="module-9"></a>

## jiuwenswarm/extensions/app_market

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [D-20261103-1444-app-role-session](designs/active/D-20261103-1444-app-role-session/design.md) | D | 应用角色会话的最小提示词运行配置 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | — | — |
| [F_20261103_1501_app-role-session](features/F_20261103_1501_app-role-session.md) | F | 保留基础系统提示的轻量 NPC 会话 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_session_create_runtime_boundary.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/agentserver/test_agent_warm_pool.py | [D-20261103-1444-app-role-session](designs/active/D-20261103-1444-app-role-session/design.md) |
| [B_20261103_1433_npc-session-project](bugfix/jiuwenswarm/extensions/app_market/B_20261103_1433_npc-session-project.md) | B | 微缩生活岛 NPC 会话归属错误 | kdgmpw | jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | jiuwenswarm/extensions/app_market/app_market/market_service.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md, tests/unit_tests/test_app_market_runtime.py | 无独立 D：根据明确的故障修复要求落实既有项目绑定接口，无独立设计。 |
| [B_20261105_0941_app-stop-graceful-and-page-close](bugfix/jiuwenswarm/extensions/app_market/B_20261105_0941_app-stop-graceful-and-page-close.md) | B | 应用市场停止强杀跳过应用清理与页面关闭无感知 | kdgmpw | jiuwenswarm/extensions/app_market, jiuwenswarm/channels/web/frontend | jiuwenswarm/extensions/app_market/app_market/static_server.py, jiuwenswarm/extensions/app_market/app_market/status_manager.py, jiuwenswarm/extensions/app_market/app_market/market_service.py, jiuwenswarm/extensions/app_market/app_market/process_supervisor.py, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx | 无独立 D：修复依据既有契约（app-builder SKILL.md 的"应用负责优雅退出和会话清理、市场只终止进程"与扩展 README 的运行描述），无独立 D；新增契约已固化为 specs/jiuwenswarm/extensions/app_market/S_02_app-stop-and-page-lifecycle.md。 |
| [B_20261105_1130_stale-page-cache-port-reuse](bugfix/jiuwenswarm/extensions/app_market/B_20261105_1130_stale-page-cache-port-reuse.md) | B | 端口复用后启动新应用显示上一个应用的缓存页面 | kdgmpw | jiuwenswarm/extensions/app_market | jiuwenswarm/extensions/app_market/app_market/static_server.py, jiuwenswarm/extensions/app_market/manager.py, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | 无独立 D：依据页面容器契约（specs/jiuwenswarm/channels/desktop/S_01_app-market-windows.md 与 specs/jiuwenswarm/extensions/app_market/S_02_app-stop-and-page-lifecycle.md）；缓存约束已补入 S_02 不变量与应用开发技能，无独立 D。 |
| [B_20261105_1459_web-url-host-403](bugfix/jiuwenswarm/extensions/app_market/B_20261105_1459_web-url-host-403.md) | B | Web 应用入口 403：静态服务未放行应用专属 localhost 主机 | kdgmpw | jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder | jiuwenswarm/extensions/app_market/app_market/static_server.py, tests/unit_tests/test_app_market_runtime.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | 无独立 D：依据 S_02_app-stop-and-page-lifecycle 的 Web 入口与来源隔离契约；本次修复使实现重新满足既有契约，无独立 D。 |
| [S_01_app-session-project](specs/jiuwenswarm/extensions/app_market/S_01_app-session-project.md) | S | 应用角色会话的项目归属 | kdgmpw | jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | jiuwenswarm/extensions/app_market/app_market/market_service.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | — |
| [S_02_app-stop-and-page-lifecycle](specs/jiuwenswarm/extensions/app_market/S_02_app-stop-and-page-lifecycle.md) | S | 应用停止协议与页面联动 | kdgmpw | jiuwenswarm/extensions/app_market, jiuwenswarm/channels/web/frontend | jiuwenswarm/extensions/app_market/app_market/static_server.py, jiuwenswarm/extensions/app_market/app_market/status_manager.py, jiuwenswarm/extensions/app_market/app_market/market_service.py, jiuwenswarm/extensions/app_market/app_market/process_supervisor.py, jiuwenswarm/extensions/app_market/manager.py, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/appMarketViews.ts, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | — |

<a id="module-10"></a>

## jiuwenswarm/gateway/channel_manager/web

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0025](designs/proposals/SDD-0025-trajectory-session-store-and-serving/design.md) | D | 轨迹数据面：会话级 SQLite 存储、实时变更提示与读取 API | @alan_cheng | jiuwenswarm/observability, jiuwenswarm/gateway/channel_manager/web | — | — |

<a id="module-11"></a>

## jiuwenswarm/observability

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0025](designs/proposals/SDD-0025-trajectory-session-store-and-serving/design.md) | D | 轨迹数据面：会话级 SQLite 存储、实时变更提示与读取 API | @alan_cheng | jiuwenswarm/observability, jiuwenswarm/gateway/channel_manager/web | — | — |

<a id="module-12"></a>

## jiuwenswarm/resources/agent/workspace/skills

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [D-20261103-1123-builtin-skill-catalog](designs/active/D-20261103-1123-builtin-skill-catalog/design.md) | D | 内置 Skill 总目录与按需加载设计方案 | kdgmpw | jiuwenswarm/resources/agent/workspace/skills, jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/agents/harness/common | — | — |
| [F_20261103_1149_builtin-skill-catalog](features/F_20261103_1149_builtin-skill-catalog.md) | F | 内置能力总目录实施记录 | kdgmpw | jiuwenswarm/resources/agent/workspace/skills, jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/channels/web/frontend | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/common/utils.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/server/runtime/agent_adapter/user_turn.py, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |
| [B_20261103_1636_builtin-catalog-upgrade](bugfix/jiuwenswarm/server/runtime/skill/B_20261103_1636_builtin-catalog-upgrade.md) | B | 旧内置技能未归一与应用市场入口不明显 | kdgmpw | jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/resources/agent/workspace/skills | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/legacy_versions.json, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/SKILL.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/skill_display.md, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |
| [S_01_builtin-catalog](specs/jiuwenswarm/server/runtime/skill/S_01_builtin-catalog.md) | S | 内置能力目录规约 | kdgmpw | jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/resources/agent/workspace/skills | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/common/utils.py, jiuwenswarm/server/runtime/skill/skill_manager.py | — |

<a id="module-13"></a>

## jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [D-20261103-1444-app-role-session](designs/active/D-20261103-1444-app-role-session/design.md) | D | 应用角色会话的最小提示词运行配置 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | — | — |
| [F_20261103_1501_app-role-session](features/F_20261103_1501_app-role-session.md) | F | 保留基础系统提示的轻量 NPC 会话 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_session_create_runtime_boundary.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/agentserver/test_agent_warm_pool.py | [D-20261103-1444-app-role-session](designs/active/D-20261103-1444-app-role-session/design.md) |
| [B_20261103_1433_npc-session-project](bugfix/jiuwenswarm/extensions/app_market/B_20261103_1433_npc-session-project.md) | B | 微缩生活岛 NPC 会话归属错误 | kdgmpw | jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | jiuwenswarm/extensions/app_market/app_market/market_service.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md, tests/unit_tests/test_app_market_runtime.py | 无独立 D：根据明确的故障修复要求落实既有项目绑定接口，无独立设计。 |
| [S_01_app-session-project](specs/jiuwenswarm/extensions/app_market/S_01_app-session-project.md) | S | 应用角色会话的项目归属 | kdgmpw | jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | jiuwenswarm/extensions/app_market/app_market/market_service.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | — |

<a id="module-14"></a>

## jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261105_1459_web-url-host-403](bugfix/jiuwenswarm/extensions/app_market/B_20261105_1459_web-url-host-403.md) | B | Web 应用入口 403：静态服务未放行应用专属 localhost 主机 | kdgmpw | jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder | jiuwenswarm/extensions/app_market/app_market/static_server.py, tests/unit_tests/test_app_market_runtime.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | 无独立 D：依据 S_02_app-stop-and-page-lifecycle 的 Web 入口与来源隔离契约；本次修复使实现重新满足既有契约，无独立 D。 |

<a id="module-15"></a>

## jiuwenswarm/runtime

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [D-20261103-1444-app-role-session](designs/active/D-20261103-1444-app-role-session/design.md) | D | 应用角色会话的最小提示词运行配置 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | — | — |
| [F_20261103_1501_app-role-session](features/F_20261103_1501_app-role-session.md) | F | 保留基础系统提示的轻量 NPC 会话 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_session_create_runtime_boundary.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/agentserver/test_agent_warm_pool.py | [D-20261103-1444-app-role-session](designs/active/D-20261103-1444-app-role-session/design.md) |

<a id="module-16"></a>

## jiuwenswarm/server/runtime/agent_adapter

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [D-20261103-1444-app-role-session](designs/active/D-20261103-1444-app-role-session/design.md) | D | 应用角色会话的最小提示词运行配置 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | — | — |
| [F_20261103_1149_builtin-skill-catalog](features/F_20261103_1149_builtin-skill-catalog.md) | F | 内置能力总目录实施记录 | kdgmpw | jiuwenswarm/resources/agent/workspace/skills, jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/channels/web/frontend | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/common/utils.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/server/runtime/agent_adapter/user_turn.py, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |
| [F_20261103_1501_app-role-session](features/F_20261103_1501_app-role-session.md) | F | 保留基础系统提示的轻量 NPC 会话 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_session_create_runtime_boundary.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/agentserver/test_agent_warm_pool.py | [D-20261103-1444-app-role-session](designs/active/D-20261103-1444-app-role-session/design.md) |

<a id="module-17"></a>

## jiuwenswarm/server/runtime/session

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_1501_app-role-session](features/F_20261103_1501_app-role-session.md) | F | 保留基础系统提示的轻量 NPC 会话 | kdgmpw | jiuwenswarm/runtime, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/server/runtime/session, jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | jiuwenswarm/runtime/app_role.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface.py, jiuwenswarm/server/runtime/agent_manager.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/agents/harness/common/session_ops_service.py, tests/unit_tests/agentserver/test_app_role_adapter.py, tests/unit_tests/agentserver/test_session_create_runtime_boundary.py, tests/unit_tests/runtime/test_session_create_provisioner.py, tests/unit_tests/agentserver/test_agent_warm_pool.py | [D-20261103-1444-app-role-session](designs/active/D-20261103-1444-app-role-session/design.md) |

<a id="module-18"></a>

## jiuwenswarm/server/runtime/skill

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [D-20261103-1123-builtin-skill-catalog](designs/active/D-20261103-1123-builtin-skill-catalog/design.md) | D | 内置 Skill 总目录与按需加载设计方案 | kdgmpw | jiuwenswarm/resources/agent/workspace/skills, jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/agents/harness/common | — | — |
| [F_20261103_1149_builtin-skill-catalog](features/F_20261103_1149_builtin-skill-catalog.md) | F | 内置能力总目录实施记录 | kdgmpw | jiuwenswarm/resources/agent/workspace/skills, jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/channels/web/frontend | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/common/utils.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/server/runtime/agent_adapter/user_turn.py, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |
| [B_20261103_1636_builtin-catalog-upgrade](bugfix/jiuwenswarm/server/runtime/skill/B_20261103_1636_builtin-catalog-upgrade.md) | B | 旧内置技能未归一与应用市场入口不明显 | kdgmpw | jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/resources/agent/workspace/skills | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/legacy_versions.json, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/SKILL.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/skill_display.md, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |
| [S_01_builtin-catalog](specs/jiuwenswarm/server/runtime/skill/S_01_builtin-catalog.md) | S | 内置能力目录规约 | kdgmpw | jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/resources/agent/workspace/skills | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/common/utils.py, jiuwenswarm/server/runtime/skill/skill_manager.py | — |

<a id="module-19"></a>

## 未分类

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [README](designs/README.md) | D | 设计资产 | 待确认 | — | — | — |
| [REGISTRY](designs/REGISTRY.md) | D | SDD 编号台账 (REGISTRY) | 待确认 | — | — | — |
| [legacy-documents](designs/legacy-documents.md) | D | 旧格式设计资料 | 待确认 | — | — | — |
| [migration-20261026](designs/migration-20261026.md) | D | 设计文档迁移与跨仓引用 | 待确认 | — | — | — |
| [team_expert_group_manifest_design](designs/archive/team_expert_group_manifest_design.md) | D | Team 模式接入专家团 Manifest 加载设计 | 待确认 | — | — | — |
| [team_permission_design](designs/archive/team_permission_design.md) | D | Team 模式 Permissions 设计方案 | 待确认 | — | — | — |
| [team_plan_design](designs/archive/team_plan_design.md) | D | Team Plan 模式设计文档 | 待确认 | — | — | — |
| [team_worktree_isolation_agenttool_cn](designs/archive/team_worktree_isolation_agenttool_cn.md) | D | Team Teammate Worktree Isolation 设计文档 | 待确认 | — | — | — |
| [B_20261026_1602_code-multimodal-loading](bugfix/jiuwenswarm/server/runtime/agent_adapter/B_20261026_1602_code-multimodal-loading.md) | B | 配置多模态模型后，Code 未按 Work 规则加载工具 | 待确认 | — | — | 待核实 |
| [B_20261026_1606_issue-5753-session-project-binding](bugfix/jiuwenswarm/server/runtime/session/B_20261026_1606_issue-5753-session-project-binding.md) | B | Issue #5753：普通会话发送消息后混入全局项目目录 | 待确认 | — | — | 待核实 |
| [B_20261026_2035_gateway-contract-collection](bugfix/tests/unit_tests/common/B_20261026_2035_gateway-contract-collection.md) | B | Gateway 拆包后协议契约测试无法收集 | 待确认 | — | — | 待核实 |
| [S_01_multimodal-tool-loading](specs/jiuwenswarm/server/runtime/agent_adapter/S_01_multimodal-tool-loading.md) | S | Work / Code 多模态工具加载契约 | 待确认 | — | — | — |
