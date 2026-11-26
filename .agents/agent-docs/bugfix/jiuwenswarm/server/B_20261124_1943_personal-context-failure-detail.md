---
id: B_20261124_1943_personal-context-failure-detail
title: 个人知识空间采集失败只显示「采集失败」，无法定位失败阶段与失败文件
type: B
author: openjiuwen_ai
author_email: openjiuwen_ai@163.com
modules:
  - jiuwenswarm/server/personal_context
  - jiuwenswarm/channels/web
files:
  - jiuwenswarm/server/personal_context/error_messages.py
  - jiuwenswarm/channels/web/frontend/src/components/PersonalContext/ServicesPanel.tsx
  - jiuwenswarm/channels/web/frontend/src/components/PersonalContext/ServicesPanel.css
  - jiuwenswarm/channels/web/frontend/src/services/personalContextApi.ts
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
  - jiuwenswarm/channels/web/frontend/tests/personalContextRunState.test.mjs
  - tests/unit_tests/personal_context/test_error_messages.py
designs: []
design_note: 只修采集失败诊断信息的透出与展示，不改变采集调度、存储契约和授权流程；无独立设计。
---

# 个人知识空间采集失败只显示「采集失败」，无法定位失败阶段与失败文件

## 现象与根因

#7008：本地文件夹采集失败后，卡片仅显示「采集失败 ⓘ」，下方只有
「本次新增 0 个节点（0 个文件）」。用户无法知道：

- 失败发生在扫描、抓取、切分、向量化还是写入阶段；
- 业务错误码（如 `154002`）与对应中文解释；
- 实际扫到多少项、成功多少、失败多少；
- 哪些文件失败、各自原因。

根因有两处：

1. `localize_payload` 对运行记录的 `last_error` 只保留本地化后的一句话。
   该字段在 Core 侧是 `"[154002] context proactive_file execution error,
   reason: ..."` 形式的字符串，本地化后业务码与阶段信息一起丢失，只留下
   「文件读取失败，请检查文件是否存在以及访问权限。」这类笼统提示。
2. Web 端把这句话塞进 `title` 悬浮提示，没有就地展示，也没有消费运行记录
   里已有的 `item_errors` / `omitted_item_errors` / 计数字段。

## 修复决策

- 后端只补「稳定码 + 阶段标识」，不把内部原因文本下发：
  - 新增 `_resolve_error_key` / `_resolve_error_code` / `describe_error`，
    复用原有 `localize_error` 的原因解析逻辑；
  - `localize_payload` 在为运行记录（含 `run_state`）投影时追加
    `last_error_code`（如 `154002`）与 `last_error_stage`
    （`scan` / `fetch` / `pipeline` / `publish` / `config` / `state` /
    `timeout` / `unknown`）。原 `last_error` 本地化文案保持不变，属于向后
    兼容的字段扩充，中文/英文文案仍由前端 i18n 决定。
- 前端在卡片内新增失败诊断面板（仅终端失败或存在失败项时展示）：
  - 失败阶段与业务错误码；
  - 已本地化的失败说明；
  - 「扫描 N 项 · 成功 N 项 · 失败 N 项」；
  - 失败项列表最多 5 条，超出用「另有 N 条失败项未展示」概括。
- 保持既有约束：错误文本仍走 `localize_payload` 白名单，不暴露原始
  `reason`、凭据或内部堆栈；`fetch_service_errors` 的字符串形态不变。

本次不覆盖 Issue 中的采集前大目录预估、排除规则配置与「仅重跑失败项」，
三者属于独立能力，需另行评估。

## 验证

- `pytest tests/unit_tests/personal_context/test_error_messages.py`：19 passed
  （新增运行记录码/阶段用例，断言原始 reason 不下发）。
- `node --test tests/personalContextRunState.test.mjs`：18 passed
  （新增失败面板阶段/错误码/计数/失败项/省略数用例，部分成功与成功回归）。
- `npx tsc --noEmit`：改动文件无类型错误；仓库内 `antd`、`oidc-client-ts`
  等可选依赖缺失导致的既有报错与本次改动无关。

## 遗留

- 采集前目录规模预估与风险提示、排除规则入口、仅重跑失败项未实现，
  Issue 中可作为后续独立任务跟进。
