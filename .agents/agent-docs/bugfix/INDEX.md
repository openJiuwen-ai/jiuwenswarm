---
title: 故障修复 B索引
author: kdgmpw
author_email: lishuqi4@huawei.com
generated: true
---

# 故障修复 B索引

由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。

共 9 篇文档，按模块分组；跨模块文档在各相关分组展示，总数按文档去重。

## 模块导航

| 模块 | 文档数 |
|---|---|
| [jiuwenswarm/channels/web/frontend](#module-1) | 2 |
| [jiuwenswarm/common](#module-2) | 1 |
| [jiuwenswarm/extensions/app_market](#module-3) | 4 |
| [jiuwenswarm/resources/agent/workspace/skills](#module-4) | 1 |
| [jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin](#module-5) | 1 |
| [jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder](#module-6) | 1 |
| [jiuwenswarm/server/runtime/skill](#module-7) | 1 |
| [未分类](#module-8) | 3 |

<a id="module-1"></a>

## jiuwenswarm/channels/web/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261105_0941_app-stop-graceful-and-page-close](jiuwenswarm/extensions/app_market/B_20261105_0941_app-stop-graceful-and-page-close.md) | B | 应用市场停止强杀跳过应用清理与页面关闭无感知 | kdgmpw | jiuwenswarm/extensions/app_market, jiuwenswarm/channels/web/frontend | jiuwenswarm/extensions/app_market/app_market/static_server.py, jiuwenswarm/extensions/app_market/app_market/status_manager.py, jiuwenswarm/extensions/app_market/app_market/market_service.py, jiuwenswarm/extensions/app_market/app_market/process_supervisor.py, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx | 无独立 D：修复依据既有契约（app-builder SKILL.md 的"应用负责优雅退出和会话清理、市场只终止进程"与扩展 README 的运行描述），无独立 D；新增契约已固化为 specs/jiuwenswarm/extensions/app_market/S_02_app-market-stop-and-page-lifecycle.md。 |
| [B_20261105_1500_start-completion-steals-view](jiuwenswarm/extensions/app_market/B_20261105_1500_start-completion-steals-view.md) | B | 应用启动完成抢走用户后来切换的页面 | kdgmpw | jiuwenswarm/channels/web/frontend | jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx, jiuwenswarm/channels/web/frontend/tests/appMarketPanel.test.mjs | 无独立 D：依据 S_01_app-market-browser-containers 与 S_02_app-market-stop-and-page-lifecycle 的“按稳定 app_id 复用页面、失败/停止不误关其他页面”契约；本次修复补充用户意图排序，无独立 D。 |

<a id="module-2"></a>

## jiuwenswarm/common

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261103_1636_builtin-catalog-upgrade](jiuwenswarm/server/runtime/skill/B_20261103_1636_builtin-catalog-upgrade.md) | B | 旧内置技能未归一与应用市场入口不明显 | kdgmpw | jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/resources/agent/workspace/skills | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/legacy_versions.json, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/SKILL.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/skill_display.md, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](../designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |

<a id="module-3"></a>

## jiuwenswarm/extensions/app_market

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261103_1433_npc-session-project](jiuwenswarm/extensions/app_market/B_20261103_1433_npc-session-project.md) | B | 微缩生活岛 NPC 会话归属错误 | kdgmpw | jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | jiuwenswarm/extensions/app_market/app_market/market_service.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md, tests/unit_tests/test_app_market_runtime.py | 无独立 D：根据明确的故障修复要求落实既有项目绑定接口，无独立设计。 |
| [B_20261105_0941_app-stop-graceful-and-page-close](jiuwenswarm/extensions/app_market/B_20261105_0941_app-stop-graceful-and-page-close.md) | B | 应用市场停止强杀跳过应用清理与页面关闭无感知 | kdgmpw | jiuwenswarm/extensions/app_market, jiuwenswarm/channels/web/frontend | jiuwenswarm/extensions/app_market/app_market/static_server.py, jiuwenswarm/extensions/app_market/app_market/status_manager.py, jiuwenswarm/extensions/app_market/app_market/market_service.py, jiuwenswarm/extensions/app_market/app_market/process_supervisor.py, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx | 无独立 D：修复依据既有契约（app-builder SKILL.md 的"应用负责优雅退出和会话清理、市场只终止进程"与扩展 README 的运行描述），无独立 D；新增契约已固化为 specs/jiuwenswarm/extensions/app_market/S_02_app-market-stop-and-page-lifecycle.md。 |
| [B_20261105_1130_stale-page-cache-port-reuse](jiuwenswarm/extensions/app_market/B_20261105_1130_stale-page-cache-port-reuse.md) | B | 端口复用后启动新应用显示上一个应用的缓存页面 | kdgmpw | jiuwenswarm/extensions/app_market | jiuwenswarm/extensions/app_market/app_market/static_server.py, jiuwenswarm/extensions/app_market/manager.py, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | 无独立 D：依据页面容器契约（specs/jiuwenswarm/channels/desktop/S_01_app-market-browser-containers.md 与 specs/jiuwenswarm/extensions/app_market/S_02_app-market-stop-and-page-lifecycle.md）；缓存约束已补入 S_02 不变量与应用开发技能，无独立 D。 |
| [B_20261105_1459_web-url-host-403](jiuwenswarm/extensions/app_market/B_20261105_1459_web-url-host-403.md) | B | Web 应用入口 403：静态服务未放行应用专属 localhost 主机 | kdgmpw | jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder | jiuwenswarm/extensions/app_market/app_market/static_server.py, tests/unit_tests/test_app_market_runtime.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | 无独立 D：依据 S_02_app-market-stop-and-page-lifecycle 的 Web 入口与来源隔离契约；本次修复使实现重新满足既有契约，无独立 D。 |

<a id="module-4"></a>

## jiuwenswarm/resources/agent/workspace/skills

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261103_1636_builtin-catalog-upgrade](jiuwenswarm/server/runtime/skill/B_20261103_1636_builtin-catalog-upgrade.md) | B | 旧内置技能未归一与应用市场入口不明显 | kdgmpw | jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/resources/agent/workspace/skills | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/legacy_versions.json, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/SKILL.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/skill_display.md, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](../designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |

<a id="module-5"></a>

## jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261103_1433_npc-session-project](jiuwenswarm/extensions/app_market/B_20261103_1433_npc-session-project.md) | B | 微缩生活岛 NPC 会话归属错误 | kdgmpw | jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | jiuwenswarm/extensions/app_market/app_market/market_service.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md, tests/unit_tests/test_app_market_runtime.py | 无独立 D：根据明确的故障修复要求落实既有项目绑定接口，无独立设计。 |

<a id="module-6"></a>

## jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261105_1459_web-url-host-403](jiuwenswarm/extensions/app_market/B_20261105_1459_web-url-host-403.md) | B | Web 应用入口 403：静态服务未放行应用专属 localhost 主机 | kdgmpw | jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder | jiuwenswarm/extensions/app_market/app_market/static_server.py, tests/unit_tests/test_app_market_runtime.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md | 无独立 D：依据 S_02_app-market-stop-and-page-lifecycle 的 Web 入口与来源隔离契约；本次修复使实现重新满足既有契约，无独立 D。 |

<a id="module-7"></a>

## jiuwenswarm/server/runtime/skill

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261103_1636_builtin-catalog-upgrade](jiuwenswarm/server/runtime/skill/B_20261103_1636_builtin-catalog-upgrade.md) | B | 旧内置技能未归一与应用市场入口不明显 | kdgmpw | jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/resources/agent/workspace/skills | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/legacy_versions.json, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/SKILL.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/skill_display.md, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](../designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |

<a id="module-8"></a>

## 未分类

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261026_1602_code-multimodal-loading](jiuwenswarm/server/runtime/agent_adapter/B_20261026_1602_code-multimodal-loading.md) | B | 配置多模态模型后，Code 未按 Work 规则加载工具 | 待确认 | — | — | 待核实 |
| [B_20261026_1606_issue-5753-session-project-binding](jiuwenswarm/server/runtime/session/B_20261026_1606_issue-5753-session-project-binding.md) | B | Issue #5753：普通会话发送消息后混入全局项目目录 | 待确认 | — | — | 待核实 |
| [B_20261026_2035_gateway-contract-collection](tests/unit_tests/common/B_20261026_2035_gateway-contract-collection.md) | B | Gateway 拆包后协议契约测试无法收集 | 待确认 | — | — | 待核实 |
