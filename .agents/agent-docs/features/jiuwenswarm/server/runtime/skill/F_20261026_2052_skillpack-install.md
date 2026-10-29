---
id: F_20261026_2052_skillpack-install
title: SkillPack 安装与库存保护实施记录
type: F
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

# SkillPack 安装与库存保护实施记录

## 关联依据

- 设计：SDD-0013 v9，本期旧 Hub 兼容分发与本地使用。
- 规约：[安装与库存规约](../../../../../specs/jiuwenswarm/server/runtime/skill/S_01_skillpack-install.md)。
- 日期：2026-10-26。
- 范围：成员 lock、精确原始制品下载、确认计划、暂存和恢复、来源/内容校验、共享成员保护，以及现有前端入口。

## 实现与决策

统一使用 `skillpack_lock.py` 解析发布方和安装方锁定格式。普通 ZIP 摘要与工作区内容摘要分别保存，前者校验下载身份，后者识别本地改动。匿名 Hub 精确版本访问用于证明普通用户可下载，避免把发布者可见的待审版本当作公开成员。

`SkillPackInstaller` 准备阶段生成私有计划，不登记技能；确认后重新校验 Hub 与实时本地状态。全部下载通过检查后持有库存锁，保存事务日志，先提交独立成员、最后提交 Pack 根，再严格登记状态与索引。异常恢复；启动恢复未完成日志。既有普通 Skill 入口保持行为，安装计划返回不会重建 Agent。

复用既有 SkillManager / My Skills / Picker 类型与详情基础，增加成员锁定版本和安装确认弹窗。技能广场、在线搜索、TeamSkillsHub、专家管理共用确认流程。Pack 卸载保留成员，成员卸载受引用保护。

检视补齐了 SkillNet 后台写盘的库存锁，禁止强制覆盖被标准 Pack 引用的成员。普通 Hub 安装和本地导入的最终写盘也使用同一锁，下载阶段不持锁。

## 验证

2026-10-26 使用本分支候选源码及配套 agent-core 源码：

- 最终组合回归：`tests/unit_tests/symphony`、`tests/unit_tests/server/marketplace` 与 SkillPack、Hub 安装、SkillNet、在线搜索、Symphony 确认/超时相关 agentserver 测试，共 **730 通过**。
- 前端 `test:skillpack-install` **7 通过**，`test:asset-publish-ui` **17 通过**，文案测试 **4 通过**；Vite 生产构建通过，仅已有大体积 chunk 提示。
- 覆盖成员类型伪装、摘要不符、来源/版本冲突、确认后状态变化、登记失败回滚、中断恢复、共享引用、真实 SkillManager 安装入口，以及模拟 Hub 上发布器到安装器的契约链路。
- 安装确认截图由实际组件和明确标注的模拟数据生成，未连接真实 Hub。

扩展运行既有 `test:asset-publish` 为 **10 通过、1 失败**。独立 HEAD 源码重新构建仍同样失败：冷缓存重试上限为 150，旧测试循环 31 次便预期耗尽。相关源码及测试与 HEAD 完全一致，本次不修改该无关问题。

## 交付限制

2026-10-28 更新：当前 pin/lock 已切换配套 Core PR 的可解析 fork 提交，正式合入前仍需切回官方仓库中的对应提交。2026-10-27 已完成真实隔离 Hub 发布、人工审核、同机独立新用户下载安装及真实模型直达执行；生产 OAuth、自动安全扫描及跨机器/桌面平台未验收。Hub 下一版本原生依赖审核仍在本期之外。

事务支持进程中断恢复，未提供断电持久性承诺。没有版本门禁的旧 Hub 只能在隔离环境验收。前端仍使用现有通用确认/问题组件，正式 UCD 可继续在相同契约上调整样式。
