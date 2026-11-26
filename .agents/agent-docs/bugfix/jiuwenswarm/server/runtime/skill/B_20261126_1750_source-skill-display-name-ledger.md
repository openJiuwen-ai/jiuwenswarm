---
id: B_20261126_1750_source-skill-display-name-ledger
title: 企业源技能广场改名后账本 display_name 冻结及前后端展示优先级不一致
type: B
author: meizi6
author_email: meizi1@huawei.com
modules:
  - jiuwenswarm/server/runtime/skill
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/server/runtime/skill/skill_manager.py
  - jiuwenswarm/channels/web/frontend/src/components/SkillPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/components/SkillPanel/SkillDetailView.tsx
  - jiuwenswarm/channels/web/frontend/src/features/EnterpriseSkillSourcePanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/features/EnterpriseSkillSourcePanel/installMetadata.ts
  - jiuwenswarm/channels/web/frontend/src/components/SkillPanel/skillPanelUtils.ts
  - jiuwenswarm/channels/web/frontend/src/components/SkillPanel/types.ts
  - tests/unit_tests/agentserver/test_skill_manager_enterprise_state.py
  - jiuwenswarm/channels/web/frontend/tests/enterpriseSkillSourceInstallMetadata.test.mjs
designs: []
design_note: 线上缺陷（Issue #5195）与检视反馈驱动的契约澄清与实现修复；无独立 D，依据现有企业技能源安装账本与 skills.list/get 行为。
---

# 企业源技能广场改名后账本 display_name 冻结及前后端展示优先级不一致

## 现象与影响

企业技能源首次安装时，广场卡片中文名写入安装账本的 `market_display_name`，并同步落到 `display_name`。广场侧改名后，用户以 `force=True` 重装/升级：`market_display_name` 更新为新标题，但 `display_name` 仍停在「广场初装名」。

读侧 `_apply_skill_installation_meta` 把陈旧 `display_name` 抄进 meta；`_promote_market_display_name` 又规定「非 slug 不覆盖」，于是 `skills.list` / `skills.get` 与容器包等只读 `display_name` 的消费方长期返回旧标题。

同期前端曾引入 `skillPresentationName`（`market || display || name`），与后端「Hub / local `display_name` 优先于 market」及单测 `test_hub_local_display_name_wins_over_market_on_list` 冲突，造成 API 与 UI 两套名字。

## 关联设计与当前契约

无独立设计文档。当前有效约定：

| 字段 | 角色 |
|---|---|
| `name`（slug） | 包/目录安全名，机器身份侧 |
| `display_name` | 列表/详情/前端主展示字段；权威输出 |
| `market_display_name` | 企业源安装时透传的广场卡片名；账本备份与读侧兜底，不是前端主读字段 |

优先级（读侧与产品展示）：

1. Hub / ClawHub / 本地导入写入的非 slug `display_name`（含 `local_skills`）优先。
2. 否则账本 `display_name`；若为空或仅等于 slug，由 `_promote_market_display_name` 用 `market_display_name` 填充。
3. 再否则回退 slug。

写侧（`record_skill_installation`）：在覆盖 `market_display_name` 之前保留上一版广场名；当 `display_name` 为空、等于 slug、或等于上一版 `market_display_name` 时，刷新为新广场名。与上一版广场名不同的 Hub/ClawHub 定制名不得覆盖。

前端：删除独立展示名 helper，列表/详情/安装 toast 只信后端已 promote 的 `display_name`（缺省再 `name`），不再消费 `market_display_name` 做展示优先级。

## 根因与修复决策

**根因 1（账本冻结）**：`record_skill_installation` 落盘守卫仅为 `not current_display or current_display == safe_name`。首次安装写入的广场中文名是非 slug，后续升级无法识别「仍等于上一版广场名」，误当成需保护的 Hub 定制名。

**修复**：覆盖 `market_display_name` 前读取 `previous_market`；允许在 `current_display == previous_market` 时刷新。保留「不等于上一版广场名」的定制 `display_name`。

**根因 2（前后端分叉）**：前端无条件 `market` 优先，与后端 promote / Hub 优先契约相反；且同面板仍有多处直接 `display_name || name`，helper 未成为单一入口。

**修复**：删除 `skillPresentationName.ts` 及 re-export；调用方改为 `display_name || name`。展示权威收敛到后端账本 + promote，避免双通道。

备选「前端改为非 slug display 优先再 market」未采用：在账本刷新已修复后，前端再维护一套与 promote 平行的优先级只会重复契约；只信 `display_name` 更短、与 API 一致。

## 回归证据与适用范围

后端（源码根，相关用例）：

```bash
uv run pytest \
  tests/unit_tests/agentserver/test_skill_manager_enterprise_state.py::test_record_refreshes_display_name_when_equal_to_previous_market \
  tests/unit_tests/agentserver/test_skill_manager_enterprise_state.py::test_record_keeps_non_slug_display_name_when_market_updates \
  tests/unit_tests/agentserver/test_skill_manager_enterprise_state.py::test_skills_list_promotes_market_display_name_for_source_install \
  tests/unit_tests/agentserver/test_skill_manager_enterprise_state.py::test_hub_local_display_name_wins_over_market_on_list \
  -q
```

结果：**4 passed**。覆盖「广场改名后 display_name 刷新」「Hub 定制名不被 market 覆盖」「仅 market 时 list/get promote」「local_skills Hub 名优先」。

前端：

```bash
cd jiuwenswarm/channels/web/frontend && npm run test:enterprise-skill-source-install
```

结果：**4 passed**（已移除原 `skillPresentationName` / market 优先用例）。

未覆盖：真实 SwarmSkillHub 联调改名重装、浏览器 UI 目视、旧账本仅有陈旧 `display_name` 且无机会再装时的一次性迁移。未跑格式化与全仓静态检查。

## 防错模式与资料同步

- 企业源广场标题变更必须能区分「上一版 plaza 名」与「Hub 定制名」；只认 empty/slug 不够。
- 跨端展示字段以后端 `display_name` 为唯一产品主读；`market_display_name` 仅账本/兜底，前端不另建优先级。
- 本文为契约与故障记忆；若后续抽长期不变量，可迁入 `specs/` 下 skill 模块 S，并回链本文。

## 遗留与后续

- 已安装且从未再 force 重装的技能，若账本 `display_name` 已冻结为旧广场名，不会自动纠正；需升级/重装或另行迁移脚本（当前未做）。
- Issue #5195 关闭条件以合入后的企业源改名重装验收为准。
