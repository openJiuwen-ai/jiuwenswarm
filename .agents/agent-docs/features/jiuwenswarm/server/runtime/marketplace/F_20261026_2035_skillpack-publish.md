---
id: F_20261026_2035_skillpack-publish
title: SkillPack 成员优先发布
type: F
author: 霆龙(吴建宇)
author_email: wujianyu@xiaohongshu.com
modules:
- jiuwenswarm/server/runtime/marketplace
- jiuwenswarm/channels/web/frontend/src/components/AssetPublishDrawer
files:
- jiuwenswarm/server/runtime/marketplace/skillpack_publish_service.py
- jiuwenswarm/server/runtime/marketplace/skillpack_publish_hub.py
- jiuwenswarm/server/runtime/marketplace/asset_publish_adapters.py
- jiuwenswarm/server/runtime/marketplace/asset_publish_models.py
- jiuwenswarm/channels/web/frontend/src/components/AssetPublishDrawer/index.tsx
designs:
- symphony-design-docs:SDD-0013
design_note: 依据 symphony-design-docs 仓库 proposals/SDD-0013-jiuwenswarm-skill-pack-productization/design.md（v9）；设计仓库：https://gitcode.com/lin-xiaoyu/symphony-design-docs。
---

# SkillPack 成员优先发布

| 项 | 值 |
| --- | --- |
| 日期 | 2026-10-26 |
| 设计 | SDD-0013 v9，当前 Hub 兼容发布阶段 |
| 范围 | Marketplace 发布服务及 Web 发布抽屉 |
| 规约 | [SkillPack 发布规约](../../../../../specs/jiuwenswarm/server/runtime/marketplace/S_01_skillpack-publish.md) |

## 实际实现

在普通资源发布入口增加 `skillpack` 分支。预检识别可复用成员、需先发布的本地普通 Skill 和阻断项；用户一次确认后，持久化协调器发布成员、等待成员审核，再生成 thin Pack lock/ZIP 并发布。前端沿用详情发布按钮和现有抽屉，增加成员计划与阶段展示。

新增专用 Pack adapter，普通 adapter 明确拒绝 Pack。`PublishRequest.transport_profile` 在最后上传响应校验层区分旧 Hub 的 `skill` 和后续原生 Hub 的 `skillpack`，本地类型始终不变。发布来源、版本与原始 ZIP 摘要保留在协调器记录。

## 决策与理由

- 复用 `AssetPublishService` 的有界快照、确定性 ZIP、一次上传和持久化幂等任务，减少普通发布行为分叉；另用协调器保存多成员审核进度。
- 使用 Hub 已有 `/api/v1/auth/me` 验证的账号 ID 派生 Pack scope，避免 token 刷新导致任务无法恢复。凭据只保留在当前后台任务内存中。
- 公开资格用匿名精确版本读取验证。用户自己的认证读取可能看到尚未公开版本，不能作为 Pack 公开依赖已就绪的证据。
- 根文档在预检时保存私有快照；成员 ZIP 也在确认前生成。后续再读工作区只用于发现内容变化，不用未确认的新正文替换发布内容。
- 审核等待没有有限时间保证；重启后随用户重新打开记录恢复。未保存认证凭据，不能承诺重启后无登录自动完成。

## 未采用的做法

- 不把成员源码复制到 Pack：独立资产保留作者、版本与审核关系，共享成员安装由安装器处理。
- 不探测 capability 404 后自动切换类型：本期由部署配置锁定，避免网络错误或服务异常改变资产类型。
- 不在上传响应丢失时重新 POST：先查本人版本并校验原始摘要，仍无法确认则等待核对。
- 不回滚已经发布的成员：它们是用户确认过的独立公开资产，Pack 后续失败不授权删除。

## 实际验证

Python 3.11.6，使用新 Swarm/Core 源码路径与已有只读测试环境运行。发布 adapter、builder、service、store、API、wire、HubClient 与 SkillPack 用例组合 **220 项通过**；其中新增 SkillPack 用例 **14 项**，覆盖真实 SQLite 重新连接、上传前进程中断恢复、审核阶段恢复、未知结果核对、来源复用和内容变化后重新确认。

Phase A 契约 E2E 使用模拟 Hub、真实发布协调器和 ZIP builder：用户 A 先发布成员再发布 Pack，将生成的原始 ZIP 交给真实 `SkillPackInstaller`，在用户 B 的新库存确认并原子安装。验证根 lock、独立成员目录、原始制品及本地内容摘要、归档副本；确认前没有写入目标 Skill。

持久化阶段变更记录仅输出 operation ID 与 stage；同阶段轮询不会重复打印，不记录正文或认证凭据。

前端 `npm run test:asset-publish-ui` **17 项通过**，新增用例验证一次成员计划、仅公开范围、确认后审核进度和表单锁定。未运行格式化或静态检查。

## 验证边界

2026-10-27 补充真实隔离环境验收：使用 SkillHub develop、MySQL 和 MinIO，完成成员先发布与人工审核、Pack 发布与人工审核，以及同机独立新用户通过 WorkSwarm 技能广场下载、校验 lock、安装并确认直达执行。测试使用本地身份提供方，未向正式市场上传；生产 OAuth、自动安全扫描和跨机器可移植性尚未验证。

当前默认关闭功能，发布还需要显式声明隔离市场或版本门禁。后续原生 Hub 后端和原生审核/详情页面不在此次改动内。审核等待和上传结果未知的自动恢复由行为测试覆盖，不把单次真实链路通过扩大为全部故障场景已联调。
