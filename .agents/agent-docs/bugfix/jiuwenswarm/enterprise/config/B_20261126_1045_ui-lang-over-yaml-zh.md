---
id: B_20261126_1045_ui-lang-over-yaml-zh
title: 企业版 UI 语言被 AgentServer yaml preferred_language zh 盖过
type: B
author: whds5279
author_email: 2755136018@qq.com
designs: []
design_note: 无独立 D；依据 issue #7000 与既有 preferred_language 请求 memo 契约（#10836）落地收口，不新增设计方案。
modules:
  - jiuwenswarm/enterprise/config
  - jiuwenswarm/agents/harness/common/recommendation
  - jiuwenswarm/agents/harness/team
  - jiuwenswarm/server/runtime/agent_adapter
files:
  - jiuwenswarm/enterprise/config/preferred_language.py
  - jiuwenswarm/agents/harness/common/recommendation/topic_controls.py
  - jiuwenswarm/agents/harness/team/config_loader.py
  - jiuwenswarm/server/runtime/agent_adapter/team_helpers.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/server/runtime/designer/pipeline/content_language.py
  - jiuwenswarm/runtime/symphony/service.py
  - jiuwenswarm/extensions/video_duplex/backend/settings.py
  - tests/unit_tests/enterprise/test_preferred_language_memo.py
  - tests/unit_tests/agentserver/test_team_config_loader.py
---

# 企业版 UI 语言被 AgentServer yaml preferred_language zh 盖过

## 现象与影响

企业版管理端已登录用户将界面语言切到英文后，AgentServer 侧 yaml 仍常写
`preferred_language: zh`。推荐话题、symphony 编排文案、诊断 locale、输入守卫、
设计内容语言、video_duplex 工具语言、Team 运行时语言、队友/swarmflow 配额提示等
用户可见框架文案仍按 yaml 出中文（fixes #7000）。

## 关联设计与当前契约

无独立设计文档。语言解析契约以
`jiuwenswarm.enterprise.config.preferred_language` 为准：请求入口
`bind_preferred_language` / memo 优先，未 bind 时回落 yaml，再无键则 `_yaml_or_en`
兜底 `en`。产品模板 `config.yaml` 的 `preferred_language` 缺省保持 `zh`。

## 根因与修复决策

**一句话**：多处用户可见文案调用点直读 `get_config()["preferred_language"]` /
yaml，绕过请求 memo；企业版进程级语言缓存与延迟配额回调在 memo 复位后进一步放大
yaml zh。

修复模式（可复用契约）：

1. **统一收口**：用户可见文案经 `resolve_preferred_language()`（memo > yaml > en），
   不再直读 yaml。
2. **同步 / 延迟两级传递**：同步路径依赖请求 bind 窗口内 resolve；延迟回调
   （如 `on_quota_exceeded`）在 bind 期内捕获 `notice_language` 再显式传入，避免
   memo reset 后回落 yaml。spawn_member 与 swarmflow 配额提示走同一捕获值。
3. **企业版绕进程缓存**：`recommendation_language()` 企业态直接 resolve，不复用
   进程级 `_LANGUAGE_CACHE`（防跨用户串缓存）；个人版保留 TTL 缓存。
4. **Team 运行时语言**：`_resolve_team_runtime_language` 只走
   `resolve_preferred_language()` → `to_runtime_language`；`config_base["preferred_language"]`
   不得盖过 memo/yaml（UT 固化）。
5. **产品缺省**：模板键值保持 `zh`；`_yaml_or_en` 无键兜底 `en`，与单 Agent 框架路径一致。

拒绝「把模板缺省改成 en」作为主修：会改变未切语言的存量部署默认可见文案；本缺陷
只需 UI/memo 覆盖 yaml，不必翻转产品缺省。

## 回归证据与适用范围

- 本机 / 定向 UT：`tests/unit_tests/enterprise/test_preferred_language_memo.py`、
  `tests/unit_tests/agentserver/test_team_config_loader.py`（team_runtime_language*）、
  design / topic recommendation / user_turn / member_limits 等相关用例；jiuwen-review
  复跑 PR 涉及文件约 296+202 passed。
- 适用范围：企业版已 bind 的请求路径；个人版仍以 yaml / `_config_base_cache` 为准。
- 未验证：完整集成/系统套件与真实 UI 端到端切语种联调（依赖部署节点）。

## 防错模式与资料同步

新增用户可见文案调用点时：

- 同步路径调用 `resolve_preferred_language()`（或 adapter 已封装的
  `_resolve_prompt_language`），禁止直读 `get_config()["preferred_language"]`。
- 可能在 bind 作用域外执行的回调，必须在 bind 期内捕获语种并显式传递。
- 企业版禁止把 per-request 语种放进进程级共享缓存。

本记录即契约留存；不另增全仓禁令。

## 遗留与后续

- yaml 缺 `preferred_language` 键的边缘部署会经 `_yaml_or_en` 回落 `en`（NH 观察项，
  维持与框架路径一致）。
- 同文件若另有语言入口，应与 `_resolve_prompt_language` 收敛，避免个人版
  `config_base` 与 yaml 分叉时语义分裂。
