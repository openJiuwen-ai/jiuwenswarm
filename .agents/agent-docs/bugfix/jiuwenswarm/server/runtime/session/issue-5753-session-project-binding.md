---
title: 'Issue #5753：普通会话发送消息后混入全局项目目录'
author: null
author_note: 历史作者待确认；缺少原 owner 和可靠创建署名
id: B_20261026_1606_issue-5753-session-project-binding
type: B
modules: []
files: []
designs: null
design_note: 历史设计关联待核实，不等同于没有关联设计
---

# Issue #5753：普通会话发送消息后混入全局项目目录

日期：2026-10-26。涉及 `jiuwenswarm/server/runtime/session/session_metadata.py`
及 `jiuwenswarm/runtime/request.py`。

## 根因

已有普通会话携带 `project_id=default_code`、空目录；创建项目更新全局
`selectedProject` 后，发送逻辑逐字段回退，组合出会话 ID 与全局项目目录。
后端独立锁定两个字段，将混合绑定写入磁盘。侧栏按 ID 归组，项目展示按目录匹配。

## 决策

- 后端保留明确的项目 ID；默认 ID 对应空目录，真实项目 ID 对应项目记录目录。
  入队及最终写盘时保证一致，延迟写入跟随当前绑定。
  读取项目记录显式使用 `cache_bust=True`，避免跨进程更新目录后被本进程旧缓存反向覆盖。
- 本轮请求的项目字段及 Agent 执行目录同步采用会话绑定；空目录时移除错误候选值。
- 保留缺失 ID、项目记录不存在时的旧数据兼容行为；AgentOS 默认工作目录继续只用于执行。
  请求未带目录时，仍保留从旧会话解析出的执行 fallback，持久化项目目录保持为空。

## 未采用的方案

按请求目录反查并修改项目 ID 会把全局状态串入升级为真实会话迁移，因此不采用。
本次只修后端写入和执行链路，不改前端、diff/watch 接口，也不增加读取时的存量自修复或全量迁移。
已有错误目录不会因读取被主动修正，后续写入或执行时才按 ID 处理。

## 验证

使用项目 `.venv`，定向运行 `test_session_project_binding.py`、
`runtime/test_request.py`，并验证原有目录锁定、缺失 ID 推断、重绑并发及 TUI 创建用例：32 项通过。
AgentOS 回归覆盖请求显式带目录和未带目录，检查执行目录、请求上下文及持久化目录。
缓存回归直接更新磁盘项目表、保留本进程旧缓存，验证入队与直接落盘均使用新目录。
CI 报告的三个 TUI 创建用例因项目测试桩缺少 `project_dir` 而失败，补齐项目目录后定向复测三项通过。
报告另有一项 `test_req_method_contract.py` 收集错误，使用拆分前的 gateway 导入路径；
该问题在本次修复父版本已存在，不混入 #5753 修复。
未执行全量 UT、格式化或静态检查；未进行真实服务 UI 验证。
