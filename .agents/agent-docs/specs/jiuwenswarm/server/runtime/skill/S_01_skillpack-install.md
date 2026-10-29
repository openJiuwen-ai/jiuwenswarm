---
id: S_01_skillpack-install
title: SkillPack 安装与库存规约
type: S
author: 霆龙(吴建宇)
author_email: wujianyu@xiaohongshu.com
modules:
- jiuwenswarm/server/runtime/skill
- jiuwenswarm/channels/web/frontend/src/components/SkillPanel
files:
- jiuwenswarm/server/runtime/skill/skillpack_install.py
- jiuwenswarm/server/runtime/skill/skillpack_inventory.py
- jiuwenswarm/server/runtime/skill/skillpack_lock.py
- jiuwenswarm/server/runtime/skill/skill_manager.py
- jiuwenswarm/channels/web/frontend/src/components/SkillPanel/SkillPackInstallPlan.tsx
- jiuwenswarm/channels/web/frontend/src/components/SkillPanel/skillPackInstall.ts
designs:
- symphony-design-docs:SDD-0013
design_note: 依据 symphony-design-docs 仓库 proposals/SDD-0013-jiuwenswarm-skill-pack-productization/design.md（v9）；设计仓库：https://gitcode.com/lin-xiaoyu/symphony-design-docs。
---

# SkillPack 安装与库存规约

| 项 | 值 |
| --- | --- |
| 类型 | spec |
| 关联模块 | `jiuwenswarm/server/runtime/skill`、现有 SkillPanel 与安装入口 |
| 修订日期 | 2026-10-26 |
| 设计依据 | SDD-0013 v9 §4.2.4、§4.3.2、§4.3.4 |

## 类型和锁定

标准 Pack 根包含 `kind: skillpack` 和有序普通 Skill 成员。根、成员、发布和库存健康检查共用实际文档类型判定；SwarmSkill、嵌套 Pack、缺失或非法文档均不作为普通成员。Hub 标签仅触发严格检查，不能替代根和 lock。

`skillpack.lock.json` schema 为 `openjiuwen.skillpack.lock/v1`，含 Pack 名称及版本、有序成员 `name / asset_id / version / artifact_sha256`。名称与顺序须与根一致，资产 ID 唯一，版本不得使用范围，摘要为原始 ZIP 的 SHA-256。Pack ZIP 的内部技能根只含 `SKILL.md` 与 lock。

## 准备与确认

沿用 `skills.teamskillshub.install` 和 `skills.online.search.install`。准备请求下载并验证根，使用同一 Hub 匿名精确版本接口确认成员可公开下载，所有 artifact 请求设 `is_cli_download=true`。返回 `confirmation_required=true`、一小时有效的 `plan_id`、`can_install` 和逐项 `reuse/download/blocked`。此阶段不登记技能、不重建 Agent。

再次提交原资产标识、`plan_id` 和 `confirmed=true` 才执行安装。后端复核计划的 Hub、资产及根快照，重新查询全部精确版本、原始摘要和本地状态。来源或版本冲突、成员被禁用、文件被改动均要求重新准备，不覆盖现有数据。

新安装内容先进入临时目录，校验 ZIP 安全边界、根类型和哈希后获取同一库存锁。锁内再次复核状态，备份登记文件，写入事务日志，先放入成员、最后放入 Pack 根，再登记状态和索引。异常统一恢复；进程重启时在暴露库存前恢复未完成事务。该保证覆盖进程中断，不声明断电后文件系统持久性。

## 状态与卸载

独立成员保存 `.archive` 首装副本和 Hub 来源信息。ZIP 摘要证明取得的原始制品，内容摘要用于检测用户后续本地修改，两者不可互换。

仅当 Hub、资产 ID、精确版本、ZIP 摘要、本地内容和启用状态一致时复用。一个成员可被多个 Pack 引用；同名不同版本或来源不能并存覆盖。成员改变时，所有引用 Pack 的聚合状态随实时检查更新。

卸载 Pack 保留成员。普通 Skill 仍被标准 Pack 引用时返回 `SKILL_REFERENCED_BY_PACK` 和引用方列表。引用关系从实际根文档重建。安装、导入、启停、卸载和库存快照使用统一锁；网络下载不持有锁。

## 前端与限制

四个既有安装入口共用确认弹窗：技能广场、TeamSkillsHub、在线搜索及专家管理。阻断项禁止确认，取消不报告成功。安装成功后才刷新列表及 Agent。详情显示成员版本和来源，沿用“我的技能”和任务技能 Picker。

`SKILLPACK_PRODUCTIZATION_ENABLED` 默认关闭。旧 Hub 原生依赖审核、自动升级和多版本并存不在本期；客户端验证不能代表 Hub 已完成闭包审核。检索使用实时库存快照和既有技能目录，不新增独立 Pack 索引。

## 验证入口

- `tests/unit_tests/agentserver/test_skillpack_product_install.py`
- `tests/unit_tests/agentserver/test_skillpack_backend.py`
- `tests/unit_tests/agentserver/test_skills_uninstall_lightweight_reload.py`
- `tests/unit_tests/agentserver/test_skillnet_install_session.py`
- 前端 `tests/skillPackInstall.test.mjs`
