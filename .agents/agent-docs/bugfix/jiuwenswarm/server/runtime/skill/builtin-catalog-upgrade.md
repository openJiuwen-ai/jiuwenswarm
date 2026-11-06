---
id: B_20261103_1636_builtin-catalog-upgrade
title: 旧内置技能未归一与应用市场入口不明显
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/server/runtime/skill
  - jiuwenswarm/resources/agent/workspace/skills
files:
  - jiuwenswarm/common/builtin_skills.py
  - jiuwenswarm/server/runtime/skill/skill_manager.py
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/legacy_versions.json
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/SKILL.md
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/skill_display.md
  - tests/unit_tests/test_builtin_catalog.py
designs:
  - D-20261103-1123-builtin-skill-catalog
---

# 旧内置技能未归一与应用市场入口不明显

## 现象与证据

用户反馈前端仍显示多个内置技能，且找不到应用市场技能。分支 `codex/npc-minimal-prompts`、基线 `5609937ed`。通过当前 Web Gateway `skills.list` 确认 15 个 builtin 条目，其中包含总目录。工作区迁移记录已有 7 个名字，5 个已迁移能力又存在顶层副本。应用开发 `references/capabilities/workswarm-app-builder/SKILL.md` 仍存在，不是资源丢失。

## 根因

迁移仅接受收拢前单个修订的树指纹。对实际副本逐一核对 Git 历史后，8 个副本可验证为旧发行原版；docx-pro 有 6 个脚本与已核对版本不同，不能确认原版，保留用户资产。此前即使顶层副本指纹可信，只要 `.legacy/<name>` 已存在也不迁移，使重新安装的技能继续显示。SkillManager 长生命周期缓存 state，外部迁移后的数据不一定立即被列表看到。

总目录此前已包含应用开发路径，但独立技能卡片取消后，展示描述没有 app-market 检索词；已有总目录启动补齐不覆盖，导航不能随新种子更新。

## 修复与边界

- 在 `legacy_versions.json` 记录 8 个完整 Git 修订及对应历史资源树指纹，计算时读取 Git tree/blob、归一文本换行；每项再与实际副本树核对。不将未知修改的摘要加入可信原版表。
- 可信重复副本在 `.legacy/` 下分别归档，保留内容和中断恢复能力。未知修改保留原路径与启用状态，以 local 来源独立显示；不删除自定义技能来减少卡片数量。
- 列表在已确认安装内置总目录时重载磁盘 state，并加锁重试迁移。原有扫描前补登记本地技能的行为保留。
- 仅对精确匹配旧发行指纹的总目录导航文件进行更新，并归档旧导航；用户编辑的导航不覆盖。展示文案与总入口明确 App Market / app-market → workswarm-app-builder。没有重装子技能或修改前端 JSX。

## 验证

Windows，项目 `.venv` Python 3.12.13，未运行全量 UT、格式化或静态检查。

定向执行 `pytest tests/unit_tests/test_builtin_catalog.py tests/unit_tests/test_app_market_skill.py -q --no-cov`：初次 10 passed、1 failed。失败为新增导航用例使用未归一的 CRLF 计算测试指纹，修正测试构造后单独重验 1 passed。覆盖历史发行迁移、同名归档冲突、旧状态重载与重新安装副本、修改副本按 local 展示、可信导航更新与用户导航保留；原有发现器、按需加载与应用技能契约用例通过。

真实工作区修复前备份 `skills_state.json` 到总目录 `.legacy/`；调用当前安装补齐/迁移实现后，builtin 安装记录只剩 workswarm-builtin，累计迁移 15 个名字，docx-pro 作为本地副本保留。源码与原文未删除。

Web Channel 不暴露 `skills.sync.reload`，首次直接 WS 通知得到 METHOD_NOT_FOUND；改用产品已有 `_call_agent_skill_rpc` 经 AgentServer 的一次性 RPC 连接通知 `SKILL_SYNC_RELOAD`，传 `applied: []`，只重载缓存，不重建 Agent 实例。随后从真实 Web Gateway 再次查询 `skills.list`：builtin 列表严格为 `[workswarm-builtin]`，docx-pro 来源为 local，总目录描述含 App Market。没有重启宿主或中断会话。浏览器页面最终渲染及真实模型自主检索未验证，不将列表 RPC 证据描述为 UI 点击验收。

## 交接

当前运行数据已修正，刷新技能面板可获取新列表。持续处理以后旧安装残留的代码需要服务下一次重启加载；当前进程的资源与状态已通过既有接口刷新。本次修复尚未提交。当前契约见 `S_01_builtin-catalog`，初次实施见 `F_20261103_1149_builtin-skill-catalog`。
