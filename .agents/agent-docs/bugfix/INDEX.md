---
title: 故障修复 B索引
author: kdgmpw
author_email: lishuqi4@huawei.com
generated: true
---

# 故障修复 B索引

由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。

共 5 篇文档，按模块分组；跨模块文档在各相关分组展示，总数按文档去重。

## 模块导航

| 模块 | 文档数 |
|---|---|
| [jiuwenswarm/common](#module-1) | 1 |
| [jiuwenswarm/extensions/app_market](#module-2) | 1 |
| [jiuwenswarm/resources/agent/workspace/skills](#module-3) | 1 |
| [jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin](#module-4) | 1 |
| [jiuwenswarm/server/runtime/skill](#module-5) | 1 |
| [未分类](#module-6) | 3 |

<a id="module-1"></a>

## jiuwenswarm/common

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261103_1636_builtin-catalog-upgrade](jiuwenswarm/server/runtime/skill/B_20261103_1636_builtin-catalog-upgrade.md) | B | 旧内置技能未归一与应用市场入口不明显 | kdgmpw | jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/resources/agent/workspace/skills | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/legacy_versions.json, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/SKILL.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/skill_display.md, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](../designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |

<a id="module-2"></a>

## jiuwenswarm/extensions/app_market

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261103_1433_npc-session-project](jiuwenswarm/extensions/app_market/B_20261103_1433_npc-session-project.md) | B | 微缩生活岛 NPC 会话归属错误 | kdgmpw | jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | jiuwenswarm/extensions/app_market/app_market/market_service.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md, tests/unit_tests/test_app_market_runtime.py | 无独立 D：根据明确的故障修复要求落实既有项目绑定接口，无独立设计。 |

<a id="module-3"></a>

## jiuwenswarm/resources/agent/workspace/skills

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261103_1636_builtin-catalog-upgrade](jiuwenswarm/server/runtime/skill/B_20261103_1636_builtin-catalog-upgrade.md) | B | 旧内置技能未归一与应用市场入口不明显 | kdgmpw | jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/resources/agent/workspace/skills | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/legacy_versions.json, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/SKILL.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/skill_display.md, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](../designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |

<a id="module-4"></a>

## jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261103_1433_npc-session-project](jiuwenswarm/extensions/app_market/B_20261103_1433_npc-session-project.md) | B | 微缩生活岛 NPC 会话归属错误 | kdgmpw | jiuwenswarm/extensions/app_market, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin | jiuwenswarm/extensions/app_market/app_market/market_service.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md, tests/unit_tests/test_app_market_runtime.py | 无独立 D：根据明确的故障修复要求落实既有项目绑定接口，无独立设计。 |

<a id="module-5"></a>

## jiuwenswarm/server/runtime/skill

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261103_1636_builtin-catalog-upgrade](jiuwenswarm/server/runtime/skill/B_20261103_1636_builtin-catalog-upgrade.md) | B | 旧内置技能未归一与应用市场入口不明显 | kdgmpw | jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/resources/agent/workspace/skills | jiuwenswarm/common/builtin_skills.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/legacy_versions.json, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/SKILL.md, jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/skill_display.md, tests/unit_tests/test_builtin_catalog.py | [D-20261103-1123-builtin-skill-catalog](../designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |

<a id="module-6"></a>

## 未分类

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261026_1602_code-multimodal-loading](jiuwenswarm/server/runtime/agent_adapter/B_20261026_1602_code-multimodal-loading.md) | B | 配置多模态模型后，Code 未按 Work 规则加载工具 | 待确认 | — | — | 待核实 |
| [B_20261026_1606_issue-5753-session-project-binding](jiuwenswarm/server/runtime/session/B_20261026_1606_issue-5753-session-project-binding.md) | B | Issue #5753：普通会话发送消息后混入全局项目目录 | 待确认 | — | — | 待核实 |
| [B_20261026_2035_gateway-contract-collection](tests/unit_tests/common/B_20261026_2035_gateway-contract-collection.md) | B | Gateway 拆包后协议契约测试无法收集 | 待确认 | — | — | 待核实 |
