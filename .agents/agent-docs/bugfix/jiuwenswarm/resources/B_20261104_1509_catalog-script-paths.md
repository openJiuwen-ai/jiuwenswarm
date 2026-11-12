---
id: B_20261104_1509_catalog-script-paths
title: 内置能力搬迁后的生成目录与 PPT 同步路径回归
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/resources/agent/workspace/skills
files:
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/skill-omni-creation/scripts/common.py
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/skill-omni-creation/scripts/save_images.py
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/skill-omni-creation/scripts/finalize_scripts.py
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/skill-omni-creation/SKILL.md
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/skill-omni-creation/agents/code-writer.md
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/ppt-creation/scripts/sync-runtime-skill.js
  - tests/unit_tests/resources/test_catalog_script_paths.py
designs:
  - D-20261103-1123-builtin-skill-catalog
---

# 内置能力搬迁后的生成目录与 PPT 同步路径回归

## 现象与根因

在 `96fe7b777` 相对第一父提交 `b74891a6c` 的检视中确认：能力下移到总目录后，omni 的 save_images/finalize_scripts 仍用固定三级父目录作为输出根，生成物进入 capabilities 而被父 SKILL.md 的发现边界遮蔽。PPT 同步脚本继续更新旧顶层 ppt-creation，校验也对着旧副本，因此显示 ALL IN SYNC 但实际使用的总目录副本没有更新。manifest 根 key、子 key 与文件 path 也未统一迁移。

## 修复决定

omni 两个脚本共用默认技能库解析：优先宿主 `get_agent_skills_dir()`；独立环境从脚本祖先定位 skills，不依赖目录层数；显式 --skills-dir 可指定其他输出。调用说明以 save_images 的 SKILL_DIR 输出为后续写入事实源。

PPT 的复制、manifest 更新与校验共用同一 catalog 路径，修正包内及用户 manifest 的祖先层级，迁移旧 key、去重同名记录并修正已存在文件的 path。保留既有同步资源范围和运行目录管理策略，不删除旧用户技能资产。

## 验证

Windows、项目 .venv Python 3.12.13，基线 `96fe7b777` 的当前工作区。`tests/unit_tests/resources/test_catalog_script_paths.py` 两项行为测试随本轮定向集执行，整体 `17 passed in 11.48s`。omni 使用复制后的真实 common/save_images/finalize_scripts 主入口，在临时技能库独立读取输出目录、stage03 和脚本保留/删除结果；只替换外部环境安装门禁。PPT 通过真实 Node 子进程在临时 home 执行同步，独立读取正在使用的副本与两份 manifest，断言不存在额外顶层技能。

后续在同一 PPT 用例加入旧新 key 并存的去重场景，单独重验该用例，结果 `1 passed in 1.63s`。没有访问真实用户工作区，没有网络或真实模型调用；未运行全量 UT、格式化或静态检查。普通源码、已安装资源更新仍遵循既有安装策略，本轮未操作用户已安装的目录。
