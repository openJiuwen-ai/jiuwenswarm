---
id: F_20261103_1149_builtin-skill-catalog
title: 内置能力总目录实施记录
type: F
author: kdgmpw
author_email: lishuqi4@huawei.com
modules: [jiuwenswarm/resources/agent/workspace/skills, jiuwenswarm/common, jiuwenswarm/server/runtime/skill, jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/channels/web/frontend]
files: [jiuwenswarm/common/builtin_skills.py, jiuwenswarm/common/utils.py, jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/server/runtime/agent_adapter/user_turn.py, tests/unit_tests/test_builtin_catalog.py]
designs: [D-20261103-1123-builtin-skill-catalog]
---

# 内置能力总目录实施记录

## 关联依据与范围

用户 2026-11-03 明确授权实施并直接提交中文 commit。设计为 [D-20261103-1123-builtin-skill-catalog](../designs/active/D-20261103-1123-builtin-skill-catalog/design.md)，当前契约见 [内置能力目录规约](../specs/jiuwenswarm/server/runtime/skill/S_01_builtin-catalog.md)。实施基于 `codex/builtin-skill-catalog`、父提交 `b74891a6ca533b1ddce862eb84079ef1ea1c06d1`。

## 实际实现与取舍

- 将 35 个包内能力及配套资源移动到 `workswarm-builtin/references/capabilities/`，总目录负责分组导航与按需读取，实际说明仍独立维护。同步固定 Unix/Windows 路径、创建入口、资源测试路径、agent-data 导航和中英文文档。移除 project-maintainer 旧路径的打包排除项，使本次全量目录包含该能力。
- 总目录提示词直接声明应用开发适用于 code 模式，并区分选择总目录、选择应用开发、授权发布；子技能同步声明。没有新增任何模式过滤、Rail 门禁或发布接口限制。
- 默认安装和补齐只安装总目录。首次复制使用临时目录，工作区锁串行处理；现有目录不被启动补齐覆盖。
- 迁移按旧发行内容指纹及 builtin 来源识别，未改动副本归档于父技能 `.legacy/`。不删除用户修改或来源不明副本；旧版本差异不能确认时保留。保存禁用偏好给模型读取；旧名称显式选择补充总目录，保持原任务意图。
- 原有 Skill Creator 仍承担专项 creator 路由，改成同包文件读取。没有增加另一套模型选择器。

## 与设计的偏差和限制

保留旧资源的方式采用总目录内部 `.legacy/` 归档而不是删除，便于核对与中断恢复；它处于父 SKILL.md 边界内，不额外注册。没有尝试为历史版本生成未经核实的指纹，因此内容不匹配的旧副本可能继续作为独立项存在。逐子技能禁用迁入模型可读偏好，无法承诺旧独立执行开关相同强度的拦截，这是提示词方案的已知取舍。

此前应用市场检视的身份缺失、脚本覆盖、数据根隔离、运行态重新打开入口不属于本次 skill 收拢范围，未宣称修复。

## 实际验证

环境：Windows、项目 `.venv` Python 3.12；本地 core 安装的 `direct_url.json` 对应锁定提交 `b2a0a248c554753ba31727415fe17a03eed60b46`，未修改 core。

1. 定向运行 `test_builtin_catalog.py` 最初 6 个行为用例、`test_app_market_skill.py` 和 `test_utils.py` 的两个安装用例，8 通过、1 失败。失败为原应用开发测试使用系统 GBK 解码中文；增加明确 UTF-8 后单独重跑该用例通过。
2. 增加实际 core SkillTool 调用与模型可见 `data.content` 的按需读取验证。模拟文件系统适配器读取真实资源；初次测试断言错误地使用子技能并未完整拼写的环境变量名，改为已有会话契约的断言后通过。总入口读取只请求 SKILL.md，包含 code/发布约束；显式请求子路径后才读取子技能正文。
3. 已证明安装/补齐单入口、两种实际发现器不展开子技能、35 条目录链接可读、子脚本存在、来源/内容识别、禁用偏好、修改与同名本地副本保留、重复迁移、移动后中断恢复和复制失败不迁移。
4. 执行 `node jiuwenswarm/scripts/generate-agent-folders.js` 更新本地资源导航（该生成文件在现有仓库中被忽略，不纳入提交）。
5. 通过现有 setuptools 后端实际构建 wheel，检查产物包含总入口、目录、35 个能力入口、应用发布脚本与 project-maintainer。构建产物仅放临时目录，未提交。未运行格式化、静态检查或全量 UT。

上述通过用例共 10 个不同场景。实际模型自主选择、非 code 场景遵循率、浏览器点击与团队模型执行未做端到端验证；只证明资源结构、工具可见文本及本地迁移行为，不声称完成真实模型或浏览器验收。

## 交付与复查

2026-11-03 后续修复：实际工作区中 8 个历史原版未被单版本指纹覆盖，重复安装副本又因已有归档留下独立卡片；修改副本继续以 builtin 来源展示，总目录导航升级也未显式提供 app-market 检索词。修复及真实运行服务取证见 `B_20261103_1636_builtin-catalog-upgrade`。此前“内容不匹配的旧副本可能继续作为独立项”的历史验证结论保留，当前可验证旧发行已扩展迁移范围，无法确认原版的副本改以本地技能保留。

复查重点是新的目录资源均进入提交与打包范围、已有用户资产无损、提示词加载边界和新旧创建入口。状态与索引由文档同步工具维护；提交不包含真实配置、密钥、日志或覆盖率产物。实际提交 SHA 由 Git 提交结果提供，不在提交前编造。
