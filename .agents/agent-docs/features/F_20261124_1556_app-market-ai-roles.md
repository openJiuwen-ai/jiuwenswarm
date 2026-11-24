---
id: F_20261124_1556_app-market-ai-roles
title: 应用市场 AI 角色清单与接入指引
type: F
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/extensions/app_market
  - jiuwenswarm/server/runtime/marketplace
  - jiuwenswarm/channels/web/frontend
  - jiuwenswarm/resources/agent/workspace/skills
files:
  - jiuwenswarm/common/builtin_skills.py
  - jiuwenswarm/extensions/app_market/app_ai_roles.py
  - jiuwenswarm/extensions/app_market/manager.py
  - jiuwenswarm/extensions/app_market/extension.py
  - jiuwenswarm/server/runtime/marketplace/asset_publish_adapters.py
  - jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/AppMarketAiRoles.tsx
  - jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md
designs: []
design_note: 依据对话中确认的 Skill 优化、角色清单及已安装应用详情范围实施，沿用现有发布和应用会话契约。
---

# 应用市场 AI 角色清单与接入指引

## 实施决策

- 单角色接入指南补齐就绪与停止、WebChannel 请求和响应、取消与重试。Skill 校正当前 Gateway 的参数与返回语义，并按静态、单角色 AI 和 Team 的能力组织验收。
- 已确认的角色清单方案作为应用包内元数据。同一校验器在本地登记和 Hub 预检快照读取清单。有效清单生成现有 `ai` 声明，缺少清单时兼容旧应用。
- 本地及已安装 Hub 应用的详情经 `app_market.ai_roles` 懒加载角色卡片。后端按连接所有者读取清单并返回公开字段与受限大小的头像。部署包更新时只刷新匹配可信旧发行指纹的应用开发入口，已自定义的文件保留。
- 角色清单不接管运行授权与会话状态。安装前详情沿用 Hub 现有版本信息。契约见 [应用分发](../specs/jiuwenswarm/extensions/app_market/S_03_app-hub-distribution.md) 和 [内置能力升级](../specs/jiuwenswarm/server/runtime/skill/builtin-catalog.md)。

## 验证

- Windows 项目 `.venv`，候选基线 `816ee815b`，锁定 Gateway `77c62403`。市场发布及 Skill 相关 pytest 定向用例 24 项通过，能力目录升级用例 6 项通过。覆盖所有者隔离、图片和纯文字角色、无清单兼容、无效清单拒绝以及实际上传请求中的包装 ZIP。Hub 上传使用模拟 HTTP 端点。
- `npm run test:app-market` 81 项通过。覆盖详情按需请求、Team 会话说明、队长标签、角色卡片和头像回退。
- 内置 Chromium 使用模拟应用详情响应核对中文详情页，显示「AI 角色」、团队会话说明、角色卡片和名称头像。
- 审查修订补充 UTF-8 文本校验。Windows 项目 `.venv` 定向 pytest 4 项通过，覆盖未配对 Unicode 代理字符在详情与本地发布入口被拒绝、旧登记保留、补充平面字符正常返回，以及 Hub 发布快照兼容。
- 基于 `upstream/develop` 的 `d5bc9f237` 解决冲突后，Windows 项目 `.venv` 定向 pytest 11 项通过，覆盖内置能力升级与自定义保留、AI 角色详情、无效清单拒绝、Hub 应用转换和发布快照。
- 本次未连接真实模型或实际 Hub 发布服务。
