---
id: B_20261127_1452_builtin-catalog-direct-naming
title: 内置能力总目录未列出子技能名称导致点名失效
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules: [jiuwenswarm/resources/agent/workspace/skills]
files: [jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/SKILL.md, tests/unit_tests/test_builtin_catalog.py]
designs: [D-20261103-1123-builtin-skill-catalog]
---

# 内置能力总目录未列出子技能名称导致点名失效

## 现象与根因

用户直接点名某个内置能力（如“用 xlsx 整理表格”“用 swarmskill-creator 做团队技能”）时，模型无法定位到实际技能。

收拢为单一入口后，`skill_mode: all` 只注入总目录 `workswarm-builtin` 的一条 `description`。原描述按任务类别说明用途（Skill、Agent、插件、文档、图像、检索等），既没有列出 37 个子技能的名称，也没有一句话简介，`xlsx` 这类名字不在提示词里，类别词也无法唯一映射到具体能力；模型若直接按名字调用 `skill_tool(skill_name="xlsx")`，core 只按已注册顶层技能解析，返回 `Skill not found: xlsx`。子技能自身的 `description` 与 `references/catalog.md` 只有在模型先读取总目录后才可见，第一跳缺失。

## 修复与决策

- 总目录 `SKILL.md` 的 `description` 在原有类别说明后按目录顺序列出全部 37 个能力的名称与一句话简介（简介取 `references/catalog.md` 的适用任务短句，每条不超过 24 字），让模型在只看到总入口时也能按名称与用途判断相关性。描述 1401 字符，约为收拢前 37 条描述合计 9323 字符的七分之一；不为此追加“点名即读”之类的特殊说明，具体读取方式仍由 `SKILL.md` 正文与 `references/catalog.md` 承担。
- `SKILL.md` 正文与 `references/catalog.md` 保持原样，不新增顶层技能，发现边界仍由一个 `SKILL.md` 决定。入口简介与导航表同出一源，改导航文案时同步入口。
- 新增发布校验：`description` 中的 `名称（简介）` 集合必须等于 `references/capabilities/*` 目录集合，每条简介长度在 4–24 字之间，描述整体不超过 1800 字符；另有渲染级测试用真实 `SkillUseRail` 生成 `all` 模式提示词，确认只有一条技能、且能读到代表性名称与简介。
- 升级沿用既有导航刷新规则：`SKILL.md` 属于可信导航文件，未修改的已安装副本在下次启动按 `builtin_catalog_installed_digests` 刷新；用户编辑过的副本不覆盖。

未采用“把子技能重新注册为顶层技能”或“按子技能名解析 `skill_tool`”的方案：前者恢复每个技能一条描述的注入量，后者需要修改 core 的 `SkillUseRail` 解析逻辑，与单一入口契约冲突。

## 回归证据

`tests/unit_tests/test_builtin_catalog.py` 25 项通过（`-o addopts=""`），包含新增断言：真实包资源安装后只有 `workswarm-builtin` 一个发现入口，`catalog.md` 链接与实际能力目录一致，`description` 的 `名称（简介）` 集合覆盖全部 37 个能力、简介长度 4–24 字、整体不超过 1800 字符。渲染级测试用 `SkillUseRail` 与 stub 文件系统生成 `all` 模式提示词，确认注入条目含 `xlsx（Excel、表格、公式与图表）`、`swarmskill-creator（团队技能、SwarmFlow、多角色工作流）`，且整段小于 1700 字符。相关套件（UI 投影、系统插件技能资源、技能启停、`test_utils`）91 项通过。未运行完整测试套件；模型侧定位效果的端到端验证需真实会话。
