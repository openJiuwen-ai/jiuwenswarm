---
id: S_01_builtin-catalog
title: 内置能力目录规约
type: S
author: kdgmpw
author_email: lishuqi4@huawei.com
modules: [jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/resources/agent/workspace/skills]
files: [jiuwenswarm/common/builtin_skills.py, jiuwenswarm/common/utils.py, jiuwenswarm/server/runtime/skill/skill_manager.py]
---

# 内置能力目录规约

## 入口与资源

包内默认内置技能统一注册为 `workswarm-builtin`，37 个能力位于其 `references/capabilities/<name>/`，由 `references/catalog.md` 导航。总入口 `description` 按目录顺序列出全部能力的名称与一句话简介（简介取 `references/catalog.md` 的适用任务短句，不超过 24 字），使模型在只看到总入口时也能按名称与用途判断相关性；具体读取方式仍由 `SKILL.md` 正文与 `references/catalog.md` 承担，不额外为点名写专用说明。发布测试校验能力集合、简介长度与描述上限（1800 字符），保持单条注入。模型以文件读取方式按需加载，子技能的脚本和相对资源按子目录解析。用户、项目、在线市场与 MCP 技能保持各自发现语义。

Swarm UI 与默认安装只发布总目录；现有 Symphony 直接子目录扫描和 core SkillUseRail 的首个 SKILL.md 边界保证子技能不成为初始独立候选。读取总入口不会读取子技能正文。

应用开发适用于 code 模式；此规则和选择总目录不等于授权发布的规则维护在总目录提示词及应用开发子技能中。后端、Rail 和工具没有新增模式门禁。提示词约束不等同于强制权限隔离。

## 安装与升级

`_install_default_builtin_skills()` 同时承接初始化和启动补齐。首次复制先写临时目录再重命名，完整安装后才迁移旧副本；补齐不覆盖现有总目录。显式覆盖安装更新种子资源，保留 `.legacy/` 归档和迁移偏好。安装与迁移使用同一工作区的锁串行处理。

已安装且来源确认为 builtin 的总目录，在同一安装锁内补齐包内新增但本地尚不存在的能力叶子。每个叶子先复制到同文件系统的临时目录，再整体重命名发布；复制失败不留下可发现的半份能力，也不提前更新导航。已有能力叶子的整目录内容匹配可信发行指纹时，可整体更新并归档旧目录。按文件维护可信指纹的入口可按下述规则更新。保留用户修改。不重新创建顶层独立技能。`jiuwen-system-plugin-dev` 按此规则位于总目录 `references/capabilities/`，用于系统插件开发、修改、测试、打包及明确授权后的安装。

`legacy_hashes.json` 记录收拢前资源指纹，`legacy_versions.json` 补充可追溯 Git 修订的历史发行内容指纹。迁移只归档 state 中来源明确为 builtin、没有同名本地注册、内容和可信指纹一致的副本；文本换行差异与 Python 缓存不算用户修改。无法确认原版的修改副本保留原目录和启用配置，安装来源改为 local，记录 `legacy_builtin: true` 和 `builtin_catalog_customized`，不再占内置分组。符号链接不迁移；未登记或来源不明的资产不按 builtin 处理。

未修改副本移动到总目录 `.legacy/<name>/`，不删除原内容。成功后从 `installed_plugins` 移除旧独立安装记录，并将名字记录在 `builtin_catalog_migrated`。移动后提交 state 前中断，下一次通过归档指纹识别并继续；state 用临时文件替换提交。总目录尚未安装成功时不能迁移旧目录。

已存在归档时，可信的重复顶层副本移动到同一 `.legacy/` 下带内容摘要与序号的归档，保留两份内容，不因归档冲突保留独立卡片。恢复同时检查默认和带摘要的归档。`skills.list` 在总目录存在且来源确认为 builtin 时，先重载磁盘状态、在目录锁下补齐迁移，再重载后扫描；迁移失败保留旧资产并记录错误，不使列表整体不可用。

总目录的 `SKILL.md` / `skill_display.md` / `references/catalog.md` 仅在内容精确匹配可信旧发行指纹时更新导航，旧文件保存在 `.legacy/catalog-files/`；用户修改的导航不覆盖。总目录在内容和中英文展示描述中明确提供 App Market / app-market 的应用开发入口，子技能保持按需加载。

`workswarm-app-builder/SKILL.md` 沿用可信旧发行指纹更新。先放入缺失的单角色 AI 接入示例与角色清单规范，再替换旧入口并保留归档。用户修改的入口或与新指南同名的自定义内容保持原样。

禁用选择来自旧 `skill_configs` 或插件 enabled 标志，保留原记录并写入 `builtin_catalog_disabled` 和 `references/migration.md`，由总目录提示词要求模型遵循。子技能禁用在收拢后属于提示词偏好；总目录的整体开关仍用现有机制。

## 历史引用与限制

`UserTurn` 在显式指定已迁移且原路径不存在的旧名字时，补充总目录入口，同时保留旧名字表达的能力选择；已有用户同名目录不重定向。新建 Skill/Agent/专家团/插件的前端入口和知识转技能后台任务直接选择总目录。

未确认来源的副本保持原发现语义，内容改变且来源明确为 builtin 的副本作为本地保留项继续独立显示，这是保留用户资产的迁移边界。没有执行真实模型端到端行为验证时，不以提示词包含约束证明模型一定遵循。初次实施证据见 `F_20261103_1149_builtin-skill-catalog`，历史版本与列表修复见 `B_20261103_1636_builtin-catalog-upgrade`。

## 子技能脚本的路径契约

子能力迁入目录后，不得通过固定父目录层数推导用户技能库。`skill-omni-creation` 的保存与收口脚本优先调用 `get_agent_skills_dir()`，独立环境按 `skills` 祖先定位；显式 `--skills-dir` 覆盖默认目录。生成物位于用户技能库的独立 `<slug>/`，后续写脚本以 `SKILL_DIR` 为准，不写入总目录内部。

PPT 同步脚本的目标、manifest key、文件 path 与 hash 校验统一使用 `workswarm-builtin/references/capabilities/ppt-creation`。旧 manifest key 迁往新 key，同名文件去重并修正路径；报告同步成功必须对应实际使用的总目录副本，不额外创建顶层 `ppt-creation` 技能。修复证据见 `B_20261104_1509_catalog-script-paths`。
