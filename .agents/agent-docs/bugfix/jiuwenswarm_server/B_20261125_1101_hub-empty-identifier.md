---
author: ZHANgPP_
author_email: zhangpu161@163.com
id: B-20261125-1101-hub-empty-identifier
title: 技能广场空 identifier 条目导致前端 key 与安装态串扰
type: B
modules: [jiuwenswarm/server/runtime/skill, jiuwenswarm/channels/web/frontend/src/components/SkillPanel]
files: [jiuwenswarm/server/runtime/skill/skill_manager.py, jiuwenswarm/channels/web/frontend/src/components/SkillPanel/skillPanelUtils.ts]
designs: []
design_note: 独立缺陷修复，无设计文档。
---

# 技能广场空 identifier 条目导致前端 key 与安装态串扰

日期：2026-11-25。

## 现象与根因

在线搜索聚合（`_normalize_online_search_item`）对 SkillNet/ClawHub/TeamSkillsHub 三个来源的
identifier（`skill_url||url` / `slug` / `asset_id||identifier`）缺失时回落为空字符串；聚合处仅过滤
"identifier 与 name 都空"的条目，有名无 id 的半残条目照常下发。前端 `hubMarketplaceItemKey` 产出
`{source}:` 形态的重复 React key（列表状态复用错乱），且安装态匹配 `install:` 前缀命中全部空 id
卡片同时进入 installing 态，并发出 `identifier: ""` 的必失败安装请求。

根因是把 identifier 当展示字段兜底，而它实际是安装句柄：来源数据缺 id 字段时该条目不具备可安装性，
归一化层应丢弃而不是带病下发。

## 修复决策

- 聚合层（`_aggregate_online_search_results`）跳过空 identifier 条目，日志保留来源上下文便于
  排查第三方数据质量。
- 前端 `hubMarketplaceItemKey` 增加 `skill.name` 兜底，防止未来其它通道再产出空 id 时 key 碰撞；
  后端过滤是唯一权威约束，前端仅作渲染层防御。

## 回归证据

- `tests/unit_tests/agentserver/test_skill_manager_online_search.py` 新增
  `test_online_search_drops_items_without_install_identifier`：无 URL 的 SkillNet 条目与无 slug 的
  ClawHub 条目被过滤，正常条目保留。20 passed。
- 前端 `npm run test:skill-marketplace` 4 passed；`tsc --noEmit` 通过。

## 遗留

`skills.swarmskillshub.recommend` 推荐路径的条目映射未复用同一归一化，`asset_id` 缺失时列表 key
仍有同源碰撞可能（低危，React 警告级），待该路径接入统一归一化时一并处理。
